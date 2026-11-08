"""The Pi target, `~/.pi/agent`.

Pi has no permission system or named agents of its own. The compiler installs
`@gotgenes/pi-permission-system` and `@gotgenes/pi-subagents` as pinned Pi
packages and writes the configuration they read, so both versions are the
ones this lowering was checked against.
"""

import functools
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import ConfigDict, Field, field_validator

from ..ir import applies_to, resolve
from ..models import SyncContext
from ..output.patches import PatchError, display_pointer, touches
from ..output.plan import (
    Diagnostic,
    NativePatch,
    NativeValue,
    OwnedFile,
    OwnedTree,
    Plan,
)
from ..sources import (
    AgentTool,
    McpServerSource,
    PermissionSource,
    PermissionTool,
    SkillSource,
    SourceBundle,
    StrictModel,
    resolve_model_policy,
)
from .claude import shell_entries as claude_shell_entries
from .permissions import (
    UNWRAP_HOOK,
    bucket_entries,
    bucket_patterns,
    denied_paths,
    deny_reasons,
    glob_variants,
    secret_name_variants,
    unwrap_rules_file,
    wrapper_prefixes,
)
from .support import (
    declared_trees,
    frontmatter,
    markdown,
    mcp_servers,
    mcp_tool_decisions,
    native_patch,
    one_of,
    raw_files,
    shared_skill,
    shared_skills_root,
    skill_tree,
    with_skill_scripts,
)

PERMISSION_PACKAGE = "npm:@gotgenes/pi-permission-system@42.0.0"
SUBAGENTS_PACKAGE = "npm:@gotgenes/pi-subagents@25.0.5"
PERMISSION_CONFIG = Path("extensions") / "pi-permission-system" / "config.json"
# pi-autoformat's config gets no generated values; a pi-settings patch installs
# the package and a pi-autoformat patch configures it.
AUTOFORMAT_CONFIG = Path("extensions") / "pi-autoformat" / "config.json"
THINKING_LEVELS = frozenset({"off", "minimal", "low", "medium", "high", "xhigh", "max"})
PROMPT_MODES = frozenset({"append", "replace"})

# Each portable tool class and the Pi tools that read or write files for it.
# Unlike Claude and OpenCode, Pi gates `write` apart from `edit`.
PI_TOOL_SURFACES: dict[PermissionTool, tuple[str, ...]] = {
    "read": ("read", "grep", "find", "ls"),
    "edit": ("edit",),
    "write": ("write",),
}
# A child of @gotgenes/pi-subagents receives only the tools its file names, so
# an agent without native `tools` names the built-ins and every MCP tool, which
# is what an agent inherits on the other targets.
PI_AGENT_TOOLS = (
    "read",
    "bash",
    "edit",
    "write",
    "grep",
    "find",
    "ls",
    "codemode",
    "tool_search",
    "mcp__*",
)
# The `permission:` entries each portable agent `deny` lowers to. Pi has no
# built-in web tool and an agent's allowlist above admits no extension tool,
# and the subagents package strips its own tools from every child, so web and
# delegate denials need no entry.
PI_AGENT_DENIES: dict[AgentTool, dict[str, str]] = {
    "shell": {"bash": "deny"},
    "edit": {"edit": "deny", "write": "deny"},
    "webfetch": {},
    "websearch": {},
    "delegate": {},
}


class PiSkillNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    description: str | None = None
    license: str | None = None
    metadata: dict[str, Any] | None = None
    compatibility: str | None = None


class PiCommandNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str | None = None
    argument_hint: str | None = Field(None, alias="argument-hint")


