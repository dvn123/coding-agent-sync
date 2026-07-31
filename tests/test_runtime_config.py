from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest
import tomlkit
from typer.testing import CliRunner

from coding_agents_sync.artifacts import (
    ManagedRootArtifact,
    NativeConfigArtifact,
    TextArtifact,
)
from coding_agents_sync.cli import main
from coding_agents_sync.io import MANAGED_MANIFEST, ManagedEntryConflict, write_bytes
from coding_agents_sync.patches import (
    Operation,
    Patch,
    PatchError,
    apply_operations,
    decode_pointer,
    load_patch,
    merge_patches,
    validate_generated_conflicts,
)
from coding_agents_sync.runtime_config import (
    GeneratedRuntimeConfig,
    NativeConfigError,
    NativePlan,
    apply_native_plan,
)
from coding_agents_sync.runtime_config import (
    prepare_native_plan as _prepare_native_plan,
)
from coding_agents_sync.writer import preflight_artifacts


def write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def patch(path: Path, content: str) -> Patch:
    write(path, content)
    return load_patch(path)


def prepare_native_plan(
    *,
    home: Path,
    generated: GeneratedRuntimeConfig,
    config_root: Path | None = None,
) -> NativePlan:
    return _prepare_native_plan(
        config_root=config_root or home / ".config/coding-agents",
        home=home,
        generated=generated,
    )


def test_pointer_decoding_and_invalid_pointers() -> None:
    assert decode_pointer("/a~1b/c~0d") == ("a/b", "c~d")
    for value in ("", "plain", "/bad~", "/bad~2"):
        with pytest.raises(PatchError):
            decode_pointer(value)


def test_recursive_duplicate_keys_and_sanitized_parse_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "patch.yaml"
        write(
            path,
            "schema: coding-agents/patch/v1\n"
            "set:\n  /outer:\n  - secret-one\n  /outer:\n  - secret-two\n",
        )
        with pytest.raises(PatchError) as caught:
            load_patch(path)
        assert "secret-one" not in str(caught.value)
        assert "secret-two" not in str(caught.value)


def test_patch_value_rules_and_conflicts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name, body in {
            "mapping-set": "set:\n  /x: {a: 1}\n",
            "scalar-replace": "replace:\n  /x: 1\n",
            "scalar-extend": "extend:\n  /x: 1\n",
            "list-overlay": "overlay:\n  /x: [1]\n",
            "overlap": "set:\n  /x: 1\n  /x/y: 2\n",
        }.items():
            with pytest.raises(PatchError):
                patch(root / name, f"schema: coding-agents/patch/v1\n{body}")


def test_apply_patch_operations_and_mapping_only_traversal() -> None:
    document = {
        "unknown": {"keep": True},
        "gone": 1,
        "table": {"old": 1},
        "rules": {"portable": "allow"},
    }
    apply_operations(
        document,
        Patch(
            (
                Operation("set", ("new", "leaf"), [1, 2]),
                Operation("replace", ("table",), {"fresh": 2}),
                Operation("extend", ("items",), ["a", "b", "a"]),
                Operation(
                    "overlay", ("rules",), {"portable": "ask", "native": "allow"}
                ),
                Operation("delete", ("gone",)),
                Operation("delete", ("missing", "leaf")),
            )
        ),
    )
    assert document == {
        "unknown": {"keep": True},
        "new": {"leaf": [1, 2]},
        "table": {"fresh": 2},
        "items": ["a", "b"],
        "rules": {"portable": "ask", "native": "allow"},
    }
    for existing in ([1], "scalar"):
        with pytest.raises(PatchError):
            apply_operations(
                {"x": existing}, Patch((Operation("set", ("x", "child"), 1),))
            )


def test_local_exact_override_and_remaining_overlap() -> None:
    committed = Patch((Operation("set", ("a",), 1),))
    assert (
        merge_patches(committed, Patch((Operation("set", ("a",), 2),)))
        .operations[0]
        .value
        == 2
    )
    assert merge_patches(
        Patch((Operation("extend", ("items",), ["committed"]),)),
        Patch((Operation("extend", ("items",), ["local"]),)),
    ).operations == (Operation("extend", ("items",), ["local"]),)
    with pytest.raises(PatchError):
        merge_patches(committed, Patch((Operation("set", ("a", "b"), 2),)))


