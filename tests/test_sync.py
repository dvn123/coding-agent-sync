from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
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
from coding_agents_sync.runtime_config import NativeConfigError
from coding_agents_sync.sources import SourceSchemaError


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
        "schema": "coding-agents/v3",
        "kind": kind,
        "id": id_,
        "name": name,
        "description": description,
    }
    if extra:
        meta.update(extra)
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
        "schema": "coding-agents/v3",
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
        "schema": "coding-agents/v3",
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
    def test_an_ask_guards_an_allow_only_where_an_ask_channel_exists(self) -> None:
        """An allow is unconditional; its guard is best-effort.

        Neither Cursor Agent nor Cursor Desktop has an ask channel, so the
        guard is simply absent there and the allow is still emitted.
        Withholding the allow instead cost far more Cursor Agent traffic than
        it protected, and lowering the ask to a Cursor Agent deny would hold
        even under `--force`, making the command unrunnable rather than
        approvable.
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

            cursor_agent = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_agent["permissions"]["allow"],
                [
                    "Shell(fd)",
                    "Shell(gh:api)",
                    "Shell(gh:api *)",
                    "Shell(terraform:show)",
                    "Shell(terraform:show *)",
                ],
            )
            # An ask must never reach the Cursor Agent deny list.
            self.assertNotIn("deny", cursor_agent["permissions"])
            cursor_desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                cursor_desktop["terminalAllowlist"],
                ["fd", "gh:api", "gh:api *", "terraform:show", "terraform:show *"],
            )

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
            cursor_agent = json.loads((home / ".cursor/cli-config.json").read_text())
            cursor_desktop = json.loads((home / ".cursor/permissions.json").read_text())

            self.assertIn(
                "Bash(mytool run --dry-run *)", claude["permissions"]["allow"]
            )
            self.assertIn(
                "Shell(mytool:run --dry-run *)", cursor_agent["permissions"]["allow"]
            )
            self.assertIn("mytool:run --dry-run", cursor_desktop["terminalAllowlist"])
            for command, decision in {
                "mytool run --dry-run": "allow",
                "mytool run plan --dry-run": "allow",
                "mytool run --apply": "ask",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

            # `text` has no verified Cursor Agent form, so a text-narrowed
            # allow reaches the two glob targets and nowhere else.
            self.assertIn("Bash(othertool*--check*)", claude["permissions"]["allow"])
            self.assertEqual(resolve_opencode_bash(bash, "othertool --check"), "allow")
            self.assertEqual(resolve_opencode_bash(bash, "othertool --write"), "ask")
            self.assertNotIn("othertool", json.dumps(cursor_agent["permissions"]))
            self.assertNotIn(
                "othertool", json.dumps(cursor_desktop["terminalAllowlist"])
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

            cursor_agent = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_agent["permissions"]["allow"],
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
            cursor_desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertIn("git:-C status", cursor_desktop["terminalAllowlist"])
            self.assertIn("git:-C * status *", cursor_desktop["terminalAllowlist"])

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

            cursor_agent = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(
                cursor_agent["permissions"]["deny"],
                [
                    "Shell(kubectl:apply)",
                    "Shell(kubectl:apply *)",
                    "Shell(kubectl:--context apply)",
                    "Shell(kubectl:--context apply *)",
                    "Shell(kubectl:--context * apply)",
                    "Shell(kubectl:--context * apply *)",
                ],
            )
            cursor_desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertNotIn("permissions", cursor_desktop)
            self.assertNotIn("apply", json.dumps(cursor_desktop["terminalAllowlist"]))

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
        does not resolve `env`, so it receives both for that one.
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

            cursor_agent = json.loads((home / ".cursor/cli-config.json").read_text())
            cursor_desktop = json.loads((home / ".cursor/permissions.json").read_text())
            for wrapper in ("timeout", "env"):
                with self.subTest(wrapper=wrapper):
                    self.assertIn(
                        f"Shell({wrapper})", cursor_agent["permissions"]["allow"]
                    )
                    self.assertIn(wrapper, cursor_desktop["terminalAllowlist"])

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
                "schema: coding-agents/v3\n"
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
                "- [local-tool, wipe]\n",
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
            cursor_agent = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertIn(
                "Shell(local-tool:status *)", cursor_agent["permissions"]["allow"]
            )
            self.assertIn(
                "Shell(local-tool:--profile status)",
                cursor_agent["permissions"]["allow"],
            )
            self.assertIn(
                "Shell(local-tool:wipe *)", cursor_agent["permissions"]["deny"]
            )
            self.assertIn("Shell(git:status *)", cursor_agent["permissions"]["allow"])

    def test_local_command_fragment_conflicting_with_committed_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
            write(
                config_root / "permissions.local/dup.yaml",
                "schema: coding-agents/v3\n"
                "kind: permission-rules\n"
                "id: dup\n"
                "name: Dup\n"
                "description: Conflicts with the committed corpus.\n"
                "ask:\n"
                "- [git, status]\n",
            )

            with self.assertRaisesRegex(SourceSchemaError, "duplicate permission rule"):
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
                NativeConfigError, "cannot contribute command permissions"
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

    def test_user_permissions_project_with_native_semantics_and_ownership(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[
                    ["git", "status"],
                    ["pytest"],
                    {
                        "command": "gh",
                        "subcommand": ["auth", "status"],
                        "exact": True,
                    },
                ],
                ask=[["rmdir"]],
                deny=[["git", "push", "--force"]],
                secret_paths=["**/.env"],
                tools={"read": "allow", "edit": "allow", "websearch": "allow"},
                workspace={"allow": ["~/Developer", "/tmp"], "ask": ["~/.ssh"]},
            )
            write(
                config_root / "patches/claude-settings.yaml",
                "schema: coding-agents/patch/v1\n"
                "extend:\n"
                "  /permissions/allow: [WebFetch(*)]\n",
            )
            write(
                config_root / "patches/cursor-cli-config.yaml",
                "schema: coding-agents/patch/v1\n"
                "extend:\n"
                "  /permissions/allow: [Read(*), Write(**)]\n",
            )
            write(
                home / ".claude/settings.json",
                json.dumps({"model": "native"}),
            )
            write(
                home / ".cursor/permissions.json",
                json.dumps({"mcpAllowlist": ["native:tool"]}),
            )
            write(
                home / ".config/opencode/opencode.json",
                json.dumps({"share": "disabled"}),
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(claude["model"], "native")
            self.assertIn("WebFetch(*)", claude["permissions"]["allow"])
            self.assertIn("Bash(git status *)", claude["permissions"]["allow"])
            self.assertIn("Bash(pytest *)", claude["permissions"]["allow"])
            self.assertIn("Bash(gh auth status)", claude["permissions"]["allow"])
            self.assertNotIn("Bash(gh auth status *)", claude["permissions"]["allow"])
            self.assertEqual(
                claude["permissions"]["ask"],
                ["Bash(rmdir *)"],
            )
            self.assertEqual(
                claude["permissions"]["deny"],
                [
                    "Bash(git push --force *)",
                    "Read(**/.env)",
                    "Edit(**/.env)",
                ],
            )
            self.assertNotIn("Read(**/.env*)", claude["permissions"]["deny"])

            cursor_agent = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(cursor_agent["approvalMode"], "allowlist")
            self.assertIn("Read(*)", cursor_agent["permissions"]["allow"])
            self.assertIn("Write(**)", cursor_agent["permissions"]["allow"])
            self.assertIn("Shell(git:status)", cursor_agent["permissions"]["allow"])
            self.assertIn("Shell(gh:auth status)", cursor_agent["permissions"]["allow"])
            self.assertNotIn(
                "Shell(gh:auth status *)", cursor_agent["permissions"]["allow"]
            )
            # The ask is conveyed by absence; only the deny reaches this list.
            self.assertEqual(
                cursor_agent["permissions"]["deny"],
                [
                    "Read(**/.env)",
                    "Write(**/.env)",
                    "Shell(git:push --force)",
                    "Shell(git:push --force *)",
                ],
            )
            self.assertNotIn("rmdir", json.dumps(cursor_agent["permissions"]))
            cursor_desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(cursor_desktop["mcpAllowlist"], ["native:tool"])
            self.assertEqual(cursor_desktop["approvalMode"], "allowlist")
            self.assertIn("git:status", cursor_desktop["terminalAllowlist"])
            self.assertIn("git:status *", cursor_desktop["terminalAllowlist"])
            self.assertIn("pytest", cursor_desktop["terminalAllowlist"])
            self.assertIn("gh:auth status", cursor_desktop["terminalAllowlist"])
            self.assertNotIn("gh:auth status *", cursor_desktop["terminalAllowlist"])
            # Cursor Desktop has no ask channel and no deny key, so both are
            # conveyed by absence and the portable deny degrades to a prompt.
            self.assertNotIn("rmdir", cursor_desktop["terminalAllowlist"])
            self.assertNotIn("git:push --force", cursor_desktop["terminalAllowlist"])
            self.assertNotIn(".env", json.dumps(cursor_desktop))

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            self.assertEqual(opencode["share"], "disabled")
            self.assertEqual(
                list(opencode["permission"]["bash"]),
                [
                    "*",
                    "gh auth status",
                    "git status *",
                    "pytest *",
                    "rmdir *",
                    "git push --force *",
                ],
            )
            self.assertEqual(opencode["permission"]["bash"]["git status *"], "allow")
            self.assertEqual(opencode["permission"]["bash"]["gh auth status"], "allow")
            self.assertEqual(
                opencode["permission"]["bash"]["git push --force *"], "deny"
            )
            self.assertNotIn("gh auth status *", opencode["permission"]["bash"])
            self.assertEqual(
                opencode["permission"]["read"],
                {"*": "allow", "**/.env": "deny"},
            )
            self.assertEqual(
                opencode["permission"]["edit"], opencode["permission"]["read"]
            )
            # Portable tool classes reach each target under its own spelling.
            self.assertEqual(opencode["permission"]["websearch"], "allow")
            self.assertNotIn("webfetch", opencode["permission"])
            self.assertIn("Read(**)", claude["permissions"]["allow"])
            self.assertIn("Edit(**)", claude["permissions"]["allow"])
            self.assertIn("WebSearch(*)", claude["permissions"]["allow"])
            # Cursor Agent has no narrow Edit entry. Web search is a boolean
            # rather than an allowlist pattern.
            self.assertIn("Read(**)", cursor_agent["permissions"]["allow"])
            self.assertIn("Write(**)", cursor_agent["permissions"]["allow"])
            self.assertNotIn("Edit(**)", cursor_agent["permissions"]["allow"])
            self.assertNotIn("WebSearch(*)", cursor_agent["permissions"]["allow"])
            self.assertIs(cursor_agent["autoAcceptWebSearch"], True)
            # One workspace list, two native spellings; Codex is not wired.
            self.assertEqual(
                claude["permissions"]["additionalDirectories"],
                ["~/Developer", "/tmp"],
            )
            self.assertEqual(
                opencode["permission"]["external_directory"],
                {
                    "*": "ask",
                    "~/Developer/**": "allow",
                    "/tmp/**": "allow",
                    "~/.ssh/**": "ask",
                },
            )

            # Codex receives no user command permissions at all.
            self.assertFalse((home / ".codex/rules/coding-agents.rules").exists())

            manifest = json.loads(
                (config_root / ".coding-agents-native.json").read_text()
            )
            owned = {
                (entry["target"], entry["pointer"]) for entry in manifest["entries"]
            }
            self.assertTrue(
                {
                    ("claude", "/permissions/allow"),
                    ("claude", "/permissions/deny"),
                    ("cursor-cli", "/approvalMode"),
                    ("cursor-cli", "/permissions/allow"),
                    ("cursor-cli", "/permissions/deny"),
                    ("cursor-desktop", "/approvalMode"),
                    ("cursor-desktop", "/terminalAllowlist"),
                    ("opencode", "/permission/bash"),
                    ("opencode", "/permission/read"),
                    ("opencode", "/permission/edit"),
                }
                <= owned
            )
            self.assertEqual(
                run_sync(config_root=config_root, home=home, check=True), ()
            )

            shutil.rmtree(config_root / "permissions")
            run_sync(config_root=config_root, home=home)
            retired_claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(
                retired_claude["permissions"],
                {
                    "allow": ["WebFetch(*)"],
                },
            )
            retired_cursor_agent = json.loads(
                (home / ".cursor/cli-config.json").read_text()
            )
            self.assertEqual(
                retired_cursor_agent["permissions"],
                {
                    "allow": ["Read(*)", "Write(**)"],
                },
            )
            retired_desktop = json.loads(
                (home / ".cursor/permissions.json").read_text()
            )
            self.assertEqual(retired_desktop, {"mcpAllowlist": ["native:tool"]})
            retired_opencode = json.loads(
                (home / ".config/opencode/opencode.json").read_text()
            )
            self.assertEqual(
                retired_opencode,
                {
                    "share": "disabled",
                    "permission": {},
                    "instructions": [],
                },
            )
            self.assertFalse((home / ".codex/rules/coding-agents.rules").exists())

    def test_permission_schema_rejects_lossy_or_unsafe_rules(self) -> None:
        invalid_rules = {
            "wildcard subcommand": (
                permission_rules_doc(allow=[["git", "*"]]),
                "subcommand must contain portable literal argv tokens",
            ),
            "delimiter in command": (
                permission_rules_doc(allow=[["cursor:delimiter"]]),
                "command must be one portable literal argv token",
            ),
            "shell expansion in command": (
                permission_rules_doc(allow=[["shell$expansion"]]),
                "command must be one portable literal argv token",
            ),
            "duplicate within a bucket": (
                permission_rules_doc(allow=[["git", "status"], ["git", "status"]]),
                "duplicate permission rule",
            ),
            "duplicate across buckets": (
                permission_rules_doc(
                    allow=[["git", "status"]], deny=[["git", "status"]]
                ),
                "duplicate permission rule",
            ),
            "exact with tail": (
                permission_rules_doc(
                    allow=[
                        {
                            "command": "gh",
                            "subcommand": ["auth", "status"],
                            "exact": True,
                            "tail": ["-t"],
                        }
                    ]
                ),
                "exact rules match nothing after the subcommand",
            ),
            "tail with text": (
                permission_rules_doc(
                    allow=[{"command": "gh", "tail": ["-t"], "text": ["json"]}]
                ),
                "tail with text lowers to one pattern",
            ),
            "unsplit tail token": (
                permission_rules_doc(allow=[{"command": "fd", "tail": [["-x rm"]]}]),
                "tail must contain non-empty portable literal argv tokens",
            ),
            "duplicate tail entries": (
                permission_rules_doc(allow=[{"command": "fd", "tail": ["-x", "-x"]}]),
                "tail must be unique",
            ),
            # A leading token that is not option-shaped is a subcommand, and
            # tolerating it would open a hole at an arbitrary argument position.
            "options token without a dash": (
                permission_rules_doc(
                    allow=[["git", "status"]], options={"git": ["status"]}
                ),
                "must be leading option tokens beginning with '-'",
            ),
            "options for a command with no rule": (
                permission_rules_doc(
                    allow=[["git", "status"]], options={"kubectl": ["--context"]}
                ),
                "options declared for kubectl, which has no permission rule",
            ),
            # Strictness runs allow < ask < deny; only a stricter rule may
            # narrow a looser one, and same-bucket containment is redundant.
            "ask contains an allow": (
                permission_rules_doc(
                    allow=[["git", "push", "--dry-run"]], ask=[["git", "push"]]
                ),
                "only a stricter rule may narrow a looser one",
            ),
            "deny contains an ask": (
                permission_rules_doc(
                    ask=[["git", "push", "--force"]], deny=[["git", "push"]]
                ),
                "only a stricter rule may narrow a looser one",
            ),
            "allow contains an allow": (
                permission_rules_doc(allow=[["git", "push"], ["git", "push", "--all"]]),
                "only a stricter rule may narrow a looser one",
            ),
        }
        for label, (source, message) in invalid_rules.items():
            with self.subTest(rule=label), tempfile.TemporaryDirectory() as tmp:
                config_root, home = config_root_home(tmp)
                write(
                    config_root / "permissions" / "policy.yaml",
                    permission_policy_doc(),
                )
                write(config_root / "permissions" / "commands" / "test.yaml", source)
                with self.assertRaisesRegex(SourceSchemaError, message):
                    run_sync(config_root=config_root, home=home)

    def test_a_patch_may_only_tighten_the_generated_workspace_roots(self) -> None:
        """The portable workspace block owns which roots the agent may reach.

        A machine-local patch still needs somewhere to deny a credential path,
        so an overlay of denials is accepted while a widening one is not.
        """
        for decision, expected in (("deny", ""), ("allow", "may only add deny")):
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
                if not expected:
                    run_sync(config_root=config_root, home=home)
                    entry = json.loads(
                        (home / ".config/opencode/opencode.json").read_text()
                    )["permission"]["external_directory"]
                    self.assertEqual(entry["~/.netrc"], "deny")
                    self.assertEqual(entry["~/Developer/**"], "allow")
                else:
                    with self.assertRaisesRegex(Exception, expected):
                        run_sync(config_root=config_root, home=home)

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
                        "tools": {"inherit": True, "deny": ["Write"]},
                        "effort": "high",
                        "color": "blue",
                        "claude:model": "claude-override",
                        "cursor:tools": ["edit"],
                        "cursor:model": "cursor-model",
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
            write(
                config_root / "target-config" / "codex" / "rules" / "git.rules",
                'prefix_rule(pattern=["git"], decision="allow")\n',
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
            cursor_agent = (home / ".cursor" / "agents" / "reviewer.md").read_text(
                encoding="utf-8"
            )
            opencode_agent = (
                home / ".config" / "opencode" / "agents" / "reviewer.md"
            ).read_text(encoding="utf-8")
            self.assertIn("model: claude-override", claude_agent)
            self.assertIn("tools: inherit", claude_agent)
            self.assertIn("disallowedTools: Write", claude_agent)
            self.assertIn("effort: high", claude_agent)
            self.assertIn("color: blue", claude_agent)
            self.assertIn("- edit", cursor_agent)
            self.assertIn("model: cursor-model", cursor_agent)
            self.assertNotIn("model: claude-override", cursor_agent)
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
            self.assertIn('path = "~/.codex/skills/review-pr"', cfg)
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

            codex_command_skill = (
                home / ".codex" / "skills" / "review-pr" / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertIn("name: review-pr", codex_command_skill)
            self.assertIn("run the review-pr command", codex_command_skill)
            self.assertIn("Do not auto-invoke this skill", codex_command_skill)
            self.assertIn("not a Codex slash command", codex_command_skill)
            self.assertIn("Original command agent: `reviewer`", codex_command_skill)
            self.assertIn(
                "Original command requested a forked subtask context",
                codex_command_skill,
            )
            codex_rules = (home / ".codex" / "rules" / "coding-agents.rules").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                'prefix_rule(pattern=["python"], decision="prompt", '
                'justification="Python scripts need review.")',
                codex_rules,
            )
            codex_native_rules = (home / ".codex" / "rules" / "git.rules").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                'prefix_rule(pattern=["git"], decision="allow")',
                codex_native_rules,
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
            self.assertFalse((cursor / MANAGED_MANIFEST).exists())
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
            self.assertTrue((cursor / "agents" / "reviewer.md").exists())

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

    def test_unrecognized_prefixed_field_passes_through_with_warning(self) -> None:
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

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run_sync(config_root=config_root, home=home)

            opencode_command = (
                home / ".config" / "opencode" / "commands" / "one.md"
            ).read_text(encoding="utf-8")
            self.assertIn("agent: reviewer", opencode_command)
            self.assertIn("unknown: kept", opencode_command)
            self.assertIn(
                "unrecognized opencode command field `unknown`", stderr.getvalue()
            )

    def test_agent_permission_lowering_derived_for_cursor_codex_opencode(self) -> None:
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

            run_sync(config_root=config_root, home=home)

            cursor_agent = (home / ".cursor" / "agents" / "readonly.md").read_text(
                encoding="utf-8"
            )
            codex_agent = (home / ".codex" / "agents" / "readonly.toml").read_text(
                encoding="utf-8"
            )
            opencode_agent = (
                home / ".config" / "opencode" / "agents" / "readonly.md"
            ).read_text(encoding="utf-8")
            self.assertIn("readonly: true", cursor_agent)
            self.assertIn('sandbox_mode = "read-only"', codex_agent)
            self.assertIn("edit: deny", opencode_agent)
            self.assertIn("bash: deny", opencode_agent)

    def test_agent_permission_override_wins_over_derivation(self) -> None:
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

            run_sync(config_root=config_root, home=home)

            cursor_agent = (home / ".cursor" / "agents" / "writer.md").read_text(
                encoding="utf-8"
            )
            codex_agent = (home / ".codex" / "agents" / "writer.toml").read_text(
                encoding="utf-8"
            )
            self.assertIn("readonly: true", cursor_agent)
            self.assertIn('sandbox_mode = "read-only"', codex_agent)

    def test_agent_inherit_false_with_no_allow_list_denies_write_tools(self) -> None:
        # tools: {inherit: false} with no explicit allow/deny is the "start
        # from nothing" case; it must derive the same read-only signal as an
        # explicit allow-list containing no write-capable tools, on every
        # tool -- including Claude, where an absent `tools` key would
        # otherwise mean "inherit everything" (the opposite of the intent).
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

            run_sync(config_root=config_root, home=home)

            claude_agent = (home / ".claude" / "agents" / "bare.md").read_text(
                encoding="utf-8"
            )
            cursor_agent = (home / ".cursor" / "agents" / "bare.md").read_text(
                encoding="utf-8"
            )
            codex_agent = (home / ".codex" / "agents" / "bare.toml").read_text(
                encoding="utf-8"
            )
            opencode_agent = (
                home / ".config" / "opencode" / "agents" / "bare.md"
            ).read_text(encoding="utf-8")
            self.assertIn("disallowedTools:", claude_agent)
            self.assertIn("Bash", claude_agent)
            self.assertIn("readonly: true", cursor_agent)
            self.assertIn('sandbox_mode = "read-only"', codex_agent)
            self.assertIn("edit: deny", opencode_agent)
            self.assertIn("bash: deny", opencode_agent)

    def test_codex_rules_full_dsl_supports_justification_and_match_fixtures(
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

            run_sync(config_root=config_root, home=home)

            rules_file = home / ".codex" / "rules" / "coding-agents.rules"
            content = rules_file.read_text(encoding="utf-8")
            self.assertIn(
                'prefix_rule(pattern=["git", "push", "--force"], '
                'decision="forbidden", '
                'justification="Use --force-with-lease instead.")',
                content,
            )
            self.assertNotIn("match", content)

    def test_codex_rules_translation_order_and_filtering(self) -> None:
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

            run_sync(config_root=config_root, home=home)
            rules_file = home / ".codex" / "rules" / "coding-agents.rules"
            self.assertTrue(rules_file.exists())
            content = rules_file.read_text(encoding="utf-8")
            self.assertLess(
                content.find('prefix_rule(pattern=["python"], decision="allow")'),
                content.find('prefix_rule(pattern=["git"], decision="forbidden")'),
            )
            self.assertNotIn("maybe", content)

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

    def test_codex_skill_copy_preserves_supported_frontmatter(self) -> None:
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

            run_sync(config_root=config_root, home=home)

            codex_skill = (home / ".codex" / "skills" / "s1" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("license: MIT", codex_skill)
            self.assertIn("allowed-tools:", codex_skill)
            self.assertIn("- Bash", codex_skill)
            self.assertIn("metadata:", codex_skill)
            self.assertIn("owner: platform", codex_skill)
            self.assertIn("unknown-native-field: kept", codex_skill)
            claude_skill = (home / ".claude" / "skills" / "s1" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("allowed-tools:", claude_skill)
            self.assertNotIn("metadata:", claude_skill)

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
            codex_command_skill_dir = home / ".codex" / "skills" / "one"
            self.assertTrue((commands_dir / "one.md").exists())
            self.assertTrue((claude_commands_dir / "one.md").exists())
            self.assertTrue((codex_command_skill_dir / "SKILL.md").exists())

            shutil.rmtree(config_root / "commands")
            run_sync(config_root=config_root, home=home)

            self.assertFalse((commands_dir / "one.md").exists())
            self.assertFalse((claude_commands_dir / "one.md").exists())
            self.assertFalse(codex_command_skill_dir.exists())
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
            codex_manifest = json.loads(
                (home / ".codex" / "skills" / ".coding-agents-managed.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(codex_manifest["entries"], {})

    def test_codex_command_skill_uses_source_prefix_on_name_collision(self) -> None:
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
            self.assertIn('path = "~/.codex/skills/source-command-one"', cfg)
            self.assertTrue(
                (
                    home / ".codex" / "skills" / "source-command-one" / "SKILL.md"
                ).exists()
            )
            generated = (
                home / ".codex" / "skills" / "source-command-one" / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertIn("name: source-command-one", generated)
            self.assertIn("run the one command", generated)

    def test_codex_command_skill_cascading_collision_matches_config_registration(
        self,
    ) -> None:
        # Skill "one" forces command "one" to rename to "source-command-one".
        # A second command whose stem literally IS "source-command-one" must
        # then cascade to "source-command-source-command-one" -- and
        # config.toml's registration list must name the exact same three
        # directories that land on disk, not diverge on the second rename.
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
            for path in (
                "~/.codex/skills/one",
                "~/.codex/skills/source-command-one",
                "~/.codex/skills/source-command-source-command-one",
            ):
                self.assertIn(f'path = "{path}"', cfg)
                self.assertTrue(
                    (home / ".codex" / path.removeprefix("~/.codex/")).exists()
                )

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
