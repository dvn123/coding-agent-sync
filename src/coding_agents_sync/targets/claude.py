from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import AliasChoices, ConfigDict, Field

from ..models import SyncContext
from ..patches import Patch, validate_generated_conflicts
from ..plan import ManifestMode, NativePatch, NativeValue, OwnedFile, OwnedTree, Plan
from ..sources import AgentSource, SkillSource, SourceBundle, StrictModel
from .permissions import (
    CLAUDE_RESOLVED_WRAPPERS,
    CLAUDE_TOOL_PATTERNS,
    bucket_patterns,
    glob_variants,
    literal_directories,
    secret_name_variants,
    tool_patterns,
)
from .support import (
    block,
    frontmatter,
    markdown,
    native_patch,
    omissions,
    raw_files,
    strict_native,
    unhandled_target_block,
)


class ClaudeSkillNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

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
    argument_hint: str | None = Field(
        None,
        serialization_alias="argument-hint",
        validation_alias=AliasChoices("argument-hint", "argument_hint"),
    )
    arguments: list[str] | None = None
    user_invocable: bool | None = Field(
        None,
        serialization_alias="user-invocable",
        validation_alias=AliasChoices("user-invocable", "user_invocable"),
    )
    allowed_tools: list[str] | None = Field(
        None,
        serialization_alias="allowed-tools",
        validation_alias=AliasChoices("allowed-tools", "allowed_tools"),
    )
    disallowed_tools: list[str] | None = Field(
        None,
        serialization_alias="disallowed-tools",
        validation_alias=AliasChoices("disallowed-tools", "disallowed_tools"),
    )
    model: str | None = None
    effort: str | None = None
    context: str | None = None
    agent: str | None = None
    shell: str | None = None


class ClaudeCommandNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    description: str | None = None
    agent: str | None = None
    context: str | None = None
    model: str | None = None
    argument_hint: str | None = Field(
        None,
        serialization_alias="argument-hint",
        validation_alias=AliasChoices("argument-hint", "argument_hint"),
    )
    allowed_tools: list[str] | None = Field(
        None,
        serialization_alias="allowed-tools",
        validation_alias=AliasChoices("allowed-tools", "allowed_tools"),
    )


class ClaudeAgentNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    name: str | None = None
    description: str | None = None
    tools: str | list[str] | None = None
    disallowed_tools: str | list[str] | None = Field(
        None,
        serialization_alias="disallowedTools",
        validation_alias=AliasChoices("disallowedTools", "disallowed_tools"),
    )
    effort: str | None = None
    background: bool | None = None
    color: str | None = None
    model: str | None = None
    permission_mode: str | None = Field(
        None,
        serialization_alias="permissionMode",
        validation_alias=AliasChoices("permissionMode", "permission_mode"),
    )
    max_turns: int | None = Field(
        None,
        serialization_alias="maxTurns",
        validation_alias=AliasChoices("maxTurns", "max_turns"),
    )
    isolation: str | None = None
    memory: str | None = None
    initial_prompt: str | None = Field(
        None,
        serialization_alias="initialPrompt",
        validation_alias=AliasChoices("initialPrompt", "initial_prompt"),
    )
    skills: list[str] | None = None