def test_generated_path_reservations() -> None:
    generated = {("agents", "review", "description"): "generated"}
    validate_generated_conflicts(
        Patch((Operation("set", ("agents", "review", "description"), "generated"),)),
        generated,
    )
    validate_generated_conflicts(
        Patch((Operation("extend", ("permissions", "allow"), ["native"]),)),
        {("permissions", "allow"): ["generated"]},
    )
    validate_generated_conflicts(
        Patch((Operation("overlay", ("permission", "bash"), {"git": "ask"}),)),
        {("permission", "bash"): {"git": "allow"}},
    )
    with pytest.raises(PatchError, match="reserved generated path"):
        validate_generated_conflicts(
            Patch((Operation("set", ("agents", "review", "description"), "override"),)),
            generated,
        )
    validate_generated_conflicts(
        Patch((Operation("set", ("agents", "review", "model"), "m"),)),
        generated,
    )
    with pytest.raises(PatchError):
        validate_generated_conflicts(
            Patch((Operation("set", ("permission", "bash", "git"), "ask"),)),
            {("permission", "bash"): {"git": "allow"}},
        )
    for operation in (
        Operation("delete", ("agents", "review", "description")),
        Operation("replace", ("agents", "review"), {}),
        Operation("extend", ("agents", "review", "description"), ["invalid"]),
        Operation("overlay", ("agents", "review", "description"), {"invalid": True}),
    ):
        with pytest.raises(PatchError):
            validate_generated_conflicts(Patch((operation,)), generated)


def test_json_unknown_fields_atomic_modes_check_and_idempotence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        native = home / ".claude/settings.json"
        write(
            native, json.dumps({"unknown": {"nested": True}, "managed": "old"}), 0o640
        )
        write(
            home / ".config/coding-agents/patches/claude-settings.yaml",
            "schema: coding-agents/patch/v1\n"
            "set:\n  /managed: new\ndelete:\n  - /tombstone\n",
        )
        plan = prepare_native_plan(home=home, generated=GeneratedRuntimeConfig())
        assert native in plan.drift
        before = native.read_bytes()
        prepare_native_plan(home=home, generated=GeneratedRuntimeConfig())
        assert native.read_bytes() == before
        apply_native_plan(plan)
        assert json.loads(native.read_text()) == {
            "unknown": {"nested": True},
            "managed": "new",
        }
        assert stat.S_IMODE(native.stat().st_mode) == 0o640
        assert not prepare_native_plan(
            home=home, generated=GeneratedRuntimeConfig()
        ).drift


def test_toml_comments_and_unknown_agent_siblings_survive_stale_cleanup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        native = home / ".codex/config.toml"
        write(native, "# keep\nunknown = true\n")
        first = GeneratedRuntimeConfig(
            codex_skill_paths=("~/.codex/skills/a",),
        )
        apply_native_plan(prepare_native_plan(home=home, generated=first))
        document = tomlkit.parse(native.read_text())
        document.setdefault("agents", {})["old"] = {
            "description": "generated",
            "config_file": "generated",
            "model": "tool-owned",
        }
        native.write_text(tomlkit.dumps(document))
        manifest = json.loads(
            (home / ".config/coding-agents/.coding-agents-native.json").read_text()
        )
        for pointer in ("/agents/old/description", "/agents/old/config_file"):
            manifest["entries"].append(
                {
                    "target": "codex",
                    "pointer": pointer,
                    "hash": "sha256:invalid",
                }
            )
        write(
            home / ".config/coding-agents/.coding-agents-native.json",
            json.dumps(manifest),
        )
        with pytest.raises(NativeConfigError):
            prepare_native_plan(home=home, generated=first)
        assert "# keep" in native.read_text()


def test_local_patch_requires_private_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        write(
            home / ".config/coding-agents/patches.local/claude-settings.yaml",
            "schema: coding-agents/patch/v1\n",
            0o644,
        )
        with pytest.raises(NativeConfigError, match="0600"):
            prepare_native_plan(home=home, generated=GeneratedRuntimeConfig())


def test_native_reconciliation_uses_explicit_config_root() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config_root = root / "config"
        home = root / "home"
        write(
            config_root / "patches/claude-settings.yaml",
            "schema: coding-agents/patch/v1\nset:\n  /from_config_root: true\n",
        )
        write(
            config_root / "patches.local/cursor-settings.yaml",
            "schema: coding-agents/patch/v1\nset:\n  /from_local_patch: true\n",
        )
        write(
            home / ".config/coding-agents/patches/opencode.yaml",
            "not a valid patch",
        )

        plan = prepare_native_plan(
            config_root=config_root,
            home=home,
            generated=GeneratedRuntimeConfig(),
        )
        apply_native_plan(plan)

        assert json.loads((home / ".claude/settings.json").read_text()) == {
            "from_config_root": True
        }
        assert json.loads((home / ".cursor/settings.json").read_text()) == {
            "from_local_patch": True
        }
        assert (config_root / ".coding-agents-native.json").is_file()
        assert not (home / ".config/coding-agents/.coding-agents-native.json").exists()


