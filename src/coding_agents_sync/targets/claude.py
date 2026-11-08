import json
import re
import shlex
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from pydantic import (
    AliasChoices,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from ..ir import resolve
from ..models import SyncContext
from ..output.patches import (
    Operation,
    Patch,
    PatchError,
    apply_operations,
    contributes_to_generated,
    display_pointer,
    touches,
)
from ..output.plan import (
    Diagnostic,
    NativePatch,
    NativeValue,
    OwnedFile,
    OwnedTree,
    Plan,
)
from ..sources import (
    EFFORT_LEVELS,
    AgentSource,
    AgentTool,
    Decision,
    McpServerSource,
    PermissionSource,
    PermissionTool,
    SourceBundle,
    StrictModel,
    resolve_model_policy,
)
from .permissions import (
    CLAUDE_PEELED_PREFIXES,
    CLAUDE_TOOL_PATTERNS,
    UNWRAP_HOOK,
    UNWRAP_HOOK_PATH,
    UNWRAP_TIMEOUT_SECONDS,
    bucket_entries,
    bucket_patterns,
    claude_path,
    denied_paths,
    fold_edit_write,
    glob_variants,
    literal_directories,
    secret_name_variants,
    tool_patterns,
    unwrap_command,
    unwrap_reasons_file,
)
from .support import (
    collision_error,
    declared_trees,
    frontmatter,
    markdown,
    mcp_servers,
    mcp_tool_decisions,
    native_patch,
    one_of,
    raw_files,
    skill_collisions,
    skill_tree,
    with_skill_scripts,
)


class ClaudeSkillNative(StrictModel):
    name: str | None = None
    description: str | None = None
    paths: list[str] | None = None
    disable_model_invocation: bool | None = Field(
        None,
        serialization_alias="disable-model-invocation",
        validation_alias=AliasChoices(
            "disable-model-invocation", "disable_model_invocation"
        ),
    )
    when_to_use: str | None = None
    argument_hint: str | None = Field(None, alias="argument-hint")
    arguments: list[str] | None = None
    user_invocable: bool | None = Field(None, alias="user-invocable")
    allowed_tools: list[str] | None = Field(None, alias="allowed-tools")
    disallowed_tools: list[str] | None = Field(None, alias="disallowed-tools")
    model: str | None = None
    effort: str | None = None
    context: str | None = None
    agent: str | None = None
    # Only with `context: fork`: the fork reports back as a task notification.
    background: bool | None = None
    # Same shape as settings.json `hooks`, registered while the skill is active.
    hooks: dict[str, Any] | None = None
    shell: str | None = None
    license: str | None = None
    metadata: dict[str, Any] | None = None


CLAUDE_MCP_WILDCARD = "mcp__*"
CLAUDE_AGENT_TOOL = re.compile(
    r"[A-Z][A-Za-z0-9]*|mcp__[\w.-]+(?:__(?:[\w.-]+|\*))?", re.ASCII
)
# Closed vocabularies of Claude's agent frontmatter schema. `model` is
# excluded: it also accepts any full model ID, so it has no closed set.
CLAUDE_AGENT_VOCABULARIES = {
    "permission_mode": frozenset(
        {
            "default",
            "acceptEdits",
            "auto",
            "dontAsk",
            "bypassPermissions",
            "plan",
            "manual",
        }
    ),
    "memory": frozenset({"user", "project", "local"}),
    "isolation": frozenset({"worktree", "remote"}),
    "effort": EFFORT_LEVELS,
}


def _tool_entries(value: str | list[str]) -> list[str]:
    """Claude accepts a comma-separated string or a list in either tool field."""
    items = value.split(",") if isinstance(value, str) else value
    return [item.strip() for item in items]


class ClaudeAgentNative(StrictModel):
    name: str | None = None
    description: str | None = None
    tools: str | list[str] | None = None
    disallowed_tools: str | list[str] | None = Field(None, alias="disallowedTools")
    effort: str | None = None
    background: bool | None = None
    color: str | None = None
    model: str | None = None
    permission_mode: str | None = Field(None, alias="permissionMode")
    max_turns: int | None = Field(None, alias="maxTurns")
    isolation: str | None = None
    memory: str | None = None
    initial_prompt: str | None = Field(None, alias="initialPrompt")
    skills: list[str] | None = None
    # Server names from the user's config, or inline server definitions.
    mcp_servers: list[str | dict[str, Any]] | None = Field(None, alias="mcpServers")
    hooks: dict[str, Any] | None = None
    auto_compact_window: int | None = Field(None, alias="autoCompactWindow", gt=0)
    omit_claude_md: bool | None = Field(None, alias="omitClaudeMd")
    # An agent type spawned beside this one to watch its activity.
    observer: str | None = None
    observer_message: str | None = Field(None, alias="observerMessage")
    observe_subagents: bool | None = Field(None, alias="observeSubagents")

    @field_validator("tools", "disallowed_tools")
    @classmethod
    def _validate_tool_names(
        cls, value: str | list[str] | None, info: ValidationInfo
    ) -> str | list[str] | None:
        if value is None:
            return value
        denylist = info.field_name == "disallowed_tools"
        entries = _tool_entries(value)
        if not denylist and not entries:
            raise ValueError("tools must not be empty; omit it to inherit every tool")
        for entry in entries:
            if entry == "inherit":
                raise ValueError(
                    "tools does not accept `inherit`; omit the field to inherit "
                    "every tool available to subagents (`inherit` is a `model` value)"
                )
            if entry == CLAUDE_MCP_WILDCARD and denylist:
                continue
            if not CLAUDE_AGENT_TOOL.fullmatch(entry):
                raise ValueError(
                    f"{info.field_name} entry {entry!r} is not a tool name; expected "
                    f"an exact name such as `Read`, `mcp__<server>`, or "
                    f"`mcp__<server>__*`"
                    + (f", or `{CLAUDE_MCP_WILDCARD}`" if denylist else "")
                )
        return value

    @field_validator("permission_mode", "memory", "isolation", "effort")
    @classmethod
    def _validate_vocabularies(
        cls, value: str | None, info: ValidationInfo
    ) -> str | None:
        field = str(info.field_name)
        return one_of(field, value, CLAUDE_AGENT_VOCABULARIES[field])

    @model_validator(mode="after")
    def _validate_tools_resolve(self) -> ClaudeAgentNative:
        if self.tools is None:
            return self
        denied = set(_tool_entries(self.disallowed_tools or []))
        if denied and not set(_tool_entries(self.tools)) - denied:
            raise ValueError(
                "every tools entry is also in disallowedTools, so the agent would "
                "resolve to zero tools and fail to launch"
            )
        return self


# The Claude tools each portable agent `deny` entry removes.
CLAUDE_AGENT_TOOLS: dict[AgentTool, tuple[str, ...]] = {
    "shell": ("Bash",),
    "edit": ("Edit", "Write", "NotebookEdit"),
    "webfetch": ("WebFetch",),
    "websearch": ("WebSearch",),
    "delegate": ("Agent",),
}


def _agent_meta(
    agent: AgentSource,
    native: ClaudeAgentNative,
    policy: object | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": agent.name, "description": agent.description}
    if native.tools is not None:
        meta["tools"] = native.tools
    disallowed = [tool for entry in agent.deny for tool in CLAUDE_AGENT_TOOLS[entry]]
    if native.disallowed_tools is not None:
        disallowed += _tool_entries(native.disallowed_tools)
    if disallowed:
        meta["disallowedTools"] = list(dict.fromkeys(disallowed))
    if agent.effort and policy is None:
        meta["effort"] = agent.effort
    if agent.background:
        meta["background"] = True
    if agent.color:
        meta["color"] = agent.color
    return meta


def shell_entries(permissions: PermissionSource) -> Iterator[tuple[str, str, str]]:
    return bucket_entries(permissions, glob_variants, CLAUDE_PEELED_PREFIXES)


# Claude matches an allow only against a flat command, peels `xargs` only bare,
# and never shows the model a deny rule's reason. The bundled PreToolUse hook
# allows a command when every command inside it matches an allow, and denies or
# asks for one that any peeled spelling of a deny or ask matches, with the
# deny's reason; Claude still applies its own deny and ask rules after a hook
# allow. When it cannot run, it asks, since the rules leave `xargs` with options
# to it.


# Printed when Bun cannot run the hook. The deny rules leave `xargs` with
# options to the hook, so its absence must not pass a command silently, except
# in bypass permissions mode, which still shows a hook's ask.
UNWRAP_UNAVAILABLE = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": (
            "shell-unwrap could not run, so its guards were not checked"
        ),
    }
}


