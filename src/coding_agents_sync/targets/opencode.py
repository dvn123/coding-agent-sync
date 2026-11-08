import fnmatch
import functools
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, field_validator

from ..ir import applies_to, resolve
from ..models import SyncContext
from ..output.patches import (
    PatchError,
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
    AgentTool,
    Decision,
    McpServerSource,
    PermissionSource,
    PermissionTool,
    SkillSource,
    SourceBundle,
    StrictModel,
    resolve_model_policy,
)
from .permissions import (
    bucket_entries,
    bucket_patterns,
    denied_paths,
    external_directory_rules,
    fold_edit_write,
    glob_variants,
    opencode_paths,
    secret_name_variants,
    unwrap_reasons_file,
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
    skill_tree,
    with_skill_scripts,
)


class OpenCodeSkillNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    description: str | None = None
    license: str | None = None
    metadata: dict[str, Any] | None = None
    compatibility: str | None = None


class OpenCodeCommandNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    description: str | None = None
    agent: str | None = None
    subagent: bool | None = None
    model: str | None = None


# Agent frontmatter holds only the native ConfigAgent.Info fields
# (packages/schema/src/config/agent.ts); any other key sends the whole file
# through OpenCode's v1 migration. `mode` is a closed set, and `color` is hex
# only: the migration turns anything else into #aaaaaa.
OPENCODE_AGENT_MODES = frozenset({"subagent", "primary", "all"})
OPENCODE_HEX_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
# Claude's agent color names, so one portable color serves both targets.
PORTABLE_COLORS = {
    "red": "#ef4444",
    "orange": "#f97316",
    "yellow": "#eab308",
    "green": "#22c55e",
    "cyan": "#06b6d4",
    "blue": "#3b82f6",
    "purple": "#a855f7",
    "pink": "#ec4899",
}


def _opencode_color(value: object) -> bool:
    return isinstance(value, str) and bool(OPENCODE_HEX_COLOR.fullmatch(value))


class OpenCodeRule(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: str
    resource: str = "*"
    effect: Literal["allow", "ask", "deny"]


# The OpenCode permission action each portable agent `deny` entry denies.
OPENCODE_AGENT_TOOLS: dict[AgentTool, str] = {
    "shell": "shell",
    "edit": "edit",
    "webfetch": "webfetch",
    "websearch": "websearch",
    "delegate": "subagent",
}


class OpenCodeAgentNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str | None = None
    # `provider/model`, optionally `#variant`.
    model: str | None = None
    # Provider request extras: sampling and `reasoningEffort` go in `body`.
    request: dict[str, Any] | None = None
    mode: str | None = None
    hidden: bool | None = None
    color: str | None = None
    steps: int | None = None
    disabled: bool | None = None
    # Appended after the global rules, so an agent's rules win.
    permissions: list[OpenCodeRule] | None = None

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, value: str | None) -> str | None:
        return one_of("mode", value, OPENCODE_AGENT_MODES)


def opencode_skill(
    skill: SkillSource, root: Path
) -> tuple[tuple[OwnedTree, ...], list[Diagnostic]]:
    """The skill's tree below `root`, if its native block validates."""
    resolved, diagnostics = resolve("opencode", [skill], OpenCodeSkillNative)
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


# OpenCode matches a shell rule against a command's raw text. shell-unwrap's
# bundled OpenCode plugin denies or asks for a command when the rules deny or
# ask for it with its assignments, program path, or these wrappers peeled, so
# guard copies behind them are dead; a declared wrapper outside this list keeps
# its copies.
UNWRAP_PLUGIN = Path(__file__).with_name("shell-unwrap") / "opencode.js"
UNWRAP_PLUGIN_NAME = "shell-unwrap.js"
UNWRAP_PEELED = frozenset(
    prefix
    for wrapper in (
        "builtin",
        "command",
        "doas",
        "env",
        "exec",
        "nice",
        "nocorrect",
        "noglob",
        "nohup",
        "stdbuf",
        "sudo",
        "time",
        "timeout",
        "xargs",
    )
    for prefix in wrapper_prefixes(wrapper)
)


def shell_entries(permissions: PermissionSource) -> Iterator[tuple[str, str, str]]:
    return bucket_entries(
        permissions,
        functools.partial(glob_variants, optional_trailing=True),
        UNWRAP_PEELED,
    )


def _opencode_server(fields: dict[str, Any]) -> dict[str, Any]:
    """OpenCode's native server shape: one command array, `environment`."""
    if "url" in fields:
        return {"type": "remote", "url": fields["url"]}
    return {
        "type": "local",
        "command": [fields["command"], *fields.get("args", ())],
        **({"environment": fields["env"]} if "env" in fields else {}),
    }


