from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from pathlib import Path
from typing import Any

import pytest

from coding_agents_sync import run_sync
from coding_agents_sync.io import ManagedEntryConflict, sync_manifested_entries
from coding_agents_sync.patches import PatchError, decode_pointer, get_value
from coding_agents_sync.plan import NativeValue, OwnedFile, OwnedTree, Plan
from coding_agents_sync.runtime_config import (
    NativeConfigError,
    NativePlan,
    apply_native_plan,
    prepare_plan_native,
    semantic_hash,
)

FIXTURES = Path(__file__).parent / "fixtures"
SOURCE_FIXTURE = FIXTURES / "config_root"
BASELINE = json.loads(
    (FIXTURES / "characterization" / "isolated-home.json").read_text()
)
NATIVE_PATHS = frozenset(
    {
        Path(".claude.json"),
        Path(".claude/settings.json"),
        Path(".codex/config.toml"),
        Path(".config/opencode/opencode.json"),
        Path(".cursor/cli-config.json"),
        Path(".cursor/mcp.json"),
        Path(".cursor/permissions.json"),
        Path(".cursor/settings.json"),
    }
)
V4_PORTABLE_ADDED = {
    ".claude/.coding-agents-managed.json",
    ".codex/.coding-agents-managed.json",
    ".config/opencode/.coding-agents-managed.json",
    ".cursor/.coding-agents-managed.json",
}
V4_PORTABLE_REMOVED = {
    ".codex/skills/source-command-sample/SKILL.md",
    ".cursor/agents/.coding-agents-managed.json",
    ".cursor/agents/sample.md",
}
V4_PORTABLE_CHANGED = {
    ".claude/agents/.coding-agents-managed.json",
    ".claude/agents/sample.md",
    ".claude/commands/.coding-agents-managed.json",
    ".claude/commands/sample.md",
    ".codex/skills/.coding-agents-managed.json",
    ".codex/skills/sample/SKILL.md",
    ".config/opencode/agents/.coding-agents-managed.json",
    ".config/opencode/agents/sample.md",
    ".cursor/rules/.coding-agents-managed.json",
    ".cursor/rules/always.mdc",
    ".cursor/rules/coding-agents-global.mdc",
    ".cursor/rules/scoped.mdc",
}
V4_NATIVE_CHANGED = {
    "config-root/.coding-agents-native.json",
    # Claude's `write` tool class folds onto Edit(**); the baseline emitted a
    # Write(**) rule that never matched.
    "home/.claude/settings.json",
    "home/.codex/config.toml",
    # OpenCode's write tool asks for its `edit` permission, so the baseline's
    # permission.write entry was never consulted.
    "home/.config/opencode/opencode.json",
    "home/.cursor/cli-config.json",
    "home/.cursor/permissions.json",
}


def isolated_roots(tmp_path: Path) -> tuple[Path, Path]:
    config_root = tmp_path / "config-root"
    shutil.copytree(SOURCE_FIXTURE, config_root)
    return config_root, tmp_path / "home"


def digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def normalize(value: Any, config_root: Path) -> Any:
    if isinstance(value, str):
        return value.replace(str(config_root), "<CONFIG_ROOT>")
    if isinstance(value, list):
        return [normalize(item, config_root) for item in value]
    if isinstance(value, dict):
        return {key: normalize(item, config_root) for key, item in value.items()}
    return value


def semantic_digest(path: Path, config_root: Path) -> str:
    value = (
        tomllib.loads(path.read_text())
        if path.suffix == ".toml"
        else json.loads(path.read_text())
    )
    value = normalize(value, config_root)
    if path.name == ".coding-agents-native.json":
        value["entries"] = [
            {**entry, "hash": "<SEMANTIC_HASH>"} for entry in value["entries"]
        ]
    content = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return digest(content)