def test_invalid_candidate_prevents_all_writes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        first = home / ".claude/settings.json"
        second = home / ".config/opencode/opencode.json"
        write(first, '{"managed":"old"}\n')
        write(second, "not json")
        write(
            home / ".config/coding-agents/patches/claude-settings.yaml",
            "schema: coding-agents/patch/v1\nset:\n  /managed: new\n",
        )
        before = first.read_bytes()
        with pytest.raises(NativeConfigError):
            prepare_native_plan(
                home=home,
                generated=GeneratedRuntimeConfig(opencode_instructions=("rule",)),
            )
        assert first.read_bytes() == before


def test_new_native_config_uses_private_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        write(
            home / ".config/coding-agents/patches/cursor-settings.yaml",
            "schema: coding-agents/patch/v1\nset:\n  /managed: true\n",
        )
        plan = prepare_native_plan(home=home, generated=GeneratedRuntimeConfig())
        apply_native_plan(plan)
        path = home / ".cursor/settings.json"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not any(
            child.name.startswith(f".{path.name}.") for child in path.parent.iterdir()
        )


def test_v2_migration_retires_github_but_fresh_surfaces_preserve_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        existing_json = {
            "mcpServers": {
                "github": {"url": "https://api.githubcopilot.com/mcp/"},
                "tool-owned": {"command": "tool-owned"},
            }
        }
        write(home / ".claude.json", json.dumps(existing_json))
        write(home / ".cursor/mcp.json", json.dumps(existing_json))
        write(
            home / ".codex/config.toml",
            """[mcp_servers.github]
url = "https://api.githubcopilot.com/mcp/"
[mcp_servers.tool-owned]
command = "tool-owned"
""",
        )
        write(
            home / ".config/opencode/opencode.json",
            json.dumps(
                {
                    "mcp": {
                        "github": {"url": "https://api.githubcopilot.com/mcp/"},
                        "tool-owned": {"type": "local", "command": ["tool-owned"]},
                    }
                }
            ),
        )
        write(
            home / ".config/coding-agents/.coding-agents-native.json",
            json.dumps({"version": 2, "entries": []}),
        )

        apply_native_plan(
            prepare_native_plan(home=home, generated=GeneratedRuntimeConfig())
        )

        configs = (
            (json.loads((home / ".claude.json").read_text()), "mcpServers"),
            (json.loads((home / ".cursor/mcp.json").read_text()), "mcpServers"),
            (
                tomlkit.parse((home / ".codex/config.toml").read_text()).unwrap(),
                "mcp_servers",
            ),
            (
                json.loads((home / ".config/opencode/opencode.json").read_text()),
                "mcp",
            ),
        )
        for config, root in configs:
            assert "github" not in config[root]
            assert "tool-owned" in config[root]
        assert (
            json.loads(
                (home / ".config/coding-agents/.coding-agents-native.json").read_text()
            )["version"]
            == 3
        )

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        write(
            home / ".claude.json",
            json.dumps(
                {"mcpServers": {"github": {"url": "https://tool-owned.example/mcp"}}}
            ),
        )

        apply_native_plan(
            prepare_native_plan(home=home, generated=GeneratedRuntimeConfig())
        )

        claude = json.loads((home / ".claude.json").read_text())
        assert claude["mcpServers"]["github"]["url"] == (
            "https://tool-owned.example/mcp"
        )


def test_v1_manifest_applies_every_intervening_retirement() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        write(
            home / ".config/opencode/opencode.json",
            json.dumps(
                {
                    "mcp": {
                        "context7": {"type": "remote"},
                        "notion": {"type": "remote"},
                        "github": {"type": "remote"},
                        "tool-owned": {"type": "local"},
                    }
                }
            ),
        )
        write(
            home / ".config/coding-agents/.coding-agents-native.json",
            json.dumps({"version": 1, "entries": []}),
        )

        apply_native_plan(
            prepare_native_plan(home=home, generated=GeneratedRuntimeConfig())
        )

        config = json.loads((home / ".config/opencode/opencode.json").read_text())
        assert config["mcp"] == {"tool-owned": {"type": "local"}}


