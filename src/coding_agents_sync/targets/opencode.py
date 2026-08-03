from __future__ import annotations

import functools
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, ConfigDict, Field

from ..models import SyncContext
from ..patches import (
    Patch,
    PatchError,
    Pointer,
    display_pointer,
    validate_generated_conflicts,
)
from ..plan import (
    Diagnostic,
    ManifestMode,
    NativePatch,
    NativeValue,
    OwnedFile,
    OwnedTree,
    Plan,
)
from ..sources import RuleSource, SkillSource, SourceBundle, StrictModel
from .permissions import (
    bucket_patterns,
    external_directory_map,
    glob_variants,
    secret_name_variants,
)
from .support import (
    applies_to,
    block,
    bundled_files,
    frontmatter,
    markdown,
    native_patch,
    omissions,
    raw_files,
    strict_native,
    unhandled_target_block,
)


class OpenCodeRuleNative(StrictModel):
    instructions: str | None = None


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
    subtask: bool | None = None
    model: str | None = None


class OpenCodeAgentNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    name: str | None = None
    description: str | None = None
    model: str | None = None
    provider: str | None = None
    color: str | None = None
    reasoning_effort: str | None = Field(
        None,
        serialization_alias="reasoningEffort",
        validation_alias=AliasChoices("reasoningEffort", "reasoning_effort"),
    )
    permission: dict[str, Any] | None = None
    mode: str | None = None
    steps: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    disable: bool | None = None
    hidden: bool | None = None


def _tree(skill: SkillSource, root: Path, meta: dict[str, Any]) -> OwnedTree:
    files, executables = bundled_files(skill.source_dir)
    files[Path("SKILL.md")] = markdown(meta, skill.body)
    return OwnedTree(
        root / skill.source_dir.name,
        tuple(sorted(files.items(), key=lambda item: item[0].as_posix())),
        root,
        executables=executables,
    )


def _instruction(rule: RuleSource, home: Path, native: OpenCodeRuleNative) -> str:
    if native.instructions:
        return native.instructions
    try:
        return f"~/{rule.path.relative_to(home)}"
    except ValueError:
        return str(rule.path)


def _permissions(sources: SourceBundle, config: Path) -> tuple[NativeValue, ...]:
    if not (permissions := sources.permissions):
        return ()
    buckets = bucket_patterns(
        permissions,
        functools.partial(glob_variants, optional_trailing=True),
        lambda wrapper: f"{wrapper} *",
    )
    bash: dict[str, str] = {"*": "ask"}

    def append(patterns: Iterable[str], decision: str) -> None:
        for pattern in patterns:
            bash.pop(pattern, None)
            bash[pattern] = decision

    append(buckets["allow"], "allow")
    append(buckets["ask"], "ask")
    append(secret_name_variants(permissions.secret_names), "ask")
    append(buckets["deny"], "deny")
    values = [NativeValue("opencode", config, ("permission", "bash"), bash)]
    for tool in ("read", "edit", "write"):
        decision = permissions.tools.get(tool)
        if decision is not None or permissions.secret_paths:
            values.append(
                NativeValue(
                    "opencode",
                    config,
                    ("permission", tool),
                    {
                        **({"*": decision} if decision else {}),
                        **dict.fromkeys(permissions.secret_paths, "deny"),
                    },
                )
            )
    values.extend(
        NativeValue("opencode", config, ("permission", tool), decision)
        for tool in ("webfetch", "websearch")
        if (decision := permissions.tools.get(tool)) is not None
    )
    if permissions.workspace.allow or permissions.workspace.ask:
        values.append(
            NativeValue(
                "opencode",
                config,
                ("permission", "external_directory"),
                external_directory_map(permissions.workspace),
            )
        )
    return tuple(values)


def _touches(left: Pointer, right: Pointer) -> bool:
    return left[: min(len(left), len(right))] == right[: min(len(left), len(right))]


def _validate_patches(
    patches: list[NativePatch], generated: tuple[NativeValue, ...]
) -> None:
    values = {value.pointer: value.value for value in generated}
    for patch in patches:
        validate_generated_conflicts(Patch(patch.operations), values)
        for operation in patch.operations:
            if _touches(operation.pointer, ("permission", "bash")):
                raise PatchError(
                    "opencode patch cannot contribute command permissions at "
                    f"{display_pointer(operation.pointer)}"
                )


def compile_opencode(ctx: SyncContext, sources: SourceBundle) -> Plan:
    root = ctx.opencode
    config = root / "opencode.json"
    files: list[OwnedFile] = []
    managed_roots: tuple[tuple[str, ManifestMode], ...] = (
        ("skills", "dir"),
        ("commands", "file"),
        ("agents", "file"),
    )
    trees = [
        OwnedTree(
            root / name, manifest_root=root / name, manifest_mode=mode, declaration=True
        )
        for name, mode in managed_roots
    ]
    diagnostics = []
    if sources.globals:
        global_source = sources.globals[0]
        if applies_to(global_source, "opencode"):
            value = block(global_source, "opencode")
            diagnostics.extend(
                unhandled_target_block(global_source.path, "opencode", value)
            )
            diagnostics.extend(omissions(global_source.path, "opencode", value, set()))
            files.append(
                OwnedFile(
                    root / "AGENTS.md",
                    (global_source.body.rstrip() + "\n").encode(),
                )
            )
        else:
            files.append(
                OwnedFile(
                    root / "AGENTS.md",
                    None,
                    retire_if=(global_source.body.rstrip() + "\n").encode(),
                )
            )
    instructions = []
    for rule in sources.rules:
        if not applies_to(rule, "opencode"):
            continue
        value = block(rule, "opencode")
        native, issues = strict_native(rule.path, "opencode", value, OpenCodeRuleNative)
        diagnostics.extend(issues)
        if value.raw:
            diagnostics.append(
                Diagnostic("error", "opencode raw rule fields are not valid", rule.path)
            )
        diagnostics.extend(omissions(rule.path, "opencode", value, set()))
        if native:
            instructions.append(_instruction(rule, ctx.home, native))
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
                command_meta["subtask"] = True
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
        if native:
            agent_meta: dict[str, Any] = {
                "name": agent.name,
                "description": agent.description,
            }
            if agent.effort:
                agent_meta["reasoningEffort"] = agent.effort
            if agent.color:
                agent_meta["color"] = agent.color
            meta, issues = frontmatter(agent.path, agent_meta, native, value.raw)
            diagnostics.extend(issues)
            files.append(
                OwnedFile(
                    root / "agents" / agent.path.name,
                    markdown(meta, agent.body),
                    root / "agents",
                )
            )
    if permissions := sources.permissions:
        value = block(permissions, "opencode")
        diagnostics.extend(
            unhandled_target_block(permissions.paths[0], "opencode", value)
        )
        diagnostics.extend(omissions(permissions.paths[0], "opencode", value, set()))
        for path, targets, _ in permissions.command_targets:
            value = targets.root.get("opencode", type(value)())
            diagnostics.extend(unhandled_target_block(path, "opencode", value))
            diagnostics.extend(omissions(path, "opencode", value, set()))
    native_values = (
        NativeValue("opencode", config, ("instructions",), instructions),
        *_permissions(sources, config),
    )
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