def _tree(skill: SkillSource, root: Path, meta: dict[str, Any]) -> OwnedTree:
    files = {
        path.relative_to(skill.source_dir): path.read_bytes()
        for path in skill.source_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    files[Path("SKILL.md")] = markdown(meta, skill.body)
    return OwnedTree(
        root / skill.source_dir.name,
        tuple(sorted(files.items(), key=lambda item: item[0].as_posix())),
        root,
    )


def _agent_meta(agent: AgentSource, native: ClaudeAgentNative) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": agent.name, "description": agent.description}
    if native.tools is not None:
        meta["tools"] = native.tools
    if native.disallowed_tools is not None:
        meta["disallowedTools"] = native.disallowed_tools
    if agent.effort:
        meta["effort"] = agent.effort
    if agent.background:
        meta["background"] = True
    if agent.color:
        meta["color"] = agent.color
    return meta


def _permission_values(
    sources: SourceBundle,
) -> tuple[tuple[tuple[str, ...], Any], ...]:
    if not (permissions := sources.permissions):
        return ()
    buckets = bucket_patterns(
        permissions,
        glob_variants,
        lambda wrapper: f"{wrapper} *",
        CLAUDE_RESOLVED_WRAPPERS,
    )
    ask = buckets["ask"] + list(secret_name_variants(permissions.secret_names))
    deny = [f"Bash({pattern})" for pattern in buckets["deny"]] + [
        f"{operation}({path})"
        for path in permissions.secret_paths
        for operation in ("Read", "Edit")
    ]
    values: list[tuple[tuple[str, ...], Any]] = [
        (
            ("permissions", "allow"),
            [f"Bash({pattern})" for pattern in buckets["allow"]]
            + list(tool_patterns(permissions.tools, CLAUDE_TOOL_PATTERNS, "allow")),
        ),
        (
            ("permissions", "ask"),
            [f"Bash({pattern})" for pattern in ask]
            + list(tool_patterns(permissions.tools, CLAUDE_TOOL_PATTERNS, "ask")),
        ),
    ]
    deny += list(tool_patterns(permissions.tools, CLAUDE_TOOL_PATTERNS, "deny"))
    if deny:
        values.append((("permissions", "deny"), deny))
    if directories := literal_directories(permissions.workspace):
        values.append((("permissions", "additionalDirectories"), directories))
    return tuple(values)


def _validate_patches(
    patches: list[NativePatch], generated: tuple[NativeValue, ...]
) -> None:
    values = {
        value.pointer: value.value for value in generated if value.target == "claude"
    }
    for patch in patches:
        if patch.target == "claude":
            validate_generated_conflicts(Patch(patch.operations), values)


def compile_claude(ctx: SyncContext, sources: SourceBundle) -> Plan:
    root = ctx.claude
    files: list[OwnedFile] = []
    managed_roots: tuple[tuple[str, ManifestMode], ...] = (
        ("rules", "file"),
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
        value = block(global_source, "claude")
        diagnostics.extend(unhandled_target_block(global_source.path, "claude", value))
        diagnostics.extend(omissions(global_source.path, "claude", value, set()))
        files.append(
            OwnedFile(root / "CLAUDE.md", (global_source.body.rstrip() + "\n").encode())
        )
    for rule in sources.rules:
        value = block(rule, "claude")
        diagnostics.extend(unhandled_target_block(rule.path, "claude", value))
        diagnostics.extend(omissions(rule.path, "claude", value, set()))
        files.append(
            OwnedFile(
                root / "rules" / f"{rule.stem}.md",
                (rule.body.rstrip() + "\n").encode(),
                root / "rules",
            )
        )
    for skill in sources.skills:
        value = block(skill, "claude")
        native, issues = strict_native(skill.path, "claude", value, ClaudeSkillNative)
        diagnostics.extend(issues)
        diagnostics.extend(
            omissions(
                skill.path,
                "claude",
                value,
                {
                    *({"license"} if skill.license else set()),
                    *({"metadata"} if skill.metadata else set()),
                },
            )
        )
        if native:
            meta, issues = frontmatter(
                skill.path,
                {
                    "name": skill.name,
                    "description": skill.description,
                    **({"paths": skill.paths} if skill.paths else {}),
                    **(
                        {"disable-model-invocation": True}
                        if skill.disable_model_invocation
                        else {}
                    ),
                },
                native,
                value.raw,
            )
            diagnostics.extend(issues)
            trees.append(_tree(skill, root / "skills", meta))
    for command in sources.commands:
        value = block(command, "claude")
        native, issues = strict_native(
            command.path, "claude", value, ClaudeCommandNative
        )
        diagnostics.extend(issues)
        diagnostics.extend(omissions(command.path, "claude", value, set()))
        if native:
            canonical = {"description": command.description}
            if command.execution.agent:
                canonical["agent"] = command.execution.agent
            if command.execution.subtask:
                canonical["context"] = "fork"
            meta, issues = frontmatter(command.path, canonical, native, value.raw)
            diagnostics.extend(issues)
            files.append(
                OwnedFile(
                    root / "commands" / command.path.name,
                    markdown(meta, command.body),
                    root / "commands",
                )
            )
    for agent in sources.agents:
        value = block(agent, "claude")
        native, issues = strict_native(agent.path, "claude", value, ClaudeAgentNative)
        diagnostics.extend(issues)
        diagnostics.extend(omissions(agent.path, "claude", value, set()))
        if native:
            meta, issues = frontmatter(
                agent.path,
                _agent_meta(agent, native),
                native,
                value.raw,
            )
            diagnostics.extend(issues)
            files.append(
                OwnedFile(
                    root / "agents" / agent.path.name,
                    markdown(meta, agent.body),
                    root / "agents",
                )
            )
    if permissions := sources.permissions:
        value = block(permissions, "claude")
        diagnostics.extend(
            unhandled_target_block(permissions.paths[0], "claude", value)
        )
        diagnostics.extend(
            omissions(
                permissions.paths[0],
                "claude",
                value,
                {"workspace.ask"} if permissions.workspace.ask else set(),
            )
        )
        for path, targets, _ in permissions.command_targets:
            value = targets.root.get("claude", type(value)())
            diagnostics.extend(unhandled_target_block(path, "claude", value))
            diagnostics.extend(omissions(path, "claude", value, set()))
    permission_values = tuple(
        NativeValue("claude", root / "settings.json", pointer, value)
        for pointer, value in _permission_values(sources)
    )
    patches = []
    for target, path, patch_name in (
        ("claude", root / "settings.json", "claude-settings"),
        ("claude-mcp", ctx.home / ".claude.json", "claude-mcp"),
    ):
        patch, issues = native_patch(
            config_root=ctx.config_root, target=target, path=path, patch_name=patch_name
        )
        patches.append(patch)
        diagnostics.extend(issues)
    _validate_patches(patches, permission_values)
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