def _unwrap_hook(script: Path) -> dict[str, Any]:
    directory = shlex.quote(script.parent.as_posix())
    command = unwrap_command(
        directory, "claude", UNWRAP_UNAVAILABLE, silent_in_bypass=True
    )
    return {
        "matcher": "Bash",
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": UNWRAP_TIMEOUT_SECONDS,
            }
        ],
    }


def _permission_values(
    sources: SourceBundle,
    tools: Mapping[PermissionTool, Decision],
    mcp: Mapping[Decision, list[str]],
) -> tuple[tuple[tuple[str, ...], Any], ...]:
    if not (permissions := sources.permissions):
        return tuple(
            (("permissions", decision), rules)
            for decision, rules in mcp.items()
            if rules
        )
    buckets = bucket_patterns(shell_entries(permissions))
    workspace = permissions.workspace
    # Claude has no unmatched-command setting, so an allow lands as the bare
    # tool rule; the deny rules still outrank it.
    allow = [
        *(["Bash"] if permissions.unmatched == "allow" else []),
        *(f"Bash({pattern})" for pattern in buckets["allow"]),
        *tool_patterns(tools, CLAUDE_TOOL_PATTERNS, "allow"),
        *mcp["allow"],
    ]
    # `Read(**)` and `Edit(**)` anchor at the cwd. Reads in the extra
    # directories need no rule; edits there, and anything elsewhere once
    # `workspace.unmatched` allows it, do.
    if tools.get("read") == "allow" and workspace.unmatched == "allow":
        allow.append("Read(//**)")
    if tools.get("edit") == "allow":
        allow.extend(
            ["Edit(//**)"]
            if workspace.unmatched == "allow"
            else [f"Edit({claude_path(directory)}/**)" for directory in workspace.allow]
        )
    deny = [
        f"Bash({pattern})"
        for pattern in (
            *buckets["deny"],
            *secret_name_variants(permissions.secret_names),
        )
    ] + [
        f"{operation}({claude_path(path)})"
        for path in denied_paths(permissions)
        for operation in ("Read", "Edit")
    ]
    values: list[tuple[tuple[str, ...], Any]] = [
        (("permissions", "allow"), allow),
        (
            ("permissions", "ask"),
            [f"Bash({pattern})" for pattern in buckets["ask"]]
            + list(tool_patterns(tools, CLAUDE_TOOL_PATTERNS, "ask"))
            # An ask directory stays out of additionalDirectories; its ask rules
            # outrank the broader `Read(//**)` and `Edit(//**)` allows.
            + [
                f"{operation}({claude_path(directory)}/**)"
                for directory in workspace.ask
                for operation in ("Read", "Edit")
            ]
            + mcp["ask"],
        ),
    ]
    deny += [*tool_patterns(tools, CLAUDE_TOOL_PATTERNS, "deny"), *mcp["deny"]]
    if deny:
        values.append((("permissions", "deny"), deny))
    if directories := literal_directories(permissions.workspace):
        values.append((("permissions", "additionalDirectories"), directories))
    # `unmatched: sandbox` turns the sandbox on and lets a sandboxed command run
    # unasked; leaving the sandbox goes through the rules above, so only an
    # allow rule lets a command out without a prompt.
    if permissions.unmatched == "sandbox":
        values += [
            (("sandbox", "enabled"), True),
            (("sandbox", "autoAllowBashIfSandboxed"), True),
        ]
    # The deny above only stops a command that names the variable; the sandbox
    # also unsets it, so `env` cannot print it either.
    if permissions.secret_names:
        values.append(
            (
                ("sandbox", "credentials", "envVars"),
                [{"name": name, "mode": "deny"} for name in permissions.secret_names],
            )
        )
    return tuple(values)


