from __future__ import annotations

import fnmatch
import functools
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, field_validator

from ..models import SyncContext
from ..patches import (
    Patch,
    PatchError,
    display_pointer,
    touches,
    validate_generated_conflicts,
)
from ..plan import (
    Diagnostic,
    NativePatch,
    NativeValue,
    OwnedFile,
    OwnedTree,
    Plan,
)
from ..sources import (
    PermissionSource,
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
    wrapper_prefixes,
)
from .support import (
    applies_to,
    block,
    bundled_files,
    command_target_diagnostics,
    declared_trees,
    frontmatter,
    markdown,
    native_patch,
    omissions,
    one_of,
    raw_files,
    strict_native,
    unhandled_target_block,
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


def _tree(skill: SkillSource, root: Path, meta: dict[str, Any]) -> OwnedTree:
    files, executables = bundled_files(skill.source_dir)
    files[Path("SKILL.md")] = markdown(meta, skill.body)
    return OwnedTree(
        root / skill.source_dir.name,
        tuple(sorted(files.items(), key=lambda item: item[0].as_posix())),
        root,
        executables=executables,
    )


# OpenCode matches a shell rule against a command's raw text. The bundled
# opencode-unwrap plugin denies a command when the rules deny it with its
# assignments, program path, or these wrappers peeled, so deny copies behind
# them are dead. It carries over only a deny, so asks keep their copies, as
# does a declared wrapper outside this list.
UNWRAP_PLUGIN = Path(__file__).with_name("opencode-unwrap.js")
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
        peels_ask=False,
    )


# The plugin reads deny reasons from beside its directory, keyed by the exact
# resource of the rule that decided the command.
UNWRAP_REASONS = Path("opencode-unwrap.json")


def deny_reasons(permissions: PermissionSource) -> dict[str, str]:
    """Each emitted deny pattern of a rule that carries a reason, to that reason."""
    commands = permissions.commands
    reasons: dict[str, str] = {}
    for rule in commands.deny:
        if rule.reason is None:
            continue
        alone = permissions.model_copy(
            update={
                "commands": commands.model_copy(
                    update={"allow": (), "ask": (), "deny": (rule,)}
                )
            }
        )
        for _bucket, _origin, pattern in shell_entries(alone):
            reasons.setdefault(pattern, rule.reason)
    return reasons


def _permissions(
    sources: SourceBundle, config: Path, tools: Mapping[str, str]
) -> tuple[NativeValue, ...]:
    if not (permissions := sources.permissions):
        return ()
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
    add("shell", permissions.unmatched, ["*"])
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
    return (NativeValue("opencode", config, ("permissions",), rules),)


