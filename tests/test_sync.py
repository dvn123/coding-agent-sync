import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
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
from coding_agents_sync.io import (
    MANAGED_MANIFEST,
    ManagedEntryConflict,
    entry_hash,
    sync_manifested_entries,
)
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
from coding_agents_sync.targets.permissions import (
    UNWRAP_NAME,
    unwrap_binary,
    unwrap_command,
)
from coding_agents_sync.targets.pi import UNWRAP_EXTENSION


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


TARGET_NAMES = ("claude", "cursor", "codex", "opencode", "pi")


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
                    "pi": {"model": "anthropic/claude-sonnet-5", "effort": "xhigh"},
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


def codex_ask_warning(rules: str) -> str:
    return (
        "Warning: Codex prefix rules cannot hold a text predicate or an exact "
        "match, and its hooks cannot ask, so these asks fall to its approval "
        f"flow: {rules}: <config>/permissions/policy.yaml"
    )


def cursor_guards(home: Path) -> Any:
    """The asks and denies shell-unwrap's Cursor hook reads, or None."""
    path = home / ".cursor/hooks/shell-unwrap.json"
    return load_json(path)["permissions"] if path.exists() else None


def claude_shell_guards(home: Path) -> dict[str, list[str]]:
    permissions = claude_settings(home)["permissions"]
    return {
        bucket: [r for r in permissions.get(bucket, []) if r.startswith("Bash(")]
        for bucket in ("ask", "deny")
    }


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
                (
                    "disabled mcp server without a cursor omit",
                    {"mcp/browser.yaml": "command: browser-mcp\nenabled: false\n"},
                    r"enabled is unsupported on cursor: .* targets\.cursor",
                ),
                (
                    "claude-mcp patch overwriting a disabled server's command",
                    {
                        "mcp/browser.yaml": "only: [claude]\ncommand: browser-mcp\n"
                        "enabled: false\n",
                        "patches/claude-mcp.yaml": "set:\n"
                        "  /mcpServers/browser/command: other-mcp\n",
                    },
                    "Overlapping native pointers: /mcpServers/browser/command",
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

    def test_disabling_a_server_moves_its_claude_entry_to_an_opt_in_file(self) -> None:
        """Claude has no user-level off switch, so a disabled server leaves
        `.claude.json` for a `--mcp-config` file, taking its patched fields."""
        config_root, home = self.new_roots()
        server = "only: [claude]\nurl: https://example.test/mcp\n"
        write_files(
            config_root,
            {
                "mcp/remote.yaml": server,
                "patches/claude-mcp.yaml": "set:\n"
                "  /mcpServers/remote/headers/X-Key: ${KEY}\n",
            },
        )
        opt_in = home / ".claude/mcp/remote.json"

        def registered() -> dict[str, Any]:
            return json.loads((home / ".claude.json").read_text()).get("mcpServers", {})

        run_sync(config_root=config_root, home=home)
        self.assertEqual(
            registered()["remote"],
            {
                "type": "http",
                "url": "https://example.test/mcp",
                "headers": {"X-Key": "${KEY}"},
            },
        )
        self.assertFalse(opt_in.exists())

        write(config_root / "mcp/remote.yaml", server + "enabled: false\n")
        run_sync(config_root=config_root, home=home)
        self.assertNotIn("remote", registered())
        self.assertEqual(
            json.loads(opt_in.read_text()),
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.test/mcp",
                        "headers": {"X-Key": "${KEY}"},
                    }
                }
            },
        )

        write(config_root / "mcp/remote.yaml", server)
        run_sync(config_root=config_root, home=home)
        self.assertIn("remote", registered())
        self.assertFalse(opt_in.exists())

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

    def test_a_pruned_nested_entry_takes_the_directories_it_emptied(self) -> None:
        _, root = self.new_roots()
        write(root / "plugins/guard/hooks/index.js", "generated\n")
        write(root / "plugins/kept.js", "user\n")
        sync_manifested_entries(root, {"plugins/guard/hooks/index.js"}, "file")

        sync_manifested_entries(root, set(), "file")

        self.assertFalse((root / "plugins/guard").exists())
        self.assertTrue((root / "plugins/kept.js").exists())


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
        # The subagents package strips its own tool from every child, so
        # `delegate` needs no entry.
        pi = frontmatter(home / ".pi/agent/agents/reviewer.md")
        self.assertEqual(
            pi["permission"], {"bash": "deny", "edit": "deny", "write": "deny"}
        )
        self.assertIn("mcp__*", pi["tools"])

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
        pi_cheap = frontmatter(home / ".pi/agent/agents/cheap.md")
        pi_open = frontmatter(home / ".pi/agent/agents/open.md")
        self.assertEqual(
            (pi_cheap["model"], pi_cheap["thinking"]),
            ("anthropic/claude-sonnet-5", "xhigh"),
        )
        self.assertFalse({"model", "thinking"} & set(pi_open))

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

    def test_a_disabled_source_retires_its_deployed_copy(self) -> None:
        # kind: (source, its Claude output); a command reaches Claude as a skill.
        cases = {
            "global": ("global/AGENTS.md", ".claude/CLAUDE.md"),
            "rule": ("rules/paused.md", ".claude/rules/paused.md"),
            "skill": ("skills/paused/SKILL.md", ".claude/skills/paused/SKILL.md"),
            "command": ("commands/paused.md", ".claude/skills/paused/SKILL.md"),
            "agent": ("agents/paused.md", ".claude/agents/paused.md"),
        }
        for kind, (source, output) in cases.items():
            with self.subTest(kind):
                config_root, home = self.new_roots()
                write(config_root / source, source_doc(kind, "Body\n", description="P"))
                run_sync(config_root=config_root, home=home)
                self.assertTrue((home / output).exists())

                write(
                    config_root / source,
                    source_doc(
                        kind, "Body\n", description="P", extra={"enabled": False}
                    ),
                )
                run_sync(config_root=config_root, home=home)

                self.assertFalse((home / output).exists())


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
        # Desktop lists every allow; its hook carries Claude's guards.
        self.assertIn("fd", cursor_desktop(home)["terminalAllowlist"])
        self.assertEqual(cursor_guards(home), claude_shell_guards(home))

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
        """A guard matches any options before its subcommand; an allow carries
        over only the declared ones, which the corpus adds to the built-in
        table for its own tools."""
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["kube", "get"]],
            deny=[["kube", "apply"]],
            options={"kube": ["--context"]},
        )

        run_sync(config_root=config_root, home=home)

        claude = claude_settings(home)
        self.assertEqual(
            claude["permissions"]["deny"],
            ["Bash(kube apply *)", "Bash(kube -* apply *)", "Bash(kube -* apply)"],
        )
        bash = opencode_rules(opencode_config(home), "shell")
        expected = {
            "kube get pods": "allow",
            "kube --context prod get pods": "allow",
            "kube --as admin get pods": "ask",
            "kube apply -f x.yaml": "deny",
            "kube --context prod apply -f x.yaml": "deny",
            "kube --as admin apply -f x.yaml": "deny",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)

        self.assertFalse((home / ".cursor/cli-config.json").exists())
        desktop = load_json(home / ".cursor/permissions.json")
        self.assertEqual(
            desktop["terminalAllowlist"],
            [
                "kube get",
                "kube --context get",
                "kube:--context * get",
                "kube:--context * get *",
            ],
        )

    def test_built_in_options_carry_a_well_known_cli_allow(self) -> None:
        """`git -C repo status` runs `git status`, so the allow carries over it
        with no corpus vocabulary."""
        config_root, home = self.new_roots()
        write_permissions(config_root, allow=[["git", "status"]])

        run_sync(config_root=config_root, home=home)

        bash = opencode_rules(opencode_config(home), "shell")
        expected = {
            "git -C repo status": "allow",
            "git --no-pager status": "allow",
            "git -c core.pager=x status": "ask",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)

    def test_whole_program_allows_land_behind_wrappers_the_target_cannot_see(
        self,
    ) -> None:
        """Cursor sees through neither `command` nor `xargs` for an allow, and
        Pi only through `xargs`; both wrappers run the program as given."""
        config_root, home = self.new_roots()
        write_permissions(
            config_root, allow=[["rg"], ["git", "status"], ["sh"]], unmatched="ask"
        )

        run_sync(config_root=config_root, home=home)

        allowlist = load_json(home / ".cursor/permissions.json")["terminalAllowlist"]
        self.assertIn("xargs rg", allowlist)
        self.assertIn("command rg", allowlist)
        self.assertNotIn("xargs git", allowlist)
        self.assertFalse([entry for entry in allowlist if entry.endswith(" sh")])
        pi = load_json(home / ".pi/agent/extensions/pi-permission-system/config.json")
        self.assertNotIn("xargs rg *", pi["permission"]["bash"])
        self.assertEqual(pi["permission"]["bash"].get("command rg *"), "allow")

    def test_pi_extension_carries_wrapped_denies(self) -> None:
        """shell-unwrap's extension denies behind any wrapper, so a deny lands
        bare; it never asks, so an ask keeps its copy behind `command`, which
        the permission system does not know."""
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            deny=[["shred"]],
            ask=[["rmdir"]],
            wrappers=["command", "env"],
            unmatched="allow",
        )

        run_sync(config_root=config_root, home=home)

        bash = load_json(
            home / ".pi/agent/extensions/pi-permission-system/config.json"
        )["permission"]["bash"]
        self.assertEqual(bash.get("shred *"), "deny")
        self.assertNotIn("command shred *", bash)
        self.assertEqual(bash.get("command rmdir *"), "ask")
        extension = home / ".pi/agent/extensions/shell-unwrap"
        self.assertEqual(
            (extension / "index.js").read_bytes(), UNWRAP_EXTENSION.read_bytes()
        )
        binary = extension / UNWRAP_NAME
        self.assertEqual(binary.read_bytes(), unwrap_binary())
        self.assertTrue(os.access(binary, os.X_OK))
        guards = load_json(extension / "shell-unwrap.json")["permissions"]
        self.assertEqual(guards, {"deny": ["Bash(shred *)"]})

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

    def test_unmatched_sandbox_runs_unlisted_commands_where_a_sandbox_exists(
        self,
    ) -> None:
        """Claude gets no bare `Bash` allow but its sandbox, auto-allowing what
        runs inside it; Codex and Cursor need no omit, since their own sandbox
        runs an unlisted command; OpenCode and Pi have none, so they ask."""
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["kubectl", "get"]],
            deny=[["kubectl", "apply"]],
            unmatched="sandbox",
        )

        run_sync(config_root=config_root, home=home)

        claude = claude_settings(home)
        self.assertNotIn("Bash", claude["permissions"]["allow"])
        self.assertIn("Bash(kubectl get *)", claude["permissions"]["allow"])
        self.assertEqual(
            claude["sandbox"], {"enabled": True, "autoAllowBashIfSandboxed": True}
        )
        self.assertEqual(opencode_rules(opencode_config(home), "shell")["*"], "ask")
        pi = load_json(home / ".pi/agent/extensions/pi-permission-system/config.json")
        self.assertEqual(pi["permission"]["bash"]["*"], "ask")
        desktop = load_json(home / ".cursor/permissions.json")
        self.assertEqual(desktop["approvalMode"], "allowlist")
        self.assertIn("kubectl get", desktop["terminalAllowlist"])
        cli = load_json(home / ".cursor/cli-config.json")
        self.assertEqual(cli["sandbox"], {"mode": "enabled"})
        self.assertIn(
            'default_permissions = "coding-agents"',
            (home / ".codex/config.toml").read_text(),
        )

    def test_unmatched_sandbox_refuses_codex_full_access(self) -> None:
        """A profile with no sandbox would run every unlisted command freely."""
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["kubectl", "get"]],
            unmatched="sandbox",
            targets={
                "codex": {"native": {"default_permissions": ":danger-full-access"}}
            },
        )

        with self.assertRaisesRegex(ValueError, "danger-full-access turns off"):
            run_sync(config_root=config_root, home=home)

    def test_cursor_desktop_lists_every_allow_and_its_hook_holds_the_guards(
        self,
    ) -> None:
        """Cursor's shared allowlist has no deny or ask channel, so every allow
        lands in it and shell-unwrap's hook carries Claude's shell guards."""
        rows = (
            (
                # The bare-exact live scenarios prove `Shell(prog:)` runs the
                # bare command and rejects any argument.
                "bare exact rules project the exact bare form",
                {"allow": [{"exact": ["fd"]}], "deny": [{"exact": ["shred"]}]},
                desktop_allowlist("fd:"),
                [],
            ),
            (
                "a guard leaves the allow it narrows in place",
                {
                    "allow": [["tool", "push"], ["tool", "status"]],
                    "deny": [{"prefix": ["tool", "push"], "tail": ["--force"]}],
                    "ask": [{"prefix": ["tool", "status"], "text": ["--ignored"]}],
                },
                desktop_allowlist("tool push", "tool status"),
                [codex_ask_warning("tool status")],
            ),
            (
                "the declared option vocabulary expands every allow",
                {
                    "allow": [["tool", "status"]],
                    "ask": [["tool", "-C", "foo", "status"]],
                    "options": {"tool": ["-C"]},
                },
                desktop_allowlist(
                    "tool status",
                    "tool -C status",
                    "tool:-C * status",
                    "tool:-C * status *",
                ),
                [],
            ),
            (
                "interpreter allows are never projected",
                {"allow": [["sh"], ["bash"], ["zsh"], ["eval"], ["tool", "status"]]},
                desktop_allowlist("tool status"),
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
                guards = claude_shell_guards(home)
                self.assertEqual(
                    cursor_guards(home), guards if any(guards.values()) else None
                )

    def test_a_home_relative_command_projects_in_every_spelling_agents_type(
        self,
    ) -> None:
        """Agents type `~/bin/tool`, `$HOME/bin/tool`, and the absolute path,
        and glob targets match the text as typed, so a `~/` program lands in
        all three, and a `~/` argument under its absolute path too."""
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["~/bin/tool", "status"], ["agent", "load", "~/x.plist"]],
            deny=[["~/bin/tool", "push"]],
        )

        run_sync(config_root=config_root, home=home)

        bash = opencode_rules(opencode_config(home), "shell")
        expected = {
            "~/bin/tool status": "allow",
            "$HOME/bin/tool status -v": "allow",
            f"{home.as_posix()}/bin/tool status": "allow",
            "$HOME/bin/tool push": "deny",
            f"{home.as_posix()}/bin/tool push": "deny",
            "$HOME/bin/tool reset": "ask",
            "agent load ~/x.plist": "allow",
            f"agent load {home.as_posix()}/x.plist": "allow",
        }
        self.assertEqual(opencode_decisions(bash, expected), expected)
        allow = claude_settings(home)["permissions"]["allow"]
        self.assertIn("Bash(~/bin/tool status *)", allow)
        self.assertIn("Bash($HOME/bin/tool status *)", allow)
        self.assertIn(f"Bash({home.as_posix()}/bin/tool status *)", allow)
        self.assertFalse((home / ".cursor/cli-config.json").exists())

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
            ["Bash(kube get*yaml*)", "Bash(kube -* get*yaml*)"],
        )
        self.assertEqual(
            {
                resource: effect
                for resource, effect in opencode_rules(
                    opencode_config(home), "shell"
                ).items()
                if "kube" in resource
            },
            {"kube get*yaml*": "deny", "kube -* get*yaml*": "deny"},
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

    def test_the_no_op_builtin_is_a_command_but_a_drive_path_is_not(self) -> None:
        """`:` may name a command; a colon elsewhere, as in `C:/x`, may not."""
        config_root, home = self.new_roots()
        write_permissions(config_root, allow=[[":"]])

        run_sync(config_root=config_root, home=home)

        self.assertIn("Bash(: *)", claude_settings(home)["permissions"]["allow"])
        bash = opencode_rules(opencode_config(home), "shell")
        expected = {":": "allow", ": x": "allow", "::": "ask", "C:/x": "ask"}
        self.assertEqual(opencode_decisions(bash, expected), expected)

        write_permissions(config_root, allow=[["C:/x"]])
        with self.assertRaisesRegex(ValueError, "should match pattern"):
            run_sync(config_root=config_root, home=home)

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

        # Desktop keeps `kubectl get`; the hook asks for the secret dump.
        self.assertFalse((home / ".cursor/cli-config.json").exists())
        self.assertIn("kubectl get", cursor_desktop(home)["terminalAllowlist"])
        self.assertEqual(cursor_guards(home), claude_shell_guards(home))

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
                "git -C* status *": "allow",
                "git --no-pager* status *": "allow",
                "rmdir *": "ask",
                "shred *": "deny",
                "git push *": "deny",
                "git -* push *": "deny",
                "*EXAMPLE_TOKEN*": "deny",
            },
        )
        self.assertEqual(list(bash)[-1], "*EXAMPLE_TOKEN*")
        expected = {
            "shred f": "deny",
            # The bundled shell-unwrap plugin peels program paths,
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
            (home / ".config/opencode/plugins/shell-unwrap.js").read_bytes(),
            UNWRAP_PLUGIN.read_bytes(),
        )
        # The plugin asks the binary one directory above it.
        binary = home / ".config/opencode" / UNWRAP_NAME
        self.assertEqual(binary.read_bytes(), unwrap_binary())
        self.assertTrue(os.access(binary, os.X_OK))
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

        Claude Code peels `timeout`, `env`, and a bare `xargs` before matching
        a guard, and shell-unwrap's hook and plugin peel all three, so neither
        Claude nor OpenCode needs a copy. A blanket `env *` allow would match
        any payload the wrapper carries, so a wrapped allowed command falls to
        `unmatched` on the glob targets and prompts on Cursor, which lists no
        wrapped spelling.
        """
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["tool", "status"]],
            ask=[["rmdir"]],
            wrappers=["timeout", "env", "xargs"],
        )

        run_sync(config_root=config_root, home=home)

        claude = claude_settings(home)
        self.assertEqual(claude["permissions"]["allow"], ["Bash(tool status *)"])
        self.assertEqual(claude["permissions"]["ask"], ["Bash(rmdir *)"])

        bash = opencode_rules(opencode_config(home), "shell")
        self.assertEqual(list(bash), ["*", "tool status *", "rmdir *"])
        expected = {
            "tool status": "allow",
            "timeout 5 tool status": "ask",
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
            ["tool status"],
        )

    def test_codex_receives_asks_as_prompt_and_denies_as_forbidden(self) -> None:
        """Asks and denies land as native prefixes beside the hand-authored
        Starlark.

        Codex exec policy has no wildcards and lapses entirely on any `$VAR`
        or substitution, so it is not a containment boundary; a prefix still
        governs the plain spelling. Allows stay with the sandbox, because a
        Codex allow rule runs outside it. A deny with no prefix form lands in
        shell-unwrap's hook instead, which denies every peeled spelling.
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
        self.assertNotIn("Codex prefix rules", stderr.getvalue())
        guards = json.loads((home / ".codex/hooks/shell-unwrap.json").read_text())
        self.assertEqual(set(guards["permissions"]), {"deny"})
        for pattern in ("Bash(ls)", "Bash(rg*--pre*)", "Bash(shred *)"):
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, guards["permissions"]["deny"])
        config = tomllib.loads((home / ".codex/config.toml").read_text())
        (hook,) = config["hooks"]["PreToolUse"]
        self.assertEqual(hook["matcher"], "Bash")
        script = home / ".codex/hooks" / UNWRAP_NAME
        self.assertIn(f"{script} codex", hook["hooks"][0]["command"])
        self.assertIn('"permissionDecision": "deny"', hook["hooks"][0]["command"])
        self.assertEqual(script.read_bytes(), unwrap_binary())
        self.assertTrue(os.access(script, os.X_OK))

    def test_unwrap_command_runs_the_binary_and_passes_in_bypass(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            # Prints its host, or fails as a missing or broken binary would.
            binary = root / "shell-unwrap"
            binary.write_text(
                '#!/bin/sh\ncat >/dev/null\n[ -z "$FAIL" ] && echo "$1"\n'
            )
            binary.chmod(0o755)
            command = unwrap_command(
                str(binary), "claude", {"decision": "ask"}, silent_in_bypass=True
            )

            def run(mode: str, *, fail: bool) -> str:
                event = json.dumps({"permission_mode": mode}, separators=(",", ":"))
                env = {"PATH": "/usr/bin:/bin"} | ({"FAIL": "1"} if fail else {})
                return subprocess.run(
                    ["/bin/sh", "-c", command],
                    input=event,
                    env=env,
                    cwd=root,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout

            self.assertEqual(run("default", fail=False), "claude\n")
            self.assertEqual(run("default", fail=True), '{"decision": "ask"}')
            self.assertEqual(run("bypassPermissions", fail=True), "")

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
        desktop = load_json(home / ".cursor/permissions.json")
        self.assertIn("local-tool status", desktop["terminalAllowlist"])
        self.assertEqual(cursor_guards(home), claude_shell_guards(home))

    def test_a_disabled_command_fragment_contributes_no_rules(self) -> None:
        config_root, home = self.new_roots()
        write_permissions(config_root, allow=[["git", "status"]])
        write(
            config_root / "permissions.local/paused.yaml",
            "enabled: false\nallow:\n- [local-tool, status]\n",
        )

        run_sync(config_root=config_root, home=home)

        allow = claude_settings(home)["permissions"]["allow"]
        self.assertIn("Bash(git status *)", allow)
        self.assertEqual([rule for rule in allow if "local-tool" in rule], [])

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
        git_status = {"allow": [["tool", "status"]]}
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
                ["Bash(tool status *)", "WebFetch(*)", "mcp__github__get_me"],
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

        sidecar = home / ".config/opencode/shell-unwrap.json"
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

    def test_shell_unwrap_hook_carries_deny_reasons_beside_foreign_hooks(
        self,
    ) -> None:
        config_root, home = self.new_roots()
        settings = home / ".claude/settings.json"
        foreign = {"matcher": "Write", "hooks": [{"type": "command", "command": "x"}]}
        write(settings, json.dumps({"hooks": {"PreToolUse": [foreign]}}))
        write_permissions(
            config_root,
            allow=[["git", "status"]],
            deny=[{"prefix": ["find"], "reason": "use fd"}, ["rm"]],
            wrappers=["timeout"],
        )
        run_sync(config_root=config_root, home=home)

        script = home / ".claude/hooks" / UNWRAP_NAME
        self.assertEqual(script.read_bytes(), unwrap_binary())
        self.assertTrue(os.access(script, os.X_OK))
        hooks = claude_settings(home)["hooks"]["PreToolUse"]
        self.assertEqual(hooks[0], foreign)
        self.assertEqual(hooks[1]["matcher"], "Bash")
        # The deployed hook, run as Claude runs it, names the reason.
        event = {
            "tool_name": "Bash",
            "tool_input": {"command": "timeout 5 find . -delete"},
            "cwd": str(home),
        }
        output = subprocess.run(
            hooks[1]["hooks"][0]["command"],
            shell=True,
            input=json.dumps(event),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertEqual(
            json.loads(output)["hookSpecificOutput"]["permissionDecisionReason"],
            "`find . -delete` is denied by the shell rules: use fd",
        )
        sidecar = home / ".claude/shell-unwrap.json"
        reasons = json.loads(sidecar.read_text())["reasons"]
        denies = {
            rule.removeprefix("Bash(").removesuffix(")")
            for rule in claude_settings(home)["permissions"]["deny"]
        }
        # Every pattern emitted for the reasoned rule, and only those.
        self.assertEqual(
            set(reasons), {pattern for pattern in denies if "find" in pattern}
        )
        self.assertEqual(set(reasons.values()), {"use fd"})

        write_permissions(config_root, deny=[["find"]])
        run_sync(config_root=config_root, home=home)
        self.assertFalse(sidecar.exists())
        self.assertEqual(len(claude_settings(home)["hooks"]["PreToolUse"]), 2)

    def test_cursor_hook_denies_and_asks_beside_foreign_hooks(self) -> None:
        config_root, home = self.new_roots()
        foreign = {"command": "/bin/true", "timeout": 5}
        write(
            config_root / "patches" / "cursor-hooks.yaml",
            f"extend:\n  /hooks/beforeShellExecution:\n  - {json.dumps(foreign)}\n",
        )
        write_permissions(
            config_root,
            allow=[["git"]],
            ask=[["git", "push"]],
            deny=[{"prefix": ["find"], "reason": "use fd"}],
            wrappers=["timeout"],
        )
        run_sync(config_root=config_root, home=home)

        script = home / ".cursor/hooks" / UNWRAP_NAME
        self.assertEqual(script.read_bytes(), unwrap_binary())
        self.assertTrue(os.access(script, os.X_OK))
        hooks = load_json(home / ".cursor/hooks.json")
        hook, extended = hooks["hooks"]["beforeShellExecution"]
        self.assertEqual(extended, foreign)
        self.assertTrue(hook["failClosed"])

        def verdict(command: str, environment: dict[str, str]) -> Any:
            output = subprocess.run(
                hook["command"],
                shell=True,
                input=json.dumps({"command": command, "cwd": str(home)}),
                capture_output=True,
                text=True,
                check=True,
                env=environment,
            ).stdout
            return json.loads(output)

        # The deployed hook, run as Cursor runs it, with $HOME set.
        environment = {**os.environ, "HOME": str(home)}
        self.assertEqual(
            verdict("timeout 5 find . -delete", environment)["user_message"],
            "`find . -delete` is denied by the shell rules: use fd",
        )
        self.assertEqual(verdict("git push", environment)["permission"], "ask")
        self.assertEqual(verdict("git status", environment), {})
        # The hook runs by its path, so Desktop's bare PATH does not matter,
        # and without the binary the guards fail closed.
        bare = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
        self.assertEqual(verdict("git status", bare), {})
        moved = script.rename(script.with_name("moved"))
        self.assertEqual(verdict("git status", bare)["permission"], "ask")
        moved.rename(script)

        write_permissions(config_root, allow=[["git"]])
        run_sync(config_root=config_root, home=home)
        self.assertFalse(script.exists())
        hooks = load_json(home / ".cursor/hooks.json")
        self.assertEqual(hooks["hooks"]["beforeShellExecution"], [foreign])

    def test_a_raw_hooks_file_hands_over_to_the_generated_hook(self) -> None:
        """A raw `hooks.json` that becomes a patch keeps its hooks beside the
        generated one rather than being pruned as a stale raw file."""
        config_root, home = self.new_roots()
        foreign = {"command": "/bin/true", "timeout": 5}
        # What an earlier version left: the raw file and its manifest entry.
        hooks_file = home / ".cursor/hooks.json"
        write(hooks_file, json.dumps({"version": 1, "hooks": {"stop": [foreign]}}))
        write(
            home / ".cursor/.coding-agents-managed.json",
            json.dumps({"entries": {"hooks.json": entry_hash(hooks_file, "file")}}),
        )
        write(
            config_root / "patches" / "cursor-hooks.yaml",
            f"set:\n  /hooks/stop:\n  - {json.dumps(foreign)}\n",
        )
        write_permissions(config_root, allow=[["git"]], deny=[["rm"]])
        # A run that failed after writing the native values changed the file.
        hooks_file.write_text(hooks_file.read_text() + "\n")
        run_sync(config_root=config_root, home=home)

        hooks = load_json(home / ".cursor/hooks.json")["hooks"]
        self.assertEqual(hooks["stop"], [foreign])
        self.assertEqual(len(hooks["beforeShellExecution"]), 1)
        self.assertNotIn(
            "hooks.json",
            load_json(home / ".cursor/.coding-agents-managed.json")["entries"],
        )

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


class PiTargetTests(SyncCase):
    def pi_permissions(self, home: Path) -> Any:
        return load_json(home / ".pi/agent/extensions/pi-permission-system/config.json")

    def test_policy_lowers_to_the_permission_systems_surfaces(self) -> None:
        """Each map is ordered for last-match-wins; Pi gates `write` apart from
        `edit`, and a deny keeps its reason."""
        config_root, home = self.new_roots()
        write_permissions(
            config_root,
            allow=[["git", "status"]],
            ask=[["rmdir"]],
            deny=[{"prefix": "git push", "tail": ["--force"], "reason": "lease it"}],
            wrappers=["env", "command"],
            secret_names=["EXAMPLE_TOKEN"],
            secret_paths=["**/.env"],
            tools={"read": "allow", "edit": "allow", "write": "ask"},
            workspace={"allow": ["~/Developer"], "deny": ["~/.ssh"]},
        )
        write(config_root / "skills" / "shared" / "SKILL.md", "---\n---\nbody\n")
        write(
            config_root / "mcp" / "browser.yaml",
            "command: browser-mcp\ndescription: A browser.\ntools:\n  click: ask\n",
        )

        run_sync(config_root=config_root, home=home)

        config = self.pi_permissions(home)
        permission = config["permission"]
        bash = permission["bash"]
        self.assertEqual(list(bash)[0], "*")
        self.assertEqual(bash["*"], "ask")
        self.assertEqual(bash["git status *"], "allow")
        # The permission system raises a command behind `env` to at least
        # ask itself; it does not know `command`, which keeps its copies.
        self.assertFalse([pattern for pattern in bash if pattern.startswith("env ")])
        self.assertEqual(bash["command * rmdir *"], "ask")
        self.assertEqual(
            bash["git push --force *"], {"action": "deny", "reason": "lease it"}
        )
        self.assertEqual(list(bash)[-1], "*EXAMPLE_TOKEN*")
        self.assertEqual(
            {tool: permission[tool] for tool in ("read", "grep", "edit", "write")},
            {"read": "allow", "grep": "allow", "edit": "allow", "write": "ask"},
        )
        self.assertEqual(
            permission["path"],
            {"*": "allow", "**/.env": "deny", "~/.ssh/**": "deny"},
        )
        self.assertEqual(
            permission["external_directory"],
            {
                "*": "ask",
                "~/Developer": "allow",
                "~/Developer/*": "allow",
                "~/.ssh": "deny",
                "~/.ssh/*": "deny",
            },
        )
        self.assertEqual(permission["mcp"], {"browser:click": "ask"})
        self.assertEqual(config["piInfrastructureReadPaths"], ["~/.agents/skills"])
        self.assertEqual(
            load_json(home / ".pi/agent/mcp.json")["mcpServers"]["browser"],
            {"command": "browser-mcp", "description": "A browser."},
        )
        self.assertEqual(
            load_json(home / ".pi/agent/settings.json")["packages"],
            ["npm:@gotgenes/pi-permission-system@42.0.0"],
        )

    def test_permission_system_installs_only_when_something_needs_it(self) -> None:
        """An agent deny alone installs it, with a permissive base so the
        extension's ask default changes nothing else."""
        config_root, home = self.new_roots()
        write(config_root / "global" / "AGENTS.md", "Global\n")
        write(config_root / "rules" / "style.md", "Rule\n")

        run_sync(config_root=config_root, home=home)

        self.assertEqual((home / ".pi/agent/AGENTS.md").read_text(), "Global\n\nRule\n")
        self.assertFalse((home / ".pi/agent/settings.json").exists())

        write(
            config_root / "agents" / "reader.md",
            source_doc("agent", "Read\n", extra={"deny": ["shell"]}),
        )
        run_sync(config_root=config_root, home=home)

        self.assertEqual(
            self.pi_permissions(home)["permission"],
            {"*": "allow", "bash": {"*": "allow"}},
        )
        self.assertEqual(
            load_json(home / ".pi/agent/settings.json")["packages"],
            [
                "npm:@gotgenes/pi-permission-system@42.0.0",
                "npm:@gotgenes/pi-subagents@25.0.5",
            ],
        )

    def test_a_patch_cannot_write_command_policy(self) -> None:
        config_root, home = self.new_roots()
        write_permissions(config_root, allow=[["git", "status"]])
        write(
            config_root / "patches" / "pi-permissions.yaml",
            "set:\n  /permission/bash/rm *: allow\n",
        )

        with self.assertRaisesRegex(PatchError, "cannot touch /permission/bash"):
            run_sync(config_root=config_root, home=home)

    def test_patches_install_and_configure_autoformat(self) -> None:
        config_root, home = self.new_roots()
        write_permissions(config_root, allow=[["git", "status"]])
        write(
            config_root / "patches" / "pi-settings.yaml",
            "extend:\n  /packages: ['npm:@gotgenes/pi-autoformat@5.1.11']\n",
        )
        write(
            config_root / "patches" / "pi-autoformat.yaml",
            "replace:\n  /chains: {.toml: [{fallback: [taplo]}]}\n",
        )

        run_sync(config_root=config_root, home=home)

        self.assertEqual(
            load_json(home / ".pi/agent/settings.json")["packages"],
            [
                "npm:@gotgenes/pi-permission-system@42.0.0",
                "npm:@gotgenes/pi-autoformat@5.1.11",
            ],
        )
        self.assertEqual(
            load_json(home / ".pi/agent/extensions/pi-autoformat/config.json"),
            {"chains": {".toml": [{"fallback": ["taplo"]}]}},
        )

    def test_a_command_becomes_a_prompt_template(self) -> None:
        config_root, home = self.new_roots()
        write(
            config_root / "commands" / "review.md",
            source_doc(
                "command",
                "Review $ARGUMENTS\n",
                description="Review a change",
                extra={"pi:argument-hint": "[focus]"},
            ),
        )

        run_sync(config_root=config_root, home=home)

        self.assertEqual(
            (home / ".pi/agent/prompts/review.md").read_text(),
            "---\ndescription: Review a change\nargument-hint: '[focus]'\n---\n\n"
            "Review $ARGUMENTS\n",
        )


if __name__ == "__main__":
    unittest.main()