def _claude_server(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "http" if "url" in fields else "stdio", **fields}


def _optional_servers(
    patch: NativePatch,
    servers: list[tuple[McpServerSource, dict[str, Any]]],
    root: Path,
) -> tuple[NativePatch, list[OwnedFile], list[Diagnostic]]:
    """Each disabled server as `<root>/mcp/<id>.json`, which a session loads
    with `claude --mcp-config`; Claude Code has no user-level off switch, only
    a per-project one.

    The file keeps the `mcpServers/<id>` shape of `.claude.json`, so a
    `claude-mcp` patch operation inside that entry, such as an auth header,
    applies to the file instead.
    """
    ids = {server.id for server, _ in servers}

    def owner(pointer: tuple[str, ...]) -> str | None:
        match pointer:
            case ("mcpServers", server_id, _, *_) if server_id in ids:
                return server_id
            case _:
                return None

    routed: dict[str, list[Operation]] = {server_id: [] for server_id in ids}
    kept: list[Operation] = []
    for operation in patch.operations:
        server_id = owner(operation.pointer)
        (kept if server_id is None else routed[server_id]).append(operation)
    files: list[OwnedFile] = []
    diagnostics: list[Diagnostic] = []
    for server, fields in servers:
        entry = _claude_server(fields)
        document: dict[str, Any] = {"mcpServers": {server.id: entry}}
        operations = tuple(routed[server.id])
        # The rule the writer applies to `.claude.json` (`writer._validate_plan`).
        overlapping = sorted(
            display_pointer(operation.pointer)
            for operation in operations
            for key, value in entry.items()
            if touches(operation.pointer, pointer := ("mcpServers", server.id, key))
            and not contributes_to_generated(operation, pointer, value)
        )
        if overlapping:
            diagnostics.append(
                Diagnostic(
                    "error",
                    f"Overlapping native pointers: {', '.join(overlapping)}",
                    patch.source,
                )
            )
            continue
        try:
            apply_operations(document, Patch(operations))
        except PatchError as exc:
            diagnostics.append(Diagnostic("error", str(exc), patch.source))
            continue
        files.append(
            OwnedFile(
                root / "mcp" / f"{server.id}.json",
                (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode(),
                root / "mcp",
            )
        )
    # Deleting the whole entry also clears what a patch wrote there while the
    # server was registered, which would otherwise be left as a broken stub.
    kept.extend(
        Operation("delete", ("mcpServers", server_id)) for server_id in sorted(ids)
    )
    return (
        NativePatch(patch.target, patch.path, tuple(kept), patch.source),
        files,
        diagnostics,
    )


def compile_claude(ctx: SyncContext, sources: SourceBundle) -> Plan:
    root = ctx.claude
    sources = with_skill_scripts(sources, "claude", root, ctx.home)
    files: list[OwnedFile] = []
    trees = declared_trees(
        root,
        (("rules", "file"), ("skills", "dir"), ("agents", "file"), ("mcp", "file")),
    )
    diagnostics: list[Diagnostic] = []
    globals_, issues = resolve("claude", sources.globals[:1])
    diagnostics.extend(issues)
    for global_ in globals_:
        files.append(
            OwnedFile(
                root / "CLAUDE.md",
                (global_.source.body.rstrip() + "\n").encode(),
                root,
            )
        )
    rules, issues = resolve("claude", sources.rules)
    diagnostics.extend(issues)
    for rule in rules:
        files.append(
            OwnedFile(
                root / "rules" / f"{rule.source.stem}.md",
                (rule.source.body.rstrip() + "\n").encode(),
                root / "rules",
            )
        )
    skills, issues = resolve("claude", sources.skills, ClaudeSkillNative)
    diagnostics.extend(issues)
    for resolved in skills:
        skill = resolved.source
        meta, issues = frontmatter(
            skill.path,
            {
                "name": skill.name,
                "description": skill.description,
                **({"license": skill.license} if skill.license else {}),
                **({"metadata": skill.metadata} if skill.metadata else {}),
                **({"paths": skill.paths} if skill.paths else {}),
                **(
                    {"disable-model-invocation": True}
                    if skill.disable_model_invocation
                    else {}
                ),
            },
            resolved.native,
            resolved.raw,
        )
        diagnostics.extend(issues)
        trees.append(skill_tree(skill, root / "skills", meta))
    collisions = skill_collisions(sources, "claude")
    # Claude merged commands into skills: a skill answers the same `/name` and
    # takes the same frontmatter.
    commands, issues = resolve("claude", sources.commands, ClaudeSkillNative)
    diagnostics.extend(issues)
    for resolved in commands:
        command = resolved.source
        if command.stem in collisions:
            diagnostics.append(collision_error(command))
            continue
        canonical = {"name": command.stem, "description": command.description}
        if command.execution.agent:
            canonical["agent"] = command.execution.agent
        if command.execution.subtask:
            canonical["context"] = "fork"
        meta, issues = frontmatter(
            command.path, canonical, resolved.native, resolved.raw
        )
        diagnostics.extend(issues)
        trees.append(
            OwnedTree(
                root / "skills" / command.stem,
                ((Path("SKILL.md"), markdown(meta, command.body)),),
                root / "skills",
            )
        )
    agents, issues = resolve("claude", sources.agents, ClaudeAgentNative)
    diagnostics.extend(issues)
    for resolved in agents:
        agent, native = resolved.source, resolved.native
        policy = resolve_model_policy(sources, agent, "claude")
        if policy is not None:
            conflicts = {
                key
                for key in ("model", "effort")
                if key in resolved.block.native or key in resolved.raw
            }
            if agent.effort:
                conflicts.add("effort")
            if conflicts:
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "model policy owns Claude fields: "
                        + ", ".join(sorted(conflicts)),
                        agent.path,
                    )
                )
            native = native.model_copy(
                update={
                    "model": policy.model or "inherit",
                    "effort": policy.effort,
                }
            )
        meta, issues = frontmatter(
            agent.path,
            _agent_meta(agent, native, policy),
            # The portable deny list and the native one land as one list.
            native.model_copy(update={"disallowed_tools": None}),
            resolved.raw,
        )
        diagnostics.extend(issues)
        files.append(
            OwnedFile(
                root / "agents" / agent.path.name,
                markdown(meta, agent.body),
                root / "agents",
            )
        )
    folded = fold_edit_write(
        sources.permissions.tools if sources.permissions else {}, "Claude"
    )
    hook_values: list[NativeValue] = []
    if permissions := sources.permissions:
        _, issues = resolve("claude", [permissions])
        diagnostics.extend(issues)
        if note := folded[1]:
            diagnostics.append(Diagnostic("warning", note, permissions.paths[0]))
        script = root / UNWRAP_HOOK_PATH
        files.append(OwnedFile(script, UNWRAP_HOOK.read_bytes(), root))
        files.extend(unwrap_reasons_file(root, permissions, shell_entries))
        hook_values.append(
            NativeValue(
                "claude",
                root / "settings.json",
                ("hooks", "PreToolUse"),
                [_unwrap_hook(script)],
            )
        )
    servers, issues = mcp_servers(sources, "claude")
    diagnostics.extend(issues)
    mcp_rules: dict[Decision, list[str]] = {"allow": [], "ask": [], "deny": []}
    # A disabled server's rules stay, so a session that loads it keeps them.
    for server, tool, decision in mcp_tool_decisions(servers):
        mcp_rules[decision].append(f"mcp__{server}__{tool}")
    registered = [(server, fields) for server, fields in servers if server.enabled]
    optional = [(server, fields) for server, fields in servers if not server.enabled]
    permission_values = (
        *(
            NativeValue("claude", root / "settings.json", pointer, value)
            for pointer, value in _permission_values(sources, folded[0], mcp_rules)
        ),
        *hook_values,
        *(
            NativeValue(
                "claude-mcp",
                ctx.home / ".claude.json",
                ("mcpServers", server.id, key),
                value,
            )
            for server, fields in registered
            for key, value in _claude_server(fields).items()
        ),
    )
    patches = []
    for target, path, patch_name in (
        ("claude", root / "settings.json", "claude-settings"),
        ("claude-mcp", ctx.home / ".claude.json", "claude-mcp"),
    ):
        patch, issues = native_patch(
            config_root=ctx.config_root, target=target, path=path, patch_name=patch_name
        )
        if target == "claude-mcp" and optional:
            patch, optional_files, issues_ = _optional_servers(patch, optional, root)
            files.extend(optional_files)
            diagnostics.extend(issues_)
        patches.append(patch)
        diagnostics.extend(issues)
    raw, raw_tree, issues = raw_files(
        config_root=ctx.config_root,
        target="claude",
        root=root,
        excluded=(Path("settings.json"),),
    )
    return Plan(
        tuple((*files, *raw)),
        tuple((*trees, raw_tree)),
        permission_values,
        tuple(patches),
        tuple(diagnostics + list(issues)),
    )
