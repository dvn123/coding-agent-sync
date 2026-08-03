from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any, Literal

import yaml
from typer.testing import CliRunner

from coding_agents_sync import run_sync
from coding_agents_sync.cli import main
from coding_agents_sync.io import (
    MANAGED_MANIFEST,
    ManagedEntryConflict,
    sync_manifested_entries,
)
from coding_agents_sync.patches import PatchError
from coding_agents_sync.runtime_config import NativeConfigError
from coding_agents_sync.sources import SourceSchemaError, load_permissions


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def source_doc(
    kind: str,
    id_: str,
    name: str,
    body: str,
    *,
    description: str = "",
    extra: dict[str, Any] | None = None,
) -> str:
    meta: dict[str, Any] = {
        "schema": "coding-agents/v4",
        "kind": kind,
        "id": id_,
        "name": name,
        "description": description,
    }
    target_blocks: dict[str, dict[str, Any]] = {}
    if extra:
        for key, value in extra.items():
            if key == "internal" or key.startswith("internal:"):
                continue
            if ":" in key and (target := key.partition(":")[0]) in {
                "claude",
                "cursor",
                "codex",
                "opencode",
            }:
                target_blocks.setdefault(target, {}).setdefault("native", {})[
                    key.partition(":")[2]
                ] = value
            elif key in {"claude", "cursor", "codex", "opencode"} and isinstance(
                value, dict
            ):
                target_blocks.setdefault(key, {}).setdefault("native", {}).update(value)
            else:
                meta[key] = value
    if kind == "command":
        only = set(meta.get("only") or ())
        if not only or "codex" in only:
            target_blocks.setdefault("codex", {}).setdefault("omit", {})["command"] = (
                "Codex has no native command delivery."
            )
        if not only or "cursor" in only:
            target_blocks.setdefault("cursor", {}).setdefault("omit", {})["command"] = (
                "Cursor has no user command delivery."
            )
    if kind == "agent":
        only = set(meta.get("only") or ())
        if not only or "cursor" in only:
            target_blocks.setdefault("cursor", {}).setdefault("omit", {})["agent"] = (
                "Cursor Agent does not load user agents."
            )
        if meta.get("background"):
            if not only or "codex" in only:
                target_blocks.setdefault("codex", {}).setdefault("omit", {})[
                    "background"
                ] = "Codex agents have no background field."
            if not only or "opencode" in only:
                target_blocks.setdefault("opencode", {}).setdefault("omit", {})[
                    "background"
                ] = "OpenCode agents have no background field."
        if meta.get("color") and (not only or "codex" in only):
            target_blocks.setdefault("codex", {}).setdefault("omit", {})["color"] = (
                "Codex agents have no color field."
            )
    if kind == "rule" and isinstance(activation := meta.pop("activation", None), dict):
        native = target_blocks.setdefault("cursor", {}).setdefault("native", {})
        native["always_apply"] = activation.get("always") is not False
        if activation.get("globs"):
            native["globs"] = activation["globs"]
    if kind == "skill":
        only = set(meta.get("only") or ())
        if meta.get("license"):
            if not only or "claude" in only:
                target_blocks.setdefault("claude", {}).setdefault("omit", {})[
                    "license"
                ] = "Claude skills have no license field."
            if not only or "cursor" in only:
                target_blocks.setdefault("cursor", {}).setdefault("omit", {})[
                    "license"
                ] = "Cursor skills have no license field."
        if meta.get("metadata") and (not only or "claude" in only):
            target_blocks.setdefault("claude", {}).setdefault("omit", {})[
                "metadata"
            ] = "Claude skills have no metadata field."
        if meta.get("paths"):
            if not only or "codex" in only:
                target_blocks.setdefault("codex", {}).setdefault("omit", {})[
                    "paths"
                ] = "Codex skills have no path activation."
            if not only or "opencode" in only:
                target_blocks.setdefault("opencode", {}).setdefault("omit", {})[
                    "paths"
                ] = "OpenCode skills have no path activation."
        if meta.get("disable_model_invocation"):
            if not only or "codex" in only:
                target_blocks.setdefault("codex", {}).setdefault("omit", {})[
                    "disable_model_invocation"
                ] = "Codex skills have no invocation toggle."
            if not only or "opencode" in only:
                target_blocks.setdefault("opencode", {}).setdefault("omit", {})[
                    "disable_model_invocation"
                ] = "OpenCode skills have no invocation toggle."
    if target_blocks:
        meta["targets"] = target_blocks
    yaml_text = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{yaml_text}\n---\n{body}"


type Rules = list[list[str] | dict[str, Any]]


def permission_policy_doc(
    *,
    secret_paths: list[str] | None = None,
    secret_names: list[str] | None = None,
    wrappers: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    source: dict[str, Any] = {
        "schema": "coding-agents/v4",
        "kind": "permission-policy",
        "id": "user",
        "name": "user",
        "description": "permissions",
        "wrappers": wrappers or [],
        "secret_paths": secret_paths or [],
        "secret_names": secret_names or [],
    }
    if extra:
        source.update(extra)
    targets: dict[str, dict[str, Any]] = {}
    if source.get("tools"):
        targets.setdefault("cursor", {}).setdefault("omit", {})["tools"] = (
            "Cursor lacks one shared tool permission surface."
        )
        targets.setdefault("codex", {}).setdefault("omit", {})["tools"] = (
            "Codex has no portable permission surface."
        )
    workspace = source.get("workspace", {})
    if workspace.get("allow") or workspace.get("ask"):
        targets.setdefault("cursor", {}).setdefault("omit", {})["workspace"] = (
            "Cursor lacks one shared workspace permission surface."
        )
        targets.setdefault("codex", {}).setdefault("omit", {})["workspace"] = (
            "Codex has no portable workspace permission surface."
        )
    if source["secret_paths"]:
        targets.setdefault("cursor", {}).setdefault("omit", {})["secret_paths"] = (
            "Cursor lacks a shared secret-path channel."
        )
        targets.setdefault("codex", {}).setdefault("omit", {})["secret_paths"] = (
            "Codex has no portable secret-path channel."
        )
    if source["secret_names"]:
        targets.setdefault("cursor", {}).setdefault("omit", {})["secret_names"] = (
            "Cursor has no secret-name ask channel."
        )
        targets.setdefault("codex", {}).setdefault("omit", {})["secret_names"] = (
            "Codex has no portable secret-name channel."
        )
    if workspace.get("ask"):
        targets.setdefault("claude", {}).setdefault("omit", {})["workspace.ask"] = (
            "Claude has no workspace ask channel."
        )
    if targets:
        source["targets"] = targets
    return yaml.safe_dump(source, sort_keys=False)


def permission_rules_doc(
    *,
    allow: Rules | None = None,
    ask: Rules | None = None,
    deny: Rules | None = None,
    options: dict[str, list[str]] | None = None,
    id_: str = "test",
    extra: dict[str, Any] | None = None,
) -> str:
    source: dict[str, Any] = {
        "schema": "coding-agents/v4",
        "kind": "permission-rules",
        "id": id_,
        "name": id_,
        "description": "permission rules",
        "options": options or {},
        "allow": allow or [],
        "ask": ask or [],
        "deny": deny or [],
    }
    if extra:
        source.update(extra)
    targets: dict[str, dict[str, Any]] = {}
    if source["allow"] or source["ask"] or source["deny"]:
        targets["codex"] = {
            "omit": {"commands": "Codex has no portable command policy."}
        }
    if source["deny"]:
        targets["cursor"] = {
            "omit": {"commands.deny": "Cursor Desktop has no deny channel."}
        }
    if targets:
        source["targets"] = targets
    return yaml.safe_dump(source, sort_keys=False)


def write_permissions(
    config_root: Path,
    *,
    allow: Rules | None = None,
    ask: Rules | None = None,
    deny: Rules | None = None,
    options: dict[str, list[str]] | None = None,
    wrappers: list[str] | None = None,
    secret_paths: list[str] | None = None,
    secret_names: list[str] | None = None,
    tools: dict[str, str] | None = None,
    workspace: dict[str, list[str]] | None = None,
) -> None:
    extra: dict[str, Any] = {}
    if tools is not None:
        extra["tools"] = tools
    if workspace is not None:
        extra["workspace"] = workspace
    write(
        config_root / "permissions" / "policy.yaml",
        permission_policy_doc(
            secret_paths=secret_paths,
            secret_names=secret_names,
            wrappers=wrappers,
            extra=extra or None,
        ),
    )
    write(
        config_root / "permissions" / "commands" / "test.yaml",
        permission_rules_doc(allow=allow, ask=ask, deny=deny, options=options),
    )


def config_root_home(tmp: str) -> tuple[Path, Path]:
    root = Path(tmp)
    return root / "coding-agents", root / "home"


class ManifestTests(unittest.TestCase):
    def test_prunes_unchanged_stale_entry_and_preserves_unmanifested_sibling(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "generated.md", "generated\n")
            write(root / "manual.md", "manual\n")
            sync_manifested_entries(root, {"generated.md"}, "file")

            sync_manifested_entries(root, set(), "file")

            self.assertFalse((root / "generated.md").exists())
            self.assertEqual((root / "manual.md").read_text(), "manual\n")

    def test_modified_generated_file_or_tree_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cases: tuple[tuple[str, Literal["dir", "file"]], ...] = (
                ("generated.md", "file"),
                ("skill", "dir"),
            )
            for name, mode in cases:
                target = root / name
                write(target if mode == "file" else target / "SKILL.md", "original\n")
                sync_manifested_entries(root, {name}, mode)
                write(target if mode == "file" else target / "SKILL.md", "modified\n")

                with (
                    self.subTest(mode=f"{mode} prune"),
                    self.assertRaisesRegex(ManagedEntryConflict, "prune modified"),
                ):
                    sync_manifested_entries(root, set(), mode)

                if mode == "file":
                    target.unlink()
                else:
                    shutil.rmtree(target)
                (root / MANAGED_MANIFEST).unlink()

    def test_legacy_manifest_is_migrated_to_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root / "generated.md", "generated\n")
            write(root / MANAGED_MANIFEST, '["generated.md"]\n')

            sync_manifested_entries(root, {"generated.md"}, "file")

            manifest = json.loads((root / MANAGED_MANIFEST).read_text())
            self.assertEqual(manifest["version"], 1)
            self.assertRegex(
                manifest["entries"]["generated.md"], r"^sha256:[0-9a-f]{64}$"
            )


def resolve_opencode_bash(patterns: dict[str, str], command: str) -> str:
    """Resolve one command against an OpenCode `permission.bash` map.

    Ports OpenCode v1.18.4 `Wildcard.match` and `Permission.evaluate`: patterns
    become anchored dotall regexes, a trailing " *" is optional, and the last
    matching entry in insertion order wins with "ask" as the default.
    """
    result = "ask"
    for pattern, action in patterns.items():
        escaped = re.sub(r"[.+^${}()|\[\]\\]", lambda m: "\\" + m.group(0), pattern)
        escaped = escaped.replace("*", ".*").replace("?", ".")
        if escaped.endswith(" .*"):
            escaped = escaped[:-3] + "( .*)?"
        if re.fullmatch(escaped, command, re.S):
            result = action
    return result


