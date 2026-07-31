from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from coding_agents_sync.metadata import markdown_document
from coding_agents_sync.sources import (
    AgentSource,
    AgentTools,
    CommandSource,
    SkillSource,
)
from coding_agents_sync.target_fields import (
    CLAUDE_AGENT_RENAME,
    CLAUDE_COMMAND_RENAME,
    CLAUDE_SKILL_RENAME,
    OPENCODE_AGENT_RENAME,
    unknown_fields,
)

WRITE_CAPABLE_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"}
)
WRITE_CAPABLE_EDIT_TOOLS: frozenset[str] = frozenset(
    {"Edit", "Write", "MultiEdit", "NotebookEdit"}
)


def agent_write_tools_denied(tools: AgentTools) -> bool:
    """Derive whether canonical tool policy denies every write-capable tool."""
    if tools.deny and WRITE_CAPABLE_TOOLS.issubset(set(tools.deny)):
        return True
    if tools.inherit:
        return False
    return not (WRITE_CAPABLE_TOOLS & set(tools.allow))


def opencode_permission_from_tools(tools: AgentTools) -> dict[str, str]:
    permission: dict[str, str] = {}
    if tools.deny:
        deny = set(tools.deny)
        if WRITE_CAPABLE_EDIT_TOOLS & deny:
            permission["edit"] = "deny"
        if "Bash" in deny:
            permission["bash"] = "deny"
    elif not tools.inherit:
        allow = set(tools.allow)
        if not (WRITE_CAPABLE_EDIT_TOOLS & allow):
            permission["edit"] = "deny"
        if "Bash" not in allow:
            permission["bash"] = "deny"
    return permission


def target_meta(
    source_targets: dict[str, dict[str, Any]], tool: str, kind: str
) -> dict[str, Any]:
    meta = source_targets.get(tool, {})
    if not isinstance(meta, dict):
        return {}
    for key in unknown_fields(tool, kind, meta.keys()):
        print(f"Warning: unrecognized {tool} {kind} field `{key}`", file=sys.stderr)
    return dict(meta)


def skill_markdown_document(meta: dict[str, Any], body: str) -> str:
    if not meta:
        return body.rstrip() + "\n"
    yaml_text = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{yaml_text}\n---\n\n{body.rstrip()}\n"


type SkillRenderer = Callable[[dict[str, Any], str], str]


def skill_meta_for(skill: SkillSource, tool: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": skill.name, "description": skill.description}
    if tool in ("claude", "cursor") and skill.paths:
        meta["paths"] = skill.paths
    if tool in ("claude", "cursor") and skill.disable_model_invocation:
        meta["disable_model_invocation"] = skill.disable_model_invocation
    if tool in ("codex", "opencode") and skill.license:
        meta["license"] = skill.license
    if tool in ("codex", "opencode", "cursor") and skill.metadata:
        meta["metadata"] = skill.metadata
    meta.update(target_meta(skill.targets, tool, "skill"))
    return {CLAUDE_SKILL_RENAME.get(key, key): value for key, value in meta.items()}


def skill_tree_artifact_override(
    skill: SkillSource,
    *,
    meta: dict[str, Any] | None = None,
    renderer: SkillRenderer = skill_markdown_document,
) -> dict[Path, str]:
    resolved = meta if meta is not None else skill_meta_for(skill, "claude")
    return {Path("SKILL.md"): renderer(resolved, skill.body)}


def command_meta_for(command: CommandSource, tool: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"description": command.description}
    if command.execution.agent:
        meta["agent"] = command.execution.agent
    if tool == "opencode" and command.execution.subtask:
        meta["subtask"] = True
    if tool == "claude" and command.execution.subtask:
        meta["context"] = "fork"
    meta.update(target_meta(command.targets, tool, "command"))
    rename = CLAUDE_COMMAND_RENAME if tool == "claude" else {}
    return {rename.get(key, key): value for key, value in meta.items()}


def agent_identity_meta(agent: AgentSource) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": agent.name}
    if agent.description:
        meta["description"] = agent.description
    return meta


def claude_agent_meta(agent: AgentSource) -> dict[str, Any]:
    meta = agent_identity_meta(agent)
    if agent.tools.inherit:
        meta["tools"] = "inherit"
    elif agent.tools.allow:
        meta["tools"] = ", ".join(agent.tools.allow)
    disallowed = list(agent.tools.deny)
    if not agent.tools.inherit and not agent.tools.allow and not disallowed:
        # Omitting `tools` would restore Claude's inherit-all default.
        disallowed = sorted(WRITE_CAPABLE_TOOLS)
    if disallowed:
        meta["disallowedTools"] = ", ".join(disallowed)
    if agent.effort:
        meta["effort"] = agent.effort
    if agent.background:
        meta["background"] = agent.background
    if agent.color:
        meta["color"] = agent.color
    prefixed = target_meta(agent.targets, "claude", "agent")
    meta.update({CLAUDE_AGENT_RENAME.get(k, k): v for k, v in prefixed.items()})
    return meta


def cursor_agent_meta(agent: AgentSource) -> dict[str, Any]:
    meta = agent_identity_meta(agent)
    if agent_write_tools_denied(agent.tools):
        meta["readonly"] = True
    if agent.background:
        meta["is_background"] = agent.background
    meta.update(target_meta(agent.targets, "cursor", "agent"))
    return meta


CODEX_REASONING_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh"})


def codex_agent_meta(agent: AgentSource) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if agent.effort in CODEX_REASONING_EFFORTS:
        meta["model_reasoning_effort"] = agent.effort
    meta["sandbox_mode"] = (
        "read-only" if agent_write_tools_denied(agent.tools) else "workspace-write"
    )
    meta.update(target_meta(agent.targets, "codex", "agent"))
    return meta


def opencode_agent_meta(agent: AgentSource) -> dict[str, Any]:
    meta = agent_identity_meta(agent)
    permission = opencode_permission_from_tools(agent.tools)
    if permission:
        meta["permission"] = permission
    if agent.effort:
        meta["reasoningEffort"] = agent.effort
    if agent.color:
        meta["color"] = agent.color
    prefixed = target_meta(agent.targets, "opencode", "agent")
    override_permission = prefixed.pop("permission", None)
    if isinstance(override_permission, dict):
        meta.setdefault("permission", {})
        meta["permission"] = {**meta.get("permission", {}), **override_permission}
    meta.update({OPENCODE_AGENT_RENAME.get(k, k): v for k, v in prefixed.items()})
    return meta


def render_markdown(meta: dict[str, Any], body: str) -> str:
    return markdown_document(meta, body)