def _validate_patches(
    patches: list[NativePatch], generated: tuple[NativeValue, ...]
) -> None:
    values = {value.pointer: value.value for value in generated}
    for patch in patches:
        validate_generated_conflicts(Patch(patch.operations), values)
        for operation in patch.operations:
            # The legacy map migrates ahead of the native list, so a patch may
            # only retire it. Contributions append after every generated rule;
            # command policy has one writer, so none of them may be a shell rule.
            if touches(operation.pointer, ("permission",)):
                if operation.kind != "delete":
                    raise PatchError(
                        "opencode patch may only delete the legacy "
                        f"{display_pointer(operation.pointer)}; use /permissions"
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
        root, (("skills", "dir"), ("commands", "file"), ("agents", "file"))
    )
    diagnostics = []
    # OpenCode 2 loads only AGENTS.md files: it accepts an `instructions` list
    # but never resolves it, so rule bodies follow the global one here.
    globals_ = [item for item in sources.globals if applies_to(item, "opencode")]
    rules_ = [item for item in sources.rules if applies_to(item, "opencode")]
    for source in (*globals_[:1], *rules_):
        value = block(source, "opencode")
        diagnostics.extend(unhandled_target_block(source.path, "opencode", value))
        diagnostics.extend(omissions(source.path, "opencode", value, set()))
    if globals_ or rules_:
        parts = [source.body.rstrip() for source in (*globals_[:1], *rules_)]
        files.append(
            OwnedFile(root / "AGENTS.md", ("\n\n".join(parts) + "\n").encode())
        )
    elif sources.globals or sources.rules:
        # Every global and rule was filtered by `only`; the host file is
        # compiler-owned whenever those sources exist, so retire it.
        files.append(OwnedFile(root / "AGENTS.md", None))
    for skill in sources.skills:
        if not applies_to(skill, "opencode"):
            continue
        value = block(skill, "opencode")
        native, issues = strict_native(
            skill.path, "opencode", value, OpenCodeSkillNative
        )
        diagnostics.extend(issues)
        diagnostics.extend(
            omissions(
                skill.path,
                "opencode",
                value,
                {
                    *({"paths"} if skill.paths else set()),
                    *(
                        {"disable_model_invocation"}
                        if skill.disable_model_invocation
                        else set()
                    ),
                },
            )
        )
        if native:
            meta, issues = frontmatter(
                skill.path,
                {
                    "name": skill.name,
                    "description": skill.description,
                    **({"license": skill.license} if skill.license else {}),
                    **({"metadata": skill.metadata} if skill.metadata else {}),
                },
                native,
                value.raw,
            )
            diagnostics.extend(issues)
            trees.append(_tree(skill, root / "skills", meta))
    for command in sources.commands:
        if not applies_to(command, "opencode"):
            continue
        value = block(command, "opencode")
        native, issues = strict_native(
            command.path, "opencode", value, OpenCodeCommandNative
        )
        diagnostics.extend(issues)
        diagnostics.extend(omissions(command.path, "opencode", value, set()))
        if native:
            command_meta: dict[str, Any] = {"description": command.description}
            if command.execution.agent:
                command_meta["agent"] = command.execution.agent
            if command.execution.subtask:
                command_meta["subagent"] = True
            meta, issues = frontmatter(command.path, command_meta, native, value.raw)
            diagnostics.extend(issues)
            files.append(
                OwnedFile(
                    root / "commands" / command.path.name,
                    markdown(meta, command.body),
                    root / "commands",
                )
            )
    for agent in sources.agents:
        if not applies_to(agent, "opencode"):
            continue
        value = block(agent, "opencode")
        native, issues = strict_native(
            agent.path, "opencode", value, OpenCodeAgentNative
        )
        diagnostics.extend(issues)
        diagnostics.extend(
            omissions(
                agent.path,
                "opencode",
                value,
                {"background"} if agent.background else set(),
            )
        )
        policy = resolve_model_policy(sources, agent, "opencode")
        effort = agent.effort
        if policy is not None:
            native_body = (value.native.get("request") or {}).get("body") or {}
            conflicts = {
                *({"model"} & (set(value.native) | set(value.raw))),
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
            if native:
                native = native.model_copy(update={"model": policy.model})
            effort = policy.effort
        if native:
            agent_meta: dict[str, Any] = {"description": agent.description}
            if agent.color:
                agent_meta["color"] = PORTABLE_COLORS.get(agent.color, agent.color)
            meta, issues = frontmatter(agent.path, agent_meta, native, value.raw)
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
        value = block(permissions, "opencode")
        diagnostics.extend(
            unhandled_target_block(permissions.paths[0], "opencode", value)
        )
        diagnostics.extend(omissions(permissions.paths[0], "opencode", value, set()))
        if note := folded[1]:
            diagnostics.append(Diagnostic("warning", note, permissions.paths[0]))
        diagnostics.extend(command_target_diagnostics(permissions, "opencode"))
        files.append(
            OwnedFile(root / "plugins" / UNWRAP_PLUGIN.name, UNWRAP_PLUGIN.read_bytes())
        )
    reasons = deny_reasons(permissions) if permissions else {}
    files.append(
        OwnedFile(
            root / UNWRAP_REASONS,
            (json.dumps({"reasons": reasons}, indent=2, sort_keys=True) + "\n").encode()
            if reasons
            else None,
        )
    )
    native_values = (*_permissions(sources, config, folded[0]),)
    patch, issues = native_patch(
        config_root=ctx.config_root,
        target="opencode",
        path=config,
        patch_name="opencode",
    )
    diagnostics.extend(issues)
    _validate_patches([patch], native_values)
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