def test_isolated_home_matches_913bd2d_characterization(tmp_path: Path) -> None:
    config_root, home = isolated_roots(tmp_path)

    run_sync(config_root=config_root, home=home)

    portable = {
        path.relative_to(home).as_posix(): digest(path.read_bytes())
        for path in sorted(home.rglob("*"))
        if path.is_file() and path.relative_to(home) not in NATIVE_PATHS
    }
    native = {
        **{
            f"home/{path.as_posix()}": semantic_digest(home / path, config_root)
            for path in sorted(NATIVE_PATHS)
        },
        "config-root/.coding-agents-native.json": semantic_digest(
            config_root / ".coding-agents-native.json", config_root
        ),
    }

    baseline_portable = BASELINE["portable_sha256"]
    assert set(portable) - set(baseline_portable) == V4_PORTABLE_ADDED
    assert set(baseline_portable) - set(portable) == V4_PORTABLE_REMOVED
    assert {
        path
        for path in set(portable) & set(baseline_portable)
        if portable[path] != baseline_portable[path]
    } == V4_PORTABLE_CHANGED
    assert {
        path: hash_
        for path, hash_ in portable.items()
        if path not in V4_PORTABLE_CHANGED | V4_PORTABLE_ADDED
    } == {
        path: hash_
        for path, hash_ in baseline_portable.items()
        if path not in V4_PORTABLE_CHANGED | V4_PORTABLE_REMOVED
    }
    for path in V4_PORTABLE_ADDED:
        assert json.loads((home / path).read_text()) == {"version": 1, "entries": {}}

    baseline_native = BASELINE["native_semantic_sha256"]
    assert {
        path
        for path in set(native) & set(baseline_native)
        if native[path] != baseline_native[path]
    } == V4_NATIVE_CHANGED
    assert {
        path: hash_ for path, hash_ in native.items() if path not in V4_NATIVE_CHANGED
    } == {
        path: hash_
        for path, hash_ in baseline_native.items()
        if path not in V4_NATIVE_CHANGED
    }
    codex = tomllib.loads((home / ".codex/config.toml").read_text())
    assert codex["skills"]["config"] == [
        {"path": "~/.codex/skills/sample", "enabled": True}
    ]
    assert not (home / ".cursor/agents/sample.md").exists()
    manifest = json.loads((config_root / ".coding-agents-native.json").read_text())
    target_paths = {
        "claude": home / ".claude/settings.json",
        "codex": home / ".codex/config.toml",
        "cursor-cli": home / ".cursor/cli-config.json",
        "cursor-desktop": home / ".cursor/permissions.json",
        "opencode": home / ".config/opencode/opencode.json",
    }
    for entry in manifest["entries"]:
        path = target_paths[entry["target"]]
        document = (
            tomllib.loads(path.read_text())
            if path.suffix == ".toml"
            else json.loads(path.read_text())
        )
        present, value = get_value(document, decode_pointer(entry["pointer"]))
        assert present
        assert semantic_hash(value) == entry["hash"]


def test_characterized_drift_and_modified_output_refusal(tmp_path: Path) -> None:
    config_root, home = isolated_roots(tmp_path)
    run_sync(config_root=config_root, home=home)
    generated = home / ".claude/agents/sample.md"
    generated.unlink()

    drift = run_sync(config_root=config_root, home=home, check=True)

    assert generated in drift
    assert not generated.exists()
    generated.write_text("locally modified\n")
    with pytest.raises(ManagedEntryConflict, match="modified generated file"):
        run_sync(config_root=config_root, home=home)