class PiAgentNative(StrictModel):
    """The @gotgenes/pi-subagents agent file, plus the permission system's
    `permission:` map."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str | None = None
    display_name: str | None = None
    tools: str | list[str] | None = None
    # `provider/modelId` or a fuzzy name.
    model: str | None = None
    thinking: str | None = None
    max_turns: int | None = Field(None, ge=0)
    prompt_mode: str | None = None
    inherit_context: bool | None = None
    run_in_background: bool | None = None
    enabled: bool | None = None
    locked: bool | list[str] | None = None
    permission: dict[str, Any] | None = None

    @field_validator("thinking")
    @classmethod
    def _validate_thinking(cls, value: str | None) -> str | None:
        return one_of("thinking", value, THINKING_LEVELS)

    @field_validator("prompt_mode")
    @classmethod
    def _validate_prompt_mode(cls, value: str | None) -> str | None:
        return one_of("prompt_mode", value, PROMPT_MODES)


def pi_skill(
    skill: SkillSource, root: Path
) -> tuple[tuple[OwnedTree, ...], list[Diagnostic]]:
    """The skill's tree below `root`, if its native block validates."""
    resolved, diagnostics = resolve("pi", [skill], PiSkillNative)
    if not resolved:
        return (), diagnostics
    meta, issues = frontmatter(
        skill.path,
        {
            "name": skill.name,
            "description": skill.description,
            **({"license": skill.license} if skill.license else {}),
            **({"metadata": skill.metadata} if skill.metadata else {}),
            **(
                {"disable-model-invocation": True}
                if skill.disable_model_invocation
                else {}
            ),
        },
        resolved[0].native,
        resolved[0].raw,
    )
    return (skill_tree(skill, root, meta),), [*diagnostics, *issues]


# The permission system strips leading assignments itself, decides a command
# behind an execution modifier (`time`, `timeout`, `nice`, `stdbuf`, `setsid`)
# as the command it runs, and raises one behind any other wrapper here to at
# least ask, so a guard needs no copy behind them: a deny there prompts instead.
# It does not know `command`, `builtin`, or `exec`, which keep their ask copies.
PI_PEELED = frozenset(
    prefix
    for wrapper in (
        "doas",
        "env",
        "flock",
        "nice",
        "nohup",
        "parallel",
        "setsid",
        "stdbuf",
        "sudo",
        "time",
        "timeout",
        "watch",
        "xargs",
    )
    for prefix in wrapper_prefixes(wrapper)
)


def shell_entries(permissions: PermissionSource) -> Iterator[tuple[str, str, str]]:
    """shell-unwrap's extension (`_unwrap_extension`) denies behind every
    wrapper, so a deny needs no wrapper copy. It never asks, so an ask keeps
    its copies behind the wrappers the permission system does not know."""
    for entry in bucket_entries(
        permissions,
        functools.partial(glob_variants, optional_trailing=True),
        PI_PEELED,
        wrapped_allows=True,
    ):
        bucket, origin, _ = entry
        if bucket != "deny" or origin in ("base", "option"):
            yield entry


# Pi runs extensions in Node; this one starts shell-unwrap's hook bundle, its
# Codex host, on every `bash` call and blocks the call when the hook denies or
# cannot run, so the guards fail closed. Pi discovers it by its directory.
UNWRAP_EXTENSION = Path(__file__).with_name("shell-unwrap") / "pi.js"
UNWRAP_ROOT = Path("extensions/shell-unwrap")


def _unwrap_extension(root: Path, permissions: PermissionSource) -> list[OwnedFile]:
    """The extension, the hook it runs, and their deny rules, or nothing when
    no command is denied."""
    directory = root / UNWRAP_ROOT
    rules = unwrap_rules_file(
        directory / "shell-unwrap.json",
        root,
        permissions,
        claude_shell_entries,
        asks=False,
    )
    if rules is None:
        return []
    return [
        OwnedFile(
            directory / "index.js",
            UNWRAP_EXTENSION.read_bytes(),
            root,
            structured=False,
        ),
        OwnedFile(
            directory / "shell-unwrap.js",
            UNWRAP_HOOK.read_bytes(),
            root,
            structured=False,
        ),
        rules,
    ]


def _bash_rules(permissions: PermissionSource) -> dict[str, Any]:
    """One ordered map, whose last matching pattern wins: the catch-all
    first, then allows, asks, and denies, each a deny's reason attached."""
    reasons = deny_reasons(permissions, shell_entries)
    rules: dict[str, Any] = {}

    def add(pattern: str, decision: Any) -> None:
        # A repeated pattern moves to its latest position.
        rules.pop(pattern, None)
        rules[pattern] = decision

    add("*", permissions.unmatched)
    buckets = bucket_patterns(shell_entries(permissions))
    for bucket in ("allow", "ask", "deny"):
        for pattern in buckets[bucket]:
            reason = reasons.get(pattern) if bucket == "deny" else None
            add(pattern, {"action": "deny", "reason": reason} if reason else bucket)
    for pattern in secret_name_variants(permissions.secret_names):
        add(pattern, "deny")
    return rules


