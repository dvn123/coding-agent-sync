"""The neutral fixture's whole compiled output, pinned file by file.

Each generated file is compared with its copy under `tests/golden/`. After an
intended output change, run
`uv run pytest tests/test_golden.py --inline-snapshot=fix` and review the
golden diff: it is the compiler's observable behavior.
"""

import re
import shutil
from pathlib import Path

from inline_snapshot import external_file, snapshot

from coding_agents_sync import run_sync

FIXTURE_ROOT = Path(__file__).parent / "fixtures/config_root"
GOLDEN = Path(__file__).parent / "golden"
HASH = re.compile(r"sha256:[0-9a-f]{64}")


def compiled_tree(tmp_path: Path) -> dict[str, str]:
    config_root = shutil.copytree(FIXTURE_ROOT, tmp_path / "config")
    home = tmp_path / "home"
    run_sync(config_root=config_root, home=home)
    outputs = {
        f"home/{path.relative_to(home).as_posix()}": path
        for path in home.rglob("*")
        if path.is_file()
    }
    outputs["config/.coding-agents-native.json"] = (
        config_root / ".coding-agents-native.json"
    )
    texts = {
        name: path.read_text().replace(str(tmp_path), "<tmp>")
        for name, path in sorted(outputs.items())
    }
    # Hashes of values that hold the temporary path change on every run; the
    # values themselves are pinned in the target files.
    texts["config/.coding-agents-native.json"] = HASH.sub(
        "sha256:<hash>", texts["config/.coding-agents-native.json"]
    )
    return texts


def test_fixture_compiles_to_the_golden_tree(tmp_path: Path) -> None:
    outputs = compiled_tree(tmp_path)

    assert list(outputs) == snapshot(
        [
            "config/.coding-agents-native.json",
            "home/.agents/skills/.coding-agents-managed.json",
            "home/.agents/skills/sample/SKILL.md",
            "home/.agents/skills/sample/asset.txt",
            "home/.claude.json",
            "home/.claude/.coding-agents-managed.json",
            "home/.claude/CLAUDE.md",
            "home/.claude/agents/.coding-agents-managed.json",
            "home/.claude/agents/sample.md",
            "home/.claude/mcp/.coding-agents-managed.json",
            "home/.claude/mcp/fixture-optional.json",
            "home/.claude/rules/.coding-agents-managed.json",
            "home/.claude/rules/always.md",
            "home/.claude/rules/scoped.md",
            "home/.claude/settings.json",
            "home/.claude/skills/.coding-agents-managed.json",
            "home/.claude/skills/sample/SKILL.md",
            "home/.claude/skills/sample/asset.txt",
            "home/.codex/.coding-agents-managed.json",
            "home/.codex/AGENTS.md",
            "home/.codex/agents/.coding-agents-managed.json",
            "home/.codex/agents/sample.toml",
            "home/.codex/config.toml",
            "home/.codex/rules/.coding-agents-managed.json",
            "home/.codex/rules/coding-agents.rules",
            "home/.codex/skills/.coding-agents-managed.json",
            "home/.coding-agents-managed.json",
            "home/.config/opencode/.coding-agents-managed.json",
            "home/.config/opencode/AGENTS.md",
            "home/.config/opencode/agents/.coding-agents-managed.json",
            "home/.config/opencode/agents/sample.md",
            "home/.config/opencode/commands/.coding-agents-managed.json",
            "home/.config/opencode/commands/sample.md",
            "home/.config/opencode/opencode.json",
            "home/.config/opencode/plugins/opencode-unwrap.js",
            "home/.config/opencode/rules/.coding-agents-managed.json",
            "home/.config/opencode/rules/always.md",
            "home/.config/opencode/rules/scoped.md",
            "home/.config/opencode/skills/.coding-agents-managed.json",
            "home/.cursor/.coding-agents-managed.json",
            "home/.cursor/agents/.coding-agents-managed.json",
            "home/.cursor/agents/sample.md",
            "home/.cursor/cli-config.json",
            "home/.cursor/mcp.json",
            "home/.cursor/permissions.json",
            "home/.cursor/rules/.coding-agents-managed.json",
            "home/.cursor/rules/always.mdc",
            "home/.cursor/rules/coding-agents-global.mdc",
            "home/.cursor/rules/scoped.mdc",
            "home/.cursor/settings.json",
            "home/.cursor/skills/.coding-agents-managed.json",
            "home/.cursorignore",
        ]
    )
    for name, text in outputs.items():
        assert text == external_file(GOLDEN / name, format=".txt"), name