def test_characterized_collisions_patch_conflicts_and_unsafe_paths(
    tmp_path: Path,
) -> None:
    config_root, home = isolated_roots(tmp_path)
    claimed = home / ".claude/agents/sample.md"
    claimed.parent.mkdir(parents=True)
    claimed.write_text("user-owned\n")

    with pytest.raises(ManagedEntryConflict, match="unmanifested generated file"):
        run_sync(config_root=config_root, home=home)
    assert claimed.read_text() == "user-owned\n"
    assert not (home / ".claude/settings.json").exists()

    with pytest.raises(ValueError, match="Invalid managed entry"):
        sync_manifested_entries(tmp_path / "managed", {"../escape.md"}, "file")

    conflict_root, conflict_home = isolated_roots(tmp_path / "patch-conflict")
    (conflict_root / "patches/claude-settings.yaml").write_text(
        "schema: coding-agents/patch/v1\n"
        "set:\n"
        "  /permissions/allow: [Bash(user-owned)]\n"
    )
    with pytest.raises(PatchError, match="reserved generated path"):
        run_sync(config_root=conflict_root, home=conflict_home)


def test_raw_target_files_are_manifested_and_pruned(tmp_path: Path) -> None:
    config_root, home = isolated_roots(tmp_path)
    source = config_root / "target-config/claude/raw/hooks/example.md"
    source.parent.mkdir(parents=True)
    source.write_text("unvalidated target content\n")

    run_sync(config_root=config_root, home=home)

    output = home / ".claude/hooks/example.md"
    manifest_path = home / ".claude/.coding-agents-managed.json"
    assert output.read_text() == "unvalidated target content\n"
    assert json.loads(manifest_path.read_text())["entries"] == {
        "hooks/example.md": digest(output.read_bytes())
    }

    shutil.rmtree(source.parents[1])
    run_sync(config_root=config_root, home=home)

    assert not output.exists()
    assert json.loads(manifest_path.read_text())["entries"] == {}


def test_cursor_projects_commands_alongside_native_patches(tmp_path: Path) -> None:
    """Commands project to both Cursor surfaces; omissions cover the rest.

    The CLI receives the allow corpus (with `env` wrapper forms attached to
    their rule, never as a blanket) plus the exact deny. Desktop receives
    the same allow through its allowlist. The omitted tools, workspace,
    secret paths, and secret names still contribute nothing, and the
    hand-authored patches survive beside the generated pointers.
    """
    config_root, home = isolated_roots(tmp_path)

    run_sync(config_root=config_root, home=home)

    allowlist = [
        "fixture-tool:status",
        "fixture-tool:status *",
        "fixture-tool:-C status",
        "fixture-tool:-C status *",
        "fixture-tool:-C * status",
        "fixture-tool:-C * status *",
        "env:fixture-tool status",
        "env:fixture-tool status *",
        "env:* fixture-tool status",
        "env:* fixture-tool status *",
        "env:fixture-tool -C status",
        "env:fixture-tool -C status *",
        "env:* fixture-tool -C status",
        "env:* fixture-tool -C status *",
        "env:fixture-tool -C * status",
        "env:fixture-tool -C * status *",
        "env:* fixture-tool -C * status",
        "env:* fixture-tool -C * status *",
    ]
    assert json.loads((home / ".cursor/cli-config.json").read_text()) == {
        "fixture": {"cursor_cli": True},
        "approvalMode": "allowlist",
        "permissions": {
            "allow": [f"Shell({pattern})" for pattern in allowlist],
            "deny": [
                "Shell(fixture-tool:remove)",
                "Shell(fixture-tool:-C remove)",
                "Shell(fixture-tool:-C * remove)",
                "Shell(env:fixture-tool remove)",
                "Shell(env:* fixture-tool remove)",
                "Shell(env:fixture-tool -C remove)",
                "Shell(env:* fixture-tool -C remove)",
                "Shell(env:fixture-tool -C * remove)",
                "Shell(env:* fixture-tool -C * remove)",
            ],
        },
    }
    assert json.loads((home / ".cursor/permissions.json").read_text()) == {
        "fixture": {"cursor_desktop": True},
        "approvalMode": "allowlist",
        "terminalAllowlist": allowlist,
    }