def _directory_rules(permissions: PermissionSource) -> dict[str, str]:
    """A directory pattern matches only the directory itself, so each root
    also lands with the `/*` that reaches every file below it."""
    workspace = permissions.workspace
    rules = {"*": workspace.unmatched}
    for decision in ("allow", "ask", "deny"):
        for directory in getattr(workspace, decision):
            rules[directory] = decision
            rules[f"{directory}/*"] = decision
    return rules


def _permission_policy(
    permissions: PermissionSource | None,
    servers: list[tuple[McpServerSource, dict[str, Any]]],
) -> dict[str, Any]:
    """Each surface of the permission system's `permission` map.

    `*` keeps Pi's own default for every tool no portable field decides. A
    missing `bash` map would inherit it with a startup warning, so it is
    always written.
    """
    policy: dict[str, Any] = {"*": "allow", "bash": {"*": "allow"}}
    if permissions:
        policy["bash"] = _bash_rules(permissions)
        for tool, surfaces in PI_TOOL_SURFACES.items():
            if decision := permissions.tools.get(tool):
                policy.update(dict.fromkeys(surfaces, decision))
        # `path` gates every file tool and every path a command names.
        if denied := denied_paths(permissions):
            policy["path"] = {"*": "allow", **dict.fromkeys(denied, "deny")}
        policy["external_directory"] = _directory_rules(permissions)
    if mcp := {
        f"{server}:{tool}": decision
        for server, tool, decision in mcp_tool_decisions(servers)
    }:
        policy["mcp"] = mcp
    return policy


def _pi_server(server: McpServerSource, fields: dict[str, Any]) -> dict[str, Any]:
    """Pi's `mcpServers` entry, the Claude shape plus the description Pi lists
    the server under, and `enabled: false` for one that starts off."""
    return {
        **fields,
        **({"description": server.description} if server.description else {}),
        **({} if server.enabled else {"enabled": False}),
    }


def _validate_permission_patch(patch: NativePatch) -> None:
    for operation in patch.operations:
        # Command policy has one writer.
        if touches(operation.pointer, ("permission", "bash")):
            pointer = display_pointer(operation.pointer)
            raise PatchError(f"pi-permissions patch cannot touch {pointer}")


