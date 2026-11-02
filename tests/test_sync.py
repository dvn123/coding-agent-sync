import contextlib
import io
import json
import shutil
import stat
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any, Literal

import yaml
from capabilities.audit import OpenCodeRules

from coding_agents_sync import run_sync
from coding_agents_sync.io import (
    MANAGED_MANIFEST,
    ManagedEntryConflict,
    sync_manifested_entries,
)
from coding_agents_sync.output.native import NativeConfigError
from coding_agents_sync.output.patches import PatchError
from coding_agents_sync.sources import SourceSchemaError, load_permissions
from coding_agents_sync.targets.opencode import UNWRAP_PLUGIN
from coding_agents_sync.targets.permissions import CLAUDE_OPAQUE_COMMANDS


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
        "schema": "coding-agents/v1",
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
        if meta.get("execution"):
            for target in ("codex", "cursor"):
                if not only or target in only:
                    target_blocks.setdefault(target, {}).setdefault("omit", {})[
                        "execution"
                    ] = "Explicit-only skills have no delegated execution."
    if kind == "agent":
        only = set(meta.get("only") or ())
        if meta.get("effort") and (not only or "cursor" in only):
            target_blocks.setdefault("cursor", {}).setdefault("omit", {})["effort"] = (
                "Cursor agents have no effort field."
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
    if kind == "rule" and isinstance(activation := meta.pop("activation", None), dict):
        native = target_blocks.setdefault("cursor", {}).setdefault("native", {})
        native["alwaysApply"] = activation.get("always") is not False
        if activation.get("globs"):
            native["globs"] = activation["globs"]
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
        "schema": "coding-agents/v1",
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
        if (
            set(source["tools"].items())
            - {(tool, "allow") for tool in source["tools"]}
            - {("websearch", "deny")}
        ):
            targets.setdefault("codex", {}).setdefault("omit", {})["tools"] = (
                "Codex has no tool gate."
            )
    workspace = source.get("workspace", {})
    if workspace.get("allow") or workspace.get("ask") or workspace.get("deny"):
        targets.setdefault("cursor", {}).setdefault("omit", {})["workspace"] = (
            "Cursor lacks one shared workspace permission surface."
        )
    if workspace.get("ask"):
        targets.setdefault("codex", {}).setdefault("omit", {})["workspace.ask"] = (
            "Codex has no workspace ask channel."
        )
    if source["secret_names"]:
        targets.setdefault("cursor", {}).setdefault("omit", {})["secret_names"] = (
            "Cursor has no secret-name ask channel."
        )
    if source.get("unmatched", "ask") != "ask":
        targets.setdefault("codex", {}).setdefault("omit", {})["unmatched"] = (
            "Codex runs an unlisted command in its sandbox."
        )
        targets.setdefault("cursor", {}).setdefault("omit", {})["unmatched"] = (
            "Cursor Desktop has no deny channel; the CLI projects it."
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
        "schema": "coding-agents/v1",
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
    workspace: dict[str, Any] | None = None,
    unmatched: str | None = None,
) -> None:
    extra: dict[str, Any] = {}
    if tools is not None:
        extra["tools"] = tools
    if workspace is not None:
        extra["workspace"] = workspace
    if unmatched is not None:
        extra["unmatched"] = unmatched
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


def model_policy_doc(
    *, default: str = "sweet-spot", sweet_model: str = "claude-sonnet-5"
) -> str:
    return yaml.safe_dump(
        {
            "schema": "coding-agents/model-policy/v1",
            "default": default,
            "profiles": {
                "sweet-spot": {
                    "claude": {"model": sweet_model, "effort": "xhigh"},
                    "opencode": {
                        "model": "amazon-bedrock/global.openai.gpt-5.6-luna",
                        "effort": "xhigh",
                    },
                    "cursor": {"model": "gpt-5.6-luna"},
                },
                "inherit": {},
            },
        },
        sort_keys=False,
    )


class ModelPolicyTests(unittest.TestCase):
    def test_model_policy_lowers_named_agents_without_touching_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(config_root / "model-policy.yaml", model_policy_doc())
            write(
                config_root / "agents" / "cheap.md",
                source_doc(
                    "agent",
                    "cheap",
                    "cheap",
                    "Cheap body\n",
                    extra={
                        "opencode:mode": "subagent",
                        "codex:model": "gpt-5",
                        "codex:model_reasoning_effort": "high",
                    },
                ),
            )
            write(
                config_root / "agents" / "open.md",
                source_doc(
                    "agent",
                    "open",
                    "open",
                    "Open body\n",
                    extra={
                        "model_policy": "inherit",
                        "opencode:mode": "subagent",
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            claude_cheap = (home / ".claude/agents/cheap.md").read_text()
            claude_open = (home / ".claude/agents/open.md").read_text()
            opencode_cheap = (home / ".config/opencode/agents/cheap.md").read_text()
            opencode_open = (home / ".config/opencode/agents/open.md").read_text()
            codex_cheap = (home / ".codex/agents/cheap.toml").read_text()

            self.assertIn("model: claude-sonnet-5", claude_cheap)
            self.assertIn("effort: xhigh", claude_cheap)
            self.assertIn("model: inherit", claude_open)
            self.assertNotIn("effort:", claude_open)
            self.assertIn(
                "model: amazon-bedrock/global.openai.gpt-5.6-luna", opencode_cheap
            )
            self.assertIn("reasoningEffort: xhigh", opencode_cheap)
            self.assertNotIn("model:", opencode_open)
            self.assertNotIn("reasoningEffort:", opencode_open)
            self.assertIn('model = "gpt-5"', codex_cheap)
            self.assertIn('model_reasoning_effort = "high"', codex_cheap)
            self.assertEqual(
                (home / ".cursor/agents/cheap.md").read_text(),
                "---\nname: cheap\nmodel: gpt-5.6-luna\n---\n\nCheap body\n",
            )
            self.assertNotIn("model:", (home / ".cursor/agents/open.md").read_text())

    def test_model_policy_rejects_unknown_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(config_root / "model-policy.yaml", model_policy_doc())
            write(
                config_root / "agents" / "unknown.md",
                source_doc(
                    "agent",
                    "unknown",
                    "unknown",
                    "body\n",
                    extra={"model_policy": "missing"},
                ),
            )
            with self.assertRaisesRegex(SourceSchemaError, "unknown model policy"):
                run_sync(config_root=config_root, home=home)

    def test_model_policy_rejects_native_model_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(config_root / "model-policy.yaml", model_policy_doc())
            write(
                config_root / "agents" / "conflict.md",
                source_doc(
                    "agent",
                    "conflict",
                    "conflict",
                    "body\n",
                    extra={
                        "model_policy": "sweet-spot",
                        "claude:model": "opus",
                    },
                ),
            )
            with self.assertRaisesRegex(ValueError, "model policy owns Claude fields"):
                run_sync(config_root=config_root, home=home)

    def test_model_policy_rejects_a_native_model_override(self) -> None:
        for field, message in (
            ("opencode:model", "model policy owns OpenCode fields"),
            ("cursor:model", "model policy owns Cursor fields"),
        ):
            with self.subTest(field), tempfile.TemporaryDirectory() as tmp:
                config_root, home = config_root_home(tmp)
                write(config_root / "model-policy.yaml", model_policy_doc())
                write(
                    config_root / "agents" / "conflict.md",
                    source_doc(
                        "agent",
                        "conflict",
                        "conflict",
                        "body\n",
                        extra={"model_policy": "sweet-spot", field: "other/model"},
                    ),
                )
                with self.assertRaisesRegex(ValueError, message):
                    run_sync(config_root=config_root, home=home)

    def test_cursor_agent_lowers_to_desktop_line_frontmatter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "agents" / "scout.md",
                source_doc(
                    "agent",
                    "scout",
                    "scout",
                    "Scout body\n",
                    description="Look around: report back",
                    extra={
                        "only": ["cursor"],
                        "background": True,
                        "cursor:readonly": True,
                        "cursor:model": "composer-2",
                        "cursor:tools": ["read_file", "grep"],
                    },
                ),
            )
            run_sync(config_root=config_root, home=home)

            self.assertEqual(
                (home / ".cursor/agents/scout.md").read_text(),
                "---\nname: scout\ndescription: Look around: report back\n"
                "model: composer-2\nreadonly: true\ntools: read_file, grep\n"
                "background: true\n---\n\n"
                "Scout body\n",
            )

    def test_sweet_spot_fact_finder_keeps_read_only_tool_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(config_root / "model-policy.yaml", model_policy_doc())
            write(
                config_root / "agents" / "fact-finder.md",
                source_doc(
                    "agent",
                    "fact-finder",
                    "fact-finder",
                    "Find facts.\n",
                    extra={
                        "model_policy": "sweet-spot",
                        "claude": {"disallowedTools": ["Edit", "Write", "Agent"]},
                        "opencode": {
                            "mode": "subagent",
                            "permissions": [
                                {"action": "edit", "effect": "deny"},
                                {"action": "subagent", "effect": "deny"},
                            ],
                        },
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            claude = (home / ".claude/agents/fact-finder.md").read_text()
            opencode = (home / ".config/opencode/agents/fact-finder.md").read_text()
            self.assertIn("disallowedTools:", claude)
            self.assertIn("model: claude-sonnet-5", claude)
            self.assertIn("action: edit", opencode)
            self.assertIn("action: subagent", opencode)


class NativeListOwnershipTests(unittest.TestCase):
    """A generated list owns its own entries; tool-added entries survive."""

    def test_tool_added_entry_survives_and_only_generated_entries_are_pruned(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"], ["git", "log"]])
            run_sync(config_root=config_root, home=home)
            settings = home / ".claude" / "settings.json"
            document = json.loads(settings.read_text())
            document["permissions"]["allow"].append("Bash(npm test)")
            write(settings, json.dumps(document))

            write_permissions(config_root, allow=[["git", "status"]])
            run_sync(config_root=config_root, home=home)

            allow = json.loads(settings.read_text())["permissions"]["allow"]
            self.assertEqual(allow[0], "Bash(npm test)")
            self.assertIn("Bash(git status *)", allow)
            self.assertNotIn("Bash(git log *)", allow)
            self.assertEqual(run_sync(config_root=config_root, home=home), ())

    def test_first_claim_keeps_existing_entries_before_generated_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "a.md",
                source_doc("rule", "a", "a", "Rule A\n"),
            )
            config = home / ".config" / "opencode" / "opencode.json"
            write(config, json.dumps({"instructions": ["notes/*.md"]}))

            run_sync(config_root=config_root, home=home)

            self.assertEqual(
                json.loads(config.read_text())["instructions"],
                ["notes/*.md", "~/.config/opencode/rules/*.md"],
            )

    def test_patch_extended_entries_are_owned_and_pruned_with_the_patch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "rules" / "a.md",
                source_doc("rule", "a", "a", "Rule A\n"),
            )
            patch = config_root / "patches" / "opencode.yaml"
            write(
                patch,
                "schema: coding-agents/patch/v1\n"
                "extend:\n  /instructions: [extra/*.md]\n",
            )
            run_sync(config_root=config_root, home=home)
            config = home / ".config" / "opencode" / "opencode.json"
            self.assertEqual(
                json.loads(config.read_text())["instructions"],
                ["~/.config/opencode/rules/*.md", "extra/*.md"],
            )

            patch.unlink()
            run_sync(config_root=config_root, home=home)

            self.assertEqual(
                json.loads(config.read_text())["instructions"],
                ["~/.config/opencode/rules/*.md"],
            )

    def test_first_claim_refuses_a_differing_scalar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
            desktop = home / ".cursor" / "permissions.json"
            write(desktop, json.dumps({"approvalMode": "unrestricted"}))

            with self.assertRaisesRegex(
                NativeConfigError, "existing native value differs"
            ):
                run_sync(config_root=config_root, home=home)

            self.assertEqual(
                json.loads(desktop.read_text()), {"approvalMode": "unrestricted"}
            )


class IncidentalFileTests(unittest.TestCase):
    def test_caches_a_skill_leaves_behind_are_neither_drift_nor_a_conflict(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            skill = config_root / "skills" / "tool" / "SKILL.md"
            write(skill, source_doc("skill", "tool", "tool", "body\n"))
            write(skill.parent / "run.py", "print('hi')\n")
            run_sync(config_root=config_root, home=home)
            deployed = home / ".claude" / "skills" / "tool"
            write(deployed / "__pycache__" / "run.cpython-314.pyc", "bytecode")
            write(deployed / ".DS_Store", "finder")

            self.assertEqual(
                run_sync(config_root=config_root, home=home, check=True), ()
            )
            run_sync(config_root=config_root, home=home)
            self.assertTrue((deployed / "__pycache__" / "run.cpython-314.pyc").exists())

            skill.unlink()
            (skill.parent / "run.py").unlink()
            run_sync(config_root=config_root, home=home)
            self.assertFalse(deployed.exists())


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


def opencode_rules(config: dict[str, Any], action: str) -> dict[str, str]:
    """One action's native rules as an ordered resource map.

    Re-inserting a repeated resource keeps it at its last position, so the map
    resolves exactly as OpenCode's last-match evaluation of the list does.
    """
    rules: dict[str, str] = {}
    for rule in config["permissions"]:
        if rule["action"] == action:
            rules.pop(rule["resource"], None)
            rules[rule["resource"]] = rule["effect"]
    return rules


def resolve_opencode_bash(patterns: dict[str, str], command: str) -> str:
    return OpenCodeRules(list(patterns.items())).decision(command)


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
            bash = opencode_rules(opencode, "shell")
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

            self.assertFalse((home / ".cursor/cli-config.json").exists())
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
                deny=[{"command": "othertool", "text": [" -T", " --user "]}],
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode_rules(opencode, "shell")

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
            # A leading space anchors the substring to a token boundary.
            self.assertIn("Bash(othertool* -T*)", claude["permissions"]["deny"])
            # A trailing space also lands bare-ended for Claude, whose final
            # ` *` matches an empty end only as the sole wildcard.
            self.assertIn("Bash(othertool* --user)", claude["permissions"]["deny"])
            for command, decision in {
                "othertool --check -T f": "deny",
                "othertool --check -Tf": "deny",
                "othertool --check -H Content-Type": "allow",
                "othertool --check --user x": "deny",
                "othertool --check --user-agent x": "allow",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)
            self.assertFalse((home / ".cursor/cli-config.json").exists())
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"],
                [
                    "mytool run --dry-run",
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
            bash = opencode_rules(opencode, "shell")
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

            self.assertFalse((home / ".cursor/cli-config.json").exists())
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["approvalMode"], "allowlist")
            self.assertEqual(
                desktop["terminalAllowlist"],
                [
                    "git status",
                    "git -C status",
                    "git --no-pager status",
                    "git:-C * status",
                    "git:-C * status *",
                    "git:--no-pager * status",
                    "git:--no-pager * status *",
                ],
            )

    def test_a_deny_reaches_every_target_that_has_a_deny_channel(self) -> None:
        """Two targets degrade, for unrelated reasons.

        Cursor Desktop's shipped schema has no deny key. OpenCode has one but
        must not be given it: a denial there serializes the entire bash
        ruleset into the model-visible error, so guards land as `ask`.
        """
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
                    # Claude does not see past a leading redirect.
                    *(f"Bash({pattern})" for pattern in CLAUDE_OPAQUE_COMMANDS),
                ],
            )
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode_rules(opencode, "shell")
            for command, decision in {
                "kubectl get pods": "allow",
                "kubectl apply -f x.yaml": "deny",
                "kubectl --context prod apply -f x.yaml": "deny",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

            self.assertFalse((home / ".cursor/cli-config.json").exists())
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"],
                [
                    "kubectl get",
                    "kubectl --context get",
                    "kubectl:--context * get",
                    "kubectl:--context * get *",
                ],
            )

    def test_unmatched_allow_lands_only_where_the_guards_outrank_it(self) -> None:
        """Codex and Cursor Desktop owe an omit.

        OpenCode writes every rule after the `*` entry, Claude's deny rules
        outrank its bare `Bash` allow, and the Cursor CLI's deny list survives
        `unrestricted`, so the guards still bind on all three. Desktop has no
        deny channel, and `unrestricted` there auto-runs everything, so it
        keeps `allowlist` and prompts for a guarded command exactly as before.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["kubectl", "get"]],
                deny=[["kubectl", "apply"]],
                unmatched="allow",
            )

            run_sync(config_root=config_root, home=home)

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode_rules(opencode, "shell")
            self.assertEqual(bash["*"], "allow")
            for command, decision in {
                "anything at all": "allow",
                "kubectl get pods": "allow",
                "kubectl apply -f x.yaml": "deny",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

            self.assertFalse((home / ".cursor/cli-config.json").exists())
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["approvalMode"], "allowlist")
            self.assertNotIn("kubectl apply", desktop["terminalAllowlist"])

            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertNotIn("defaultMode", claude["permissions"])
            # The blanket allow leaves every narrower allow dead.
            self.assertEqual(claude["permissions"]["allow"], ["Bash"])
            self.assertNotIn("kubectl get *", bash)
            self.assertIn("Bash(kubectl apply *)", claude["permissions"]["deny"])

    def test_unmatched_allow_without_an_omission_fails_the_three_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "permissions/policy.yaml",
                "schema: coding-agents/v1\n"
                "kind: permission-policy\n"
                "id: user\n"
                "name: User\n"
                "unmatched: allow\n",
            )

            with self.assertRaisesRegex(
                ValueError, "unmatched is unsupported on (claude|codex|cursor)"
            ):
                run_sync(config_root=config_root, home=home)

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

            self.assertFalse((home / ".cursor/cli-config.json").exists())
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
            self.assertFalse((home / ".cursor/cli-config.json").exists())
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
            self.assertFalse((home / ".cursor/cli-config.json").exists())
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["terminalAllowlist"], ["git status"])

    def test_a_narrowing_deny_withholds_its_allow_from_cursor(self) -> None:
        """Cursor's shared allowlist has no deny channel, so an allow a deny
        narrows leaves it and the denied variant prompts instead of running."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "push"], ["git", "status"]],
                deny=[{"command": "git", "subcommand": ["push"], "tail": ["--force"]}],
            )

            run_sync(config_root=config_root, home=home)

            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["approvalMode"], "allowlist")
            self.assertEqual(desktop["terminalAllowlist"], ["git status"])
            self.assertFalse((home / ".cursor/cli-config.json").exists())

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

            self.assertFalse((home / ".cursor/cli-config.json").exists())
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["terminalAllowlist"], ["git status"])

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

            self.assertFalse((home / ".cursor/cli-config.json").exists())
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["terminalAllowlist"], ["kubectl logs"])

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode_rules(opencode, "shell")
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

            self.assertFalse((home / ".cursor/cli-config.json").exists())
            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertIn(
                "Bash(~/.config/coding-agents/sync.sh *)",
                claude["permissions"]["allow"],
            )

    def test_a_tilde_command_also_matches_its_home_variable_spelling(self) -> None:
        """Glob targets match the text as typed, and agents also type `$HOME/`."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["~/bin/tool", "status"]],
                deny=[["~/bin/tool", "push"]],
            )

            run_sync(config_root=config_root, home=home)

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode_rules(opencode, "shell")
            for command, decision in {
                "~/bin/tool status": "allow",
                "$HOME/bin/tool status -v": "allow",
                "$HOME/bin/tool push": "deny",
                "$HOME/bin/tool reset": "ask",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)
            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertIn(
                "Bash($HOME/bin/tool status *)", claude["permissions"]["allow"]
            )

    def test_a_declared_assignment_prefixes_every_rule_its_target_does_not_peel(
        self,
    ) -> None:
        """A literal `NAME=value` agents type before a command decides nothing.

        Claude strips any assignment before an ask or deny, and the
        opencode-unwrap plugin before a deny, so only the other rules get
        copies behind it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root)
            write(
                config_root / "permissions" / "commands" / "test.yaml",
                permission_rules_doc(
                    allow=[["tool", "status"]],
                    ask=[["tool", "push"]],
                    deny=[["tool", "reset"]],
                    extra={
                        "assignments": {"tool": ["EDITOR=true", "IFS=", "IFS=$'\\t'"]}
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode_rules(opencode, "shell")
            for command, decision in {
                "EDITOR=true tool status": "allow",
                "IFS= tool status -v": "allow",
                "IFS=$'\\t' tool status": "allow",
                "EDITOR=true tool reset": "deny",
                "EDITOR=true tool push": "ask",
                "EDITOR=false tool status": "ask",
                "OTHER=1 tool status": "ask",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)
            self.assertEqual(
                {
                    effect
                    for resource, effect in bash.items()
                    if resource.startswith("EDITOR=true ")
                },
                {"allow", "ask"},
            )
            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(
                {
                    bucket: [
                        rule for rule in rules if rule.startswith("Bash(EDITOR=true ")
                    ]
                    for bucket, rules in claude["permissions"].items()
                    if bucket in ("allow", "ask", "deny")
                },
                {
                    "allow": ["Bash(EDITOR=true tool status *)"],
                    "ask": [],
                    "deny": [],
                },
            )

    def test_a_text_rule_absorbs_the_token_rules_it_covers_on_glob_targets(
        self,
    ) -> None:
        """Codex keeps the token rule, its only reach; the glob targets drop it."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root)
            write(
                config_root / "permissions" / "commands" / "test.yaml",
                permission_rules_doc(
                    deny=[
                        {
                            "command": "kube",
                            "subcommand": ["get"],
                            "tail": [["-oyaml"]],
                        },
                        {"command": "kube", "subcommand": ["get"], "text": ["yaml"]},
                    ]
                ),
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(
                [rule for rule in claude["permissions"]["deny"] if "kube" in rule],
                ["Bash(kube get*yaml*)"],
            )
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            self.assertEqual(
                {
                    resource: effect
                    for resource, effect in opencode_rules(opencode, "shell").items()
                    if "kube" in resource
                },
                {"kube get*yaml*": "deny"},
            )
            self.assertIn(
                'prefix_rule(pattern=["kube", "get", "-oyaml"], decision="forbidden")',
                (home / ".codex/rules/coding-agents.rules").read_text(),
            )

    def test_a_skill_narrowed_by_only_keeps_each_readers_own_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills/narrow/SKILL.md",
                source_doc(
                    "skill",
                    "narrow",
                    "narrow",
                    "Body\n",
                    description="Narrow",
                    extra={"only": ["claude", "codex", "cursor"]},
                ),
            )

            run_sync(config_root=config_root, home=home)

            self.assertEqual(
                {
                    directory: (home / directory / "narrow/SKILL.md").exists()
                    for directory in (
                        ".agents/skills",
                        ".claude/skills",
                        ".codex/skills",
                        ".cursor/skills",
                        ".config/opencode/skills",
                    )
                },
                {
                    ".agents/skills": False,
                    ".claude/skills": True,
                    ".codex/skills": True,
                    ".cursor/skills": True,
                    ".config/opencode/skills": False,
                },
            )

    def test_a_shared_skill_rejects_readers_that_need_different_frontmatter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills/split/SKILL.md",
                source_doc(
                    "skill",
                    "split",
                    "split",
                    "Body\n",
                    description="Split",
                    extra={"cursor:description": "Cursor wording"},
                ),
            )

            with self.assertRaisesRegex(
                ValueError, "Codex and Cursor need different `description`"
            ):
                run_sync(config_root=config_root, home=home)

    def test_mcp_servers_lower_to_each_targets_native_registration(self) -> None:
        """Leaf fields only, so a patch's sibling settings stay its own, and a
        value a patch already wrote is taken over as is."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "mcp" / "browser.yaml",
                yaml.safe_dump(
                    {
                        "schema": "coding-agents/v1",
                        "kind": "mcp-server",
                        "id": "browser",
                        "command": "browser-mcp",
                        "args": ["--headless"],
                        "env": {"MODE": "quiet"},
                        "targets": {
                            "cursor": {"native": {"command": "/opt/bin/browser-mcp"}}
                        },
                    }
                ),
            )
            write(
                config_root / "mcp" / "remote.yaml",
                yaml.safe_dump(
                    {
                        "schema": "coding-agents/v1",
                        "kind": "mcp-server",
                        "id": "remote",
                        "url": "https://example.com/mcp",
                        "only": ["codex", "opencode"],
                    }
                ),
            )
            write(
                config_root / "patches" / "codex.yaml",
                "schema: coding-agents/patch/v1\nset:\n"
                "  /mcp_servers/browser/default_tools_approval_mode: auto\n",
            )
            write(
                home / ".claude.json",
                json.dumps({"mcpServers": {"browser": {"command": "browser-mcp"}}}),
            )

            run_sync(config_root=config_root, home=home)

            self.assertEqual(
                json.loads((home / ".claude.json").read_text())["mcpServers"],
                {
                    "browser": {
                        "type": "stdio",
                        "command": "browser-mcp",
                        "args": ["--headless"],
                        "env": {"MODE": "quiet"},
                    }
                },
            )
            self.assertEqual(
                tomllib.loads((home / ".codex/config.toml").read_text())["mcp_servers"],
                {
                    "browser": {
                        "command": "browser-mcp",
                        "args": ["--headless"],
                        "env": {"MODE": "quiet"},
                        "default_tools_approval_mode": "auto",
                    },
                    "remote": {"url": "https://example.com/mcp"},
                },
            )
            self.assertEqual(
                json.loads((home / ".cursor/mcp.json").read_text())["mcpServers"],
                {
                    "browser": {
                        "command": "/opt/bin/browser-mcp",
                        "args": ["--headless"],
                        "env": {"MODE": "quiet"},
                    }
                },
            )
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            self.assertEqual(
                opencode["mcp"],
                {
                    "servers": {
                        "browser": {
                            "type": "local",
                            "command": ["browser-mcp", "--headless"],
                            "environment": {"MODE": "quiet"},
                        },
                        "remote": {"type": "remote", "url": "https://example.com/mcp"},
                    }
                },
            )

    def test_mcp_tool_decisions_lower_to_each_targets_rules(self) -> None:
        """Without a permission policy too. Cursor Desktop has no MCP deny, so
        a deny there needs its omit."""
        server = {
            "schema": "coding-agents/v1",
            "kind": "mcp-server",
            "id": "browser",
            "command": "browser-mcp",
            "tools": {"click": "ask", "run_code": "deny", "snapshot": "allow"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(config_root / "mcp" / "browser.yaml", yaml.safe_dump(server))

            with self.assertRaisesRegex(
                ValueError, r"tools is unsupported on cursor; add targets\.cursor"
            ):
                run_sync(config_root=config_root, home=home)

            server["targets"] = {"cursor": {"omit": {"tools": "Desktop cannot deny."}}}
            write(config_root / "mcp" / "browser.yaml", yaml.safe_dump(server))
            run_sync(config_root=config_root, home=home)

            self.assertEqual(
                json.loads((home / ".claude/settings.json").read_text())["permissions"],
                {
                    "allow": ["mcp__browser__snapshot"],
                    "ask": ["mcp__browser__click"],
                    "deny": ["mcp__browser__run_code"],
                },
            )
            codex = tomllib.loads((home / ".codex/config.toml").read_text())
            self.assertEqual(
                codex["mcp_servers"]["browser"],
                {
                    "command": "browser-mcp",
                    "tools": {
                        "snapshot": {"approval_mode": "approve"},
                        "click": {"approval_mode": "prompt"},
                    },
                    "disabled_tools": ["run_code"],
                },
            )
            self.assertEqual(
                json.loads((home / ".cursor/permissions.json").read_text()),
                {"approvalMode": "allowlist", "mcpAllowlist": ["browser:snapshot"]},
            )
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            self.assertEqual(
                opencode["permissions"],
                [
                    {"action": "browser_click", "resource": "*", "effect": "ask"},
                    {"action": "browser_run_code", "resource": "*", "effect": "deny"},
                    {"action": "browser_snapshot", "resource": "*", "effect": "allow"},
                ],
            )

    def test_an_mcp_server_takes_exactly_one_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "mcp" / "both.yaml",
                "schema: coding-agents/v1\nkind: mcp-server\nid: both\n"
                "command: x\nurl: https://example.com/mcp\n",
            )

            with self.assertRaisesRegex(SourceSchemaError, "exactly one of command"):
                run_sync(config_root=config_root, home=home)

    def test_an_assignment_value_is_literal(self) -> None:
        for assignments, message in (
            ({"tool": ["EDITOR=*"]}, "String should match pattern"),
            ({"tool": ["EDITOR=a b"]}, "String should match pattern"),
            ({"other": ["EDITOR=true"]}, "assignments declared for other"),
        ):
            with (
                self.subTest(assignments=assignments),
                tempfile.TemporaryDirectory() as tmp,
            ):
                config_root, home = config_root_home(tmp)
                write_permissions(config_root)
                write(
                    config_root / "permissions" / "commands" / "test.yaml",
                    permission_rules_doc(
                        allow=[["tool", "status"]], extra={"assignments": assignments}
                    ),
                )
                with self.assertRaisesRegex(SourceSchemaError, message):
                    run_sync(config_root=config_root, home=home)

    def test_a_path_token_also_matches_its_quoted_spellings(self) -> None:
        """Glob targets match quotes as typed, and agents quote path arguments."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["tool", "post", "/api/search"]])

            run_sync(config_root=config_root, home=home)

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode_rules(opencode, "shell")
            for command, decision in {
                "tool post /api/search --json {}": "allow",
                "tool post '/api/search' --json {}": "allow",
                'tool post "/api/search"': "allow",
                "tool post '/api/create'": "ask",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)
            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertIn(
                "Bash(tool post '/api/search' *)", claude["permissions"]["allow"]
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

            self.assertFalse((home / ".cursor/cli-config.json").exists())
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["terminalAllowlist"], ["git status"])

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
            self.assertFalse((home / ".cursor/cli-config.json").exists())
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["terminalAllowlist"], ["git status"])

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
            self.assertFalse((home / ".cursor/cli-config.json").exists())
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
            self.assertFalse((home / ".cursor/cli-config.json").exists())
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
            self.assertFalse((home / ".cursor/cli-config.json").exists())
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
                "Codex prefix rules cannot hold a text predicate or an exact match, "
                "so these fall to its sandbox: deny git push:",
                stderr.getvalue(),
            )
            # Cursor withholds guarded allows rather than lowering the deny, so
            # a text deny needs no form there and an unrelated allow stays.
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["terminalAllowlist"], ["git status"])
            self.assertFalse((home / ".cursor/cli-config.json").exists())

    def test_opencode_orders_buckets_so_the_strictest_decision_matches_last(
        self,
    ) -> None:
        """Insertion order is the whole precedence contract on this target.

        `Permission.evaluate` applies the last matching key, so a stricter
        decision must sit after every looser pattern it has to beat, including
        the blanket wrapper allows.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                ask=[["rmdir"]],
                deny=[["shred"], ["git", "push"]],
                secret_names=["EXAMPLE_TOKEN"],
                workspace={"allow": ["~/Developer"], "deny": ["~/.ssh"]},
            )

            run_sync(config_root=config_root, home=home)

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode_rules(opencode, "shell")
            self.assertEqual(
                bash,
                {
                    "*": "ask",
                    "git status *": "allow",
                    "rmdir *": "ask",
                    "shred *": "deny",
                    "git push *": "deny",
                    "*EXAMPLE_TOKEN*": "deny",
                },
            )
            self.assertEqual(list(bash)[-1], "*EXAMPLE_TOKEN*")
            for command, decision in {
                "shred f": "deny",
                # The bundled opencode-unwrap plugin peels program paths,
                # assignments, and wrappers before judging a deny, so the
                # rules carry no copies; a mention in arguments is not peeled.
                "/usr/bin/shred f": "deny",
                "/usr/bin/git push": "deny",
                "ls /usr/bin/shred": "ask",
                "git push": "deny",
                "GIT_TRACE=1 git push": "deny",
                "A=1 B=2 timeout 5 git push origin": "deny",
                'git commit -m "retries=3 and shred it"': "ask",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)
            self.assertEqual(
                (home / ".config/opencode/plugins/opencode-unwrap.js").read_bytes(),
                UNWRAP_PLUGIN.read_bytes(),
            )
            # No `*`: OpenCode's own ask default precedes its built-in allows.
            self.assertEqual(
                opencode_rules(opencode, "external_directory"),
                {"~/Developer/**": "allow", "~/.ssh/**": "deny"},
            )
            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertIn("Bash(*EXAMPLE_TOKEN*)", claude["permissions"]["deny"])
            self.assertEqual(
                claude["sandbox"],
                {
                    "credentials": {
                        "envVars": [{"name": "EXAMPLE_TOKEN", "mode": "deny"}]
                    }
                },
            )
            self.assertIn("Bash(shred *)", claude["permissions"]["deny"])
            self.assertIn("Bash(git push *)", claude["permissions"]["deny"])
            self.assertEqual(
                claude["permissions"]["additionalDirectories"], ["~/Developer"]
            )
            self.assertIn("Read(~/.ssh/**)", claude["permissions"]["deny"])

    def test_denied_paths_reach_every_anchor_each_target_checks(self) -> None:
        """Claude anchors a bare pattern at the cwd, so `**/x` goes root
        absolute. OpenCode checks files inside the session directory by their
        relative path, which neither `**/x` nor an expanded `~/x` matches."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                tools={"read": "allow", "edit": "allow"},
                secret_paths=["**/.env", "~/.aws/credentials"],
                secret_names=["EXAMPLE_TOKEN"],
                workspace={"allow": ["~/Developer"], "deny": ["~/.ssh"]},
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            deny = claude["permissions"]["deny"]
            for rule in (
                "Read(//**/.env)",
                "Edit(//**/.env)",
                "Read(~/.aws/credentials)",
                "Edit(~/.ssh/**)",
            ):
                with self.subTest(rule=rule):
                    self.assertIn(rule, deny)
            self.assertIn("Edit(~/Developer/**)", claude["permissions"]["allow"])
            self.assertNotIn("Read(//**)", claude["permissions"]["allow"])
            # Codex selects a profile on its workspace sandbox: allowed
            # directories are written, denied paths unreadable at any depth.
            codex = tomllib.loads((home / ".codex/config.toml").read_text())
            self.assertEqual(codex["default_permissions"], "coding-agents")
            self.assertNotIn("approval_policy", codex)
            self.assertEqual(
                codex["shell_environment_policy"], {"exclude": ["EXAMPLE_TOKEN"]}
            )
            # Cursor's agent reads a .cursorignore in every workspace ancestor.
            self.assertEqual(
                (home / ".cursorignore").read_text(),
                "# Generated by coding-agents sync\n"
                "**/.env\n/.aws/credentials\n/.ssh/**\n",
            )
            self.assertEqual(
                codex["permissions"]["coding-agents"],
                {
                    "description": "permissions",
                    "extends": ":workspace",
                    "filesystem": {
                        str(home / "Developer"): "write",
                        str(home / ".ssh"): "deny",
                        "/**/.env": "deny",
                        str(home / ".aws/credentials"): "deny",
                    },
                },
            )

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            read = opencode_rules(opencode, "read")
            for resource, decision in {
                ".env": "deny",
                "app/.env": "deny",
                "/Users/x/Developer/app/.env": "deny",
                ".aws/credentials": "deny",
                ".ssh/id_work": "deny",
                "src/main.py": "allow",
            }.items():
                with self.subTest(resource=resource):
                    self.assertEqual(resolve_opencode_bash(read, resource), decision)

    def test_windows_drive_directories_reach_each_target(self) -> None:
        """A Windows directory outside the home is spelled `C:/x`. Claude
        matches rules against its POSIX form, so the rules say `//c/x`."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                tools={"read": "allow", "edit": "allow"},
                workspace={"allow": ["C:/mise"], "deny": ["D:/vault"]},
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(
                claude["permissions"]["additionalDirectories"], ["C:/mise"]
            )
            self.assertIn("Edit(//c/mise/**)", claude["permissions"]["allow"])
            self.assertIn("Read(//d/vault/**)", claude["permissions"]["deny"])
            codex = tomllib.loads((home / ".codex/config.toml").read_text())
            self.assertEqual(
                codex["permissions"]["coding-agents"]["filesystem"],
                {"C:/mise": "write", "D:/vault": "deny"},
            )
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            self.assertEqual(
                opencode_rules(opencode, "external_directory"),
                {"C:/mise/**": "allow", "D:/vault/**": "deny"},
            )

    def test_a_websearch_deny_disables_codex_web_search(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root, allow=[["git", "status"]], tools={"websearch": "deny"}
            )

            run_sync(config_root=config_root, home=home)

            codex = tomllib.loads((home / ".codex/config.toml").read_text())
            self.assertEqual(codex["web_search"], "disabled")

    def test_a_workspace_ask_directory_asks_on_claude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                tools={"read": "allow", "edit": "allow"},
                workspace={"unmatched": "allow", "ask": ["~/review"]},
            )

            run_sync(config_root=config_root, home=home)

            permissions = json.loads((home / ".claude/settings.json").read_text())[
                "permissions"
            ]
            self.assertEqual(
                permissions["ask"], ["Read(~/review/**)", "Edit(~/review/**)"]
            )
            self.assertIn("Read(//**)", permissions["allow"])
            self.assertNotIn("~/review", permissions.get("additionalDirectories", []))

    def test_workspace_unmatched_allow_opens_every_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                tools={"read": "allow", "edit": "allow"},
                workspace={"unmatched": "allow", "deny": ["~/.ssh"]},
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            allow = claude["permissions"]["allow"]
            self.assertIn("Read(//**)", allow)
            self.assertIn("Edit(//**)", allow)
            self.assertIn("Read(~/.ssh/**)", claude["permissions"]["deny"])
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            self.assertEqual(
                opencode_rules(opencode, "external_directory"),
                {"*": "allow", "~/.ssh/**": "deny"},
            )
            codex = tomllib.loads((home / ".codex/config.toml").read_text())
            self.assertEqual(
                codex["permissions"]["coding-agents"]["filesystem"],
                {":root": "write", str(home / ".ssh"): "deny"},
            )

    def test_opencode_guards_deny_over_narrower_allows(self) -> None:
        """OpenCode 2.0 reports a bash denial without the ruleset, so guards
        land as `deny` and beat the allow bucket and the wrapper allows."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["gcloud", "config"], ["git", "checkout"]],
                wrappers=["timeout"],
                deny=[
                    ["gcloud", "auth", "print-access-token"],
                    {"command": "git", "subcommand": ["checkout"], "tail": ["--force"]},
                ],
            )

            run_sync(config_root=config_root, home=home)

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode_rules(opencode, "shell")
            for command in (
                "gcloud auth print-access-token",
                "timeout 5 gcloud auth print-access-token",
                "git checkout --force main",
            ):
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), "deny")
            self.assertEqual(resolve_opencode_bash(bash, "git checkout main"), "allow")

            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertIn(
                "Bash(gcloud auth print-access-token *)",
                claude["permissions"]["deny"],
            )

    def test_wrapper_guards_bind_where_the_target_cannot_peel_the_wrapper(self) -> None:
        """A wrapped copy of every guard, and no blanket wrapper allow.

        Claude Code peels `timeout` and `env` before matching a guard, so it
        needs no copy for them, and retries a guard behind a bare `xargs`, so
        it receives only the `xargs` copy that carries options. A blanket
        `env *` allow would match any payload the wrapper carries, so a wrapped
        allowed command falls to `unmatched` on the glob targets and prompts on
        Cursor, which lists no wrapped spelling.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                ask=[["rmdir"]],
                wrappers=["timeout", "env", "xargs"],
            )

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            self.assertEqual(claude["permissions"]["allow"], ["Bash(git status *)"])
            self.assertEqual(
                claude["permissions"]["ask"],
                [
                    "Bash(rmdir *)",
                    "Bash(xargs * rmdir *)",
                    "Bash(xargs * rmdir)",
                ],
            )

            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            bash = opencode_rules(opencode, "shell")
            self.assertEqual(
                list(bash),
                [
                    "*",
                    "git status *",
                    "rmdir *",
                    "timeout rmdir *",
                    "timeout * rmdir *",
                    "env rmdir *",
                    "env * rmdir *",
                    "xargs rmdir *",
                    "xargs * rmdir *",
                ],
            )
            for command, decision in {
                "git status": "allow",
                "timeout 5 git status": "ask",
                "timeout 5 rmdir build": "ask",
                "env FOO=1 rmdir build": "ask",
                "env rmdir build": "ask",
                "timeout rmdir build": "ask",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

            self.assertFalse((home / ".cursor/cli-config.json").exists())
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(
                desktop["terminalAllowlist"],
                # A wrapped spelling is not listed, so it prompts.
                ["git status"],
            )

    def test_codex_receives_asks_as_prompt_and_denies_as_forbidden(self) -> None:
        """Asks and denies land as native prefixes beside the hand-authored
        Starlark.

        Codex exec policy has no wildcards and lapses entirely on any `$VAR`
        or substitution, so it is not a containment boundary; a prefix still
        governs the plain spelling. Allows stay with the sandbox, because a
        Codex allow rule runs outside it, and predicates with no prefix form
        are dropped with a warning.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                allow=[["git", "status"]],
                ask=[["rmdir"]],
                deny=[
                    ["shred"],
                    {"command": "gh", "subcommand": ["auth", "status"], "tail": ["-t"]},
                    {"command": "rg", "text": "--pre"},
                    {"command": "ls", "exact": True},
                ],
                wrappers=["env"],
                secret_paths=["**/.env"],
                secret_names=["EXAMPLE_TOKEN"],
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

            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                run_sync(config_root=config_root, home=home)

            codex_rules = (home / ".codex/rules/coding-agents.rules").read_text()
            self.assertIn('pattern=["python"]', codex_rules)
            for present in (
                'prefix_rule(pattern=["shred"], decision="forbidden")',
                'prefix_rule(pattern=["env", "shred"], decision="forbidden")',
                'pattern=["gh", "auth", "status", "-t"], decision="forbidden"',
                'prefix_rule(pattern=["rmdir"], decision="prompt")',
                'prefix_rule(pattern=["env", "rmdir"], decision="prompt")',
            ):
                with self.subTest(present=present):
                    self.assertIn(present, codex_rules)
            for absent in ("git", "rg", "ls", "EXAMPLE_TOKEN", ".env"):
                with self.subTest(absent=absent):
                    self.assertNotIn(f'"{absent}"', codex_rules)
            self.assertIn("fall to its sandbox: deny ls, deny rg:", stderr.getvalue())

    def test_codex_rules_share_alternatives_across_wrappers_and_siblings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                ask=[["git", "push"]],
                deny=[
                    ["aws", "configure", "get"],
                    ["aws", "configure", "export-credentials"],
                    {"command": "aws", "subcommand": ["sso"], "reason": "use SSO"},
                    {
                        "command": "git",
                        "subcommand": ["push"],
                        "tail": ["-f", "--force"],
                    },
                ],
                wrappers=["nohup", "env"],
            )
            run_sync(config_root=config_root, home=home)

            self.assertEqual(
                (home / ".codex/rules/coding-agents.rules").read_text(),
                "# Generated by coding-agents sync\n"
                'prefix_rule(pattern=["aws", "configure", ["export-credentials", '
                '"get"]], decision="forbidden")\n'
                'prefix_rule(pattern=["aws", "sso"], decision="forbidden", '
                'justification="use SSO")\n'
                'prefix_rule(pattern=[["env", "nohup"], "aws", "configure", '
                '["export-credentials", "get"]], decision="forbidden")\n'
                'prefix_rule(pattern=[["env", "nohup"], "aws", "sso"], '
                'decision="forbidden", justification="use SSO")\n'
                'prefix_rule(pattern=[["env", "nohup"], "git", "push"], '
                'decision="prompt")\n'
                'prefix_rule(pattern=[["env", "nohup"], "git", "push", '
                '["--force", "-f"]], decision="forbidden")\n'
                'prefix_rule(pattern=["git", "push"], decision="prompt")\n'
                'prefix_rule(pattern=["git", "push", ["--force", "-f"]], '
                'decision="forbidden")\n',
            )

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
                "schema: coding-agents/v1\n"
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
            bash = opencode_rules(opencode, "shell")
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
            self.assertFalse((home / ".cursor/cli-config.json").exists())
            # The narrowed local-tool rule leaves Desktop's allowlist.
            desktop = json.loads((home / ".cursor/permissions.json").read_text())
            self.assertEqual(desktop["terminalAllowlist"], ["git status"])

    def test_local_command_fragment_conflicting_with_committed_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
            write(
                config_root / "permissions.local/dup.yaml",
                "schema: coding-agents/v1\n"
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
                "schema: coding-agents/v1\n"
                "kind: permission-rules\n"
                "id: empty\n"
                "name: Empty\n",
            )

            run_sync(config_root=config_root, home=home)

            self.assertTrue((home / ".claude/settings.json").exists())

    def test_command_fragment_omissions_track_each_targets_projection_gap(
        self,
    ) -> None:
        # Cursor withholds the allows a deny narrows and Codex projects it, so
        # a deny fragment owes no target an omission.
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(config_root / "permissions/policy.yaml", permission_policy_doc())
            write(
                config_root / "permissions/commands/nonempty.yaml",
                "schema: coding-agents/v1\n"
                "kind: permission-rules\n"
                "id: nonempty\n"
                "name: Nonempty\n"
                "deny: [[git, push]]\n",
            )

            run_sync(config_root=config_root, home=home)

            self.assertTrue((home / ".codex/rules/coding-agents.rules").exists())

    def test_patch_cannot_contribute_command_permissions(self) -> None:
        """Command decisions have one writer, so the patch route is closed."""
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["kubectl", "get"]])
            for patch, error in (
                (
                    "extend:\n"
                    "  /permissions:\n"
                    "  - {action: shell, resource: 'kubectl *', effect: deny}\n",
                    "never with shell rules",
                ),
                # A wildcard action matches `shell` too, and would land last.
                (
                    "extend:\n"
                    "  /permissions:\n"
                    "  - {action: '*', resource: '*', effect: allow}\n",
                    "never with shell rules",
                ),
                (
                    "overlay:\n  /permission/bash:\n    'kubectl get secret*': ask\n",
                    "cannot touch /permission/bash",
                ),
            ):
                with self.subTest(patch=patch):
                    write(
                        config_root / "patches/opencode.yaml",
                        f"schema: coding-agents/patch/v1\n{patch}",
                    )
                    with self.assertRaisesRegex(PatchError, error):
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
            bash = opencode_rules(opencode, "shell")
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
            self.assertFalse((home / ".cursor/cli-config.json").exists())
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

            # The CLI carries no generated shell entries; the grant is the patch's.
            cli = json.loads((home / ".cursor/cli-config.json").read_text())
            self.assertEqual(cli["permissions"]["allow"], ["Mcp(looker-mcp:get_*)"])

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
                            "extend": {
                                "/permissions": [
                                    {
                                        "action": "external_directory",
                                        "resource": "~/.netrc",
                                        "effect": decision,
                                    }
                                ]
                            },
                        },
                        sort_keys=False,
                    ),
                )

                run_sync(config_root=config_root, home=home)

                config = json.loads(
                    (home / ".config/opencode/opencode.json").read_text()
                )
                external = opencode_rules(config, "external_directory")
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
            bash = opencode_rules(opencode, "shell")
            for command, decision in {
                "git push": "allow",
                "git push --force-with-lease": "ask",
                "git push --force": "deny",
            }.items():
                with self.subTest(command=command):
                    self.assertEqual(resolve_opencode_bash(bash, command), decision)

    def test_permission_composition_and_unsupported_intent_fail(self) -> None:
        for secret_paths in (
            ["**/.env", "**/.env"],
            ["path with spaces"],
            # Claude reads `/x` as settings-relative, and bare paths as
            # cwd-relative, so neither means the same thing on every target.
            ["/etc/secret"],
            [".env"],
        ):
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

        # One space may anchor a token boundary; anything else at the edge
        # (a YAML block scalar's newline, a double space) never matches.
        for text in (" -T\n", "  -T", " ", ""):
            with (
                self.subTest(text=text),
                tempfile.TemporaryDirectory() as tmp,
            ):
                config_root, home = config_root_home(tmp)
                with self.assertRaises(SourceSchemaError):
                    write_permissions(
                        config_root,
                        deny=[{"command": "curl", "text": [text]}],
                    )
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
                    extra={
                        "paths": ["**/*.py"],
                        "license": "MIT",
                        "metadata": {"owner": "platform"},
                    },
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
                        "opencode:permissions": [{"action": "shell", "effect": "ask"}],
                        "opencode:model": "opencode-model",
                        "opencode:color": "#06b6d4",
                        "codex:model": "gpt-5",
                        "codex:nickname_candidates": ["Ada"],
                        "codex:personality": "pragmatic",
                        "codex:service_tier": "flex",
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

            # Codex, Cursor, and OpenCode share one copy; Claude keeps its own.
            for directory in (".codex/skills", ".cursor/skills", ".config/opencode"):
                self.assertFalse((home / directory / "skills/s1").exists())
                self.assertFalse((home / directory / "s1").exists())
            claude_skill = (home / ".claude" / "skills" / "s1" / "SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("paths:", claude_skill)
            shared_skill = (home / ".agents/skills/s1/SKILL.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("license: MIT", shared_skill)
            self.assertIn("paths:", shared_skill)

            claude_agent = (home / ".claude" / "agents" / "reviewer.md").read_text(
                encoding="utf-8"
            )
            opencode_agent = (
                home / ".config" / "opencode" / "agents" / "reviewer.md"
            ).read_text(encoding="utf-8")
            self.assertIn("model: claude-override", claude_agent)
            self.assertIn("effort: high", claude_agent)
            self.assertIn("color: blue", claude_agent)
            self.assertEqual(
                (home / ".cursor" / "agents" / "reviewer.md").read_text(),
                "---\nname: reviewer\ndescription: Review code across multiple lines\n"
                "---\n\nAgent body\n",
            )
            cursor_command = (
                home / ".cursor" / "skills" / "review-pr" / "SKILL.md"
            ).read_text()
            _, command_frontmatter, command_body = cursor_command.split("---", 2)
            self.assertEqual(
                yaml.safe_load(command_frontmatter),
                {
                    "name": "review-pr",
                    "description": "Review a pull request",
                    "disable-model-invocation": True,
                },
            )
            self.assertEqual(command_body.strip(), "Review pull request $ARGUMENTS.")
            self.assertIn("mode: subagent", opencode_agent)
            self.assertIn("action: shell", opencode_agent)
            self.assertIn("model: opencode-model", opencode_agent)
            self.assertIn("#06b6d4", opencode_agent)
            self.assertNotIn("model: claude-override", opencode_agent)

            opencode_command = (
                home / ".config" / "opencode" / "commands" / "review-pr.md"
            ).read_text(encoding="utf-8")
            claude_command = (
                home / ".claude" / "skills" / "review-pr" / "SKILL.md"
            ).read_text(encoding="utf-8")
            self.assertIn("description: Review a pull request", opencode_command)
            self.assertIn("agent: reviewer", opencode_command)
            self.assertIn("subagent: true", opencode_command)
            self.assertIn("model: gpt-5", opencode_command)
            self.assertIn("Review pull request $ARGUMENTS.", opencode_command)
            self.assertIn("description: Review a pull request", claude_command)
            self.assertIn("agent: reviewer", claude_command)
            self.assertIn("context: fork", claude_command)
            self.assertIn("model: opus", claude_command)
            self.assertNotIn("gpt-5", claude_command)
            self.assertNotIn("subagent:", claude_command)
            self.assertIn("Review pull request $ARGUMENTS.", claude_command)

            # Codex discovers skills/ and agents/ itself: nothing to register.
            self.assertFalse((home / ".codex" / "config.toml").exists())

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
            self.assertEqual(
                (parsed_codex_agent["personality"], parsed_codex_agent["service_tier"]),
                ("pragmatic", "flex"),
            )

            codex_command = home / ".codex" / "skills" / "review-pr"
            self.assertEqual(
                (codex_command / "SKILL.md").read_text(),
                "---\nname: review-pr\ndescription: Review a pull request\n---\n\n"
                "Review pull request $ARGUMENTS.\n",
            )
            self.assertEqual(
                yaml.safe_load((codex_command / "agents" / "openai.yaml").read_text()),
                {"policy": {"allow_implicit_invocation": False}},
            )
            codex_rules = (home / ".codex" / "rules" / "coding-agents.rules").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                'prefix_rule(pattern=["python"], decision="prompt", '
                'justification="Python scripts need review.")',
                codex_rules,
            )

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

    def test_cursor_preserves_unowned_desktop_rules_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            plugin = home / ".cursor/plugins/local/coding-agents-rules/rules/python.mdc"
            write(plugin, "User plugin body\n")

            run_sync(config_root=config_root, home=home)

            self.assertEqual(plugin.read_text(), "User plugin body\n")

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
                home / ".agents" / "skills" / "s1" / "SKILL.md",
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
                    extra={
                        "opencode": {"request": {"body": {"reasoningEffort": "high"}}}
                    },
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
            shared_skill = (home / ".agents/skills/native/SKILL.md").read_text()
            self.assertIn("disable-model-invocation: true", shared_skill)
            self.assertIn("allowed-tools:", shared_skill)
            self.assertIn("- Bash", shared_skill)
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
                home / ".agents/skills/runner",
            ):
                with self.subTest(root=root):
                    script = root / "scripts/poll.sh"
                    self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o700)
                    notes = root / "references/notes.md"
                    self.assertEqual(stat.S_IMODE(notes.stat().st_mode), 0o600)

    def test_codex_explicit_skill_policy_preserves_bundled_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            skill = config_root / "skills" / "explicit"
            write(
                skill / "SKILL.md",
                source_doc(
                    "skill",
                    "explicit",
                    "explicit",
                    "Body\n",
                    description="Explicit invocation only",
                    extra={"disable_model_invocation": True},
                ),
            )
            write(
                skill / "agents/openai.yaml",
                "interface:\n  display_name: Explicit\n"
                "policy:\n  allow_implicit_invocation: true\n"
                "dependencies:\n  tools: []\n",
            )

            run_sync(config_root=config_root, home=home)

            self.assertEqual(
                yaml.safe_load(
                    (home / ".agents/skills/explicit/agents/openai.yaml").read_text()
                ),
                {
                    "interface": {"display_name": "Explicit"},
                    "policy": {"allow_implicit_invocation": False},
                    "dependencies": {"tools": []},
                },
            )

    def test_codex_explicit_skill_policy_without_bundled_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "skills/explicit/SKILL.md",
                source_doc(
                    "skill",
                    "explicit",
                    "explicit",
                    "Body\n",
                    extra={
                        "disable_model_invocation": True,
                        "codex:openai": {
                            "policy": {"products": ["codex"]},
                            "dependencies": {
                                "tools": [{"value": "docs", "command": "docs-mcp"}]
                            },
                        },
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            self.assertEqual(
                yaml.safe_load(
                    (home / ".agents/skills/explicit/agents/openai.yaml").read_text()
                ),
                {
                    "policy": {
                        "allow_implicit_invocation": False,
                        "products": ["codex"],
                    },
                    "dependencies": {
                        "tools": [
                            {"type": "mcp", "value": "docs", "command": "docs-mcp"}
                        ]
                    },
                },
            )
            self.assertIn(
                "disable-model-invocation: true",
                (home / ".agents/skills/explicit/SKILL.md").read_text(),
            )

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

    def test_bundled_skill_scripts_are_allowed_where_each_target_deploys_them(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            self._runner_skill(config_root)
            write_permissions(config_root, allow=[["git", "status"]])

            run_sync(config_root=config_root, home=home)

            claude = json.loads((home / ".claude/settings.json").read_text())
            claude_allow = "\n".join(claude["permissions"]["allow"])
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            opencode_allow = "\n".join(
                rule["resource"]
                for rule in opencode["permissions"]
                if rule["action"] == "shell" and rule["effect"] == "allow"
            )
            for text, root in (
                (claude_allow, ".claude"),
                (opencode_allow, ".agents"),
            ):
                with self.subTest(root=root):
                    for base in (f"~/{root}", f"{home.as_posix()}/{root}"):
                        self.assertIn(f"{base}/skills/runner/scripts/poll.sh", text)
                    # Only executables, and only this target's own path.
                    self.assertNotIn("notes.md", text)
            self.assertNotIn(".agents/skills", claude_allow)

    def test_deny_reasons_land_beside_the_opencode_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(
                config_root,
                deny=[{"command": "find", "reason": "use fd"}, ["rm"]],
                wrappers=["timeout"],
            )
            run_sync(config_root=config_root, home=home)

            sidecar = home / ".config/opencode/opencode-unwrap.json"
            reasons = json.loads(sidecar.read_text())["reasons"]
            opencode = json.loads((home / ".config/opencode/opencode.json").read_text())
            denies = {
                rule["resource"]
                for rule in opencode["permissions"]
                if rule["action"] == "shell" and rule["effect"] == "deny"
            }
            # Every pattern emitted for the reasoned rule, and only those.
            self.assertEqual(
                set(reasons), {pattern for pattern in denies if "find" in pattern}
            )
            self.assertEqual(set(reasons.values()), {"use fd"})
            codex = (home / ".codex/rules/coding-agents.rules").read_text()
            self.assertIn(
                'prefix_rule(pattern=["find"], decision="forbidden", '
                'justification="use fd")',
                codex,
            )
            self.assertIn('prefix_rule(pattern=["rm"], decision="forbidden")', codex)

            write_permissions(config_root, deny=[["find"]])
            run_sync(config_root=config_root, home=home)
            self.assertFalse(sidecar.exists())

    def test_only_a_deny_carries_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[{"command": "fd", "reason": "no"}])
            with self.assertRaisesRegex(SourceSchemaError, "only a deny"):
                run_sync(config_root=config_root, home=home)

    def _agent_native(self, config_root: Path, target: str, native: object) -> None:
        write(
            config_root / "agents" / "reviewer.md",
            source_doc(
                "agent",
                "reviewer",
                "reviewer",
                "body\n",
                description="Reviewer agent",
                extra={target: native},
            ),
        )

    def test_agent_native_vocabularies_reject_unknown_values(self) -> None:
        cases = (
            ("claude", {"permissionMode": "readOnly"}, "permission_mode must be one"),
            ("claude", {"memory": "session"}, "memory must be one of"),
            ("claude", {"isolation": "sandbox"}, "isolation must be one of"),
            ("claude", {"effort": "highest"}, "effort must be one of"),
            ("opencode", {"mode": "sub-agent"}, "mode must be one of"),
            (
                "opencode",
                {"permissions": [{"action": "edit", "effect": "reject"}]},
                "permissions.0.effect",
            ),
            # The v1 map would send the file through OpenCode's migration.
            ("opencode", {"permission": {"edit": "deny"}}, "permission\n"),
        )
        for target, native, message in cases:
            with (
                self.subTest(target=target, native=native),
                tempfile.TemporaryDirectory() as tmp,
            ):
                config_root, home = config_root_home(tmp)
                self._agent_native(config_root, target, native)

                with self.assertRaisesRegex(ValueError, message):
                    run_sync(config_root=config_root, home=home)

    def test_claude_folds_write_onto_the_edit_pattern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, tools={"edit": "allow", "write": "allow"})

            run_sync(config_root=config_root, home=home)

            allow = json.loads((home / ".claude/settings.json").read_text())[
                "permissions"
            ]["allow"]
            # Write(**) parses but never matches, so it must not be emitted, and
            # the shared Edit(**) pattern must appear once.
            self.assertNotIn("Write(**)", allow)
            self.assertEqual(allow.count("Edit(**)"), 1)

    def test_claude_folds_split_edit_and_write_to_the_stricter_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, tools={"edit": "allow", "write": "deny"})

            run_sync(config_root=config_root, home=home)

            permissions = json.loads((home / ".claude/settings.json").read_text())[
                "permissions"
            ]
            # One shared pattern, resolved to the stricter side rather than
            # landing in two buckets at once.
            self.assertIn("Edit(**)", permissions["deny"])
            self.assertNotIn("Edit(**)", permissions["allow"])
            self.assertNotIn("Write(**)", str(permissions))

    def test_opencode_folds_write_onto_the_edit_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, tools={"edit": "allow", "write": "deny"})

            run_sync(config_root=config_root, home=home)

            config = json.loads((home / ".config/opencode/opencode.json").read_text())
            # OpenCode's write tool asks for `edit`, so a `write` rule would
            # never be consulted; the stricter decision lands on `edit`.
            self.assertEqual(opencode_rules(config, "write"), {})
            self.assertEqual(opencode_rules(config, "edit"), {"*": "deny"})

    def test_portable_color_must_be_an_opencode_color(self) -> None:
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
                    # Valid for Claude, but OpenCode takes #RRGGBB or a theme
                    # name, and nothing overrides it there.
                    extra={"color": "chartreuse"},
                ),
            )

            with self.assertRaisesRegex(ValueError, "is not an OpenCode color"):
                run_sync(config_root=config_root, home=home)

    def test_opencode_takes_a_portable_color_as_hex(self) -> None:
        """OpenCode 2 accepts only hex, so a Claude color name maps to one and
        a native hex override still wins."""
        for override, expected in ((None, "#3b82f6"), ("#ff5733", "#ff5733")):
            with self.subTest(override=override), tempfile.TemporaryDirectory() as tmp:
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
                            "color": "blue",
                            **({"opencode": {"color": override}} if override else {}),
                        },
                    ),
                )

                run_sync(config_root=config_root, home=home)

                agent = (home / ".config/opencode/agents/reviewer.md").read_text()
                self.assertEqual(
                    yaml.safe_load(agent.split("---")[1])["color"], expected
                )
                claude = (home / ".claude/agents/reviewer.md").read_text()
                self.assertIn("color: blue", claude)

    def test_agent_native_vocabularies_accept_documented_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            self._agent_native(
                config_root,
                "claude",
                {
                    "permissionMode": "acceptEdits",
                    "memory": "project",
                    "isolation": "worktree",
                    "mcpServers": ["docs"],
                    "autoCompactWindow": 100000,
                    "omitClaudeMd": True,
                    "observer": "watcher",
                },
            )
            write(
                config_root / "agents" / "second.md",
                source_doc(
                    "agent",
                    "second",
                    "second",
                    "body\n",
                    description="Second agent",
                    extra={
                        "opencode": {
                            "mode": "subagent",
                            "permissions": [
                                {"action": "edit", "effect": "deny"},
                                {
                                    "action": "shell",
                                    "resource": "ls",
                                    "effect": "allow",
                                },
                            ],
                            # Left unvalidated: the request body is the
                            # provider's, and ReasoningEffort is an open set.
                            "request": {
                                "body": {"reasoningEffort": "model-specific-effort"}
                            },
                        },
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            claude = yaml.safe_load(
                (home / ".claude/agents/reviewer.md").read_text().split("---")[1]
            )
            self.assertEqual(
                {
                    key: claude[key]
                    for key in (
                        "permissionMode",
                        "mcpServers",
                        "autoCompactWindow",
                        "omitClaudeMd",
                        "observer",
                    )
                },
                {
                    "permissionMode": "acceptEdits",
                    "mcpServers": ["docs"],
                    "autoCompactWindow": 100000,
                    "omitClaudeMd": True,
                    "observer": "watcher",
                },
            )
            second = (home / ".config/opencode/agents/second.md").read_text()
            self.assertIn("mode: subagent", second)
            self.assertIn("reasoningEffort: model-specific-effort", second)

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
                        "effort": "high",
                        "opencode:mode": "subagent",
                        "opencode:request": {"body": {"temperature": 0.1}},
                        "opencode:steps": 5,
                        "opencode:hidden": True,
                        "opencode:disabled": False,
                        "opencode:permissions": [
                            {"action": "shell", "effect": "ask"},
                            {
                                "action": "shell",
                                "resource": "git diff*",
                                "effect": "allow",
                            },
                            {"action": "subagent", "effect": "deny"},
                        ],
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            opencode_agent = (
                home / ".config" / "opencode" / "agents" / "reviewer.md"
            ).read_text(encoding="utf-8")
            meta = yaml.safe_load(opencode_agent.split("---")[1])
            # Only native ConfigAgent.Info keys, so OpenCode skips its v1
            # migration; the id comes from the file name.
            self.assertEqual(
                set(meta),
                {
                    "description",
                    "mode",
                    "request",
                    "steps",
                    "hidden",
                    "disabled",
                    "permissions",
                },
            )
            self.assertEqual(
                meta["request"],
                {"body": {"reasoningEffort": "high", "temperature": 0.1}},
            )
            self.assertEqual(
                meta["permissions"][1],
                {"action": "shell", "resource": "git diff*", "effect": "allow"},
            )
            self.assertEqual(
                meta["permissions"][2],
                {"action": "subagent", "resource": "*", "effect": "deny"},
            )

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
                        "claude:allowed-tools": ["Read"],
                    },
                ),
            )

            run_sync(config_root=config_root, home=home)

            opencode_skill = (
                home / ".agents" / "skills" / "s1" / "SKILL.md"
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
                        "opencode:subagent": True,
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
            self.assertIn("subagent: true", opencode_command)
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
                    "Rule A\n",
                ),
            )
            write(
                config_root / "rules" / "b.md",
                source_doc("rule", "b", "b", "Rule B\n"),
            )
            write_permissions(config_root, allow=[["git", "status"]])

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
            # Rules land as files that one owned glob loads; the hand-written
            # entry stays ahead of it.
            self.assertEqual(
                config["instructions"], ["old-rule", "~/.config/opencode/rules/*.md"]
            )
            self.assertEqual(
                (home / ".config/opencode/rules/a.md").read_text(), "Rule A\n"
            )
            self.assertEqual(
                (home / ".config/opencode/rules/b.md").read_text(), "Rule B\n"
            )
            self.assertFalse((home / ".config/opencode/AGENTS.md").exists())

    def test_invalid_opencode_json_fails_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
            broken = home / ".config" / "opencode" / "opencode.json"
            write(broken, "{ broken json")

            with self.assertRaises(NativeConfigError):
                run_sync(config_root=config_root, home=home)
            self.assertEqual(broken.read_text(), "{ broken json")

    def test_opencode_sync_preserves_static_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
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
            self.assertEqual(config["instructions"], ["stale-rule"])
            self.assertEqual(config["permissions"][1]["resource"], "git status *")

    def test_opencode_sync_does_not_bake_local_config_into_config_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write_permissions(config_root, allow=[["git", "status"]])
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
            self.assertIn("permissions", config)

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
            self.assertEqual(cfg, 'model = "gpt-5.4"\napproval_policy = "on-request"\n')

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
                        "claude:argument-hint": "[path]",
                        "claude:user-invocable": False,
                        "claude:allowed-tools": ["Read", "Grep"],
                        "claude:model": "sonnet",
                        "claude:effort": "high",
                        "claude:context": "fork",
                        "claude:agent": "Explore",
                        "claude:disallowed-tools": ["Bash"],
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
            with self.assertRaisesRegex(ValueError, "Input should be .claude."):
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
            self.assertTrue(
                (home / ".claude" / "skills" / "claude-only" / "SKILL.md").exists()
            )
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

    def test_hand_written_host_file_is_refused_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_root, home = config_root_home(tmp)
            write(
                config_root / "global" / "AGENTS.md",
                source_doc("global", "global", "global", "Shared global\n"),
            )
            write(home / ".claude" / "CLAUDE.md", "My own notes\n")

            with self.assertRaisesRegex(
                ManagedEntryConflict, "claim unmanifested generated file"
            ):
                run_sync(config_root=config_root, home=home)

            self.assertEqual(
                (home / ".claude" / "CLAUDE.md").read_text(), "My own notes\n"
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
                "schema: coding-agents/v1\n"
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
                "schema: coding-agents/v1\n"
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

            claude_command = (
                home / ".claude" / "skills" / "one" / "SKILL.md"
            ).read_text(encoding="utf-8")
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
            claude_skills_dir = home / ".claude" / "skills"
            self.assertTrue((commands_dir / "one.md").exists())
            self.assertTrue((claude_skills_dir / "one" / "SKILL.md").exists())

            shutil.rmtree(config_root / "commands")
            run_sync(config_root=config_root, home=home)

            self.assertFalse((commands_dir / "one.md").exists())
            self.assertFalse((claude_skills_dir / "one").exists())
            manifest = json.loads(
                (commands_dir / ".coding-agents-managed.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["entries"], {})
            claude_manifest = json.loads(
                (claude_skills_dir / ".coding-agents-managed.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(claude_manifest["entries"], {})

    def test_a_command_named_like_a_skill_is_rejected(self) -> None:
        """Claude, Codex, and Cursor serve commands as skills, so both would
        answer one /one."""
        for target in ("claude", "codex", "cursor"):
            with self.subTest(target), tempfile.TemporaryDirectory() as tmp:
                config_root, home = config_root_home(tmp)
                write(
                    config_root / "skills" / "one" / "SKILL.md",
                    source_doc("skill", "one", "one", "body\n", description="skill"),
                )
                write(
                    config_root / "commands" / "one.md",
                    source_doc(
                        "command",
                        "one-command",
                        "one",
                        "body\n",
                        extra={"only": [target]},
                    ),
                )

                with self.assertRaisesRegex(ValueError, "collides with the skill"):
                    run_sync(config_root=config_root, home=home)

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