def test_characterized_native_hash_refusal_and_manifest_last(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_root, home = isolated_roots(tmp_path)
    run_sync(config_root=config_root, home=home)
    settings = home / ".claude/settings.json"
    document = json.loads(settings.read_text())
    document["permissions"]["allow"] = ["Bash(modified)"]
    settings.write_text(json.dumps(document))

    with pytest.raises(NativeConfigError, match="modified generated native path"):
        run_sync(config_root=config_root, home=home)

    manifest_root = tmp_path / "manifest-last"
    plan = prepare_plan_native(
        config_root=manifest_root / "config-root",
        native_values=(
            NativeValue(
                "opencode",
                manifest_root / "home/.config/opencode/opencode.json",
                ("instructions",),
                ["rule"],
            ),
        ),
        native_patches=(),
    )
    import coding_agents_sync.runtime_config as runtime_config

    write_bytes = runtime_config.write_bytes
    written: list[Path] = []

    def record(path: Path, content: bytes) -> None:
        written.append(path)
        write_bytes(path, content)

    monkeypatch.setattr(runtime_config, "write_bytes", record)
    apply_native_plan(plan)

    assert written[-1] == plan.manifest_path
    assert plan.manifest_path.is_file()


def test_characterized_portable_manifest_last(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import coding_agents_sync.io as sync_io
    import coding_agents_sync.writer as writer

    root = tmp_path / "managed"
    target = root / "generated.md"
    plan = Plan(
        files=(OwnedFile(target, b"generated\n", root),),
        trees=(
            OwnedTree(
                root,
                manifest_root=root,
                manifest_mode="file",
                declaration=True,
            ),
        ),
    )
    writer.preflight_plan(plan)
    write_bytes = writer.write_bytes
    write_manifest = sync_io.write_manifest
    written: list[Path] = []

    def record_bytes(path: Path, content: bytes) -> None:
        written.append(path)
        write_bytes(path, content)

    def record_manifest(path: Path, entries: dict[str, str]) -> None:
        written.append(path)
        write_manifest(path, entries)

    monkeypatch.setattr(writer, "write_bytes", record_bytes)
    monkeypatch.setattr(sync_io, "write_manifest", record_manifest)
    writer.write_plan(plan)
    writer.publish_plan_manifests(plan)

    assert written == [target, root / ".coding-agents-managed.json"]


def test_characterized_global_manifest_publication_last(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import coding_agents_sync.sync as sync

    config_root, home = isolated_roots(tmp_path)
    apply_transaction = sync.apply_transaction
    write_plan = sync.write_plan
    apply_native_candidates = sync.apply_native_candidates
    publish_plan_manifests = sync.publish_plan_manifests
    publish_native_manifest = sync.publish_native_manifest
    events: list[str] = []

    def record_transaction(transaction: sync.Transaction) -> None:
        events.append("transaction")
        apply_transaction(transaction)

    def record_portable(plan: Plan) -> None:
        events.append("portable-content")
        write_plan(plan)

    def record_native(plan: NativePlan) -> None:
        events.append("native-content")
        apply_native_candidates(plan)

    def record_portable_manifest(plan: Plan) -> None:
        events.append("portable-manifest")
        publish_plan_manifests(plan)

    def record_native_manifest(plan: NativePlan) -> None:
        events.append("native-manifest")
        publish_native_manifest(plan)

    monkeypatch.setattr(sync, "apply_transaction", record_transaction)
    monkeypatch.setattr(sync, "write_plan", record_portable)
    monkeypatch.setattr(sync, "apply_native_candidates", record_native)
    monkeypatch.setattr(sync, "publish_plan_manifests", record_portable_manifest)
    monkeypatch.setattr(sync, "publish_native_manifest", record_native_manifest)

    sync.run_sync(config_root=config_root, home=home)

    assert events == [
        "transaction",
        "portable-content",
        "native-content",
        "portable-manifest",
        "native-manifest",
    ]