def compile_pi(ctx: SyncContext, sources: SourceBundle) -> Plan:
    root = ctx.pi
    sources = with_skill_scripts(sources, "pi", root, ctx.home)
    settings = root / "settings.json"
    mcp_config = root / "mcp.json"
    permission_config = root / PERMISSION_CONFIG
    autoformat_config = root / AUTOFORMAT_CONFIG
    files: list[OwnedFile] = []
    trees = declared_trees(
        root, (("skills", "dir"), ("prompts", "file"), ("agents", "file"))
    )
    diagnostics: list[Diagnostic] = []
    globals_, issues = resolve("pi", sources.globals[:1])
    diagnostics.extend(issues)
    # Pi reads one context file per directory and has no rules directory, so
    # rules follow the global instructions in it.
    rules_, issues = resolve("pi", sources.rules, raw=False)
    diagnostics.extend(issues)
    if parts := [item.source.body.rstrip() for item in (*globals_, *rules_)]:
        files.append(
            OwnedFile(root / "AGENTS.md", ("\n\n".join(parts) + "\n").encode(), root)
        )
    has_shared_skills = False
    for skill in sources.skills:
        if not applies_to(skill, "pi"):
            continue
        if shared_skill(skill):
            has_shared_skills = True
            continue
        tree, issues = pi_skill(skill, root / "skills")
        diagnostics.extend(issues)
        trees.extend(tree)
    commands, issues = resolve("pi", sources.commands, PiCommandNative)
    diagnostics.extend(issues)
    for resolved in commands:
        command = resolved.source
        meta, issues = frontmatter(
            command.path,
            {"description": command.description},
            resolved.native,
            resolved.raw,
        )
        diagnostics.extend(issues)
        files.append(
            OwnedFile(
                root / "prompts" / command.path.name,
                markdown(meta, command.body),
                root / "prompts",
            )
        )
    agents, issues = resolve("pi", sources.agents, PiAgentNative)
    diagnostics.extend(issues)
    agent_denies = False
    for resolved in agents:
        agent, native = resolved.source, resolved.native
        thinking = agent.effort
        if (policy := resolve_model_policy(sources, agent, "pi")) is not None:
            owned: set[str] = {"model", "thinking"} & (
                set(resolved.block.native) | set(resolved.raw)
            )
            if agent.effort:
                owned.add("effort")
            if conflicts := sorted(owned):
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "model policy owns Pi fields: " + ", ".join(conflicts),
                        agent.path,
                    )
                )
            native = native.model_copy(update={"model": policy.model})
            thinking = policy.effort
        if agent.deny:
            agent_denies = True
            denied = {
                surface: decision
                for entry in agent.deny
                for surface, decision in PI_AGENT_DENIES[entry].items()
            }
            native = native.model_copy(
                update={"permission": {**(native.permission or {}), **denied}}
            )
        agent_meta: dict[str, Any] = {"description": agent.description}
        if native.tools is None:
            agent_meta["tools"] = list(PI_AGENT_TOOLS)
        if thinking:
            agent_meta["thinking"] = thinking
        if agent.background:
            agent_meta["run_in_background"] = True
        meta, issues = frontmatter(agent.path, agent_meta, native, resolved.raw)
        diagnostics.extend(issues)
        files.append(
            OwnedFile(
                root / "agents" / agent.path.name,
                markdown(meta, agent.body),
                root / "agents",
            )
        )
    permissions = sources.permissions
    if permissions:
        _, issues = resolve("pi", [permissions])
        diagnostics.extend(issues)
        files.extend(_unwrap_extension(root, permissions))
    servers, issues = mcp_servers(sources, "pi")
    diagnostics.extend(issues)
    gated = bool(
        permissions or agent_denies or any(server.tools for server, _ in servers)
    )
    packages = [
        *([PERMISSION_PACKAGE] if gated else []),
        *([SUBAGENTS_PACKAGE] if agents else []),
    ]
    native_values = [
        *(
            NativeValue("pi-mcp", mcp_config, ("mcpServers", server.id, key), value)
            for server, fields in servers
            for key, value in _pi_server(server, fields).items()
        ),
        *((NativeValue("pi", settings, ("packages",), packages),) if packages else ()),
    ]
    if gated:
        native_values.extend(
            NativeValue(
                "pi-permissions", permission_config, ("permission", surface), rules
            )
            for surface, rules in _permission_policy(permissions, servers).items()
        )
        # The permission system auto-allows reads below Pi's own directories
        # only, so the shared skills directory joins them.
        if has_shared_skills:
            shared = (
                f"~/{shared_skills_root(ctx.home).relative_to(ctx.home).as_posix()}"
            )
            native_values.append(
                NativeValue(
                    "pi-permissions",
                    permission_config,
                    ("piInfrastructureReadPaths",),
                    [shared],
                )
            )
    patches: list[NativePatch] = []
    for target, path, name in (
        ("pi", settings, "pi-settings"),
        ("pi-mcp", mcp_config, "pi-mcp"),
        ("pi-permissions", permission_config, "pi-permissions"),
        ("pi-autoformat", autoformat_config, "pi-autoformat"),
    ):
        patch, issues = native_patch(
            config_root=ctx.config_root, target=target, path=path, patch_name=name
        )
        diagnostics.extend(issues)
        patches.append(patch)
    _validate_permission_patch(patches[2])
    raw, raw_tree, raw_issues = raw_files(
        config_root=ctx.config_root,
        target="pi",
        root=root,
        excluded=(
            Path("settings.json"),
            Path("mcp.json"),
            PERMISSION_CONFIG,
            AUTOFORMAT_CONFIG,
        ),
    )
    return Plan(
        tuple((*files, *raw)),
        tuple((*trees, raw_tree)),
        tuple(native_values),
        tuple(patches),
        tuple((*diagnostics, *raw_issues)),
    )