class SyncTests(unittest.TestCase):
    def test_an_ask_guards_an_allow_through_the_strictest_available_channel(
        self,
    ) -> None:
        """An allow is unconditional; its guard is best-effort.

        Cursor's CLI has no ask channel, so a narrowing ask is clawed back
        through its deny channel: the allowed family still runs, but the
        guarded variant stops and a plain ask never leaves the source. The
        tradeoff is absolute -- a CLI deny holds even under `--force`, so the
        guarded variant is unrunnable rather than approvable -- but the
        alternative is letting the dangerous variant ride the allow.
        Cursor Desktop has no deny channel either, so the narrowed allow
        rules leave its allowlist and prompt per invocation, which is
        ask-equivalent, while the rest of the family stays allowlisted.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["fd"], ["terraform", "show"], ["gh", "api"]],
                ask=[
                    {"command": "fd", "tail": ["-x", "--exec"]},
                    {
                        "command": "terraform",
                        "subcommand": ["show"],
                        "tail": ["-json"],
                    },
                    {
                        "command": "gh",
                        "subcommand": ["api"],
                        "tail": [["-X", "DELETE"]],
                    },
                ],
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertIn("Bash(fd *)", claude["permissions"]["allow"])
            self.assertEqual(
                [entry for entry in claude["permissions"]["ask"] if "fd" in entry],
                [
                    "Bash(fd --exec *)",
                    "Bash(fd * --exec *)",
                    # Claude Code drops the optional trailing run once a
                    # pattern holds a second wildcard, so the bare form is
                    # required for a guard sequence that ends the command.
                    "Bash(fd * --exec)",
                    "Bash(fd -x *)",
                    "Bash(fd * -x *)",
                    "Bash(fd * -x)",
                ],
            )
            self.assertIn("Bash(gh api * -X DELETE *)", claude["permissions"]["ask"])
            self.assertIn("Bash(terraform show -json *)", claude["permissions"]["ask"])

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode["permission"]["bash"]
            self.assertEqual(
                [key for key in bash if key.startswith("fd")],
                ["fd *", "fd --exec *", "fd * --exec *", "fd -x *", "fd * -x *"],
            )
            resolutions = {
                "fd -tf src": "allow",
                "fd -extra": "allow",
                "fd -x rm {}": "ask",
                "fd . -x rm {}": "ask",
                "fd --exec rm": "ask",
                "terraform show": "allow",
                "terraform show -no-color": "allow",
                "terraform show -json": "ask",
                "gh api repos/o/r": "allow",
                "gh api repos/o/r -X DELETE": "ask",
            }
            for command, decision in resolutions.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                [
                    "Shell(fd)",
                    "Shell(gh:api)",
                    "Shell(gh:api *)",
                    "Shell(terraform:show)",
                    "Shell(terraform:show *)",
                ],
            )
            self.assertEqual(
                cursor_cli["permissions"]["deny"],
                [
                    "Shell(fd:--exec)",
                    "Shell(fd:--exec *)",
                    "Shell(fd:* --exec)",
                    "Shell(fd:* --exec *)",
                    "Shell(fd:-x)",
                    "Shell(fd:-x *)",
                    "Shell(fd:* -x)",
                    "Shell(fd:* -x *)",
                    "Shell(gh:api -X DELETE)",
                    "Shell(gh:api -X DELETE *)",
                    "Shell(gh:api * -X DELETE)",
                    "Shell(gh:api * -X DELETE *)",
                    "Shell(terraform:show -json)",
                    "Shell(terraform:show -json *)",
                    "Shell(terraform:show * -json)",
                    "Shell(terraform:show * -json *)",
                ],
            )
            # Every allow rule is narrowed, so Desktop's allowlist is empty
            # and its file is not emitted at all.
            self.assertFalse((home / ".cursor/permissions.json").exists())

    def test_a_tail_predicate_is_accepted_on_an_allow(self) -> None:
        """`tail` and `text` narrow a grant, not only a guard.

        The hazard the old prohibition named -- a wildcard reaching a literal
        the decision depends on -- lives in the leading-option region, which
        is now enumerated from a declared vocabulary. A tail sequence sits
        after the head has already pinned both command and subcommand.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[
                    {
                        "command": "mytool",
                        "subcommand": ["run"],
                        "tail": [["--dry-run"]],
                    },
                    {"command": "othertool", "text": ["--check"]},
                ],
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode["permission"]["bash"]

            self.assertIn(
                "Bash(mytool run --dry-run *)", claude["permissions"]["allow"]
            )
            for command, decision in {
                "mytool run --dry-run": "allow",
                "mytool run plan --dry-run": "allow",
                "mytool run --apply": "ask",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

            # A token-bounded tail projects to Cursor; a text predicate does
            # not, since the token matcher cannot hold a substring. Only the
            # mytool family reaches either Cursor surface, while the two glob
            # targets still receive both exact native forms.
            self.assertIn("Bash(othertool*--check*)", claude["permissions"]["allow"])
            self.assertEqual(resolve_opencode_bash(bash, "othertool --check"), "allow")
            self.assertEqual(resolve_opencode_bash(bash, "othertool --write"), "ask")
            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                [
                    "Shell(mytool:run --dry-run)",
                    "Shell(mytool:run --dry-run *)",
                    "Shell(mytool:run * --dry-run)",
                    "Shell(mytool:run * --dry-run *)",
                ],
            )
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"],
                [
                    "mytool:run --dry-run",
                    "mytool:run --dry-run *",
                    "mytool:run * --dry-run",
                    "mytool:run * --dry-run *",
                ],
            )

    def test_leading_options_open_one_hole_in_each_target_shape(self) -> None:
        """A declared option token expands every head that has a right anchor.

        The glob targets attach the hole to the token, so `git -C* status *`
        covers `-C /tmp`, `-C/tmp`, `--context=prod`, and a valueless flag
        alike. Cursor Agent's token matcher cannot attach a hole to a
        literal, so both shapes are emitted there instead.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                options={"git": ["-C", "--no-pager"]},
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(
                claude["permissions"]["allow"],
                [
                    "Bash(git status *)",
                    "Bash(git -C* status *)",
                    "Bash(git -C* status)",
                    "Bash(git --no-pager* status *)",
                    "Bash(git --no-pager* status)",
                ],
            )
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode["permission"]["bash"]
            self.assertEqual(
                [key for key in bash if key.startswith("git")],
                ["git status *", "git -C* status *", "git --no-pager* status *"],
            )
            for command, decision in {
                "git status": "allow",
                "git -C /tmp status": "allow",
                "git -C/tmp status --short": "allow",
                "git --no-pager status": "allow",
                "git -C /tmp push": "ask",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                [
                    "Shell(git:status)",
                    "Shell(git:status *)",
                    "Shell(git:-C status)",
                    "Shell(git:-C status *)",
                    "Shell(git:--no-pager status)",
                    "Shell(git:--no-pager status *)",
                    "Shell(git:-C * status)",
                    "Shell(git:-C * status *)",
                    "Shell(git:--no-pager * status)",
                    "Shell(git:--no-pager * status *)",
                ],
            )
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["approvalMode"], "allowlist")
            self.assertEqual(
                desktop["terminalAllowlist"],
                [
                    "git:status",
                    "git:status *",
                    "git:-C status",
                    "git:-C status *",
                    "git:--no-pager status",
                    "git:--no-pager status *",
                    "git:-C * status",
                    "git:-C * status *",
                    "git:--no-pager * status",
                    "git:--no-pager * status *",
                ],
            )

    def test_a_deny_reaches_every_target_that_has_a_deny_channel(self) -> None:
        """Cursor Desktop's shipped schema has no deny key, so it degrades."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["kubectl", "get"]],
                deny=[["kubectl", "apply"]],
                options={"kubectl": ["--context"]},
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(
                claude["permissions"]["deny"],
                [
                    "Bash(kubectl apply *)",
                    "Bash(kubectl --context* apply *)",
                    "Bash(kubectl --context* apply)",
                ],
            )
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode["permission"]["bash"]
            for command, decision in {
                "kubectl get pods": "allow",
                "kubectl apply -f x.yaml": "deny",
                "kubectl --context prod apply -f x.yaml": "deny",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                [
                    "Shell(kubectl:get)",
                    "Shell(kubectl:get *)",
                    "Shell(kubectl:--context get)",
                    "Shell(kubectl:--context get *)",
                    "Shell(kubectl:--context * get)",
                    "Shell(kubectl:--context * get *)",
                ],
            )
            self.assertEqual(
                cursor_cli["permissions"]["deny"],
                [
                    "Shell(kubectl:apply)",
                    "Shell(kubectl:apply *)",
                    "Shell(kubectl:--context apply)",
                    "Shell(kubectl:--context apply *)",
                    "Shell(kubectl:--context * apply)",
                    "Shell(kubectl:--context * apply *)",
                ],
            )
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"],
                [
                    "kubectl:get",
                    "kubectl:get *",
                    "kubectl:--context get",
                    "kubectl:--context get *",
                    "kubectl:--context * get",
                    "kubectl:--context * get *",
                ],
            )

    def test_a_bare_exact_rule_projects_the_exact_bare_form(self) -> None:
        """`prog:` is the exact-bare shape: the program with no arguments.

        The bare-exact live scenarios prove `Shell(prog:)` runs the bare
        command and rejects any argument, so a bare exact rule projects
        instead of being dropped from both Cursor surfaces.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[{"command": "fd", "exact": True}],
                deny=[{"command": "shred", "exact": True}],
            )

            run_sync(config_root=config_root, home=home)

            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(cursor_cli["permissions"]["allow"], ["Shell(fd:)"])
            self.assertEqual(cursor_cli["permissions"]["deny"], ["Shell(shred:)"])
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["terminalAllowlist"], ["fd:"])

    def test_an_unprovable_overlap_is_clawed_back_with_a_warning(self) -> None:
        """A prefix-sharing ask that cannot narrow is guarded, not dropped.

        The ask re-expresses the allow's tail as subcommand tokens, so
        `narrows` cannot prove containment and the overlap would ride the
        allow. The ask's own forms are clawed back through the CLI deny
        channel, the colliding allow rule leaves Desktop's allowlist, and the
        source earns a warning because the clawback is coarser than the ask.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[
                    {
                        "command": "terraform",
                        "subcommand": ["show"],
                        "tail": [["-json"]],
                    }
                ],
                ask=[{"command": "terraform", "subcommand": ["show", "-json"]}],
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run_sync(config_root=config_root, home=home)

            self.assertIn("guarded overlap terraform show -json", stderr.getvalue())
            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                [
                    "Shell(terraform:show -json)",
                    "Shell(terraform:show -json *)",
                    "Shell(terraform:show * -json)",
                    "Shell(terraform:show * -json *)",
                ],
            )
            self.assertEqual(
                cursor_cli["permissions"]["deny"],
                [
                    "Shell(terraform:show -json)",
                    "Shell(terraform:show -json *)",
                ],
            )
            self.assertFalse((home / ".cursor/permissions.json").exists())

    def test_a_distinct_subcommand_ask_stays_standalone(self) -> None:
        """No shared prefix, no overlap: the ask simply does not project."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                ask=[["git", "commit"]],
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run_sync(config_root=config_root, home=home)

            self.assertNotIn("guarded overlap", stderr.getvalue())
            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                ["Shell(git:status)", "Shell(git:status *)"],
            )
            self.assertNotIn("deny", cursor_cli["permissions"])
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"], ["git:status", "git:status *"]
            )

    def test_a_narrowing_excludes_only_the_colliding_rule_on_desktop(self) -> None:
        """Desktop keeps the family's safe rules when one rule is narrowed.

        Only the allow rule the gated variant rides leaves the Desktop
        allowlist; its siblings stay allowlisted instead of the whole
        command family prompting per invocation.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"], ["git", "push"]],
                ask=[
                    {
                        "command": "git",
                        "subcommand": ["push"],
                        "tail": [["--force"]],
                    }
                ],
            )

            run_sync(config_root=config_root, home=home)

            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                [
                    "Shell(git:push)",
                    "Shell(git:push *)",
                    "Shell(git:status)",
                    "Shell(git:status *)",
                ],
            )
            self.assertEqual(
                cursor_cli["permissions"]["deny"],
                [
                    "Shell(git:push --force)",
                    "Shell(git:push --force *)",
                    "Shell(git:push * --force)",
                    "Shell(git:push * --force *)",
                ],
            )
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"], ["git:status", "git:status *"]
            )

    def test_a_text_ask_unlowers_only_the_colliding_rule(self) -> None:
        """A text guard costs the colliding rule, not the command family.

        The kubectl secret guard has no token-matcher form, so the allow it
        narrows leaves both Cursor allowlists, while unrelated kubectl
        subcommands stay allowlisted on both surfaces.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["kubectl", "get"], ["kubectl", "logs"]],
                ask=[
                    {
                        "command": "kubectl",
                        "subcommand": ["get", "secret"],
                        "text": ["yaml"],
                    }
                ],
            )

            run_sync(config_root=config_root, home=home)

            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                ["Shell(kubectl:logs)", "Shell(kubectl:logs *)"],
            )
            self.assertNotIn("deny", cursor_cli["permissions"])
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"], ["kubectl:logs", "kubectl:logs *"]
            )

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode["permission"]["bash"]
            for command, decision in {
                "kubectl get pods": "allow",
                "kubectl get secret db -o yaml": "ask",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

    def test_a_tilde_command_projects_literally(self) -> None:
        """Home-relative command text is a valid portable token.

        Agents type `~/.config/coding-agents/sync.sh`; the tilde form is the
        command text matchers compare against, so the token alphabet carries
        `~` and every target projects it unexpanded.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["~/.config/coding-agents/sync.sh"]],
            )

            run_sync(config_root=config_root, home=home)

            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                ["Shell(~/.config/coding-agents/sync.sh)"],
            )
            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertIn(
                "Bash(~/.config/coding-agents/sync.sh *)",
                claude["permissions"]["allow"],
            )

    def test_an_option_embedding_sibling_rule_leaves_desktop_too(self) -> None:
        """A rule whose subcommand embeds declared options also collides.

        Its option-hole heads (`prog:--profile * --profile status *`) can
        carry the gated variant even though the ask narrows only the plain
        sibling, so both local-tool rules leave Desktop's allowlist.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                options={"local-tool": ["--profile"]},
                allow=[
                    ["git", "status"],
                    ["local-tool", "status"],
                    ["local-tool", "--profile", "status"],
                ],
                ask=[
                    {
                        "command": "local-tool",
                        "subcommand": ["status"],
                        "tail": [["--destroy"]],
                    }
                ],
            )

            run_sync(config_root=config_root, home=home)

            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertIn(
                "Shell(local-tool:--profile status)",
                cursor_cli["permissions"]["allow"],
            )
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"], ["git:status", "git:status *"]
            )

    def test_an_interpreter_allow_is_never_projected(self) -> None:
        """Shell(sh) would allow every payload the interpreter runs.

        The live interpreter scenarios prove the payload rides the allow, so
        a declared interpreter allow is skipped with a warning rather than
        projected to either Cursor surface.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["sh"], ["bash"], ["zsh"], ["eval"], ["git", "status"]],
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run_sync(config_root=config_root, home=home)

            warnings = stderr.getvalue()
            for interpreter in ("sh", "bash", "zsh", "eval"):
                with self.subTest(interpreter=interpreter):
                    self.assertIn(
                        f"omitted allow {interpreter}: Cursor cannot confine "
                        "an interpreter payload",
                        warnings,
                    )
            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                ["Shell(git:status)", "Shell(git:status *)"],
            )
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"], ["git:status", "git:status *"]
            )

    def test_an_option_embedding_ask_is_a_guarded_overlap(self) -> None:
        """`git -C foo status` rides the emitted `git:-C * status` head.

        The declared option vocabulary expands every allow head, so an ask
        whose subcommand embeds those tokens overlaps the allow even though
        the raw subcommand prefix differs. It is clawed back and warned.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                ask=[["git", "-C", "foo", "status"]],
                options={"git": ["-C"]},
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run_sync(config_root=config_root, home=home)

            self.assertIn("guarded overlap git -C foo status", stderr.getvalue())
            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["deny"],
                [
                    "Shell(git:-C foo status)",
                    "Shell(git:-C foo status *)",
                    "Shell(git:-C -C foo status)",
                    "Shell(git:-C -C foo status *)",
                    "Shell(git:-C * -C foo status)",
                    "Shell(git:-C * -C foo status *)",
                ],
            )
            self.assertFalse((home / ".cursor/permissions.json").exists())

    def test_an_undeclared_leading_token_ask_stays_standalone(self) -> None:
        """Only declared option tokens open the hole; `--quiet` does not."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                ask=[["git", "--quiet", "status"]],
                options={"git": ["-C"]},
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run_sync(config_root=config_root, home=home)

            self.assertNotIn("guarded overlap", stderr.getvalue())
            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertNotIn("deny", cursor_cli["permissions"])
            self.assertTrue((home / ".cursor/permissions.json").exists())

    def test_an_exact_allow_admits_nothing_for_an_extending_ask_to_overlap(
        self,
    ) -> None:
        """An exact allow matches only its bare subcommand, so a longer ask
        shares no match with it: no clawback, no exclusion, no warning."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[{"command": "git", "subcommand": ["status"], "exact": True}],
                ask=[["git", "status", "--short"]],
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run_sync(config_root=config_root, home=home)

            self.assertNotIn("guarded overlap", stderr.getvalue())
            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(cursor_cli["permissions"]["allow"], ["Shell(git:status)"])
            self.assertNotIn("deny", cursor_cli["permissions"])
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["terminalAllowlist"], ["git:status"])

    def test_a_text_deny_warns_when_it_cannot_project(self) -> None:
        """A deny the token matcher cannot hold degrades to a prompt, but
        the drop is never silent."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                deny=[{"command": "git", "subcommand": ["push"], "text": ["--force"]}],
            )

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run_sync(config_root=config_root, home=home)

            self.assertIn(
                "omitted deny git push ~--force: Cursor's token matcher "
                "cannot hold a text predicate",
                stderr.getvalue(),
            )
            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertNotIn("deny", cursor_cli["permissions"])

    def test_opencode_orders_buckets_so_the_strictest_decision_matches_last(
        self,
    ) -> None:
        """Insertion order is the whole precedence contract on this target.

        `Permission.evaluate` applies the last matching key, so a stricter
        decision must sit after every looser pattern it has to beat.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                ask=[["rmdir"]],
                deny=[["shred"]],
                secret_names=["TFE_TOKEN"],
            )

            run_sync(config_root=config_root, home=home)

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode["permission"]["bash"]
            self.assertEqual(
                list(bash),
                ["*", "git status *", "rmdir *", "*TFE_TOKEN*", "shred *"],
            )
            self.assertEqual(
                [bash[key] for key in bash],
                ["ask", "allow", "ask", "ask", "deny"],
            )

    def test_wrapper_guards_bind_where_the_target_cannot_peel_the_wrapper(self) -> None:
        """One blanket allow per wrapper, plus a wrapped copy of every guard.

        Claude Code resolves `timeout` to its payload and takes the strictest
        verdict across both forms, so it needs neither; emitting a blanket
        allow there would widen every wrapped command that has no rule. It
        does not resolve `env`, so it receives both for that one. Cursor's
        token matcher cannot peel a wrapper either, but a bare `Shell(env)`
        allow would match any payload the wrapper carries, so Cursor receives
        only rule-attached wrapper forms and no blanket.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                ask=[["rmdir"]],
                wrappers=["timeout", "env"],
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(
                claude["permissions"]["allow"], ["Bash(env *)", "Bash(git status *)"]
            )
            self.assertEqual(
                claude["permissions"]["ask"],
                [
                    "Bash(rmdir *)",
                    "Bash(env rmdir *)",
                    "Bash(env * rmdir *)",
                    "Bash(env * rmdir)",
                ],
            )

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode["permission"]["bash"]
            self.assertEqual(
                list(bash),
                [
                    "*",
                    "timeout *",
                    "env *",
                    "git status *",
                    "rmdir *",
                    "timeout rmdir *",
                    "timeout * rmdir *",
                    "env rmdir *",
                    "env * rmdir *",
                ],
            )
            for command, decision in {
                "timeout 5 git status": "allow",
                "timeout 5 rmdir build": "ask",
                "env FOO=1 rmdir build": "ask",
                # The wrapper may sit directly before its payload, and the
                # blanket allow covers that shape, so the guard must too.
                "env rmdir build": "ask",
                "timeout rmdir build": "ask",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                [
                    "Shell(git:status)",
                    "Shell(git:status *)",
                    "Shell(timeout:git status)",
                    "Shell(timeout:git status *)",
                    "Shell(timeout:* git status)",
                    "Shell(timeout:* git status *)",
                    "Shell(env:git status)",
                    "Shell(env:git status *)",
                    "Shell(env:* git status)",
                    "Shell(env:* git status *)",
                ],
            )
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"],
                [
                    "git:status",
                    "git:status *",
                    "timeout:git status",
                    "timeout:git status *",
                    "timeout:* git status",
                    "timeout:* git status *",
                    "env:git status",
                    "env:git status *",
                    "env:* git status",
                    "env:* git status *",
                ],
            )

    def test_codex_receives_no_user_command_permissions(self) -> None:
        """Its rules file carries only hand-authored Starlark.

        Codex exec policy has no wildcards and lapses entirely on any `$VAR`
        or substitution, so it is not a containment boundary.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                ask=[["rmdir"]],
                deny=[["shred"]],
                secret_paths=["**/.env"],
                secret_names=["TFE_TOKEN"],
            )
            write(
                config_root / "rules" / "python.md",
                source_doc(
                    "rule",
                    "python",
                    "python",
                    "Rule body\n",
                    extra={
                        "codex:rules": [
                            {"pattern": ["python"], "decision": "prompt"},
                        ]
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            codex_rules = (home / ".codex/rules/coding-agents.rules").read_text()
            self.assertIn('pattern=["python"]', codex_rules)
            for absent in ("git", "rmdir", "shred", "TFE_TOKEN", ".env"):
                with self.subTest(absent=absent):
                    self.assertNotIn(absent, codex_rules)

    def test_local_command_fragments_merge_and_are_validated(self) -> None:
        """Work-specific commands stay out of committed sources.

        Local fragments are the replacement for the closed patch route, so
        they must reach every target and share the overlap invariant.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
            local = config_root / "permissions.local"
            write(
                local / "local.yaml",
                "schema: coding-agents/v4\n"
                "kind: permission-rules\n"
                "id: fixture-local\n"
                "name: Fixture local\n"
                "description: Machine-local tooling.\n"
                "options:\n"
                "  local-tool: [--profile]\n"
                "allow:\n"
                "- [local-tool, status]\n"
                "ask:\n"
                "- command: local-tool\n"
                "  subcommand: [status]\n"
                "  tail: [[--destroy]]\n"
                "deny:\n"
                "- [local-tool, wipe]\n"
                "targets:\n"
                "  cursor:\n"
                "    omit:\n"
                "      commands.deny: Cursor Desktop has no deny channel.\n"
                "  codex:\n"
                "    omit:\n"
                "      commands: Codex has no portable command policy.\n",
            )

            run_sync(config_root=config_root, home=home)

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode["permission"]["bash"]
            for command, decision in {
                "local-tool status": "allow",
                "local-tool --profile fixture status": "allow",
                "local-tool status --destroy": "ask",
                "local-tool wipe": "deny",
                "git status": "allow",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)
            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertIn("Bash(local-tool status *)", claude["permissions"]["allow"])
            self.assertIn(
                "Bash(local-tool status --destroy *)", claude["permissions"]["ask"]
            )
            self.assertIn("Bash(local-tool wipe *)", claude["permissions"]["deny"])
            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_cli["permissions"]["allow"],
                [
                    "Shell(git:status)",
                    "Shell(git:status *)",
                    "Shell(local-tool:status)",
                    "Shell(local-tool:status *)",
                    "Shell(local-tool:--profile status)",
                    "Shell(local-tool:--profile status *)",
                    "Shell(local-tool:--profile * status)",
                    "Shell(local-tool:--profile * status *)",
                ],
            )
            self.assertEqual(
                cursor_cli["permissions"]["deny"],
                [
                    "Shell(local-tool:wipe)",
                    "Shell(local-tool:wipe *)",
                    "Shell(local-tool:--profile wipe)",
                    "Shell(local-tool:--profile wipe *)",
                    "Shell(local-tool:--profile * wipe)",
                    "Shell(local-tool:--profile * wipe *)",
                    "Shell(local-tool:status --destroy)",
                    "Shell(local-tool:status --destroy *)",
                    "Shell(local-tool:status * --destroy)",
                    "Shell(local-tool:status * --destroy *)",
                    "Shell(local-tool:--profile status --destroy)",
                    "Shell(local-tool:--profile status --destroy *)",
                    "Shell(local-tool:--profile status * --destroy)",
                    "Shell(local-tool:--profile status * --destroy *)",
                    "Shell(local-tool:--profile * status --destroy)",
                    "Shell(local-tool:--profile * status --destroy *)",
                    "Shell(local-tool:--profile * status * --destroy)",
                    "Shell(local-tool:--profile * status * --destroy *)",
                ],
            )
            # The narrowed local-tool rule leaves Desktop's allowlist.
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"], ["git:status", "git:status *"]
            )

    def test_local_command_fragment_conflicting_with_committed_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
            write(
                config_root / "permissions.local/dup.yaml",
                "schema: coding-agents/v4\n"
                "kind: permission-rules\n"
                "id: dup\n"
                "name: Dup\n"
                "description: Conflicts with the committed corpus.\n"
                "ask:\n"
                "- [git, status]\n",
            )

            with self.assertRaisesRegex(SourceSchemaError, "duplicate permission rule"):
                run_sync(config_root=config_root, home=home)

    def test_empty_permission_fragment_needs_no_command_omission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(config_root / "permissions/policy.yaml", permission_policy_doc())
            write(
                config_root / "permissions/commands/empty.yaml",
                "schema: coding-agents/v4\n"
                "kind: permission-rules\n"
                "id: empty\n"
                "name: Empty\n",
            )

            run_sync(config_root=config_root, home=home)

            self.assertTrue((home / ".claude/settings.json").exists())

    def test_command_fragment_omissions_track_each_targets_projection_gap(
        self,
    ) -> None:
        # Cursor projects allows losslessly, so only Codex owes an omission.
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(config_root / "permissions/policy.yaml", permission_policy_doc())
            write(
                config_root / "permissions/commands/nonempty.yaml",
                "schema: coding-agents/v4\n"
                "kind: permission-rules\n"
                "id: nonempty\n"
                "name: Nonempty\n"
                "allow: [[git, status]]\n",
            )

            with self.assertRaisesRegex(ValueError, "commands is unsupported on codex"):
                run_sync(config_root=config_root, home=home)

        # Desktop has no deny channel, so a deny fragment owes the Cursor
        # commands.deny omission even though the CLI projects it.
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(config_root / "permissions/policy.yaml", permission_policy_doc())
            write(
                config_root / "permissions/commands/nonempty.yaml",
                "schema: coding-agents/v4\n"
                "kind: permission-rules\n"
                "id: nonempty\n"
                "name: Nonempty\n"
                "deny: [[git, push]]\n"
                "targets:\n"
                "  codex:\n"
                "    omit:\n"
                "      commands: Codex has no portable command policy.\n",
            )

            with self.assertRaisesRegex(
                ValueError, "commands.deny is unsupported on cursor"
            ):
                run_sync(config_root=config_root, home=home)

    def test_patch_cannot_contribute_command_permissions(self) -> None:
        """Command decisions have one writer, so the patch route is closed."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["kubectl", "get"]])
            write(
                config_root / "patches/opencode.yaml",
                "schema: coding-agents/patch/v1\n"
                "overlay:\n"
                "  /permission/bash:\n"
                "    'kubectl get secret*yaml*': ask\n",
            )

            with self.assertRaisesRegex(
                PatchError, "cannot contribute command permissions"
            ):
                run_sync(config_root=config_root, home=home)
            self.assertFalse((home / ".config/opencode/opencode.json").exists())

    def test_secret_output_formats_are_expressible_in_the_source(self) -> None:
        """`text` covers -o yaml, -oyaml, and --output=yaml alike.

        `tail` is token-level and would miss the attached spellings, which is
        why the kubectl secret guard uses `text` instead.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["kubectl", "get"]],
                ask=[
                    {
                        "command": "kubectl",
                        "subcommand": ["get", "secret"],
                        "text": ["yaml"],
                    }
                ],
            )

            run_sync(config_root=config_root, home=home)

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode["permission"]["bash"]
            for command, decision in {
                "kubectl get pods": "allow",
                "kubectl get secret db": "allow",
                "kubectl get secret db -o yaml": "ask",
                "kubectl get secret db -oyaml": "ask",
                "kubectl get secret db --output=yaml": "ask",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

            # The guard's text predicate has no token-matcher form, so the
            # colliding kubectl get rule leaves the CLI allowlist rather than
            # letting the secret dump ride it. Desktop loses it too.
            cursor_cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertNotIn("permissions", cursor_cli)
            self.assertFalse((home / ".cursor/permissions.json").exists())

    def test_a_patch_may_add_non_command_grants_to_generated_permissions(self) -> None:
        """Tool and MCP grants have no portable schema, so a patch owns them.

        The generated Bash(...) entries must survive alongside them: extending
        contributes to the compiler's list rather than replacing it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
            write(
                config_root / "patches/claude-settings.yaml",
                "schema: coding-agents/patch/v1\n"
                "extend:\n"
                "  /permissions/allow: [WebFetch(*), mcp__github__get_me]\n",
            )

            run_sync(config_root=config_root, home=home)

            settings = json.loads((home / ".claude/settings.json").read_text())
            self.assertIn("Bash(git status *)", settings["permissions"]["allow"])
            self.assertIn("WebFetch(*)", settings["permissions"]["allow"])
            self.assertIn("mcp__github__get_me", settings["permissions"]["allow"])

    def test_a_patch_may_add_command_denials_to_claude(self) -> None:
        """A patch contributes freely where it does not clash.

        The compiler emits no Bash denies, so this adds rather than contends.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
            write(
                config_root / "patches/claude-settings.yaml",
                "schema: coding-agents/patch/v1\n"
                "extend:\n"
                "  /permissions/deny: [Bash(cat ~/.netrc)]\n",
            )

            run_sync(config_root=config_root, home=home)

            settings = json.loads((home / ".claude/settings.json").read_text())
            self.assertIn("Bash(cat ~/.netrc)", settings["permissions"]["deny"])

    def test_a_patch_may_add_mcp_grants_to_cursor_cli(self) -> None:
        """Per-server Mcp() grants have no portable schema, so a patch owns them."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
            write(
                config_root / "patches/cursor-cli-config.yaml",
                "schema: coding-agents/patch/v1\n"
                "extend:\n"
                "  /permissions/allow: [Mcp(looker-mcp:get_*)]\n",
            )

            run_sync(config_root=config_root, home=home)

            cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertIn("Shell(git:status)", cli["permissions"]["allow"])
            self.assertIn("Mcp(looker-mcp:get_*)", cli["permissions"]["allow"])

    def test_a_patch_may_overlay_the_generated_workspace_roots(self) -> None:
        """A patch adds keys the portable workspace block cannot express.

        The decision is the patch author's: a machine-local credential path can
        be denied, and a machine-local root can be granted. The generated
        entries survive either way, because an overlay adds rather than clashes.
        """
        for decision in ("deny", "allow"):
            with self.subTest(decision=decision), tempfile.TemporaryDirectory() as tmp:
                config_root, home = config_root_home(tmp)
                write_permissions(
                    config_root,
                    allow=[["git", "status"]],
                    workspace={"allow": ["~/Developer"], "ask": []},
                )
                write(
                    config_root / "patches/opencode.yaml",
                    yaml.safe_dump(
                        {
                            "schema": "coding-agents/patch/v1",
                            "overlay": {
                                "/permission/external_directory": {"~/.netrc": decision}
                            },
                        },
                        sort_keys=False,
                    ),
                )

                run_sync(config_root=config_root, home=home)

                config = json.loads(
                    (home / ".config/opencode/opencode.json").read_text()
                )
                external = config["permission"]["external_directory"]
                self.assertEqual(external["~/.netrc"], decision)
                self.assertEqual(external["~/Developer/**"], "allow")

    def test_a_stricter_rule_may_narrow_a_looser_one(self) -> None:
        """Containment is legal downward: allow > ask > deny."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "push"]],
                ask=[["git", "push", "--force-with-lease"]],
                deny=[["git", "push", "--force"]],
            )

            run_sync(config_root=config_root, home=home)

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode["permission"]["bash"]
            for command, decision in {
                "git push": "allow",
                "git push --force-with-lease": "ask",
                "git push --force": "deny",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

    def test_permission_composition_and_unsupported_intent_fail(self) -> None:
        for secret_paths in (["**/.env", "**/.env"], ["path with spaces"]):
            with (
                self.subTest(secret_paths=secret_paths),
                tempfile.TemporaryDirectory() as tmp,
            ):
                config_root, home = config_root_home(tmp)
                write(
                    config_root / "permissions" / "policy.yaml",
                    permission_policy_doc(secret_paths=secret_paths),
                )
                with self.assertRaises(SourceSchemaError):
                    run_sync(config_root=config_root, home=home)

        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
            write(
                config_root / "permissions" / "commands" / "other.yaml",
                permission_rules_doc(allow=[["git", "status"]], id_="other"),
            )
            with self.assertRaises(SourceSchemaError):
                run_sync(config_root=config_root, home=home)

        # A command's leading-option vocabulary has one home, so a second
        # declaration is an error rather than a silent union.
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root, allow=[["git", "status"]], options={"git": ["-C"]}
            )
            write(
                config_root / "permissions" / "commands" / "other.yaml",
                permission_rules_doc(
                    allow=[["git", "log"]], options={"git": ["-c"]}, id_="other"
                ),
            )
            with self.assertRaisesRegex(
                SourceSchemaError, "a command's option vocabulary has one home"
            ):
                run_sync(config_root=config_root, home=home)

        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "permissions" / "policy.yaml",
                permission_policy_doc(extra={"defaults": {"unmatched": "ask"}}),
            )
            write(
                config_root / "permissions" / "commands" / "test.yaml",
                permission_rules_doc(),
            )
            with self.assertRaises(SourceSchemaError):
                run_sync(config_root=config_root, home=home)

        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(config_root / "permissions.yaml", permission_policy_doc())
            with self.assertRaisesRegex(SourceSchemaError, "legacy permission source"):
                run_sync(config_root=config_root, home=home)

    def test_codex_rules_emit_without_global_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "python.md",
                source_doc("rule", "python", "python", "Rule body\n"),
            )

            run_sync(config_root=config_root, home=home)

            codex_global = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
            self.assertEqual(codex_global, "Rule body\n")

    def test_reserved_generated_rule_filename_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "coding-agents-global.md",
                source_doc(
                    "rule",
                    "coding-agents-global",
                    "reserved",
                    "Rule body\n",
                ),
            )

            with self.assertRaisesRegex(SourceSchemaError, "reserved rule filename"):
                run_sync(config_root=config_root, home=home)

    def test_global_rules_skills_agents_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)

            write(
                config_root / "global" / "AGENTS.md",
                source_doc("global", "global", "Team defaults", "Global body\n"),
            )
            write(
                config_root / "rules" / "python.md",
                source_doc(
                    "rule",
                    "python",
                    "python",
                    "Rule body\n",
                    description="Python rules",
                    extra={
                        "activation": {"always": True},
                        "codex:rules": [
                            {
                                "pattern": ["python"],
                                "decision": "prompt",
                                "justification": "Python scripts need review.",
                            }
                        ],
                        "opencode:instructions": (
                            "~/.config/coding-agents/rules/overridden-python.md"
                        ),
                    },
                ),
            )
            write(
                config_root / "skills" / "s1" / "SKILL.md",
                source_doc(
                    "skill",
                    "s1",
                    "s1",
                    "Skill body\n",
                    description="skill",
                    extra={"paths": ["**/*.py"], "metadata": {"owner": "platform"}},
                ),
            )
            write(
                config_root / "agents" / "reviewer.md",
                source_doc(
                    "agent",
                    "reviewer",
                    "reviewer",
                    "Agent body\n",
                    description="Review code\nacross multiple lines",
                    extra={
                        "effort": "high",
                        "color": "blue",
                        "claude:model": "claude-override",
                        "opencode:mode": "subagent",
                        "opencode:permission": {"bash": "ask"},
                        "opencode:model": "opencode-model",
                        "opencode:color": "info",
                        "codex:model": "gpt-5",
                        "codex:nickname_candidates": ["Ada"],
                    },
                ),
            )
            write(
                config_root / "commands" / "review-pr.md",
                source_doc(
                    "command",
                    "review-pr",
                    "review-pr",
                    "Review pull request $ARGUMENTS.\n",
                    description="Review a pull request",
                    extra={
                        "execution": {"agent": "reviewer", "subtask": True},
                        "claude:model": "opus",
                        "opencode:model": "gpt-5",
                    },
                ),
            )
            run_sync(config_root=config_root, home=home)

            for path in [
                home / ".claude" / "CLAUDE.md",
                home / ".config" / "opencode" / "AGENTS.md",
            ]:
                self.assertTrue(path.exists(), str(path))
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("---", text)
                self.assertIn("Global body", text)

            codex_global = (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("Global body", codex_global)
            self.assertIn("Rule body", codex_global)

            self.assertFalse((home / ".cursor" / "AGENTS.md").exists())
            cursor_global = (
                home / ".cursor" / "rules" / "coding-agents-global.mdc"
            ).read_text()
            _, global_frontmatter, global_body = cursor_global.split("---", 2)
            self.assertEqual(
                yaml.safe_load(global_frontmatter),
                {
                    "description": "Team defaults",
                    "globs": "",
                    "alwaysApply": True,
                },
            )
            self.assertEqual(global_body.strip(), "Global body")
            cursor_cli_rule = home / ".cursor" / "rules" / "python.mdc"
            self.assertTrue(cursor_cli_rule.exists())
            _, cursor_frontmatter, cursor_body = cursor_cli_rule.read_text().split(
                "---", 2
            )
            self.assertEqual(
                yaml.safe_load(cursor_frontmatter),
                {
                    "description": "Python rules",
                    "globs": "",
                    "alwaysApply": True,
                },
            )
            self.assertEqual(cursor_body.strip(), "Rule body")
            self.assertFalse(
                (
                    home / ".cursor" / "plugins" / "local" / "coding-agents-rules"
                ).exists()
            )

            claude_rule = (home / ".claude" / "rules" / "python.md").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("paths:", claude_rule)
            self.assertNotIn("---", claude_rule)
            self.assertIn("Rule body", claude_rule)

            for path in [
                home / ".cursor" / "skills" / "s1" / "SKILL.md",
                home / ".claude" / "skills" / "s1" / "SKILL.md",
                home / ".config" / "opencode" / "skills" / "s1" / "SKILL.md",
            ]:
                self.assertTrue(path.exists(), str(path))
            claude_skill = (home / ".claude" / "skills" / "s1" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("paths:", claude_skill)

            claude_agent = (home / ".claude" / "agents" / "reviewer.md").read_text(
                encoding="utf-8"
            )
            opencode_agent = (
                home / ".config" / "opencode" / "agents" / "reviewer.md"
            ).read_text(encoding="utf-8")
            self.assertIn("model: claude-override", claude_agent)
            self.assertIn("effort: high", claude_agent)
            self.assertIn("color: blue", claude_agent)
            self.assertFalse((home / ".cursor" / "agents" / "reviewer.md").exists())
            self.assertIn("mode: subagent", opencode_agent)
            self.assertIn("bash: ask", opencode_agent)
            self.assertIn("model: opencode-model", opencode_agent)
            self.assertIn("color: info", opencode_agent)
            self.assertNotIn("model: claude-override", opencode_agent)

            opencode_command = (
                home / ".config" / "opencode" / "commands" / "review-pr.md"
            ).read_text(encoding="utf-8")
            claude_command = (home / ".claude" / "commands" / "review-pr.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("description: Review a pull request", opencode_command)
            self.assertIn("agent: reviewer", opencode_command)
            self.assertIn("subtask: true", opencode_command)
            self.assertIn("model: gpt-5", opencode_command)
            self.assertIn("Review pull request $ARGUMENTS.", opencode_command)
            self.assertIn("description: Review a pull request", claude_command)
            self.assertIn("agent: reviewer", claude_command)
            self.assertIn("context: fork", claude_command)
            self.assertIn("model: opus", claude_command)
            self.assertNotIn("gpt-5", claude_command)
            self.assertNotIn("subtask:", claude_command)
            self.assertIn("Review pull request $ARGUMENTS.", claude_command)

            codex_cfg = home / ".codex" / "config.toml"
            self.assertTrue(codex_cfg.exists())
            cfg = codex_cfg.read_text(encoding="utf-8")
            self.assertIn("[[skills.config]]", cfg)
            self.assertIn('path = "~/.codex/skills/s1"', cfg)
            self.assertIn("[agents.reviewer]", cfg)
            self.assertIn('config_file = "~/.codex/agents/reviewer.toml"', cfg)
            parsed_codex_cfg = tomllib.loads(cfg)
            self.assertEqual(
                parsed_codex_cfg["agents"]["reviewer"]["description"],
                "Review code\nacross multiple lines",
            )

            codex_agent = (home / ".codex" / "agents" / "reviewer.toml").read_text(
                encoding="utf-8"
            )
            self.assertIn('name = "reviewer"', codex_agent)
            self.assertIn(
                'description = "Review code\\nacross multiple lines"', codex_agent
            )
            self.assertIn('developer_instructions = "Agent body"', codex_agent)
            self.assertIn('model = "gpt-5"', codex_agent)
            self.assertIn('model_reasoning_effort = "high"', codex_agent)
            self.assertIn('nickname_candidates = ["Ada"]', codex_agent)
            parsed_codex_agent = tomllib.loads(codex_agent)
            self.assertEqual(parsed_codex_agent["developer_instructions"], "Agent body")

            self.assertFalse((home / ".codex" / "skills" / "review-pr").exists())
            codex_rules = (home / ".codex" / "rules" / "coding-agents.rules").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                'prefix_rule(pattern=["python"], decision="prompt", '
                'justification="Python scripts need review.")',
                codex_rules,
            )

    def test_cursor_retires_legacy_global_and_emits_shared_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "global" / "AGENTS.md",
                source_doc("global", "global", "global", "Global body\n"),
            )
            write(
                config_root / "rules" / "python.md",
                source_doc("rule", "python", "python", "Rule body\n"),
            )
            write(
                config_root / "skills" / "s1" / "SKILL.md",
                source_doc("skill", "s1", "s1", "Skill body\n", description="skill"),
            )
            write(
                config_root / "agents" / "reviewer.md",
                source_doc(
                    "agent",
                    "reviewer",
                    "reviewer",
                    "Agent body\n",
                    description="Reviewer",
                ),
            )
            cursor = home / ".cursor"
            write(cursor / "AGENTS.md", "Global body\n")
            write(cursor / "manual.md", "manual\n")
            write(cursor / "rules" / "python.mdc", "generated rule\n")
            write(cursor / "rules" / "manual.mdc", "manual rule\n")
            sync_manifested_entries(cursor, {"AGENTS.md"}, "file")
            sync_manifested_entries(cursor / "rules", {"python.mdc"}, "file")

            run_sync(config_root=config_root, home=home)

            self.assertFalse((cursor / "AGENTS.md").exists())
            self.assertEqual(
                json.loads((cursor / MANAGED_MANIFEST).read_text()),
                {"version": 1, "entries": {}},
            )
            self.assertIn(
                "Global body",
                (cursor / "rules" / "coding-agents-global.mdc").read_text(),
            )
            self.assertIn("Rule body", (cursor / "rules" / "python.mdc").read_text())
            self.assertTrue((cursor / "rules" / MANAGED_MANIFEST).exists())
            self.assertEqual((cursor / "manual.md").read_text(), "manual\n")
            self.assertEqual(
                (cursor / "rules" / "manual.mdc").read_text(), "manual rule\n"
            )
            self.assertFalse(
                (cursor / "plugins" / "local" / "coding-agents-rules").exists()
            )
            self.assertTrue((cursor / "skills" / "s1" / "SKILL.md").exists())
            self.assertFalse((cursor / "agents" / "reviewer.md").exists())

    def test_cursor_rule_scoping_uses_native_mdc_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "python.md",
                source_doc(
                    "rule",
                    "python",
                    "python",
                    "Scoped body\n",
                    description="Python files",
                    extra={
                        "activation": {
                            "always": False,
                            "globs": ["**/*.py", "tests/**"],
                        }
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            rule = (home / ".cursor" / "rules" / "python.mdc").read_text()
            _, frontmatter, body = rule.split("---", 2)
            self.assertEqual(
                yaml.safe_load(frontmatter),
                {
                    "description": "Python files",
                    "globs": "**/*.py,tests/**",
                    "alwaysApply": False,
                },
            )
            self.assertEqual(body.strip(), "Scoped body")

    def test_cursor_retires_managed_desktop_rules_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            plugin_root = home / ".cursor/plugins/local"
            plugin = plugin_root / "coding-agents-rules"
            write(plugin / "rules/python.mdc", "Generated body\n")
            sync_manifested_entries(plugin_root, {"coding-agents-rules"}, "dir")

            run_sync(config_root=config_root, home=home)

            self.assertFalse(plugin.exists())
            self.assertFalse((plugin_root / MANAGED_MANIFEST).exists())

    def test_cursor_preserves_unowned_desktop_rules_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            plugin = home / ".cursor/plugins/local/coding-agents-rules/rules/python.mdc"
            write(plugin, "User plugin body\n")

            run_sync(config_root=config_root, home=home)

            self.assertEqual(plugin.read_text(), "User plugin body\n")

    def test_cursor_refuses_to_retire_modified_managed_desktop_rules_plugin(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            plugin_root = home / ".cursor/plugins/local"
            plugin = plugin_root / "coding-agents-rules/rules/python.mdc"
            write(plugin, "Generated body\n")
            sync_manifested_entries(plugin_root, {"coding-agents-rules"}, "dir")
            write(plugin, "Modified generated body\n")

            with self.assertRaisesRegex(
                ManagedEntryConflict,
                "Refusing to replace modified generated dir",
            ):
                run_sync(config_root=config_root, home=home)

    def test_cursor_cli_rules_refuse_unowned_or_modified_files(self) -> None:
        for state in ("unowned", "modified"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as tmp:
                config_root, home = config_root_home(tmp)
                write(
                    config_root / "rules" / "python.md",
                    source_doc(
                        "rule",
                        "python",
                        "python",
                        "Generated body\n",
                        description="Python rules",
                    ),
                )
                rule = home / ".cursor" / "rules" / "python.mdc"
                if state == "unowned":
                    write(rule, "User rule body\n")
                    message = "Refusing to claim unmanifested generated file"
                else:
                    run_sync(config_root=config_root, home=home)
                    write(rule, "Modified generated body\n")
                    message = "Refusing to replace modified generated file"

                with self.assertRaisesRegex(ManagedEntryConflict, message):
                    run_sync(config_root=config_root, home=home)

    def test_cursor_retires_matching_unmanifested_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "global" / "AGENTS.md",
                source_doc("global", "global", "global", "Generated body\n"),
            )
            cursor_global = home / ".cursor" / "AGENTS.md"
            write(cursor_global, "Generated body\n")

            run_sync(config_root=config_root, home=home)

            self.assertFalse(cursor_global.exists())

    def test_cursor_preserves_modified_unmanifested_global(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "global" / "AGENTS.md",
                source_doc("global", "global", "global", "Generated body\n"),
            )
            cursor_global = home / ".cursor" / "AGENTS.md"
            write(cursor_global, "User-owned body\n")

            run_sync(config_root=config_root, home=home)

            self.assertEqual(cursor_global.read_text(), "User-owned body\n")

    def test_internal_prefixed_fields_are_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills" / "s1" / "SKILL.md",
                source_doc(
                    "skill",
                    "s1",
                    "s1",
                    "body\n",
                    description="skill",
                    extra={"internal:version": "1.2.3", "internal": {"note": "x"}},
                ),
            )

            run_sync(config_root=config_root, home=home)

            for path in [
                home / ".claude" / "skills" / "s1" / "SKILL.md",
                home / ".codex" / "skills" / "s1" / "SKILL.md",
                home / ".config" / "opencode" / "skills" / "s1" / "SKILL.md",
                home / ".cursor" / "skills" / "s1" / "SKILL.md",
            ]:
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("version", text)
                self.assertNotIn("note", text)

    def test_unknown_typed_target_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "commands" / "one.md",
                source_doc(
                    "command",
                    "one",
                    "one",
                    "Body\n",
                    description="Command",
                    extra={"opencode:agent": "reviewer", "opencode:unknown": "kept"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "unknown"):
                run_sync(config_root=config_root, home=home)

    def test_target_native_aliases_use_documented_spellings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "scoped.md",
                source_doc(
                    "rule",
                    "scoped",
                    "scoped",
                    "Rule body\n",
                    extra={
                        "cursor": {
                            "alwaysApply": False,
                            "globs": ["**/*.py"],
                        }
                    },
                ),
            )
            write(
                config_root / "skills" / "native" / "SKILL.md",
                source_doc(
                    "skill",
                    "native",
                    "native",
                    "Skill body\n",
                    extra={
                        "claude": {
                            "disable-model-invocation": True,
                            "allowed-tools": ["Read"],
                        },
                        "codex": {"allowed-tools": ["Bash"]},
                        "cursor": {"disable-model-invocation": True},
                    },
                ),
            )
            write(
                config_root / "agents" / "native.md",
                source_doc(
                    "agent",
                    "native",
                    "native",
                    "Agent body\n",
                    extra={"opencode": {"reasoningEffort": "high"}},
                ),
            )

            run_sync(config_root=config_root, home=home)

            claude_skill = (home / ".claude/skills/native/SKILL.md").read_text()
            self.assertIn("disable-model-invocation: true", claude_skill)
            self.assertIn("allowed-tools:", claude_skill)
            self.assertIn("- Read", claude_skill)
            cursor_rule = (home / ".cursor/rules/scoped.mdc").read_text()
            self.assertIn("alwaysApply: false", cursor_rule)
            self.assertIn("globs: '**/*.py'", cursor_rule)
            cursor_skill = (home / ".cursor/skills/native/SKILL.md").read_text()
            self.assertIn("disable-model-invocation: true", cursor_skill)
            codex_skill = (home / ".codex/skills/native/SKILL.md").read_text()
            self.assertIn("allowed-tools:", codex_skill)
            self.assertIn("- Bash", codex_skill)
            opencode_agent = (home / ".config/opencode/agents/native.md").read_text()
            self.assertIn("reasoningEffort: high", opencode_agent)

    def test_raw_manifest_input_fails_before_portable_or_native_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "global" / "AGENTS.md",
                source_doc("global", "global", "global", "Global body\n"),
            )
            write_permissions(config_root, tools={"read": "allow"})
            write(
                config_root
                / "target-config"
                / "claude"
                / "raw"
                / "nested"
                / MANAGED_MANIFEST,
                "forbidden\n",
            )

            with self.assertRaisesRegex(ValueError, "may not be a managed manifest"):
                run_sync(config_root=config_root, home=home)

            self.assertFalse((home / ".claude/CLAUDE.md").exists())
            self.assertFalse((home / ".claude/settings.json").exists())
            self.assertFalse((config_root / ".coding-agents-native.json").exists())

    def test_agent_tools_are_not_portable_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "agents" / "readonly.md",
                source_doc(
                    "agent",
                    "readonly",
                    "readonly",
                    "body\n",
                    description="Read-only agent",
                    extra={"tools": {"allow": ["Read", "Grep"]}},
                ),
            )

            with self.assertRaisesRegex(SourceSchemaError, "tools"):
                run_sync(config_root=config_root, home=home)

    def test_agent_tools_cannot_mix_with_native_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "agents" / "writer.md",
                source_doc(
                    "agent",
                    "writer",
                    "writer",
                    "body\n",
                    description="Writer agent",
                    extra={
                        "tools": {"inherit": True},
                        "cursor:readonly": True,
                        "codex:sandbox_mode": "read-only",
                    },
                ),
            )

            with self.assertRaisesRegex(SourceSchemaError, "tools"):
                run_sync(config_root=config_root, home=home)

    def test_agent_inherit_false_is_not_portable_intent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "agents" / "bare.md",
                source_doc(
                    "agent",
                    "bare",
                    "bare",
                    "body\n",
                    description="Bare inherit-false agent",
                    extra={"tools": {"inherit": False}},
                ),
            )

            with self.assertRaisesRegex(SourceSchemaError, "tools"):
                run_sync(config_root=config_root, home=home)

    def _runner_skill(self, config_root: Path) -> Path:
        write(
            config_root / "skills" / "runner" / "SKILL.md",
            source_doc(
                "skill", "runner", "runner", "Skill body\n", description="runner"
            ),
        )
        script = config_root / "skills" / "runner" / "scripts" / "poll.sh"
        write(script, "#!/usr/bin/env bash\necho hi\n")
        script.chmod(0o755)
        write(config_root / "skills" / "runner" / "references" / "notes.md", "notes\n")
        return script

    def test_bundled_skill_scripts_keep_owner_execute_bit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            self._runner_skill(config_root)

            run_sync(config_root=config_root, home=home)

            for root in (
                home / ".claude/skills/runner",
                home / ".codex/skills/runner",
                home / ".cursor/skills/runner",
                home / ".config/opencode/skills/runner",
            ):
                with self.subTest(root=root):
                    script = root / "scripts/poll.sh"
                    self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o700)
                    notes = root / "references/notes.md"
                    self.assertEqual(stat.S_IMODE(notes.stat().st_mode), 0o600)

    def test_resync_repairs_stale_script_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            self._runner_skill(config_root)
            run_sync(config_root=config_root, home=home)
            deployed = home / ".claude/skills/runner/scripts/poll.sh"
            deployed.chmod(0o600)

            # Content is unchanged, so this only passes if the writer reconciles
            # mode independently of the content-equality short circuit.
            run_sync(config_root=config_root, home=home)

            self.assertEqual(stat.S_IMODE(deployed.stat().st_mode), 0o700)

    def test_claude_agent_native_tools_reject_inherit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "agents" / "reviewer.md",
                source_doc(
                    "agent",
                    "reviewer",
                    "reviewer",
                    "body\n",
                    description="Reviewer agent",
                    extra={
                        "claude": {
                            "tools": "inherit",
                            "disallowedTools": ["Edit", "Write"],
                        }
                    },
                ),
            )

            with self.assertRaisesRegex(ValueError, "does not accept `inherit`"):
                run_sync(config_root=config_root, home=home)
            self.assertFalse((home / ".claude/agents/reviewer.md").exists())

    def test_claude_agent_native_tools_reject_non_tool_names(self) -> None:
        for entry in ("read", "web_fetch", "mcp__*"):
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as tmp:
                config_root, home = config_root_home(tmp)
                write(
                    config_root / "agents" / "reviewer.md",
                    source_doc(
                        "agent",
                        "reviewer",
                        "reviewer",
                        "body\n",
                        description="Reviewer agent",
                        extra={"claude": {"tools": [entry]}},
                    ),
                )

                with self.assertRaisesRegex(ValueError, "is not a tool name"):
                    run_sync(config_root=config_root, home=home)

    def test_claude_agent_native_tools_reject_empty_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "agents" / "reviewer.md",
                source_doc(
                    "agent",
                    "reviewer",
                    "reviewer",
                    "body\n",
                    description="Reviewer agent",
                    extra={
                        "claude": {
                            "tools": ["Read", "Edit"],
                            "disallowedTools": "Read, Edit",
                        }
                    },
                ),
            )

            with self.assertRaisesRegex(ValueError, "zero tools"):
                run_sync(config_root=config_root, home=home)

    def test_claude_agent_native_tools_accept_names_and_mcp_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "agents" / "reviewer.md",
                source_doc(
                    "agent",
                    "reviewer",
                    "reviewer",
                    "body\n",
                    description="Reviewer agent",
                    extra={
                        "claude": {
                            "tools": ["Read", "mcp__github", "mcp__jaeger__*"],
                            "disallowedTools": ["mcp__*", "Edit"],
                        }
                    },
                ),
            )
            write(
                config_root / "agents" / "denylist.md",
                source_doc(
                    "agent",
                    "denylist",
                    "denylist",
                    "body\n",
                    description="Denylist agent",
                    extra={"claude": {"disallowedTools": ["Edit", "Write"]}},
                ),
            )

            run_sync(config_root=config_root, home=home)

            reviewer = (home / ".claude/agents/reviewer.md").read_text()
            self.assertIn("- mcp__jaeger__*", reviewer)
            denylist = (home / ".claude/agents/denylist.md").read_text()
            self.assertIn("disallowedTools:", denylist)
            self.assertNotIn("tools: inherit", denylist)

    def test_codex_rules_reject_unknown_typed_fields(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "git.md",
                source_doc(
                    "rule",
                    "git",
                    "git",
                    "body\n",
                    extra={
                        "codex:rules": [
                            {
                                "pattern": ["git", "push", "--force"],
                                "decision": "forbidden",
                                "justification": "Use --force-with-lease instead.",
                                "match": ["git push --force"],
                                "not_match": ["git push --force-with-lease"],
                            }
                        ]
                    },
                ),
            )

            with self.assertRaisesRegex(ValueError, "invalid codex native fields"):
                run_sync(config_root=config_root, home=home)

    def test_codex_rules_reject_invalid_typed_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "b.md",
                source_doc(
                    "rule",
                    "b",
                    "b",
                    "body\n",
                    extra={
                        "codex:rules": [{"pattern": ["git"], "decision": "forbidden"}]
                    },
                ),
            )
            write(
                config_root / "rules" / "a.md",
                source_doc(
                    "rule",
                    "a",
                    "a",
                    "body\n",
                    extra={
                        "codex:rules": [{"pattern": ["python"], "decision": "allow"}]
                    },
                ),
            )
            write(
                config_root / "rules" / "invalid.md",
                source_doc(
                    "rule",
                    "invalid",
                    "invalid",
                    "body\n",
                    extra={"codex:rules": [{"pattern": [], "decision": "maybe"}]},
                ),
            )

            with self.assertRaisesRegex(ValueError, "invalid codex native fields"):
                run_sync(config_root=config_root, home=home)

    def test_opencode_agent_preserves_native_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "agents" / "reviewer.md",
                source_doc(
                    "agent",
                    "reviewer",
                    "reviewer",
                    "Agent body\n",
                    description="Review code",
                    extra={
                        "opencode:mode": "subagent",
                        "opencode:temperature": 0.1,
                        "opencode:top_p": 0.9,
                        "opencode:steps": 5,
                        "opencode:hidden": True,
                        "opencode:disable": False,
                        "opencode:permission": {
                            "bash": {"*": "ask", "git diff*": "allow"},
                            "task": {"*": "deny", "explore": "allow"},
                        },
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            opencode_agent = (
                home / ".config" / "opencode" / "agents" / "reviewer.md"
            ).read_text(encoding="utf-8")
            self.assertIn("mode: subagent", opencode_agent)
            self.assertIn("permission:", opencode_agent)
            self.assertIn("git diff*: allow", opencode_agent)
            self.assertIn("temperature: 0.1", opencode_agent)
            self.assertIn("top_p: 0.9", opencode_agent)
            self.assertIn("steps: 5", opencode_agent)
            self.assertIn("hidden: true", opencode_agent)
            self.assertIn("disable: false", opencode_agent)

    def test_opencode_skill_preserves_only_native_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills" / "s1" / "SKILL.md",
                source_doc(
                    "skill",
                    "s1",
                    "s1",
                    "Skill body\n",
                    description="skill",
                    extra={
                        "internal:version": "1.2.3",
                        "license": "MIT",
                        "opencode:compatibility": "opencode",
                        "metadata": {"owner": "platform"},
                        "claude:allowed_tools": ["Read"],
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            opencode_skill = (
                home / ".config" / "opencode" / "skills" / "s1" / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertIn("name: s1", opencode_skill)
            self.assertIn("description: skill", opencode_skill)
            self.assertIn("license: MIT", opencode_skill)
            self.assertIn("compatibility: opencode", opencode_skill)
            self.assertIn("metadata:", opencode_skill)
            self.assertIn("owner: platform", opencode_skill)
            self.assertNotIn("version:", opencode_skill)
            self.assertNotIn("allowed-tools:", opencode_skill)

    def test_opencode_command_target_fields_override_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "commands" / "review.md",
                source_doc(
                    "command",
                    "review",
                    "review",
                    "Review $ARGUMENTS and @src/file.py.\n",
                    description="Portable description",
                    extra={
                        "execution": {"agent": "reviewer", "subtask": False},
                        "opencode:description": "OpenCode description",
                        "opencode:agent": "explore",
                        "opencode:subtask": True,
                        "opencode:model": "opencode/model",
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            opencode_command = (
                home / ".config" / "opencode" / "commands" / "review.md"
            ).read_text(encoding="utf-8")
            self.assertIn("description: OpenCode description", opencode_command)
            self.assertIn("agent: explore", opencode_command)
            self.assertIn("subtask: true", opencode_command)
            self.assertIn("model: opencode/model", opencode_command)
            self.assertIn("Review $ARGUMENTS and @src/file.py.", opencode_command)

    def test_opencode_json_update_preserves_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "a.md",
                source_doc(
                    "rule",
                    "a",
                    "a",
                    "Rule body\n",
                    extra={
                        "opencode:instructions": (
                            "~/.config/coding-agents/rules/a-override.md"
                        )
                    },
                ),
            )
            write(
                config_root / "rules" / "b.md",
                source_doc("rule", "b", "b", "Rule body\n"),
            )

            write(
                home / ".config" / "opencode" / "opencode.json",
                json.dumps(
                    {
                        "$schema": "https://opencode.ai/config.json",
                        "share": "disabled",
                        "instructions": ["old-rule"],
                    },
                    indent=2,
                )
                + "\n",
            )

            run_sync(config_root=config_root, home=home)

            opencode_json = home / ".config" / "opencode" / "opencode.json"
            self.assertTrue(opencode_json.exists())

            config = json.loads(opencode_json.read_text(encoding="utf-8"))
            self.assertEqual(config["$schema"], "https://opencode.ai/config.json")
            self.assertEqual(config["share"], "disabled")
            self.assertEqual(
                config["instructions"],
                [
                    "~/.config/coding-agents/rules/a-override.md",
                    str(config_root / "rules" / "b.md"),
                ],
            )

    def test_invalid_opencode_json_fails_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "a.md",
                source_doc("rule", "a", "a", "Rule body\n"),
            )
            broken = home / ".config" / "opencode" / "opencode.json"
            write(broken, "{ broken json")

            with self.assertRaises(NativeConfigError):
                run_sync(config_root=config_root, home=home)
            self.assertEqual(broken.read_text(), "{ broken json")

    def test_opencode_sync_preserves_static_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "a.md",
                source_doc("rule", "a", "a", "Rule body\n"),
            )
            write(
                home / ".config" / "opencode" / "opencode.json",
                json.dumps(
                    {
                        "share": "manual",
                        "permission": {"bash": "ask", "read": "allow"},
                        "plugin": ["opencode-scheduler"],
                        "instructions": ["stale-rule"],
                    }
                ),
            )

            run_sync(config_root=config_root, home=home)

            config = json.loads(
                (home / ".config" / "opencode" / "opencode.json").read_text()
            )
            self.assertEqual(config["share"], "manual")
            self.assertEqual(config["permission"], {"bash": "ask", "read": "allow"})
            self.assertEqual(config["plugin"], ["opencode-scheduler"])
            self.assertEqual(
                config["instructions"], [str(config_root / "rules" / "a.md")]
            )

    def test_opencode_sync_does_not_bake_local_config_into_config_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "a.md",
                source_doc("rule", "a", "a", "Rule body\n"),
            )
            write(
                home / ".config" / "opencode" / "opencode.json",
                json.dumps({"share": "disabled", "permission": {"bash": "deny"}}),
            )
            write(
                home / ".config" / "opencode" / "opencode.local.json",
                json.dumps(
                    {
                        "enabled_providers": ["anthropic", "google-vertex"],
                        "permission": {"bash": "allow"},
                    }
                ),
            )

            run_sync(config_root=config_root, home=home)

            config = json.loads(
                (home / ".config" / "opencode" / "opencode.json").read_text()
            )
            self.assertNotIn("enabled_providers", config)
            self.assertEqual(config["permission"]["bash"], "deny")
            self.assertEqual(
                config["instructions"], [str(config_root / "rules" / "a.md")]
            )

    def test_cli_removed_flags_are_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root = Path(tmp) / "coding-agents"
            config_root.mkdir(parents=True, exist_ok=True)
            result = CliRunner().invoke(
                main, ["--config-root", str(config_root), "--strict"]
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("No such option", result.output)
            self.assertIn("--strict", result.output)

    def test_codex_config_preserves_existing_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills" / "s1" / "SKILL.md",
                source_doc("skill", "s1", "s1", "body\n", description="skill"),
            )
            write(
                config_root / "agents" / "reviewer.md",
                source_doc(
                    "agent", "reviewer", "reviewer", "body\n", description="Review code"
                ),
            )
            write(
                home / ".codex" / "config.toml",
                """model = "gpt-5.4"
approval_policy = "on-request"
""",
            )

            run_sync(config_root=config_root, home=home)
            cfg = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "gpt-5.4"', cfg)
            self.assertIn('approval_policy = "on-request"', cfg)
            self.assertIn("[[skills.config]]", cfg)
            self.assertIn("[agents.reviewer]", cfg)
            self.assertIn('config_file = "~/.codex/agents/reviewer.toml"', cfg)

    def test_codex_target_fragment_is_no_longer_a_runtime_input(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills" / "s1" / "SKILL.md",
                source_doc("skill", "s1", "s1", "body\n", description="skill"),
            )
            write(
                config_root / "target-config" / "codex" / "config.toml",
                """model = "gpt-5.5"
web_search = true

[features]
hooks = false
""",
            )

            run_sync(config_root=config_root, home=home)

            cfg = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
            parsed = tomllib.loads(cfg)
            self.assertNotIn("model", parsed)
            self.assertNotIn("web_search", parsed)
            self.assertNotIn("features", parsed)
            self.assertEqual(
                parsed["skills"]["config"][0]["path"], "~/.codex/skills/s1"
            )

    def test_sync_does_not_rewrite_unchanged_generated_text_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "global" / "AGENTS.md",
                source_doc("global", "global", "global", "Global body\n"),
            )

            run_sync(config_root=config_root, home=home)
            target = home / ".codex" / "AGENTS.md"
            before = target.stat().st_mtime_ns

            run_sync(config_root=config_root, home=home)

            self.assertEqual(target.stat().st_mtime_ns, before)

    def test_codex_skill_copy_replaces_invalid_description_characters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills" / "s1" / "SKILL.md",
                source_doc(
                    "skill",
                    "s1",
                    "s1",
                    "body\n",
                    description=(
                        "Create files under days/<date>/itinerary.md: daily plan."
                    ),
                ),
            )

            run_sync(config_root=config_root, home=home)

            cfg = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertIn('path = "~/.codex/skills/s1"', cfg)
            source = (config_root / "skills" / "s1" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            codex_skill = (home / ".codex" / "skills" / "s1" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("days/<date>/itinerary.md: daily plan", source)
            self.assertIn("days/date/itinerary.md: daily plan", codex_skill)
            self.assertNotIn("<", codex_skill.split("---", 2)[1])
            self.assertNotIn(">", codex_skill.split("---", 2)[1])

    def test_codex_skill_rejects_unknown_typed_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills" / "s1" / "SKILL.md",
                source_doc(
                    "skill",
                    "s1",
                    "s1",
                    "body\n",
                    description="skill",
                    extra={
                        "license": "MIT",
                        "codex:allowed-tools": ["Bash"],
                        "codex:metadata": {"owner": "platform"},
                        "codex:unknown-native-field": "kept",
                    },
                ),
            )

            with self.assertRaisesRegex(ValueError, "invalid codex native fields"):
                run_sync(config_root=config_root, home=home)

    def test_claude_skill_preserves_native_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills" / "s1" / "SKILL.md",
                source_doc(
                    "skill",
                    "s1",
                    "s1",
                    "body\n",
                    description="skill",
                    extra={
                        "paths": ["docs/**"],
                        "disable_model_invocation": True,
                        "claude:when_to_use": "Use when editing docs.",
                        "claude:argument_hint": "[path]",
                        "claude:user_invocable": False,
                        "claude:allowed_tools": ["Read", "Grep"],
                        "claude:model": "sonnet",
                        "claude:effort": "high",
                        "claude:context": "fork",
                        "claude:agent": "Explore",
                        "claude:disallowed_tools": ["Bash"],
                        "claude:shell": "bash",
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            claude_skill = (home / ".claude" / "skills" / "s1" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("paths:", claude_skill)
            self.assertIn("disable-model-invocation: true", claude_skill)
            self.assertIn("when_to_use: Use when editing docs.", claude_skill)
            self.assertIn("argument-hint: '[path]'", claude_skill)
            self.assertIn("user-invocable: false", claude_skill)
            self.assertIn("allowed-tools:", claude_skill)
            self.assertIn("- Read", claude_skill)
            self.assertIn("context: fork", claude_skill)
            self.assertIn("disallowed-tools:", claude_skill)
            self.assertIn("- Bash", claude_skill)

    def test_missing_schema_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "old.md",
                """---
description: Old v1 rule
---
body
""",
            )

            with self.assertRaisesRegex(ValueError, "missing or invalid schema"):
                run_sync(config_root=config_root, home=home)

    def test_only_unknown_target_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "scoped.md",
                source_doc(
                    "rule",
                    "scoped",
                    "scoped",
                    "body\n",
                    extra={"only": ["cursor", "nope"]},
                ),
            )
            with self.assertRaisesRegex(ValueError, "unknown only targets"):
                run_sync(config_root=config_root, home=home)

    def test_only_rejects_targets_outside_the_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "scoped.md",
                source_doc(
                    "rule",
                    "scoped",
                    "scoped",
                    "body\n",
                    extra={
                        "only": ["cursor"],
                        "claude": {"future": True},
                    },
                ),
            )
            with self.assertRaisesRegex(ValueError, "targets outside only"):
                run_sync(config_root=config_root, home=home)

    def test_only_restricts_emission_and_skips_foreign_omits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "global" / "AGENTS.md",
                source_doc("global", "global", "global", "Shared global\n"),
            )
            write(
                config_root / "rules" / "cursor-shell.md",
                source_doc(
                    "rule",
                    "cursor-shell",
                    "cursor-shell",
                    "Cursor-only body\n",
                    description="Cursor shell guidance",
                    extra={
                        "only": ["cursor"],
                        "activation": {"always": True},
                    },
                ),
            )
            write(
                config_root / "commands" / "claude-only.md",
                source_doc(
                    "command",
                    "claude-only",
                    "claude-only",
                    "Run me\n",
                    description="Claude only",
                    extra={
                        "only": ["claude"],
                        "claude": {"description": "Claude only"},
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            cursor_rule = home / ".cursor" / "rules" / "cursor-shell.mdc"
            self.assertTrue(cursor_rule.exists())
            self.assertIn("Cursor-only body", cursor_rule.read_text(encoding="utf-8"))
            self.assertFalse((home / ".claude" / "rules" / "cursor-shell.md").exists())
            self.assertNotIn(
                "Cursor-only body",
                (home / ".codex" / "AGENTS.md").read_text(encoding="utf-8"),
            )
            self.assertTrue((home / ".claude" / "commands" / "claude-only.md").exists())
            self.assertFalse(
                (home / ".config" / "opencode" / "commands" / "claude-only.md").exists()
            )

    def test_only_on_global_retires_excluded_host_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "global" / "AGENTS.md",
                source_doc("global", "global", "global", "Shared global\n"),
            )
            run_sync(config_root=config_root, home=home)
            self.assertEqual(
                (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8"),
                "Shared global\n",
            )
            self.assertEqual(
                (home / ".config" / "opencode" / "AGENTS.md").read_text(
                    encoding="utf-8"
                ),
                "Shared global\n",
            )

            write(
                config_root / "global" / "AGENTS.md",
                source_doc(
                    "global",
                    "global",
                    "global",
                    "Shared global\n",
                    extra={"only": ["cursor"]},
                ),
            )
            run_sync(config_root=config_root, home=home)

            self.assertFalse((home / ".claude" / "CLAUDE.md").exists())
            self.assertFalse((home / ".config" / "opencode" / "AGENTS.md").exists())
            self.assertFalse((home / ".codex" / "AGENTS.md").exists())
            self.assertTrue(
                (home / ".cursor" / "rules" / "coding-agents-global.mdc").exists()
            )

    def test_invalid_sources_reports_all_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "old.md",
                """---
description: Old v1 rule
---
body
""",
            )
            write(
                config_root / "skills" / "old-skill" / "SKILL.md",
                """---
name: old-skill
description: Old v1 skill
---
body
""",
            )

            with self.assertRaisesRegex(
                ValueError, "invalid coding-agent source"
            ) as caught:
                run_sync(config_root=config_root, home=home)
            message = str(caught.exception)
            self.assertIn("rules/old.md", message)
            self.assertIn("skills/old-skill/SKILL.md", message)

    def test_duplicate_markdown_target_native_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "duplicate.md",
                "---\n"
                "schema: coding-agents/v4\n"
                "kind: rule\n"
                "id: duplicate\n"
                "name: duplicate\n"
                "targets:\n"
                "  claude:\n"
                "    native:\n"
                "      model: first\n"
                "      model: second\n"
                "---\n"
                "Rule body\n",
            )

            with self.assertRaisesRegex(
                SourceSchemaError, "duplicate YAML key `model`"
            ):
                run_sync(config_root=config_root, home=home)

    def test_permission_target_raw_keys_remain_exact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, _ = config_root_home(tmp)
            write(
                config_root / "permissions" / "policy.yaml",
                "schema: coding-agents/v4\n"
                "kind: permission-policy\n"
                "id: test\n"
                "name: test\n"
                "targets:\n"
                "  opencode:\n"
                "    raw:\n"
                "      futureNativeKey: true\n",
            )

            permissions = load_permissions(config_root / "permissions")

            self.assertEqual(
                permissions.targets.root["opencode"].raw,
                {"futureNativeKey": True},
            )

    def test_wrong_kind_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "wrong.md",
                source_doc("skill", "wrong", "wrong", "body\n"),
            )

            with self.assertRaisesRegex(ValueError, "expected kind `rule`"):
                run_sync(config_root=config_root, home=home)

    def test_command_model_is_tool_prefixed_not_canonical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "commands" / "one.md",
                source_doc(
                    "command",
                    "one",
                    "one",
                    "Body\n",
                    description="Command",
                    extra={"claude:model": "opus", "opencode:model": "gpt-5"},
                ),
            )

            run_sync(config_root=config_root, home=home)

            claude_command = (home / ".claude" / "commands" / "one.md").read_text(
                encoding="utf-8"
            )
            opencode_command = (
                home / ".config" / "opencode" / "commands" / "one.md"
            ).read_text(encoding="utf-8")
            self.assertIn("model: opus", claude_command)
            self.assertIn("model: gpt-5", opencode_command)
            self.assertNotIn("gpt-5", claude_command)

    def test_commands_prune_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)

            write(
                config_root / "commands" / "one.md",
                source_doc(
                    "command", "one", "one", "One\n", description="First command"
                ),
            )

            run_sync(config_root=config_root, home=home)

            commands_dir = home / ".config" / "opencode" / "commands"
            claude_commands_dir = home / ".claude" / "commands"
            self.assertTrue((commands_dir / "one.md").exists())
            self.assertTrue((claude_commands_dir / "one.md").exists())

            shutil.rmtree(config_root / "commands")
            run_sync(config_root=config_root, home=home)

            self.assertFalse((commands_dir / "one.md").exists())
            self.assertFalse((claude_commands_dir / "one.md").exists())
            manifest = json.loads(
                (commands_dir / ".coding-agents-managed.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["entries"], {})
            claude_manifest = json.loads(
                (claude_commands_dir / ".coding-agents-managed.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(claude_manifest["entries"], {})

    def test_codex_omits_commands_on_skill_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills" / "one" / "SKILL.md",
                source_doc(
                    "skill", "one", "one", "Skill body\n", description="Skill one"
                ),
            )
            write(
                config_root / "commands" / "one.md",
                source_doc(
                    "command",
                    "one-command",
                    "one",
                    "Command body\n",
                    description="Command one",
                ),
            )

            run_sync(config_root=config_root, home=home)

            cfg = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertIn('path = "~/.codex/skills/one"', cfg)
            self.assertNotIn('path = "~/.codex/skills/source-command-one"', cfg)
            self.assertFalse(
                (home / ".codex" / "skills" / "source-command-one").exists()
            )

    def test_codex_omits_all_commands_without_creating_collision_aliases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills" / "one" / "SKILL.md",
                source_doc(
                    "skill", "one", "one", "Skill body\n", description="Skill one"
                ),
            )
            write(
                config_root / "commands" / "one.md",
                source_doc(
                    "command",
                    "one-command",
                    "one",
                    "Command body\n",
                    description="Command one",
                ),
            )
            write(
                config_root / "commands" / "source-command-one.md",
                source_doc(
                    "command",
                    "source-command-one-command",
                    "source-command-one",
                    "Second command body\n",
                    description="Second command",
                ),
            )

            run_sync(config_root=config_root, home=home)

            cfg = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertIn('path = "~/.codex/skills/one"', cfg)
            self.assertNotIn("source-command-one", cfg)

    def test_prunes_stale_rules_agents_skills_and_codex_rules(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                home
                / ".cursor"
                / "plugins"
                / "local"
                / "manual-plugin"
                / ".cursor-plugin"
                / "plugin.json",
                '{"name": "manual-plugin"}\n',
            )
            write(
                config_root / "rules" / "x.md",
                source_doc(
                    "rule",
                    "x",
                    "x",
                    "body\n",
                    extra={"codex:rules": [{"pattern": ["x"], "decision": "prompt"}]},
                ),
            )
            write(
                config_root / "skills" / "s1" / "SKILL.md",
                source_doc("skill", "s1", "s1", "body\n", description="skill"),
            )
            write(
                config_root / "agents" / "a.md",
                source_doc("agent", "a", "a", "body\n", description="Agent A"),
            )

            run_sync(config_root=config_root, home=home)
            self.assertTrue((home / ".cursor" / "rules" / "x.mdc").exists())
            self.assertFalse(
                (
                    home / ".cursor" / "plugins" / "local" / "coding-agents-rules"
                ).exists()
            )
            self.assertTrue((home / ".claude" / "rules" / "x.md").exists())
            self.assertTrue(
                (home / ".codex" / "rules" / "coding-agents.rules").exists()
            )
            self.assertTrue((home / ".claude" / "skills" / "s1").exists())
            self.assertTrue((home / ".claude" / "agents" / "a.md").exists())
            self.assertTrue((home / ".codex" / "agents" / "a.toml").exists())

            (config_root / "rules" / "x.md").unlink()
            shutil.rmtree(config_root / "skills" / "s1")
            (config_root / "agents" / "a.md").unlink()
            run_sync(config_root=config_root, home=home)

            self.assertFalse((home / ".claude" / "rules" / "x.md").exists())
            self.assertFalse((home / ".cursor" / "rules" / "x.mdc").exists())
            self.assertFalse(
                (
                    home / ".cursor" / "plugins" / "local" / "coding-agents-rules"
                ).exists()
            )
            self.assertTrue(
                (
                    home
                    / ".cursor"
                    / "plugins"
                    / "local"
                    / "manual-plugin"
                    / ".cursor-plugin"
                    / "plugin.json"
                ).exists()
            )
            self.assertFalse(
                (home / ".codex" / "rules" / "coding-agents.rules").exists()
            )
            self.assertFalse((home / ".claude" / "skills" / "s1").exists())
            self.assertFalse((home / ".claude" / "agents" / "a.md").exists())
            self.assertFalse((home / ".codex" / "agents" / "a.toml").exists())


class NativePreservationTests(unittest.TestCase):
    def test_unpatched_json_settings_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                home / ".claude" / "settings.json",
                json.dumps(
                    {
                        "permissions": {
                            "allow": ["Read"],
                            "deny": ["Write"],
                        },
                        "model": "opus",
                    }
                ),
            )
            write(
                config_root / "target-config" / "claude" / "settings.json",
                json.dumps(
                    {
                        "permissions": {"allow": ["Read", "Grep"]},
                        "statusLine": {"type": "command", "command": "echo ok"},
                    }
                ),
            )
            write(
                home / ".cursor" / "settings.json",
                json.dumps({"editor": {"fontSize": 12}, "telemetry": False}),
            )
            write(
                config_root / "target-config" / "cursor" / "settings.json",
                json.dumps({"editor": {"tabSize": 2}, "ai": {"model": "auto"}}),
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude" / "settings.json").read_text())
            self.assertEqual(claude["permissions"]["allow"], ["Read"])
            self.assertEqual(claude["permissions"]["deny"], ["Write"])
            self.assertEqual(claude["model"], "opus")
            self.assertNotIn("statusLine", claude)
            cursor = json.loads((home / ".cursor" / "settings.json").read_text())
            self.assertEqual(cursor["editor"], {"fontSize": 12})
            self.assertNotIn("ai", cursor)
            self.assertFalse(cursor["telemetry"])


if __name__ == "__main__":
    unittest.main()
