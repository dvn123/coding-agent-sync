import contextlib
import io
import json
import shutil
import stat
import tempfile
import tomllib
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml
from capabilities.audit import OpenCodeRules
from pydantic import ValidationError

from coding_agents_sync import run_sync
from coding_agents_sync.io import MANAGED_MANIFEST, ManagedEntryConflict
from coding_agents_sync.ir import applies_to
from coding_agents_sync.limits import required_omissions
from coding_agents_sync.output.native import NativeConfigError
from coding_agents_sync.output.patches import PatchError
from coding_agents_sync.sources import (
    AgentSource,
    CommandPermissions,
    CommandSource,
    PermissionPolicyDocument,
    PermissionSource,
    SourceSchemaError,
    describe_rule,
    load_permissions,
)
from coding_agents_sync.targets.opencode import UNWRAP_PLUGIN
from coding_agents_sync.targets.permissions import CLAUDE_OPAQUE_COMMANDS


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        write(root / relative, content)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def claude_settings(home: Path) -> Any:
    return load_json(home / ".claude/settings.json")


def opencode_config(home: Path) -> Any:
    return load_json(home / ".config/opencode/opencode.json")


def frontmatter(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])


def existing(home: Path, paths: Iterable[str]) -> list[str]:
    return [path for path in paths if (home / path).exists()]


TARGET_NAMES = ("claude", "cursor", "codex", "opencode")


def owed_omits(source: Any, blocks: dict[str, dict[str, Any]]) -> None:
    """Acknowledge in `blocks` every loss `limits` says `source` owes."""
    for target in TARGET_NAMES:
        if applies_to(source, target) and (owed := required_omissions(source, target)):
            blocks.setdefault(target, {})["omit"] = sorted(owed)


def source_doc(
    kind: str,
    body: str,
    *,
    description: str = "",
    extra: dict[str, Any] | None = None,
) -> str:
    meta: dict[str, Any] = {"description": description}
    target_blocks: dict[str, dict[str, Any]] = {}
    if extra:
        for key, value in extra.items():
            if ":" in key and (target := key.partition(":")[0]) in TARGET_NAMES:
                target_blocks.setdefault(target, {}).setdefault("native", {})[
                    key.partition(":")[2]
                ] = value
            elif key in TARGET_NAMES and isinstance(value, dict):
                target_blocks.setdefault(key, {}).setdefault("native", {}).update(value)
            else:
                meta[key] = value
    if model := {"agent": AgentSource, "command": CommandSource}.get(kind):
        with contextlib.suppress(ValidationError):
            owed_omits(
                model.model_validate({**meta, "path": Path("source.md"), "body": ""}),
                target_blocks,
            )
    if kind == "rule" and isinstance(activation := meta.pop("activation", None), dict):
        native = target_blocks.setdefault("cursor", {}).setdefault("native", {})
        native["alwaysApply"] = activation.get("always") is not False
        if activation.get("globs"):
            native["globs"] = activation["globs"]
    if target_blocks:
        meta["targets"] = target_blocks
    yaml_text = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{yaml_text}\n---\n{body}"


def source_file(
    path: str, kind: str, extra: dict[str, Any] | None = None
) -> dict[str, str]:
    return {path: source_doc(kind, "body\n", description="Test source", extra=extra)}


def agent_file(extra: dict[str, Any]) -> dict[str, str]:
    return source_file("agents/reviewer.md", "agent", extra)


def claude_agent(**native: Any) -> dict[str, str]:
    return agent_file({"claude": native})


def opencode_agent(**native: Any) -> dict[str, str]:
    return agent_file({"opencode": native})


type Rules = list[list[str] | dict[str, Any]]