def _permissions(
    sources: SourceBundle,
    config: Path,
    tools: Mapping[PermissionTool, Decision],
    servers: list[tuple[McpServerSource, dict[str, Any]]],
) -> tuple[NativeValue, ...]:
    # An MCP tool's action is `<server>_<tool>`, apart from every built-in one.
    mcp = [
        {"action": f"{server}_{tool}", "resource": "*", "effect": decision}
        for server, tool, decision in mcp_tool_decisions(servers)
    ]
    if not (permissions := sources.permissions):
        return (NativeValue("opencode", config, ("permissions",), mcp),) if mcp else ()
    buckets = bucket_patterns(shell_entries(permissions))
    denied = [
        variant
        for path in denied_paths(permissions)
        for variant in opencode_paths(path)
    ]
    rules: list[dict[str, str]] = []

    def add(action: str, effect: str, resources: Iterable[str]) -> None:
        rules.extend(
            {"action": action, "resource": resource, "effect": effect}
            for resource in resources
        )

    # OpenCode applies the last matching rule, so each action's catch-all
    # precedes its narrower rules, and every guard follows every allow.
    add("shell", permissions.unsandboxed, ["*"])
    for bucket in ("allow", "ask", "deny"):
        add("shell", bucket, buckets[bucket])
    add("shell", "deny", secret_name_variants(permissions.secret_names))
    # `edit` also gates OpenCode's write and patch tools.
    for tool in ("read", "edit"):
        if decision := tools.get(tool):
            add(tool, decision, ["*"])
        add(tool, "deny", denied)
    for tool in ("webfetch", "websearch"):
        if decision := permissions.tools.get(tool):
            add(tool, decision, ["*"])
    for effect, resources in external_directory_rules(permissions.workspace):
        add("external_directory", effect, resources)
    return (NativeValue("opencode", config, ("permissions",), rules + mcp),)


def _validate_patches(patches: list[NativePatch]) -> None:
    for patch in patches:
        for operation in patch.operations:
            # OpenCode applies the V1 `/permission` map ahead of the native
            # list, so no patch may write it. Contributions append after every
            # generated rule; command policy has one writer, so none of them
            # may be a shell rule.
            if touches(operation.pointer, ("permission",)):
                pointer = display_pointer(operation.pointer)
                raise PatchError(
                    f"opencode patch cannot touch {pointer}; use /permissions"
                )
            elif touches(operation.pointer, ("permissions",)) and not (
                operation.kind == "extend"
                and operation.pointer == ("permissions",)
                and all(
                    not fnmatch.fnmatchcase("shell", rule.get("action", ""))
                    for rule in operation.value
                )
            ):
                raise PatchError(
                    "opencode patch may only extend /permissions, and never "
                    "with shell rules"
                )