def test_unmanifested_generated_name_is_never_claimed() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "user.md"
        write(target, "user\n")
        artifacts = [
            TextArtifact(target, "generated\n"),
            ManagedRootArtifact(root, {"user.md"}, "file"),
        ]
        with pytest.raises(ManagedEntryConflict, match="unmanifested"):
            preflight_artifacts(artifacts)
        assert target.read_text() == "user\n"


def test_interrupted_first_write_can_publish_its_manifest() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "generated.md"
        write(target, "generated\n")
        preflight_artifacts(
            [
                TextArtifact(target, "generated\n"),
                ManagedRootArtifact(root, {"generated.md"}, "file"),
            ]
        )


def test_stale_legacy_manifest_fails_during_preflight() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        write(root / "stale.md", "legacy\n")
        write(root / MANAGED_MANIFEST, '["stale.md"]\n')
        with pytest.raises(ManagedEntryConflict, match="legacy"):
            preflight_artifacts([ManagedRootArtifact(root, set(), "file")])
        assert (root / "stale.md").read_text() == "legacy\n"


def test_generated_structured_output_is_validated_before_writes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "agent.toml"
        artifacts = [
            TextArtifact(target, 'invalid = "unterminated'),
            ManagedRootArtifact(root, {"agent.toml"}, "file"),
        ]
        with pytest.raises(ManagedEntryConflict, match="structured"):
            preflight_artifacts(artifacts)
        assert not target.exists()


def test_binary_generated_writes_use_atomic_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "generated.bin"
        write(path, "old")
        replaced: list[tuple[str | os.PathLike[str], str | os.PathLike[str]]] = []
        real_replace = os.replace

        def replace(
            source: str | os.PathLike[str], target: str | os.PathLike[str]
        ) -> None:
            replaced.append((source, target))
            real_replace(source, target)

        monkeypatch.setattr(os, "replace", replace)
        write_bytes(path, b"new")
        assert path.read_bytes() == b"new"
        assert replaced and Path(replaced[0][1]) == path


