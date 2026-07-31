from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

from typer.testing import CliRunner

from coding_agents_sync.cli import main

FIXTURE_ROOT = Path(__file__).parent / "fixtures/config_root"


def copy_config_root(path: Path) -> Path:
    shutil.copytree(FIXTURE_ROOT, path)
    return path


def snapshot(root: Path) -> dict[Path, tuple[bytes, int, int, int]]:
    return {
        path.relative_to(root): (
            path.read_bytes(),
            stat.S_IMODE(path.stat().st_mode),
            path.stat().st_ino,
            path.stat().st_mtime_ns,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def test_xdg_default_discovery_check_and_idempotence(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    config_root = copy_config_root(tmp_path / "xdg/coding-agents")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root.parent))
    runner = CliRunner()

    before = snapshot(config_root)
    drift = runner.invoke(main, ["--check"])
    assert drift.exit_code == 1
    assert "drift " in drift.output
    assert snapshot(config_root) == before
    assert not home.exists()

    assert runner.invoke(main).exit_code == 0
    assert (home / ".claude/CLAUDE.md").is_file()
    assert (home / ".codex/AGENTS.md").is_file()
    assert (home / ".cursor/rules/coding-agents-global.mdc").is_file()
    assert (home / ".config/opencode/AGENTS.md").is_file()
    assert (config_root / ".coding-agents-native.json").is_file()
    assert not (home / ".config/coding-agents").exists()

    applied = {
        "home": snapshot(home),
        "config": snapshot(config_root),
    }
    assert runner.invoke(main).exit_code == 0
    assert snapshot(home) == applied["home"]
    assert snapshot(config_root) == applied["config"]
    assert runner.invoke(main, ["--check"]).exit_code == 0


def test_empty_xdg_uses_home_fallback(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    copy_config_root(home / ".config/coding-agents")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", "")

    result = CliRunner().invoke(main)

    assert result.exit_code == 0
    assert (home / ".claude/CLAUDE.md").is_file()
    assert (home / ".config/coding-agents/.coding-agents-native.json").is_file()


def test_explicit_roots_and_hidden_base_alias(monkeypatch, tmp_path: Path) -> None:
    explicit_home = tmp_path / "explicit-home"
    config_root = copy_config_root(tmp_path / "explicit-config")
    monkeypatch.setenv("HOME", str(tmp_path / "ignored-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ignored-xdg"))
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["--config-root", str(config_root), "--home", str(explicit_home)],
    )
    assert result.exit_code == 0
    assert (explicit_home / ".codex/AGENTS.md").is_file()
    assert not (tmp_path / "ignored-home").exists()
    assert not (tmp_path / "ignored-xdg").exists()
    assert "--config-root" in runner.invoke(main, ["--help"]).output
    assert "--base" not in runner.invoke(main, ["--help"]).output

    alias_home = tmp_path / "alias-home"
    alias = runner.invoke(
        main,
        ["--base", str(config_root), "--home", str(alias_home)],
    )
    assert alias.exit_code == 0
    assert (alias_home / ".codex/AGENTS.md").is_file()


def test_local_patch_mode_override_and_check_drift(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config_root = copy_config_root(tmp_path / "config")
    local_patch = config_root / "patches.local/claude-settings.yaml"
    local_patch.parent.mkdir()
    local_patch.write_text(
        "schema: coding-agents/patch/v1\nset:\n  /fixture/claude: local\n"
    )
    local_patch.chmod(0o600)
    runner = CliRunner()
    arguments = ["--config-root", str(config_root), "--home", str(home)]

    assert runner.invoke(main, arguments).exit_code == 0
    settings = home / ".claude/settings.json"
    assert json.loads(settings.read_text())["fixture"]["claude"] == "local"

    local_patch.write_text(
        "schema: coding-agents/patch/v1\nset:\n  /fixture/claude: changed\n"
    )
    before = snapshot(home)
    drift = runner.invoke(main, [*arguments, "--check"])
    assert drift.exit_code == 1
    assert f"drift {settings}" in drift.output
    assert snapshot(home) == before

    local_patch.chmod(0o644)
    invalid = runner.invoke(main, arguments)
    assert invalid.exit_code == 1
    assert "local patch must use mode 0600" in invalid.output
    assert snapshot(home) == before


def test_source_schema_error_is_a_clean_cli_diagnostic(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config_root = copy_config_root(tmp_path / "config")
    source = config_root / "rules/always.md"
    source.write_text(source.read_text().replace("schema: coding-agents/v3\n", ""))

    result = CliRunner().invoke(
        main,
        ["--config-root", str(config_root), "--home", str(home)],
    )

    assert result.exit_code == 1
    assert "Error: invalid coding-agent source(s)" in result.output
    assert "missing or invalid schema `coding-agents/v3`" in result.output
    assert "Traceback" not in result.output
    assert not home.exists()