def compile_opencode(ctx: SyncContext, sources: SourceBundle) -> Plan:
    root = ctx.opencode
    sources = with_skill_scripts(sources, "opencode", root, ctx.home)
    config = root / "opencode.json"
    files: list[OwnedFile] = []
    trees = declared_trees(
        root,
        (
            ("rules", "file"),
            ("skills", "dir"),
            ("commands", "file"),
            ("agents", "file"),
        ),
    )
    diagnostics: list[Diagnostic] = []
    globals_, issues = resolve("opencode", sources.globals[:1])
    diagnostics.extend(issues)
    rules_, issues = resolve("opencode", sources.rules)
    diagnostics.extend(issues)
    files.extend(
        OwnedFile(
            root / "AGENTS.md", (global_.source.body.rstrip() + "\n").encode(), root
        )
        for global_ in globals_
    )
    files.extend(
        OwnedFile(
            root / "rules" / f"{rule.source.stem}.md",
            (rule.source.body.rstrip() + "\n").encode(),
            root / "rules",
        )
        for rule in rules_
    )
    for skill in sources.skills:
        if applies_to(skill, "opencode") and not shared_skill(skill):
            tree, issues = opencode_skill(skill, root / "skills")
            diagnostics.extend(issues)
            trees.extend(tree)
    commands, issues = resolve("opencode", sources.commands, OpenCodeCommandNative)
    diagnostics.extend(issues)
    for resolved in commands:
        command = resolved.source
        command_meta: dict[str, Any] = {"description": command.description}
        if command.execution.agent:
            command_meta["agent"] = command.execution.agent
        if command.execution.subtask:
            command_meta["subagent"] = True
        meta, issues = frontmatter(
            command.path, command_meta, resolved.native, resolved.raw
        )
        diagnostics.extend(issues)
        files.append(
            OwnedFile(
                root / "commands" / command.path.name,
                markdown(meta, command.body),
                root / "commands",
            )
        )
    agents, issues = resolve("opencode", sources.agents, OpenCodeAgentNative)
    diagnostics.extend(issues)
    for resolved in agents:
        agent, native = resolved.source, resolved.native
        policy = resolve_model_policy(sources, agent, "opencode")
        effort = agent.effort
        if policy is not None:
            native_body = (resolved.block.native.get("request") or {}).get("body") or {}
            conflicts = {
                *({"model"} & (set(resolved.block.native) | set(resolved.raw))),
                *(
                    {"request.body.reasoningEffort"}
                    if "reasoningEffort" in native_body or agent.effort
                    else set()
                ),
            }
            if conflicts:
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "model policy owns OpenCode fields: "
                        + ", ".join(sorted(conflicts)),
                        agent.path,
                    )
                )
            native = native.model_copy(update={"model": policy.model})
            effort = policy.effort
        if agent.deny:
            denied = [
                OpenCodeRule(action=OPENCODE_AGENT_TOOLS[entry], effect="deny")
                for entry in agent.deny
            ]
            native = native.model_copy(
                update={"permissions": [*denied, *(native.permissions or [])]}
            )
        agent_meta: dict[str, Any] = {"description": agent.description}
        if agent.color:
            agent_meta["color"] = PORTABLE_COLORS.get(agent.color, agent.color)
        meta, issues = frontmatter(agent.path, agent_meta, native, resolved.raw)
        diagnostics.extend(issues)
        # The provider receives `request.body`, where OpenCode's own v1
        # migration also puts a top-level `reasoningEffort`.
        if effort:
            request = dict(meta.get("request") or {})
            request["body"] = {"reasoningEffort": effort, **request.get("body", {})}
            meta["request"] = request
        # Checked after the merge because a portable `color` reaches this
        # frontmatter too, and only a native override replaces it.
        if (color := meta.get("color")) is not None and not _opencode_color(color):
            diagnostics.append(
                Diagnostic(
                    "error",
                    f"agent color {color!r} is not an OpenCode color; use "
                    f"#RRGGBB or one of {sorted(PORTABLE_COLORS)}",
                    agent.path,
                )
            )
        files.append(
            OwnedFile(
                root / "agents" / agent.path.name,
                markdown(meta, agent.body),
                root / "agents",
            )
        )
    folded = fold_edit_write(
        sources.permissions.tools if sources.permissions else {}, "OpenCode"
    )
    if permissions := sources.permissions:
        _, issues = resolve("opencode", [permissions])
        diagnostics.extend(issues)
        if note := folded[1]:
            diagnostics.append(Diagnostic("warning", note, permissions.paths[0]))
        files.append(
            OwnedFile(
                root / "plugins" / UNWRAP_PLUGIN_NAME, UNWRAP_PLUGIN.read_bytes(), root
            )
        )
        files.extend(unwrap_reasons_file(root, permissions, shell_entries))
    rules_glob = f"~/{(root / 'rules').relative_to(ctx.home).as_posix()}/*.md"
    servers, issues = mcp_servers(sources, "opencode")
    diagnostics.extend(issues)
    native_values = (
        *_permissions(sources, config, folded[0], servers),
        *(
            NativeValue("opencode", config, ("mcp", "servers", server.id, key), value)
            for server, fields in servers
            for key, value in {
                **_opencode_server(fields),
                # `enabled` is the legacy `mcp/<id>` key; `mcp/servers` ignores it.
                **({} if server.enabled else {"disabled": True}),
            }.items()
        ),
        *(
            (NativeValue("opencode", config, ("instructions",), [rules_glob]),)
            if rules_
            else ()
        ),
    )
    patch, issues = native_patch(
        config_root=ctx.config_root,
        target="opencode",
        path=config,
        patch_name="opencode",
    )
    diagnostics.extend(issues)
    _validate_patches([patch])
    raw, raw_tree, raw_issues = raw_files(
        config_root=ctx.config_root,
        target="opencode",
        root=root,
        excluded=(Path("opencode.json"),),
    )
    return Plan(
        tuple((*files, *raw)),
        tuple((*trees, raw_tree)),
        native_values,
        (patch,),
        tuple((*diagnostics, *raw_issues)),
    )