@pytest.mark.parametrize(
    "entries",
    [
        [
            {
                "target": "codex",
                "pointer": "/skills/config",
                "hash": "sha256:" + "0" * 64,
            },
            {
                "target": "codex",
                "pointer": "/skills/config",
                "hash": "sha256:" + "1" * 64,
            },
        ],
        [{"target": "unknown", "pointer": "/x", "hash": "sha256:" + "0" * 64}],
        [{"target": "codex", "pointer": "/x", "hash": "invalid"}],
    ],
)
def test_generated_native_manifest_is_strict(entries: list[dict[str, str]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        write(
            home / ".config/coding-agents/.coding-agents-native.json",
            json.dumps({"version": 1, "entries": entries}),
        )
        with pytest.raises(NativeConfigError, match="manifest"):
            prepare_native_plan(home=home, generated=GeneratedRuntimeConfig())


def test_focused_validators_reject_invalid_managed_shapes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        write(
            home / ".config/coding-agents/patches/claude-settings.yaml",
            "schema: coding-agents/patch/v1\nset:\n  /permissions/allow: 1\n",
        )
        with pytest.raises(NativeConfigError, match="permissions/allow"):
            prepare_native_plan(home=home, generated=GeneratedRuntimeConfig())

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        write(
            home / ".config/coding-agents/patches/codex.yaml",
            "schema: coding-agents/patch/v1\nset:\n  /skills/config: [1]\n",
        )
        with pytest.raises(PatchError, match="skills/config"):
            prepare_native_plan(
                home=home,
                generated=GeneratedRuntimeConfig(
                    codex_skill_paths=("~/.codex/skills/example",)
                ),
            )

    for target in ("cursor-cli", "cursor-desktop"):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            generated = GeneratedRuntimeConfig(
                native=(
                    NativeConfigArtifact(
                        target,
                        ((("approvalMode",), "unsupported"),),
                    ),
                )
            )
            with pytest.raises(NativeConfigError, match="approvalMode"):
                prepare_native_plan(home=home, generated=generated)


def test_generated_permission_collision_and_modified_value_fail() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        patch_path = home / ".config/coding-agents/patches/claude-settings.yaml"
        write(
            patch_path,
            "schema: coding-agents/patch/v1\n"
            "set:\n  /permissions/allow: [Bash(other)]\n",
        )
        generated = GeneratedRuntimeConfig(
            native=(
                NativeConfigArtifact(
                    "claude",
                    ((("permissions", "allow"), ["Bash(git status)"]),),
                ),
            )
        )
        with pytest.raises(PatchError, match="reserved generated path"):
            prepare_native_plan(home=home, generated=generated)

        write(
            patch_path,
            "schema: coding-agents/patch/v1\n"
            "extend:\n  /permissions/allow: [WebFetch(*)]\n",
        )
        apply_native_plan(prepare_native_plan(home=home, generated=generated))
        settings = home / ".claude/settings.json"
        document = json.loads(settings.read_text())
        assert document["permissions"]["allow"] == [
            "Bash(git status)",
            "WebFetch(*)",
        ]
        document["permissions"]["allow"] = ["Bash(modified)"]
        settings.write_text(json.dumps(document))
        with pytest.raises(NativeConfigError, match="modified generated native path"):
            prepare_native_plan(home=home, generated=generated)


def test_generated_mapping_overlay_wins_and_is_manifest_owned() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        write(
            home / ".config/coding-agents/patches/opencode.yaml",
            "schema: coding-agents/patch/v1\n"
            "overlay:\n"
            "  /fixture/settings:\n"
            "    generated: overridden\n"
            "    native: retained\n",
        )
        generated = GeneratedRuntimeConfig(
            native=(
                NativeConfigArtifact(
                    "opencode",
                    (
                        (
                            ("fixture", "settings"),
                            {"generated": "original"},
                        ),
                    ),
                ),
            )
        )
        apply_native_plan(prepare_native_plan(home=home, generated=generated))
        document = json.loads((home / ".config/opencode/opencode.json").read_text())
        assert document["fixture"]["settings"] == {
            "generated": "overridden",
            "native": "retained",
        }
        manifest = json.loads(
            (home / ".config/coding-agents/.coding-agents-native.json").read_text()
        )
        assert any(
            entry["target"] == "opencode" and entry["pointer"] == "/fixture/settings"
            for entry in manifest["entries"]
        )


def permission_plan(root: Path, patch_body: str) -> None:
    config_root, home = root / "config", root / "home"
    write(
        config_root / "patches/opencode.yaml",
        "schema: coding-agents/patch/v1\n" + patch_body,
    )
    generated = GeneratedRuntimeConfig(
        native=(
            NativeConfigArtifact(
                "opencode",
                (
                    (("permission", "bash"), {"*": "ask", "tool *": "allow"}),
                    (("permission", "read"), {"*": "allow", "**/.env": "deny"}),
                ),
            ),
        )
    )
    prepare_native_plan(config_root=config_root, home=home, generated=generated)


@pytest.mark.parametrize(
    "patch_body",
    (
        "overlay:\n  /permission/bash:\n    'tool publish *': ask\n",
        "set:\n  /permission/bash/tool: allow\n",
        "overlay:\n  /permission:\n    bash: allow\n",
        "delete:\n- /permission/bash\n",
    ),
)
def test_patch_cannot_contribute_generated_command_permissions(
    tmp_path: Path, patch_body: str
) -> None:
    with pytest.raises(NativeConfigError, match="cannot contribute command"):
        permission_plan(tmp_path, patch_body)


def test_patch_may_add_secret_denials_but_not_relax_generated_paths(
    tmp_path: Path,
) -> None:
    permission_plan(
        tmp_path / "deny", "overlay:\n  /permission/read:\n    '~/.netrc': deny\n"
    )
    with pytest.raises(NativeConfigError, match="only add deny entries"):
        permission_plan(
            tmp_path / "allow", "overlay:\n  /permission/read:\n    '**': allow\n"
        )


def test_cli_diagnostics_do_not_expose_parser_content() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config_root = root / "config-root"
        home = root / "home"
        config_root.mkdir()
        write(
            config_root / "patches/claude-settings.yaml",
            "schema: coding-agents/patch/v1\nset: [distinctive-private-value\n",
        )
        result = CliRunner().invoke(
            main,
            ["--config-root", str(config_root), "--home", str(home)],
        )
        assert result.exit_code != 0
        assert "distinctive-private-value" not in result.output
        assert "Traceback" not in result.output

        write(
            config_root / "patches/claude-settings.yaml",
            "schema: coding-agents/patch/v1\n",
        )
        write(home / ".codex/config.toml", 'invalid = "distinctive-native-value')
        result = CliRunner().invoke(
            main,
            ["--config-root", str(config_root), "--home", str(home)],
        )
        assert result.exit_code != 0
        assert "distinctive-native-value" not in result.output
        assert "Traceback" not in result.output