def permission_policy_doc(
    *,
    secret_paths: list[str] | None = None,
    secret_names: list[str] | None = None,
    wrappers: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    source: dict[str, Any] = {
        "description": "permissions",
        "wrappers": wrappers or [],
        "secret_paths": secret_paths or [],
        "secret_names": secret_names or [],
        **(extra or {}),
    }
    with contextlib.suppress(ValidationError):
        policy = PermissionPolicyDocument.model_validate(source)
        targets: dict[str, dict[str, Any]] = {}
        owed_omits(
            PermissionSource(
                paths=(),
                commands=CommandPermissions(),
                **policy.model_dump(exclude={"targets"}),
            ),
            targets,
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
    extra: dict[str, Any] | None = None,
) -> str:
    source: dict[str, Any] = {
        "options": options or {},
        "allow": allow or [],
        "ask": ask or [],
        "deny": deny or [],
    }
    if extra:
        source.update(extra)
    return yaml.safe_dump(source, sort_keys=False)


def permission_files(
    *,
    allow: Rules | None = None,
    ask: Rules | None = None,
    deny: Rules | None = None,
    options: dict[str, list[str]] | None = None,
    assignments: dict[str, list[str]] | None = None,
    wrappers: list[str] | None = None,
    secret_paths: list[str] | None = None,
    secret_names: list[str] | None = None,
    tools: dict[str, str] | None = None,
    workspace: dict[str, Any] | None = None,
    unmatched: str | None = None,
    targets: dict[str, Any] | None = None,
) -> dict[str, str]:
    policy_extra: dict[str, Any] = {}
    if targets is not None:
        policy_extra["targets"] = targets
    if tools is not None:
        policy_extra["tools"] = tools
    if workspace is not None:
        policy_extra["workspace"] = workspace
    if unmatched is not None:
        policy_extra["unmatched"] = unmatched
    return {
        "permissions/policy.yaml": permission_policy_doc(
            secret_paths=secret_paths,
            secret_names=secret_names,
            wrappers=wrappers,
            extra=policy_extra or None,
        ),
        "permissions/commands/test.yaml": permission_rules_doc(
            allow=allow,
            ask=ask,
            deny=deny,
            options=options,
            extra={"assignments": assignments} if assignments else None,
        ),
    }


def write_permissions(config_root: Path, **rules: Any) -> None:
    write_files(config_root, permission_files(**rules))


def model_policy_doc(
    *, default: str = "sweet-spot", sweet_model: str = "claude-sonnet-5"
) -> str:
    return yaml.safe_dump(
        {
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


def opencode_decisions(
    patterns: dict[str, str], commands: Iterable[str]
) -> dict[str, str]:
    return {command: resolve_opencode_bash(patterns, command) for command in commands}


def cursor_desktop(home: Path) -> Any:
    path = home / ".cursor/permissions.json"
    return load_json(path) if path.exists() else None


def desktop_allowlist(*entries: str) -> dict[str, Any]:
    return {"approvalMode": "allowlist", "terminalAllowlist": list(entries)}


def codex_sandbox_warning(rules: str) -> str:
    return (
        "Warning: Codex prefix rules cannot hold a text predicate or an exact "
        f"match, so these fall to its sandbox: {rules}: "
        "<config>/permissions/policy.yaml"
    )


def overlap_warning(rule: str) -> str:
    return (
        f"Warning: guarded overlap {rule}: the guard shares an allow's subcommand "
        "prefix but does not provably narrow it"
    )


def runner_skill(config_root: Path) -> None:
    write(
        config_root / "skills" / "runner" / "SKILL.md",
        source_doc("skill", "Skill body\n", description="runner"),
    )
    script = config_root / "skills" / "runner" / "scripts" / "poll.sh"
    write(script, "#!/usr/bin/env bash\necho hi\n")
    script.chmod(0o755)
    write(config_root / "skills" / "runner" / "references" / "notes.md", "notes\n")


class SyncCase(unittest.TestCase):
    def new_roots(self) -> tuple[Path, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        return root / "coding-agents", root / "home"


class RejectedSourceTests(SyncCase):
    def test_invalid_agent_sources_are_rejected(self) -> None:
        def owned(field: str, value: str) -> dict[str, str]:
            files = agent_file({"model_policy": "sweet-spot", field: value})
            return {"model-policy.yaml": model_policy_doc(), **files}

        rows = {
            SourceSchemaError: (
                (
                    "unknown model profile",
                    {
                        "model-policy.yaml": model_policy_doc(),
                        **agent_file({"model_policy": "missing"}),
                    },
                    "unknown model policy",
                ),
                (
                    "portable tool allowlist",
                    agent_file({"tools": {"allow": ["Read", "Grep"]}}),
                    "tools",
                ),
                (
                    "portable tools beside a native field",
                    agent_file({"tools": {"inherit": True}, "cursor:readonly": True}),
                    "tools",
                ),
                (
                    "portable tools inherit false",
                    agent_file({"tools": {"inherit": False}}),
                    "tools",
                ),
            ),
            ValueError: (
                ("claude model", owned("claude:model", "opus"), "owns Claude fields"),
                (
                    "opencode model",
                    owned("opencode:model", "x/y"),
                    "owns OpenCode fields",
                ),
                ("cursor model", owned("cursor:model", "x/y"), "owns Cursor fields"),
                (
                    "claude permissionMode",
                    claude_agent(permissionMode="readOnly"),
                    "permission_mode must be one",
                ),
                (
                    "claude memory",
                    claude_agent(memory="session"),
                    "memory must be one of",
                ),
                (
                    "claude isolation",
                    claude_agent(isolation="sandbox"),
                    "isolation must be one of",
                ),
                (
                    "claude effort",
                    claude_agent(effort="highest"),
                    "effort must be one of",
                ),
                (
                    "opencode mode",
                    opencode_agent(mode="sub-agent"),
                    "mode must be one of",
                ),
                (
                    "opencode permission effect",
                    opencode_agent(permissions=[{"action": "edit", "effect": "no"}]),
                    "permissions.0.effect",
                ),
                (
                    # The v1 map would send the file through OpenCode's migration.
                    "opencode v1 permission map",
                    opencode_agent(permission={"edit": "deny"}),
                    "permission\n",
                ),
                (
                    "claude tools inherit",
                    claude_agent(tools="inherit", disallowedTools=["Edit", "Write"]),
                    "does not accept `inherit`",
                ),
                *(
                    (
                        f"claude tool entry {entry}",
                        claude_agent(tools=[entry]),
                        "is not a tool name",
                    )
                    for entry in ("read", "web_fetch", "mcp__*")
                ),
                (
                    "claude tools all disallowed",
                    claude_agent(tools=["Read", "Edit"], disallowedTools="Read, Edit"),
                    "zero tools",
                ),
                (
                    # Valid for Claude, but OpenCode takes #RRGGBB or a theme name.
                    "color outside OpenCode's palette",
                    agent_file({"color": "chartreuse"}),
                    "is not an OpenCode color",
                ),
            ),
        }
        for error, cases in rows.items():
            for name, files, pattern in cases:
                with self.subTest(name):
                    config_root, home = self.new_roots()
                    write_files(config_root, files)

                    with self.assertRaisesRegex(error, pattern):
                        run_sync(config_root=config_root, home=home)

    def test_invalid_permission_sources_are_rejected(self) -> None:
        git_status = permission_files(allow=[["git", "status"]])
        rows = {
            SourceSchemaError: (
                (
                    "repeated secret path",
                    permission_files(secret_paths=["**/.env", "**/.env"]),
                    "entries must be unique",
                ),
                (
                    "secret path with spaces",
                    permission_files(secret_paths=["path with spaces"]),
                    "secret_paths.0",
                ),
                (
                    # Claude reads `/x` as settings-relative and bare paths as
                    # cwd-relative, so neither means one thing on every target.
                    "absolute secret path",
                    permission_files(secret_paths=["/etc/secret"]),
                    "secret_paths.0",
                ),
                (
                    "bare secret path",
                    permission_files(secret_paths=[".env"]),
                    "secret_paths.0",
                ),
                *(
                    (
                        # One space may anchor a token boundary; anything else
                        # at the edge (a YAML block scalar's newline, a double
                        # space) never matches.
                        f"text predicate {text!r}",
                        permission_files(deny=[{"prefix": ["curl"], "text": [text]}]),
                        r"deny\.0\.text\.0",
                    )
                    for text in (" -T\n", "  -T", " ", "")
                ),
                (
                    "the same rule in two fragments",
                    {
                        **git_status,
                        "permissions/commands/other.yaml": permission_rules_doc(
                            allow=[["git", "status"]]
                        ),
                    },
                    "duplicate permission rule",
                ),
                (
                    "local fragment repeating a committed rule",
                    {
                        **git_status,
                        "permissions.local/dup.yaml": "ask:\n- [git, status]\n",
                    },
                    "duplicate permission rule",
                ),
                (
                    # A command's option vocabulary has one home, so a second
                    # declaration is an error rather than a silent union.
                    "option vocabulary declared twice",
                    {
                        **permission_files(options={"git": ["-C"]}),
                        "permissions/commands/other.yaml": permission_rules_doc(
                            options={"git": ["-c"]}
                        ),
                    },
                    "a command's option vocabulary has one home",
                ),
                (
                    "retired defaults block",
                    {
                        "permissions/policy.yaml": permission_policy_doc(
                            extra={"defaults": {"unmatched": "ask"}}
                        ),
                    },
                    "Extra inputs are not permitted",
                ),
                (
                    "reason on an allow",
                    permission_files(allow=[{"prefix": ["fd"], "reason": "no"}]),
                    "only a deny",
                ),
                *(
                    (
                        f"assignment {assignments}",
                        permission_files(
                            allow=[["tool", "status"]], assignments=assignments
                        ),
                        pattern,
                    )
                    for assignments, pattern in (
                        ({"tool": ["EDITOR=*"]}, "String should match pattern"),
                        ({"tool": ["EDITOR=a b"]}, "String should match pattern"),
                        ({"other": ["EDITOR=true"]}, "assignments declared for other"),
                    )
                ),
                (
                    "rule with both prefix and exact",
                    {
                        "permissions/policy.yaml": "",
                        "permissions/commands/rules.yaml": (
                            "allow:\n- {prefix: git, exact: git status}\n"
                        ),
                    },
                    "one of prefix or exact",
                ),
            ),
            ValueError: (
                (
                    "unmatched allow without its omissions",
                    {"permissions/policy.yaml": "unmatched: allow\n"},
                    "unmatched is unsupported on (claude|codex|cursor)",
                ),
                (
                    "cursor web fetch allow",
                    {"permissions/policy.yaml": "tools: {webfetch: allow}\n"},
                    "tools is unsupported on cursor",
                ),
                (
                    "cursor web search allow",
                    {"permissions/policy.yaml": "tools: {websearch: allow}\n"},
                    "tools is unsupported on cursor",
                ),
            ),
        }
        for error, cases in rows.items():
            for name, files, pattern in cases:
                with self.subTest(name):
                    config_root, home = self.new_roots()
                    write_files(config_root, files)

                    with self.assertRaisesRegex(error, pattern):
                        run_sync(config_root=config_root, home=home)

    def test_invalid_rule_skill_command_and_mcp_sources_are_rejected(self) -> None:
        skill = source_file("skills/one/SKILL.md", "skill")
        rows = {
            SourceSchemaError: (
                (
                    "reserved generated rule filename",
                    source_file("rules/coding-agents-global.md", "rule"),
                    "reserved rule filename",
                ),
                (
                    "duplicate native key",
                    {
                        "rules/duplicate.md": "---\ntargets:\n  claude:\n    native:\n"
                        "      model: first\n      model: second\n---\nRule body\n"
                    },
                    "duplicate YAML key `model`",
                ),
                (
                    "mcp server with two transports",
                    {"mcp/both.yaml": "command: x\nurl: https://example.com/mcp\n"},
                    "exactly one of command",
                ),
            ),
            ValueError: (
                (
                    "only naming an unknown target",
                    source_file("rules/s.md", "rule", {"only": ["cursor", "nope"]}),
                    "Input should be .claude.",
                ),
                (
                    "native block for a target outside only",
                    source_file(
                        "rules/s.md", "rule", {"only": ["cursor"], "claude": {"x": 1}}
                    ),
                    "targets outside only",
                ),
                (
                    "outdated frontmatter in two sources",
                    {
                        "rules/old.md": "---\nkind: rule\n---\nbody\n",
                        "skills/old/SKILL.md": "---\nname: renamed\n---\nbody\n",
                    },
                    r"(?s)invalid coding-agent source.*rules/old\.md"
                    r".*skills/old/SKILL\.md",
                ),
                (
                    "codex rule with unknown fields",
                    source_file(
                        "rules/git.md",
                        "rule",
                        {
                            "codex:rules": [
                                {
                                    "pattern": ["git", "push"],
                                    "decision": "forbidden",
                                    "match": ["git push --force"],
                                }
                            ]
                        },
                    ),
                    "invalid codex native fields",
                ),
                (
                    "codex rule with an empty pattern",
                    source_file(
                        "rules/e.md",
                        "rule",
                        {"codex:rules": [{"pattern": [], "decision": "prompt"}]},
                    ),
                    "at least 1 item",
                ),
                (
                    "codex rule with an unknown decision",
                    source_file(
                        "rules/m.md",
                        "rule",
                        {"codex:rules": [{"pattern": ["x"], "decision": "maybe"}]},
                    ),
                    "Input should be 'allow', 'prompt' or 'forbidden'",
                ),
                (
                    "codex skill with unknown frontmatter",
                    source_file(
                        "skills/s1/SKILL.md", "skill", {"codex:unknown-native": "kept"}
                    ),
                    "invalid codex native fields",
                ),
                (
                    "skill whose readers need different descriptions",
                    source_file(
                        "skills/split/SKILL.md",
                        "skill",
                        {"cursor:description": "Cursor wording"},
                    ),
                    "Codex and Cursor need different `description`",
                ),
                (
                    "unknown opencode command field",
                    source_file(
                        "commands/one.md", "command", {"opencode:unknown": "x"}
                    ),
                    "invalid opencode native fields",
                ),
                *(
                    (
                        # Claude, Codex, and Cursor serve commands as skills, so
                        # both would answer one /one.
                        f"command named like a skill on {target}",
                        {
                            **skill,
                            **source_file(
                                "commands/one.md", "command", {"only": [target]}
                            ),
                        },
                        "collides with the skill",
                    )
                    for target in ("claude", "codex", "cursor")
                ),
                (
                    "mcp tool decisions without a cursor omit",
                    {
                        "mcp/browser.yaml": "command: browser-mcp\n"
                        "tools: {click: ask, run_code: deny, snapshot: allow}\n"
                    },
                    r"tools is unsupported on cursor: .* targets\.cursor",
                ),
                *(
                    (
                        f"{patch} patch overwriting a generated mcp command",
                        {
                            "mcp/browser.yaml": "command: browser-mcp\n",
                            f"patches/{patch}.yaml": f"set:\n  {pointer}: other-mcp\n",
                        },
                        "Overlapping native pointers",
                    )
                    for patch, pointer in (
                        ("claude-mcp", "/mcpServers/browser/command"),
                        ("codex", "/mcp_servers/browser/command"),
                    )
                ),
            ),
        }
        for error, cases in rows.items():
            for name, files, pattern in cases:
                with self.subTest(name):
                    config_root, home = self.new_roots()
                    write_files(config_root, files)

                    with self.assertRaisesRegex(error, pattern):
                        run_sync(config_root=config_root, home=home)

    def test_patch_cannot_contribute_command_permissions(self) -> None:
        """Command decisions have one writer, so the patch route is closed."""
        config_root, home = self.new_roots()
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
                    f"{patch}",
                )
                with self.assertRaisesRegex(PatchError, error):
                    run_sync(config_root=config_root, home=home)
                self.assertFalse((home / ".config/opencode/opencode.json").exists())

    def test_unusable_native_files_are_refused_and_left_untouched(self) -> None:
        rows = (
            (
                "invalid opencode json",
                ".config/opencode/opencode.json",
                "{ broken json",
                "invalid native config",
            ),
            (
                "cursor approval mode already set",
                ".cursor/permissions.json",
                json.dumps({"approvalMode": "unrestricted"}),
                "existing native value differs",
            ),
        )
        for name, path, content, pattern in rows:
            with self.subTest(name):
                config_root, home = self.new_roots()
                write_permissions(config_root, allow=[["git", "status"]])
                write(home / path, content)

                with self.assertRaisesRegex(NativeConfigError, pattern):
                    run_sync(config_root=config_root, home=home)

                self.assertEqual((home / path).read_text(), content)

    def test_raw_manifest_input_fails_before_portable_or_native_writes(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "global" / "AGENTS.md",
            source_doc("global", "Global body\n"),
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

    def test_generated_paths_holding_unmanifested_content_are_refused(self) -> None:
        rows = (
            (
                "global file",
                source_file("global/AGENTS.md", "global"),
                ".claude/CLAUDE.md",
                "My own notes\n",
            ),
            (
                "cursor rule",
                source_file("rules/python.md", "rule"),
                ".cursor/rules/python.mdc",
                "User rule body\n",
            ),
        )
        for name, files, path, content in rows:
            with self.subTest(name):
                config_root, home = self.new_roots()
                write_files(config_root, files)
                write(home / path, content)

                with self.assertRaisesRegex(
                    ManagedEntryConflict,
                    "Refusing to claim unmanifested generated file",
                ):
                    run_sync(config_root=config_root, home=home)

                self.assertEqual((home / path).read_text(), content)

    def test_a_modified_generated_file_is_not_replaced(self) -> None:
        config_root, home = self.new_roots()
        write_files(config_root, source_file("rules/python.md", "rule"))
        run_sync(config_root=config_root, home=home)
        rule = home / ".cursor/rules/python.mdc"
        write(rule, "Modified generated body\n")

        with self.assertRaisesRegex(
            ManagedEntryConflict, "Refusing to replace modified generated file"
        ):
            run_sync(config_root=config_root, home=home)

    def test_a_modified_generated_entry_is_not_pruned(self) -> None:
        rows = (
            (
                "file",
                source_file("rules/python.md", "rule"),
                ".claude/rules/python.md",
                "rules",
                "Refusing to replace modified generated file",
            ),
            (
                "tree",
                source_file("skills/tool/SKILL.md", "skill"),
                ".claude/skills/tool/SKILL.md",
                "skills",
                "Refusing to replace modified generated dir",
            ),
        )
        for name, files, generated, source_dir, message in rows:
            with self.subTest(name):
                config_root, home = self.new_roots()
                write_files(config_root, files)
                run_sync(config_root=config_root, home=home)
                write(home / generated, "modified\n")
                shutil.rmtree(config_root / source_dir)

                with self.assertRaisesRegex(ManagedEntryConflict, message):
                    run_sync(config_root=config_root, home=home)


class ModelPolicyTests(SyncCase):
    def test_agent_deny_lowers_to_each_targets_tool_gates(self) -> None:
        """Claude and OpenCode take every entry; Cursor takes `edit` as
        readonly and Codex `shell` as a disabled tool, owing omits for the
        rest."""
        config_root, home = self.new_roots()
        write(
            config_root / "agents" / "reviewer.md",
            source_doc(
                "agent",
                "Review body\n",
                extra={
                    "deny": ["shell", "edit", "delegate"],
                    "claude:disallowedTools": ["WebFetch", "Bash"],
                    "opencode:permissions": [{"action": "webfetch", "effect": "deny"}],
                },
            ),
        )

        run_sync(config_root=config_root, home=home)

        self.assertEqual(
            frontmatter(home / ".claude/agents/reviewer.md")["disallowedTools"],
            ["Bash", "Edit", "Write", "NotebookEdit", "Agent", "WebFetch"],
        )
        self.assertEqual(
            frontmatter(home / ".config/opencode/agents/reviewer.md")["permissions"],
            [
                {"action": action, "resource": "*", "effect": "deny"}
                for action in ("shell", "edit", "subagent", "webfetch")
            ],
        )
        self.assertIs(
            frontmatter(home / ".cursor/agents/reviewer.md")["readonly"], True
        )
        codex = tomllib.loads((home / ".codex/agents/reviewer.toml").read_text())
        self.assertEqual(codex["features"], {"shell_tool": False})

    def test_model_policy_lowers_named_agents_without_touching_codex(self) -> None:
        """Native tool policy beside a model policy stays as written."""
        config_root, home = self.new_roots()
        write(config_root / "model-policy.yaml", model_policy_doc())
        write(
            config_root / "agents" / "cheap.md",
            source_doc(
                "agent",
                "Cheap body\n",
                extra={
                    "claude": {"disallowedTools": ["Edit", "Write", "Agent"]},
                    "opencode": {
                        "mode": "subagent",
                        "permissions": [
                            {"action": "edit", "effect": "deny"},
                            {"action": "subagent", "effect": "deny"},
                        ],
                    },
                    "codex:model": "gpt-5",
                    "codex:model_reasoning_effort": "high",
                },
            ),
        )
        write(
            config_root / "agents" / "open.md",
            source_doc(
                "agent",
                "Open body\n",
                extra={"model_policy": "inherit", "opencode:mode": "subagent"},
            ),
        )

        run_sync(config_root=config_root, home=home)

        claude_cheap = frontmatter(home / ".claude/agents/cheap.md")
        claude_open = frontmatter(home / ".claude/agents/open.md")
        opencode_cheap = frontmatter(home / ".config/opencode/agents/cheap.md")
        opencode_open = frontmatter(home / ".config/opencode/agents/open.md")
        codex_cheap = tomllib.loads((home / ".codex/agents/cheap.toml").read_text())
        self.assertEqual(
            (claude_cheap["model"], claude_cheap["effort"]),
            ("claude-sonnet-5", "xhigh"),
        )
        self.assertEqual(claude_cheap["disallowedTools"], ["Edit", "Write", "Agent"])
        self.assertEqual(claude_open["model"], "inherit")
        self.assertNotIn("effort", claude_open)
        self.assertEqual(
            opencode_cheap["model"], "amazon-bedrock/global.openai.gpt-5.6-luna"
        )
        self.assertEqual(
            opencode_cheap["request"], {"body": {"reasoningEffort": "xhigh"}}
        )
        self.assertEqual(
            [rule["action"] for rule in opencode_cheap["permissions"]],
            ["edit", "subagent"],
        )
        self.assertNotIn("model", opencode_open)
        self.assertNotIn("request", opencode_open)
        self.assertEqual(
            (codex_cheap["model"], codex_cheap["model_reasoning_effort"]),
            ("gpt-5", "high"),
        )
        self.assertEqual(
            (home / ".cursor/agents/cheap.md").read_text(),
            "---\nname: cheap\nmodel: gpt-5.6-luna\n---\n\nCheap body\n",
        )
        self.assertNotIn("model:", (home / ".cursor/agents/open.md").read_text())

    def test_cursor_agent_lowers_to_desktop_line_frontmatter(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "agents" / "scout.md",
            source_doc(
                "agent",
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


class NativeOwnershipTests(SyncCase):
    """A generated list owns its own entries; tool-added entries survive."""

    def test_tool_added_entry_survives_and_only_generated_entries_are_pruned(
        self,
    ) -> None:
        config_root, home = self.new_roots()
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

    def test_opencode_json_keeps_unowned_content_and_prunes_only_what_it_owns(
        self,
    ) -> None:
        config_root, home = self.new_roots()
        write(config_root / "rules" / "a.md", source_doc("rule", "Rule A\n"))
        write(config_root / "rules" / "b.md", source_doc("rule", "Rule B\n"))
        write_permissions(config_root, allow=[["git", "status"]])
        patch = config_root / "patches" / "opencode.yaml"
        write(patch, "extend:\n  /instructions: [extra/*.md]\n")
        config_path = home / ".config" / "opencode" / "opencode.json"
        unowned = {
            "$schema": "https://opencode.ai/config.json",
            "share": "manual",
            "permission": {"bash": "deny", "read": "allow"},
            "plugin": ["opencode-scheduler"],
        }
        write(config_path, json.dumps({**unowned, "instructions": ["old-rule"]}))

        run_sync(config_root=config_root, home=home)

        generated = load_json(config_path)
        self.assertEqual(generated.pop("permissions")[1]["resource"], "git status *")
        self.assertEqual(
            generated,
            {
                **unowned,
                "instructions": [
                    "old-rule",
                    "~/.config/opencode/rules/*.md",
                    "extra/*.md",
                ],
            },
        )
        self.assertEqual(
            [(home / f".config/opencode/rules/{name}.md").read_text() for name in "ab"],
            ["Rule A\n", "Rule B\n"],
        )
        self.assertFalse((home / ".config/opencode/AGENTS.md").exists())

        patch.unlink()
        run_sync(config_root=config_root, home=home)

        self.assertEqual(
            load_json(config_path)["instructions"],
            ["old-rule", "~/.config/opencode/rules/*.md"],
        )

    def test_caches_a_skill_leaves_behind_are_neither_drift_nor_a_conflict(
        self,
    ) -> None:
        config_root, home = self.new_roots()
        skill = config_root / "skills" / "tool" / "SKILL.md"
        write(skill, source_doc("skill", "body\n"))
        write(skill.parent / "run.py", "print('hi')\n")
        run_sync(config_root=config_root, home=home)
        deployed = home / ".claude" / "skills" / "tool"
        write(deployed / "__pycache__" / "run.cpython-314.pyc", "bytecode")
        write(deployed / ".DS_Store", "finder")

        self.assertEqual(run_sync(config_root=config_root, home=home, check=True), ())
        run_sync(config_root=config_root, home=home)
        self.assertTrue((deployed / "__pycache__" / "run.cpython-314.pyc").exists())

        skill.unlink()
        (skill.parent / "run.py").unlink()
        run_sync(config_root=config_root, home=home)
        self.assertFalse(deployed.exists())

    def test_a_removed_source_prunes_what_it_generated_and_nothing_else(self) -> None:
        config_root, home = self.new_roots()
        manual_plugin = (
            home / ".cursor/plugins/local/manual-plugin/.cursor-plugin/plugin.json"
        )
        write(manual_plugin, '{"name": "manual-plugin"}\n')
        write(home / ".claude/rules/manual.md", "manual\n")
        write(
            config_root / "rules" / "x.md",
            source_doc(
                "rule",
                "body\n",
                extra={"codex:rules": [{"pattern": ["x"], "decision": "prompt"}]},
            ),
        )
        write(
            config_root / "skills" / "s1" / "SKILL.md",
            source_doc("skill", "body\n", description="skill"),
        )
        write(
            config_root / "agents" / "a.md",
            source_doc("agent", "body\n", description="Agent A"),
        )
        write(
            config_root / "commands" / "c.md",
            source_doc("command", "body\n", description="Command C"),
        )
        generated = (
            ".cursor/rules/x.mdc",
            ".claude/rules/x.md",
            ".codex/rules/coding-agents.rules",
            ".claude/skills/s1",
            ".claude/agents/a.md",
            ".codex/agents/a.toml",
            ".config/opencode/commands/c.md",
            ".claude/skills/c",
        )

        run_sync(config_root=config_root, home=home)

        self.assertEqual(existing(home, generated), list(generated))

        (config_root / "rules" / "x.md").unlink()
        shutil.rmtree(config_root / "skills" / "s1")
        (config_root / "agents" / "a.md").unlink()
        shutil.rmtree(config_root / "commands")
        run_sync(config_root=config_root, home=home)

        self.assertEqual(existing(home, generated), [])
        self.assertTrue(manual_plugin.exists())
        self.assertEqual((home / ".claude/rules/manual.md").read_text(), "manual\n")
        self.assertFalse((home / ".cursor/plugins/local/coding-agents-rules").exists())
        for managed in (home / ".config/opencode/commands", home / ".claude/skills"):
            self.assertEqual(
                load_json(managed / ".coding-agents-managed.json")["entries"], {}
            )

    def test_sync_does_not_rewrite_unchanged_generated_text_files(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "global" / "AGENTS.md",
            source_doc("global", "Global body\n"),
        )

        run_sync(config_root=config_root, home=home)
        target = home / ".codex" / "AGENTS.md"
        before = target.stat().st_mtime_ns

        run_sync(config_root=config_root, home=home)

        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_only_on_global_retires_excluded_host_files(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "global" / "AGENTS.md",
            source_doc("global", "Shared global\n"),
        )
        run_sync(config_root=config_root, home=home)
        self.assertEqual(
            (home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8"),
            "Shared global\n",
        )
        self.assertEqual(
            (home / ".config" / "opencode" / "AGENTS.md").read_text(encoding="utf-8"),
            "Shared global\n",
        )

        write(
            config_root / "global" / "AGENTS.md",
            source_doc(
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


class PermissionProjectionTests(SyncCase):
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
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["fd"], ["terraform", "show"], ["gh", "api"]],
            ask=[
                {"prefix": ["fd"], "tail": ["-x", "--exec"]},
                {"prefix": ["terraform", "show"], "tail": ["-json"]},
                {"prefix": ["gh", "api"], "tail": [["-X", "DELETE"]]},
            ],
        )

        run_sync(config_root=config_root, home=home)

        claude = claude_settings(home)
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

        bash = opencode_rules(opencode_config(home), "shell")
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
        self.assertEqual(opencode_decisions(bash, resolutions), resolutions)

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
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[
                {"prefix": ["mytool", "run"], "tail": [["--dry-run"]]},
                {"prefix": ["othertool"], "text": ["--check"]},
            ],
            deny=[{"prefix": ["othertool"], "text": [" -T", " --user "]}],
        )

        run_sync(config_root=config_root, home=home)

        claude = claude_settings(home)
        bash = opencode_rules(opencode_config(home), "shell")

        self.assertIn("Bash(mytool run --dry-run *)", claude["permissions"]["allow"])
        # A token-bounded tail projects to Cursor; a text predicate does
        # not, since the token matcher cannot hold a substring. Only the
        # mytool family reaches either Cursor surface, while the two glob
        # targets still receive both exact native forms.
        self.assertIn("Bash(othertool*--check*)", claude["permissions"]["allow"])
        # A leading space anchors the substring to a token boundary.
        self.assertIn("Bash(othertool* -T*)", claude["permissions"]["deny"])
        # A trailing space also lands bare-ended for Claude, whose final
        # ` *` matches an empty end only as the sole wildcard.
        self.assertIn("Bash(othertool* --user)", claude["permissions"]["deny"])
        expected = {
            "mytool run --dry-run": "allow",
            "mytool run plan --dry-run": "allow",
            "mytool run --apply": "ask",
            "othertool --check": "allow",
            "othertool --write": "ask",
            "othertool --check -T f": "deny",
            "othertool --check -Tf": "deny",
            "othertool --check -H Content-Type": "allow",
            "othertool --check --user x": "deny",
            "othertool --check --user-agent x": "allow",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)
        self.assertFalse((home / ".cursor/cli-config.json").exists())
        desktop = load_json(home / ".cursor/permissions.json")
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
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["git", "status"]],
            options={"git": ["-C", "--no-pager"]},
        )

        run_sync(config_root=config_root, home=home)

        claude = claude_settings(home)
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
        bash = opencode_rules(opencode_config(home), "shell")
        self.assertEqual(
            [key for key in bash if key.startswith("git")],
            ["git status *", "git -C* status *", "git --no-pager* status *"],
        )
        expected = {
            "git status": "allow",
            "git -C /tmp status": "allow",
            "git -C/tmp status --short": "allow",
            "git --no-pager status": "allow",
            "git -C /tmp push": "ask",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)

        self.assertFalse((home / ".cursor/cli-config.json").exists())
        desktop = load_json(home / ".cursor/permissions.json")
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
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["kubectl", "get"]],
            deny=[["kubectl", "apply"]],
            options={"kubectl": ["--context"]},
        )

        run_sync(config_root=config_root, home=home)

        claude = claude_settings(home)
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
        bash = opencode_rules(opencode_config(home), "shell")
        expected = {
            "kubectl get pods": "allow",
            "kubectl apply -f x.yaml": "deny",
            "kubectl --context prod apply -f x.yaml": "deny",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)

        self.assertFalse((home / ".cursor/cli-config.json").exists())
        desktop = load_json(home / ".cursor/permissions.json")
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
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["kubectl", "get"]],
            deny=[["kubectl", "apply"]],
            unmatched="allow",
        )

        run_sync(config_root=config_root, home=home)

        bash = opencode_rules(opencode_config(home), "shell")
        self.assertEqual(bash["*"], "allow")
        expected = {
            "anything at all": "allow",
            "kubectl get pods": "allow",
            "kubectl apply -f x.yaml": "deny",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)

        self.assertFalse((home / ".cursor/cli-config.json").exists())
        desktop = load_json(home / ".cursor/permissions.json")
        self.assertEqual(desktop["approvalMode"], "allowlist")
        self.assertNotIn("kubectl apply", desktop["terminalAllowlist"])

        claude = claude_settings(home)
        self.assertNotIn("defaultMode", claude["permissions"])
        # The blanket allow leaves every narrower allow dead.
        self.assertEqual(claude["permissions"]["allow"], ["Bash"])
        self.assertNotIn("kubectl get *", bash)
        self.assertIn("Bash(kubectl apply *)", claude["permissions"]["deny"])

    def test_cursor_desktop_allowlist_follows_the_guards(self) -> None:
        """Cursor's shared allowlist has no deny or ask channel, so every allow
        a guard can ride leaves it; unrelated siblings stay allowlisted."""
        rows = (
            (
                # The bare-exact live scenarios prove `Shell(prog:)` runs the
                # bare command and rejects any argument.
                "bare exact rules project the exact bare form",
                {"allow": [{"exact": ["fd"]}], "deny": [{"exact": ["shred"]}]},
                desktop_allowlist("fd:"),
                [codex_sandbox_warning("deny shred")],
            ),
            (
                "an exact allow admits nothing for an extending ask to overlap",
                {
                    "allow": [{"exact": ["git", "status"]}],
                    "ask": [["git", "status", "--short"]],
                },
                desktop_allowlist("git:status"),
                [],
            ),
            (
                "an ask on a distinct subcommand stays standalone",
                {"allow": [["git", "status"]], "ask": [["git", "commit"]]},
                desktop_allowlist("git status"),
                [],
            ),
            (
                "a narrowing deny withholds its allow",
                {
                    "allow": [["git", "push"], ["git", "status"]],
                    "deny": [{"prefix": ["git", "push"], "tail": ["--force"]}],
                },
                desktop_allowlist("git status"),
                [],
            ),
            (
                "a narrowing ask withholds only the colliding allow",
                {
                    "allow": [["git", "status"], ["git", "push"]],
                    "ask": [{"prefix": ["git", "push"], "tail": [["--force"]]}],
                },
                desktop_allowlist("git status"),
                [],
            ),
            (
                "a text ask withholds only the colliding allow",
                {
                    "allow": [["kubectl", "get"], ["kubectl", "logs"]],
                    "ask": [{"prefix": ["kubectl", "get", "secret"], "text": ["yaml"]}],
                },
                desktop_allowlist("kubectl logs"),
                [codex_sandbox_warning("ask kubectl get secret")],
            ),
            (
                "a text deny needs no form and the unrelated allow stays",
                {
                    "allow": [["git", "status"]],
                    "deny": [{"prefix": ["git", "push"], "text": ["--force"]}],
                },
                desktop_allowlist("git status"),
                [codex_sandbox_warning("deny git push")],
            ),
            (
                # Its option-hole heads (`prog:--profile * --profile status *`)
                # can carry the gated variant, so both local-tool rules leave.
                "an allow embedding declared options collides with the ask",
                {
                    "options": {"local-tool": ["--profile"]},
                    "allow": [
                        ["git", "status"],
                        ["local-tool", "status"],
                        ["local-tool", "--profile", "status"],
                    ],
                    "ask": [
                        {"prefix": ["local-tool", "status"], "tail": [["--destroy"]]}
                    ],
                },
                desktop_allowlist("git status"),
                [],
            ),
            (
                "an ask that cannot provably narrow is clawed back with a warning",
                {
                    "allow": [{"prefix": ["terraform", "show"], "tail": [["-json"]]}],
                    "ask": [{"prefix": ["terraform", "show", "-json"]}],
                },
                None,
                [overlap_warning("terraform show -json")],
            ),
            (
                # The declared option vocabulary expands every allow head, so
                # `git -C foo status` rides the emitted `git:-C * status` head.
                "an ask embedding declared options is a guarded overlap",
                {
                    "allow": [["git", "status"]],
                    "ask": [["git", "-C", "foo", "status"]],
                    "options": {"git": ["-C"]},
                },
                None,
                [overlap_warning("git -C foo status")],
            ),
            (
                "an ask on an undeclared leading token stays standalone",
                {
                    "allow": [["git", "status"]],
                    "ask": [["git", "--quiet", "status"]],
                    "options": {"git": ["-C"]},
                },
                desktop_allowlist(
                    "git status",
                    "git -C status",
                    "git:-C * status",
                    "git:-C * status *",
                ),
                [],
            ),
            (
                "interpreter allows are never projected",
                {"allow": [["sh"], ["bash"], ["zsh"], ["eval"], ["git", "status"]]},
                desktop_allowlist("git status"),
                [
                    f"Warning: omitted allow {name}: Cursor cannot confine an "
                    "interpreter payload"
                    for name in ("bash", "eval", "sh", "zsh")
                ],
            ),
        )
        for name, rules, desktop, warnings in rows:
            with self.subTest(name):
                config_root, home = self.new_roots()
                write_permissions(config_root, **rules)

                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    run_sync(config_root=config_root, home=home)

                self.assertEqual(
                    stderr.getvalue()
                    .replace(str(config_root), "<config>")
                    .splitlines(),
                    warnings,
                )
                self.assertFalse((home / ".cursor/cli-config.json").exists())
                self.assertEqual(cursor_desktop(home), desktop)

    def test_a_home_relative_command_projects_unexpanded_and_as_a_variable(
        self,
    ) -> None:
        """Agents type `~/bin/tool` and also `$HOME/bin/tool`, and glob targets
        match the text as typed, so the token alphabet carries `~` and every
        target projects it unexpanded."""
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["~/bin/tool", "status"]],
            deny=[["~/bin/tool", "push"]],
        )

        run_sync(config_root=config_root, home=home)

        bash = opencode_rules(opencode_config(home), "shell")
        expected = {
            "~/bin/tool status": "allow",
            "$HOME/bin/tool status -v": "allow",
            "$HOME/bin/tool push": "deny",
            "$HOME/bin/tool reset": "ask",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)
        allow = claude_settings(home)["permissions"]["allow"]
        self.assertIn("Bash(~/bin/tool status *)", allow)
        self.assertIn("Bash($HOME/bin/tool status *)", allow)
        self.assertFalse((home / ".cursor/cli-config.json").exists())

    def test_a_declared_assignment_prefixes_every_rule_its_target_does_not_peel(
        self,
    ) -> None:
        """A literal `NAME=value` agents type before a command decides nothing.

        Claude strips any assignment before an ask or deny, and the
        opencode-unwrap plugin before a deny, so only the other rules get
        copies behind it.
        """
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["tool", "status"]],
            ask=[["tool", "push"]],
            deny=[["tool", "reset"]],
            assignments={"tool": ["EDITOR=true", "IFS=", "IFS=$'\\t'"]},
        )

        run_sync(config_root=config_root, home=home)

        bash = opencode_rules(opencode_config(home), "shell")
        expected = {
            "EDITOR=true tool status": "allow",
            "IFS= tool status -v": "allow",
            "IFS=$'\\t' tool status": "allow",
            "EDITOR=true tool reset": "deny",
            "EDITOR=true tool push": "ask",
            "EDITOR=false tool status": "ask",
            "OTHER=1 tool status": "ask",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)
        self.assertEqual(
            {
                effect
                for resource, effect in bash.items()
                if resource.startswith("EDITOR=true ")
            },
            {"allow", "ask"},
        )
        permissions = claude_settings(home)["permissions"]
        self.assertEqual(
            {
                bucket: [
                    rule
                    for rule in permissions[bucket]
                    if rule.startswith("Bash(EDITOR=true ")
                ]
                for bucket in ("allow", "ask", "deny")
            },
            {"allow": ["Bash(EDITOR=true tool status *)"], "ask": [], "deny": []},
        )

    def test_a_text_rule_absorbs_the_token_rules_it_covers_on_glob_targets(
        self,
    ) -> None:
        """Codex keeps the token rule, its only reach; the glob targets drop it."""
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            deny=[
                {"prefix": ["kube", "get"], "tail": [["-oyaml"]]},
                {"prefix": ["kube", "get"], "text": ["yaml"]},
            ],
        )

        run_sync(config_root=config_root, home=home)

        self.assertEqual(
            [
                rule
                for rule in claude_settings(home)["permissions"]["deny"]
                if "kube" in rule
            ],
            ["Bash(kube get*yaml*)"],
        )
        self.assertEqual(
            {
                resource: effect
                for resource, effect in opencode_rules(
                    opencode_config(home), "shell"
                ).items()
                if "kube" in resource
            },
            {"kube get*yaml*": "deny"},
        )
        self.assertIn(
            'prefix_rule(pattern=["kube", "get", "-oyaml"], decision="forbidden")',
            (home / ".codex/rules/coding-agents.rules").read_text(),
        )

    def test_a_path_token_also_matches_its_quoted_spellings(self) -> None:
        """Glob targets match quotes as typed, and agents quote path arguments."""
        config_root, home = self.new_roots()
        write_permissions(config_root, allow=[["tool", "post", "/api/search"]])

        run_sync(config_root=config_root, home=home)

        bash = opencode_rules(opencode_config(home), "shell")
        expected = {
            "tool post /api/search --json {}": "allow",
            "tool post '/api/search' --json {}": "allow",
            'tool post "/api/search"': "allow",
            "tool post '/api/create'": "ask",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)
        claude = claude_settings(home)
        self.assertIn("Bash(tool post '/api/search' *)", claude["permissions"]["allow"])

    def test_secret_output_formats_are_expressible_in_the_source(self) -> None:
        """`text` covers -o yaml, -oyaml, and --output=yaml alike.

        `tail` is token-level and would miss the attached spellings, which is
        why the kubectl secret guard uses `text` instead.
        """
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["kubectl", "get"]],
            ask=[{"prefix": ["kubectl", "get", "secret"], "text": ["yaml"]}],
        )

        run_sync(config_root=config_root, home=home)

        bash = opencode_rules(opencode_config(home), "shell")
        expected = {
            "kubectl get pods": "allow",
            "kubectl get secret db": "allow",
            "kubectl get secret db -o yaml": "ask",
            "kubectl get secret db -oyaml": "ask",
            "kubectl get secret db --output=yaml": "ask",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)

        # The guard's text predicate has no token-matcher form, so the
        # colliding kubectl get rule leaves the CLI allowlist rather than
        # letting the secret dump ride it. Desktop loses it too.
        self.assertFalse((home / ".cursor/cli-config.json").exists())
        self.assertFalse((home / ".cursor/permissions.json").exists())

    def test_opencode_resolves_nested_decisions_and_guards_deny_over_allows(
        self,
    ) -> None:
        """Containment is legal downward: allow > ask > deny. OpenCode 2.0
        reports a bash denial without the ruleset, so guards land as `deny` and
        beat the allow bucket and the wrapper allows."""
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["gcloud", "config"], ["git", "checkout"], ["git", "push"]],
            ask=[["git", "push", "--force-with-lease"]],
            wrappers=["timeout"],
            deny=[
                ["gcloud", "auth", "print-access-token"],
                {"prefix": ["git", "checkout"], "tail": ["--force"]},
                ["git", "push", "--force"],
            ],
        )

        run_sync(config_root=config_root, home=home)

        bash = opencode_rules(opencode_config(home), "shell")
        expected = {
            "gcloud config list": "allow",
            "gcloud auth print-access-token": "deny",
            "timeout 5 gcloud auth print-access-token": "deny",
            "git checkout main": "allow",
            "git checkout --force main": "deny",
            "git push": "allow",
            "git push --force-with-lease": "ask",
            "git push --force": "deny",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)
        self.assertIn(
            "Bash(gcloud auth print-access-token *)",
            claude_settings(home)["permissions"]["deny"],
        )

    def test_opencode_orders_buckets_so_the_strictest_decision_matches_last(
        self,
    ) -> None:
        """Insertion order is the whole precedence contract on this target.

        `Permission.evaluate` applies the last matching key, so a stricter
        decision must sit after every looser pattern it has to beat, including
        the blanket wrapper allows.
        """
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["git", "status"]],
            ask=[["rmdir"]],
            deny=[["shred"], ["git", "push"]],
            secret_names=["EXAMPLE_TOKEN"],
            workspace={"allow": ["~/Developer"], "deny": ["~/.ssh"]},
        )

        run_sync(config_root=config_root, home=home)

        opencode = opencode_config(home)
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
        expected = {
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
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)
        self.assertEqual(
            (home / ".config/opencode/plugins/opencode-unwrap.js").read_bytes(),
            UNWRAP_PLUGIN.read_bytes(),
        )
        # No `*`: OpenCode's own ask default precedes its built-in allows.
        self.assertEqual(
            opencode_rules(opencode, "external_directory"),
            {"~/Developer/**": "allow", "~/.ssh/**": "deny"},
        )
        claude = claude_settings(home)
        self.assertIn("Bash(*EXAMPLE_TOKEN*)", claude["permissions"]["deny"])
        self.assertEqual(
            claude["sandbox"],
            {"credentials": {"envVars": [{"name": "EXAMPLE_TOKEN", "mode": "deny"}]}},
        )
        self.assertIn("Bash(shred *)", claude["permissions"]["deny"])
        self.assertIn("Bash(git push *)", claude["permissions"]["deny"])
        self.assertEqual(
            claude["permissions"]["additionalDirectories"], ["~/Developer"]
        )
        self.assertIn("Read(~/.ssh/**)", claude["permissions"]["deny"])

    def test_codex_policy_selects_a_built_in_profile(self) -> None:
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["git", "status"]],
            secret_paths=["~/.aws/credentials"],
            targets={
                "codex": {"native": {"default_permissions": ":danger-full-access"}}
            },
        )

        run_sync(config_root=config_root, home=home)

        codex = tomllib.loads((home / ".codex/config.toml").read_text())
        self.assertEqual(codex["default_permissions"], ":danger-full-access")
        self.assertEqual(
            codex["permissions"]["coding-agents"]["filesystem"],
            {str(home / ".aws/credentials"): "deny"},
        )

    def test_denied_paths_reach_every_anchor_each_target_checks(self) -> None:
        """Claude anchors a bare pattern at the cwd, so `**/x` goes root
        absolute. OpenCode checks files inside the session directory by their
        relative path, which neither `**/x` nor an expanded `~/x` matches."""
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["git", "status"]],
            tools={"read": "allow", "edit": "allow"},
            secret_paths=["**/.env", "~/.aws/credentials"],
            secret_names=["EXAMPLE_TOKEN"],
            workspace={"allow": ["~/Developer"], "deny": ["~/.ssh"]},
        )

        run_sync(config_root=config_root, home=home)

        claude = claude_settings(home)
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
            "# Generated by coding-agents sync\n**/.env\n/.aws/credentials\n/.ssh/**\n",
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

        opencode = opencode_config(home)
        read = opencode_rules(opencode, "read")
        expected = {
            ".env": "deny",
            "app/.env": "deny",
            "/Users/x/Developer/app/.env": "deny",
            ".aws/credentials": "deny",
            ".ssh/id_work": "deny",
            "src/main.py": "allow",
        }
        self.assertEqual(opencode_decisions(read, expected), expected)

    def test_workspace_directories_reach_each_target(self) -> None:
        """A Windows directory outside the home is spelled `C:/x`, which Claude
        matches in its POSIX form `//c/x`; an unmatched `allow` opens every
        directory except the denied ones, and an `ask` directory asks on Claude
        only."""
        rows = (
            (
                "windows drive directories",
                {"workspace": {"allow": ["C:/mise"], "deny": ["D:/vault"]}},
                {
                    "allow": ["Edit(**)", "Read(**)", "Edit(//c/mise/**)"],
                    "ask": [],
                    "deny": ["Read(//d/vault/**)", "Edit(//d/vault/**)"],
                    "additionalDirectories": ["C:/mise"],
                },
                {"C:/mise/**": "allow", "D:/vault/**": "deny"},
                {"C:/mise": "write", "D:/vault": "deny"},
            ),
            (
                "unmatched allow with a denied directory",
                {"workspace": {"unmatched": "allow", "deny": ["~/.ssh"]}},
                {
                    "allow": ["Edit(**)", "Read(**)", "Read(//**)", "Edit(//**)"],
                    "ask": [],
                    "deny": ["Read(~/.ssh/**)", "Edit(~/.ssh/**)"],
                },
                {"*": "allow", "~/.ssh/**": "deny"},
                {":root": "write", "~/.ssh": "deny"},
            ),
            (
                "unmatched allow with an ask directory",
                {"workspace": {"unmatched": "allow", "ask": ["~/review"]}},
                {
                    "allow": ["Edit(**)", "Read(**)", "Read(//**)", "Edit(//**)"],
                    "ask": ["Read(~/review/**)", "Edit(~/review/**)"],
                },
                {"*": "allow", "~/review/**": "ask"},
                {":root": "write"},
            ),
        )
        for name, rules, claude, opencode, codex in rows:
            with self.subTest(name):
                config_root, home = self.new_roots()
                write_permissions(
                    config_root, tools={"read": "allow", "edit": "allow"}, **rules
                )

                run_sync(config_root=config_root, home=home)

                self.assertEqual(claude_settings(home)["permissions"], claude)
                self.assertEqual(
                    opencode_rules(opencode_config(home), "external_directory"),
                    opencode,
                )
                filesystem = tomllib.loads((home / ".codex/config.toml").read_text())[
                    "permissions"
                ]["coding-agents"]["filesystem"]
                self.assertEqual(
                    {
                        path.replace(str(home), "~"): access
                        for path, access in filesystem.items()
                    },
                    codex,
                )

    def test_tool_decisions_fold_onto_each_targets_gates(self) -> None:
        """Write(**) parses on Claude but never matches, and OpenCode's write
        tool asks for `edit`, so both targets fold `write` onto `edit` and keep
        the stricter decision."""
        rows = (
            (
                "edit and write allowed",
                {"edit": "allow", "write": "allow"},
                {"allow": ["Edit(**)"], "ask": []},
                {"*": "allow"},
                None,
            ),
            (
                "edit allowed and write denied",
                {"edit": "allow", "write": "deny"},
                {"allow": [], "ask": [], "deny": ["Edit(**)"]},
                {"*": "deny"},
                None,
            ),
            (
                "web search denied",
                {"websearch": "deny"},
                {"allow": [], "ask": [], "deny": ["WebSearch(*)"]},
                {},
                "disabled",
            ),
        )
        for name, tools, claude, opencode_edit, codex_web_search in rows:
            with self.subTest(name):
                config_root, home = self.new_roots()
                write_permissions(config_root, tools=tools)

                run_sync(config_root=config_root, home=home)

                config = opencode_config(home)
                self.assertEqual(claude_settings(home)["permissions"], claude)
                self.assertEqual(opencode_rules(config, "edit"), opencode_edit)
                self.assertEqual(opencode_rules(config, "write"), {})
                codex = tomllib.loads((home / ".codex/config.toml").read_text())
                self.assertEqual(codex.get("web_search"), codex_web_search)

    def test_wrapper_guards_bind_where_the_target_cannot_peel_the_wrapper(self) -> None:
        """A wrapped copy of every guard, and no blanket wrapper allow.

        Claude Code peels `timeout` and `env` before matching a guard, so it
        needs no copy for them, and retries a guard behind a bare `xargs`, so
        it receives only the `xargs` copy that carries options. A blanket
        `env *` allow would match any payload the wrapper carries, so a wrapped
        allowed command falls to `unmatched` on the glob targets and prompts on
        Cursor, which lists no wrapped spelling.
        """
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["git", "status"]],
            ask=[["rmdir"]],
            wrappers=["timeout", "env", "xargs"],
        )

        run_sync(config_root=config_root, home=home)

        claude = claude_settings(home)
        self.assertEqual(claude["permissions"]["allow"], ["Bash(git status *)"])
        self.assertEqual(
            claude["permissions"]["ask"],
            [
                "Bash(rmdir *)",
                "Bash(xargs * rmdir *)",
                "Bash(xargs * rmdir)",
            ],
        )

        bash = opencode_rules(opencode_config(home), "shell")
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
        expected = {
            "git status": "allow",
            "timeout 5 git status": "ask",
            "timeout 5 rmdir build": "ask",
            "env FOO=1 rmdir build": "ask",
            "env rmdir build": "ask",
            "timeout rmdir build": "ask",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)

        self.assertFalse((home / ".cursor/cli-config.json").exists())
        desktop = load_json(home / ".cursor/permissions.json")
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
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["git", "status"]],
            ask=[["rmdir"]],
            deny=[
                ["shred"],
                {"prefix": ["gh", "auth", "status"], "tail": ["-t"]},
                {"prefix": ["rg"], "text": "--pre"},
                {"exact": ["ls"]},
            ],
            wrappers=["env"],
            secret_paths=["**/.env"],
            secret_names=["EXAMPLE_TOKEN"],
        )
        write(
            config_root / "rules" / "python.md",
            source_doc(
                "rule",
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
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            ask=[["git", "push"]],
            deny=[
                ["aws", "configure", "get"],
                ["aws", "configure", "export-credentials"],
                {"prefix": ["aws", "sso"], "reason": "use SSO"},
                {"prefix": ["git", "push"], "tail": ["-f", "--force"]},
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
        config_root, home = self.new_roots()
        write_permissions(config_root, allow=[["git", "status"]])
        local = config_root / "permissions.local"
        write(
            local / "local.yaml",
            "options:\n"
            "  local-tool: [--profile]\n"
            "allow:\n"
            "- [local-tool, status]\n"
            "ask:\n"
            "- prefix: local-tool status\n"
            "  tail: [[--destroy]]\n"
            "deny:\n"
            "- [local-tool, wipe]\n",
        )

        run_sync(config_root=config_root, home=home)

        bash = opencode_rules(opencode_config(home), "shell")
        expected = {
            "local-tool status": "allow",
            "local-tool --profile fixture status": "allow",
            "local-tool status --destroy": "ask",
            "local-tool wipe": "deny",
            "git status": "allow",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)
        claude = claude_settings(home)
        self.assertIn("Bash(local-tool status *)", claude["permissions"]["allow"])
        self.assertIn(
            "Bash(local-tool status --destroy *)", claude["permissions"]["ask"]
        )
        self.assertIn("Bash(local-tool wipe *)", claude["permissions"]["deny"])
        self.assertFalse((home / ".cursor/cli-config.json").exists())
        # The narrowed local-tool rule leaves Desktop's allowlist.
        desktop = load_json(home / ".cursor/permissions.json")
        self.assertEqual(desktop["terminalAllowlist"], ["git status"])

    def test_command_sources_that_owe_no_omission_sync(self) -> None:
        rows = (
            (
                "a fragment whose rules are all templated away",
                {
                    "permissions/policy.yaml": permission_policy_doc(),
                    "permissions/commands/empty.yaml": "# Every rule is gone here.\n",
                },
            ),
            (
                # Cursor withholds the allows a deny narrows and Codex projects
                # it, so a deny fragment owes no target an omission.
                "a deny fragment",
                {
                    "permissions/policy.yaml": permission_policy_doc(),
                    "permissions/commands/nonempty.yaml": "deny: [[git, push]]\n",
                },
            ),
            (
                "file tool allows",
                {
                    "permissions/policy.yaml": (
                        "tools: {read: allow, edit: allow, write: allow}\n"
                    )
                },
            ),
        )
        for name, files in rows:
            with self.subTest(name):
                config_root, home = self.new_roots()
                write_files(config_root, files)

                self.assertEqual(run_sync(config_root=config_root, home=home), ())

                self.assertTrue((home / ".claude/settings.json").exists())

    def test_a_patch_adds_to_generated_native_files(self) -> None:
        """A patch contributes freely where it does not clash, and the generated
        entries survive it: extending adds to the compiler's list rather than
        replacing it, and an overlay adds rather than clashes."""
        git_status = {"allow": [["git", "status"]]}
        netrc = (
            "extend:\n  /permissions:\n  - {action: external_directory,"
            " resource: '~/.netrc', effect: %s}\n"
        )
        rows = (
            (
                # Tool and MCP grants have no portable schema, so a patch owns them.
                "claude tool and mcp grants",
                git_status,
                "claude-settings",
                "extend:\n  /permissions/allow: [WebFetch(*), mcp__github__get_me]\n",
                lambda home: claude_settings(home)["permissions"]["allow"],
                ["Bash(git status *)", "WebFetch(*)", "mcp__github__get_me"],
            ),
            (
                "claude command denials",
                git_status,
                "claude-settings",
                "extend:\n  /permissions/deny: [Bash(cat ~/.netrc)]\n",
                lambda home: claude_settings(home)["permissions"]["deny"],
                ["Bash(cat ~/.netrc)"],
            ),
            (
                # The CLI carries no generated shell entries; the grant is the patch's.
                "cursor cli mcp grants",
                git_status,
                "cursor-cli-config",
                "extend:\n  /permissions/allow: [Mcp(looker-mcp:get_*)]\n",
                lambda home: load_json(home / ".cursor/cli-config.json")["permissions"],
                {"allow": ["Mcp(looker-mcp:get_*)"]},
            ),
            *(
                (
                    f"opencode external directory {decision}",
                    {**git_status, "workspace": {"allow": ["~/Developer"], "ask": []}},
                    "opencode",
                    netrc % decision,
                    lambda home: opencode_rules(
                        opencode_config(home), "external_directory"
                    ),
                    {"~/Developer/**": "allow", "~/.netrc": decision},
                )
                for decision in ("deny", "allow")
            ),
        )
        for name, rules, patch, text, read, expected in rows:
            with self.subTest(name):
                config_root, home = self.new_roots()
                write_permissions(config_root, **rules)
                write(config_root / "patches" / f"{patch}.yaml", text)

                run_sync(config_root=config_root, home=home)

                self.assertEqual(read(home), expected)

    def test_deny_reasons_land_beside_the_opencode_plugins(self) -> None:
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            deny=[{"prefix": ["find"], "reason": "use fd"}, ["rm"]],
            wrappers=["timeout"],
        )
        run_sync(config_root=config_root, home=home)

        sidecar = home / ".config/opencode/opencode-unwrap.json"
        reasons = json.loads(sidecar.read_text())["reasons"]
        opencode = opencode_config(home)
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

    def test_rule_strings_split_on_whitespace_and_expand_braces(self) -> None:
        config_root, _ = self.new_roots()
        write(config_root / "permissions" / "policy.yaml", "")
        write(
            config_root / "permissions" / "commands" / "rules.yaml",
            "allow:\n"
            "- gh pr {list,\n    view}\n"
            "- exact: '{python,python3} --version'\n"
            "deny:\n"
            "- prefix: gh api\n"
            "  tail: ['{-X,--method} {POST,PUT}', -f]\n"
            "  reason: ask the owner\n"
            "- prefix: wt\n"
            "  text: ' {-y,--yes}'\n",
        )

        commands = load_permissions(config_root / "permissions").commands

        self.assertEqual(
            [describe_rule(rule) for rule in commands.allow],
            [
                "gh pr list",
                "gh pr view",
                "python --version (exact)",
                "python3 --version (exact)",
            ],
        )
        self.assertEqual(
            [(describe_rule(rule), rule.reason) for rule in commands.deny],
            [
                (
                    "gh api +--method POST +--method PUT +-X POST +-X PUT +-f",
                    "ask the owner",
                ),
                ("wt ~ --yes ~ -y", None),
            ],
        )

    def test_permission_target_raw_keys_remain_exact(self) -> None:
        config_root, _ = self.new_roots()
        write(
            config_root / "permissions" / "policy.yaml",
            "targets:\n  opencode:\n    raw:\n      futureNativeKey: true\n",
        )

        permissions = load_permissions(config_root / "permissions")

        self.assertEqual(
            permissions.targets.root["opencode"].raw,
            {"futureNativeKey": True},
        )


class SourceLoweringTests(SyncCase):
    def test_a_rule_needs_no_frontmatter_and_no_global_source(self) -> None:
        config_root, home = self.new_roots()
        write(config_root / "rules" / "plain.md", "Plain body\n")

        run_sync(config_root=config_root, home=home)

        self.assertEqual((home / ".claude/rules/plain.md").read_text(), "Plain body\n")
        self.assertEqual((home / ".codex/AGENTS.md").read_text(), "Plain body\n")

    def test_global_rules_skills_agents_sync(self) -> None:
        config_root, home = self.new_roots()

        write(
            config_root / "global" / "AGENTS.md",
            source_doc("global", "Global body\n", description="Team defaults"),
        )
        write(
            config_root / "rules" / "python.md",
            source_doc(
                "rule",
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
            config_root / "agents" / "reviewer.md",
            source_doc(
                "agent",
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

        generated = {
            ".claude/CLAUDE.md": "Global body\n",
            ".config/opencode/AGENTS.md": "Global body\n",
            ".codex/AGENTS.md": "Global body\n\nRule body\n",
            ".claude/rules/python.md": "Rule body\n",
            ".cursor/rules/coding-agents-global.mdc": (
                "---\ndescription: Team defaults\nglobs: ''\nalwaysApply: true\n---\n\n"
                "Global body\n"
            ),
            ".cursor/rules/python.mdc": (
                "---\ndescription: Python rules\nglobs: ''\nalwaysApply: true\n---\n\n"
                "Rule body\n"
            ),
            ".codex/rules/coding-agents.rules": (
                "# Generated by coding-agents sync\n"
                'prefix_rule(pattern=["python"], decision="prompt", '
                'justification="Python scripts need review.")\n'
            ),
            ".claude/agents/reviewer.md": (
                "---\nname: reviewer\ndescription: 'Review code\n\n"
                "  across multiple lines'\neffort: high\ncolor: blue\n"
                "model: claude-override\n---\n\nAgent body\n"
            ),
            ".config/opencode/agents/reviewer.md": (
                "---\ndescription: 'Review code\n\n  across multiple lines'\n"
                "color: '#06b6d4'\nmodel: opencode-model\nmode: subagent\n"
                "permissions:\n- action: shell\n  resource: '*'\n  effect: ask\n"
                "request:\n  body:\n    reasoningEffort: high\n---\n\nAgent body\n"
            ),
            ".cursor/agents/reviewer.md": (
                "---\nname: reviewer\ndescription: Review code across multiple lines\n"
                "---\n\nAgent body\n"
            ),
            ".codex/agents/reviewer.toml": (
                'name = "reviewer"\n'
                'description = "Review code\\nacross multiple lines"\n'
                'developer_instructions = "Agent body"\n'
                'model_reasoning_effort = "high"\nmodel = "gpt-5"\n'
                'personality = "pragmatic"\nservice_tier = "flex"\n'
                'nickname_candidates = ["Ada"]\n'
            ),
            ".claude/skills/review-pr/SKILL.md": (
                "---\nname: review-pr\ndescription: Review a pull request\n"
                "agent: reviewer\ncontext: fork\nmodel: opus\n---\n\n"
                "Review pull request $ARGUMENTS.\n"
            ),
            ".config/opencode/commands/review-pr.md": (
                "---\ndescription: Review a pull request\nagent: reviewer\n"
                "subagent: true\nmodel: gpt-5\n---\n\nReview pull request $ARGUMENTS.\n"
            ),
            ".cursor/skills/review-pr/SKILL.md": (
                "---\nname: review-pr\ndescription: Review a pull request\n"
                "disable-model-invocation: true\n---\n\n"
                "Review pull request $ARGUMENTS.\n"
            ),
            ".codex/skills/review-pr/SKILL.md": (
                "---\nname: review-pr\ndescription: Review a pull request\n---\n\n"
                "Review pull request $ARGUMENTS.\n"
            ),
            ".codex/skills/review-pr/agents/openai.yaml": (
                "policy:\n  allow_implicit_invocation: false\n"
            ),
        }
        self.assertEqual(
            {path: (home / path).read_text() for path in generated}, generated
        )
        # Codex discovers skills/ and agents/ itself: nothing to register.
        self.assertEqual(
            existing(
                home,
                [
                    ".cursor/AGENTS.md",
                    ".cursor/plugins/local/coding-agents-rules",
                    ".codex/config.toml",
                ],
            ),
            [],
        )

    def test_cursor_rule_scoping_uses_native_mdc_fields(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "rules" / "python.md",
            source_doc(
                "rule",
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
        config_root, home = self.new_roots()
        plugin = home / ".cursor/plugins/local/coding-agents-rules/rules/python.mdc"
        write(plugin, "User plugin body\n")

        run_sync(config_root=config_root, home=home)

        self.assertEqual(plugin.read_text(), "User plugin body\n")

    def test_target_native_aliases_use_documented_spellings(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "rules" / "scoped.md",
            source_doc(
                "rule",
                "Rule body\n",
                extra={"cursor": {"alwaysApply": False, "globs": ["**/*.py"]}},
            ),
        )
        write(
            config_root / "skills" / "native" / "SKILL.md",
            source_doc(
                "skill",
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

        run_sync(config_root=config_root, home=home)

        claude_skill = frontmatter(home / ".claude/skills/native/SKILL.md")
        self.assertIs(claude_skill["disable-model-invocation"], True)
        self.assertEqual(claude_skill["allowed-tools"], ["Read"])
        cursor_rule = (home / ".cursor/rules/scoped.mdc").read_text()
        self.assertIn("alwaysApply: false", cursor_rule)
        self.assertIn("globs: '**/*.py'", cursor_rule)
        shared_skill = frontmatter(home / ".agents/skills/native/SKILL.md")
        self.assertIs(shared_skill["disable-model-invocation"], True)
        self.assertEqual(shared_skill["allowed-tools"], ["Bash"])

    def test_a_skill_narrowed_by_only_keeps_each_readers_own_directory(
        self,
    ) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "skills/narrow/SKILL.md",
            source_doc(
                "skill",
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

    def test_only_restricts_emission_and_skips_foreign_omits(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "global" / "AGENTS.md",
            source_doc("global", "Shared global\n"),
        )
        write(
            config_root / "rules" / "cursor-shell.md",
            source_doc(
                "rule",
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

    def test_skill_frontmatter_reaches_each_reader_in_its_own_vocabulary(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "skills" / "s1" / "SKILL.md",
            source_doc(
                "skill",
                "body\n",
                description="skill",
                extra={
                    "paths": ["docs/**"],
                    "disable_model_invocation": True,
                    "license": "MIT",
                    "metadata": {"owner": "platform"},
                    "internal": {"version": "1.2.3", "note": "x"},
                    "opencode:compatibility": "opencode",
                    "claude:when_to_use": "Use when editing docs.",
                    "claude:argument-hint": "[path]",
                    "claude:user-invocable": False,
                    "claude:allowed-tools": ["Read", "Grep"],
                    "claude:disallowed-tools": ["Bash"],
                    "claude:model": "sonnet",
                    "claude:effort": "high",
                    "claude:context": "fork",
                    "claude:agent": "Explore",
                    "claude:shell": "bash",
                },
            ),
        )

        run_sync(config_root=config_root, home=home)

        shared = {
            "name": "s1",
            "description": "skill",
            "license": "MIT",
            "metadata": {"owner": "platform"},
            "paths": ["docs/**"],
            "disable-model-invocation": True,
        }
        self.assertEqual(
            frontmatter(home / ".claude/skills/s1/SKILL.md"),
            {
                **shared,
                "when_to_use": "Use when editing docs.",
                "argument-hint": "[path]",
                "user-invocable": False,
                "allowed-tools": ["Read", "Grep"],
                "disallowed-tools": ["Bash"],
                "model": "sonnet",
                "effort": "high",
                "context": "fork",
                "agent": "Explore",
                "shell": "bash",
            },
        )
        self.assertEqual(
            frontmatter(home / ".agents/skills/s1/SKILL.md"),
            {**shared, "compatibility": "opencode"},
        )
        # Codex, Cursor, and OpenCode share one copy; Claude keeps its own.
        self.assertEqual(
            existing(
                home,
                [
                    ".agents/skills/s1",
                    ".claude/skills/s1",
                    ".codex/skills/s1",
                    ".cursor/skills/s1",
                    ".config/opencode/skills/s1",
                ],
            ),
            [".agents/skills/s1", ".claude/skills/s1"],
        )

    def test_codex_explicit_skill_policy_merges_with_openai_metadata(self) -> None:
        rows = (
            (
                "bundled metadata",
                {"disable_model_invocation": True},
                {
                    "skills/explicit/agents/openai.yaml": "interface:\n"
                    "  display_name: Explicit\npolicy:\n"
                    "  allow_implicit_invocation: true\ndependencies:\n  tools: []\n"
                },
                {
                    "interface": {"display_name": "Explicit"},
                    "policy": {"allow_implicit_invocation": False},
                    "dependencies": {"tools": []},
                },
            ),
            (
                "native metadata",
                {
                    "disable_model_invocation": True,
                    "codex:openai": {
                        "policy": {"products": ["codex"]},
                        "dependencies": {
                            "tools": [{"value": "docs", "command": "docs-mcp"}]
                        },
                    },
                },
                {},
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
            ),
        )
        for name, extra, bundled, expected in rows:
            with self.subTest(name):
                config_root, home = self.new_roots()
                write(
                    config_root / "skills/explicit/SKILL.md",
                    source_doc(
                        "skill", "Body\n", description="Explicit only", extra=extra
                    ),
                )
                write_files(config_root, bundled)

                run_sync(config_root=config_root, home=home)

                skill = home / ".agents/skills/explicit"
                self.assertEqual(
                    yaml.safe_load((skill / "agents/openai.yaml").read_text()),
                    expected,
                )
                self.assertIn(
                    "disable-model-invocation: true", (skill / "SKILL.md").read_text()
                )

    def test_bundled_skill_scripts_keep_owner_execute_bit(self) -> None:
        config_root, home = self.new_roots()
        runner_skill(config_root)

        run_sync(config_root=config_root, home=home)

        roots = (home / ".claude/skills/runner", home / ".agents/skills/runner")
        for root in roots:
            with self.subTest(root=root):
                script = root / "scripts/poll.sh"
                self.assertEqual(stat.S_IMODE(script.stat().st_mode), 0o700)
                notes = root / "references/notes.md"
                self.assertEqual(stat.S_IMODE(notes.stat().st_mode), 0o600)

        # Content is unchanged, so this only passes if the writer reconciles
        # mode independently of the content-equality short circuit.
        deployed = roots[0] / "scripts/poll.sh"
        deployed.chmod(0o600)
        run_sync(config_root=config_root, home=home)

        self.assertEqual(stat.S_IMODE(deployed.stat().st_mode), 0o700)

    def test_bundled_skill_scripts_are_allowed_where_each_target_deploys_them(
        self,
    ) -> None:
        config_root, home = self.new_roots()
        runner_skill(config_root)
        write_permissions(config_root, allow=[["git", "status"]])

        run_sync(config_root=config_root, home=home)

        claude = claude_settings(home)
        claude_allow = "\n".join(claude["permissions"]["allow"])
        opencode = opencode_config(home)
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

    def test_claude_and_opencode_agents_keep_their_native_fields(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "agents" / "reviewer.md",
            source_doc(
                "agent",
                "body\n",
                description="Reviewer agent",
                extra={
                    "claude": {
                        "permissionMode": "acceptEdits",
                        "memory": "project",
                        "isolation": "worktree",
                        "mcpServers": ["docs"],
                        "autoCompactWindow": 100000,
                        "omitClaudeMd": True,
                        "observer": "watcher",
                        "tools": ["Read", "mcp__github", "mcp__jaeger__*"],
                        "disallowedTools": ["mcp__*", "Edit"],
                    },
                    "effort": "high",
                    "opencode:mode": "subagent",
                    "opencode:request": {"body": {"temperature": 0.1}},
                    "opencode:steps": 5,
                    "opencode:hidden": True,
                    "opencode:disabled": False,
                    "opencode:permissions": [
                        {"action": "shell", "effect": "ask"},
                        {"action": "shell", "resource": "git diff*", "effect": "allow"},
                        {"action": "subagent", "effect": "deny"},
                    ],
                },
            ),
        )
        write(
            config_root / "agents" / "denylist.md",
            source_doc(
                "agent",
                "body\n",
                description="Denylist agent",
                extra={"claude": {"disallowedTools": ["Edit", "Write"]}},
            ),
        )
        write(
            config_root / "agents" / "second.md",
            source_doc(
                "agent",
                "body\n",
                description="Second agent",
                extra={
                    "opencode": {
                        "mode": "subagent",
                        "permissions": [{"action": "edit", "effect": "deny"}],
                        # Left unvalidated: the request body is the provider's,
                        # and ReasoningEffort is an open set.
                        "request": {"body": {"reasoningEffort": "model-specific"}},
                    },
                },
            ),
        )

        run_sync(config_root=config_root, home=home)

        claude = frontmatter(home / ".claude/agents/reviewer.md")
        self.assertEqual(
            {key: value for key, value in claude.items() if key != "effort"},
            {
                "name": "reviewer",
                "description": "Reviewer agent",
                "tools": ["Read", "mcp__github", "mcp__jaeger__*"],
                "disallowedTools": ["mcp__*", "Edit"],
                "permissionMode": "acceptEdits",
                "isolation": "worktree",
                "memory": "project",
                "mcpServers": ["docs"],
                "autoCompactWindow": 100000,
                "omitClaudeMd": True,
                "observer": "watcher",
            },
        )
        self.assertEqual(
            frontmatter(home / ".claude/agents/denylist.md"),
            {
                "name": "denylist",
                "description": "Denylist agent",
                "disallowedTools": ["Edit", "Write"],
            },
        )
        # Only native ConfigAgent.Info keys, so OpenCode skips its v1 migration;
        # the id comes from the file name.
        self.assertEqual(
            frontmatter(home / ".config/opencode/agents/reviewer.md"),
            {
                "description": "Reviewer agent",
                "request": {"body": {"reasoningEffort": "high", "temperature": 0.1}},
                "mode": "subagent",
                "hidden": True,
                "steps": 5,
                "disabled": False,
                "permissions": [
                    {"action": "shell", "resource": "*", "effect": "ask"},
                    {"action": "shell", "resource": "git diff*", "effect": "allow"},
                    {"action": "subagent", "resource": "*", "effect": "deny"},
                ],
            },
        )
        self.assertEqual(
            frontmatter(home / ".config/opencode/agents/second.md"),
            {
                "description": "Second agent",
                "request": {"body": {"reasoningEffort": "model-specific"}},
                "mode": "subagent",
                "permissions": [{"action": "edit", "resource": "*", "effect": "deny"}],
            },
        )

    def test_opencode_takes_a_portable_color_as_hex(self) -> None:
        """OpenCode 2 accepts only hex, so a Claude color name maps to one and
        a native hex override still wins."""
        for override, expected in ((None, "#3b82f6"), ("#ff5733", "#ff5733")):
            with self.subTest(override=override):
                config_root, home = self.new_roots()
                write_files(
                    config_root,
                    agent_file(
                        {
                            "color": "blue",
                            **({"opencode": {"color": override}} if override else {}),
                        }
                    ),
                )

                run_sync(config_root=config_root, home=home)

                opencode = frontmatter(home / ".config/opencode/agents/reviewer.md")
                self.assertEqual(opencode["color"], expected)
                self.assertEqual(
                    frontmatter(home / ".claude/agents/reviewer.md")["color"], "blue"
                )

    def test_opencode_command_target_fields_override_execution(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "commands" / "review.md",
            source_doc(
                "command",
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

    def test_codex_config_preserves_existing_content(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "skills" / "s1" / "SKILL.md",
            source_doc("skill", "body\n", description="skill"),
        )
        write(
            config_root / "agents" / "reviewer.md",
            source_doc("agent", "body\n", description="Review code"),
        )
        config = 'model = "gpt-5.4"\napproval_policy = "on-request"\n'
        write(home / ".codex" / "config.toml", config)

        run_sync(config_root=config_root, home=home)

        self.assertEqual((home / ".codex" / "config.toml").read_text(), config)


class NativePreservationTests(SyncCase):
    def test_unpatched_json_settings_are_preserved(self) -> None:
        config_root, home = self.new_roots()
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

        claude = claude_settings(home)
        self.assertEqual(claude["permissions"]["allow"], ["Read"])
        self.assertEqual(claude["permissions"]["deny"], ["Write"])
        self.assertEqual(claude["model"], "opus")
        self.assertNotIn("statusLine", claude)
        cursor = load_json(home / ".cursor" / "settings.json")
        self.assertEqual(cursor["editor"], {"fontSize": 12})
        self.assertNotIn("ai", cursor)
        self.assertFalse(cursor["telemetry"])


if __name__ == "__main__":
    unittest.main()
