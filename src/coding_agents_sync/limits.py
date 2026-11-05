"""Portable fields a target cannot represent.

A source that uses one must acknowledge the loss by listing the field in
`targets.<target>.omit`, and an omit no limitation asks for is an error. Each
limitation's `probe` names the negative live capability case that shows the
target still lacks the feature: when that case observes support, the live run
fails and names the limitation for removal.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .sources import (
    AgentSource,
    CommandSource,
    McpServerSource,
    PermissionSource,
    ToolName,
)


@dataclass(frozen=True, slots=True)
class Limitation:
    target: ToolName
    source: type
    field: str
    reason: str
    used: Callable[[Any], bool]
    probe: str

    @property
    def id(self) -> str:
        return f"{self.target}.{self.source.__name__}.{self.field}"


def _delegated(command: CommandSource) -> bool:
    return bool(command.execution.agent or command.execution.subtask)


def _codex_gated(permissions: PermissionSource) -> bool:
    """A tool decision Codex cannot carry: everything but allow, and a websearch
    deny, which disables the tool."""
    return any(
        decision != "allow" and (tool, decision) != ("websearch", "deny")
        for tool, decision in permissions.tools.items()
    )


def _cursor_gated(permissions: PermissionSource) -> bool:
    """A tool decision other than Desktop's default: file tools run unasked in
    the workspace (`cursor-desktop.tool-defaults`), while web tools ask in a
    session but not in a subagent, and an in-app toggle can change that."""
    return any(
        decision != "allow" or tool in {"webfetch", "websearch"}
        for tool, decision in permissions.tools.items()
    )


def _directories(permissions: PermissionSource) -> bool:
    workspace = permissions.workspace
    return bool(
        workspace.allow
        or workspace.ask
        or workspace.deny
        or workspace.unmatched != "ask"
    )


LIMITATIONS = (
    Limitation(
        "codex",
        CommandSource,
        "execution",
        "Codex skills have no delegated execution.",
        _delegated,
        probe="codex.skill-execution",
    ),
    Limitation(
        "codex",
        AgentSource,
        "background",
        "Codex agent roles have no background field.",
        lambda agent: agent.background,
        probe="codex.agent-background",
    ),
    Limitation(
        "codex",
        AgentSource,
        "deny",
        "A Codex role keeps its parent's sandbox, web search, and spawn depth; "
        "only its shell tool can be turned off.",
        lambda agent: bool(set(agent.deny) - {"shell"}),
        probe="codex.agent-tool-gates",
    ),
    Limitation(
        "codex",
        PermissionSource,
        "tools",
        "Codex allows every tool and has no per-tool ask or deny channel; only "
        "web search can be turned off.",
        _codex_gated,
        probe="codex.tool-gates",
    ),
    Limitation(
        "codex",
        PermissionSource,
        "workspace.ask",
        "Codex has no directory ask channel.",
        lambda permissions: bool(permissions.workspace.ask),
        probe="codex.workspace-ask",
    ),
    Limitation(
        "codex",
        PermissionSource,
        "unmatched",
        "`on-request`, the only approval policy that asks, runs an unlisted "
        "command in the sandbox; Codex retired `untrusted`.",
        lambda permissions: permissions.unmatched != "ask",
        probe="codex.unmatched-ask",
    ),
    Limitation(
        "cursor",
        CommandSource,
        "execution",
        "Cursor skills have no delegated execution.",
        _delegated,
        probe="cursor-desktop.skill-execution",
    ),
    Limitation(
        "cursor",
        AgentSource,
        "effort",
        "Cursor agent files have no effort field.",
        lambda agent: agent.effort is not None,
        probe="cursor-desktop.agent-fields",
    ),
    Limitation(
        "cursor",
        AgentSource,
        "deny",
        "Cursor agent files list the tools an agent may use, not those it may "
        "not; `readonly` stands in for edits only.",
        lambda agent: bool(set(agent.deny) - {"edit"}),
        probe="cursor-desktop.agent-fields",
    ),
    Limitation(
        "cursor",
        PermissionSource,
        "tools",
        "Cursor Desktop's permission file, which the CLI reads too, has no "
        "per-tool channel; only its default holds, file tools running unasked "
        "in the workspace.",
        _cursor_gated,
        probe="cursor-desktop.permission-channels",
    ),
    Limitation(
        "cursor",
        PermissionSource,
        "workspace",
        "Cursor Desktop's permission file, which the CLI reads too, has no "
        "directory channel.",
        _directories,
        probe="cursor-desktop.permission-channels",
    ),
    Limitation(
        "cursor",
        PermissionSource,
        "secret_names",
        "Cursor Desktop's permission file, which the CLI reads too, has no "
        "environment-variable channel.",
        lambda permissions: bool(permissions.secret_names),
        probe="cursor-desktop.permission-channels",
    ),
    Limitation(
        "cursor",
        PermissionSource,
        "unmatched",
        "Cursor Desktop's permission file, which the CLI reads too, has no deny "
        "list, so `unrestricted` there would run every guarded command.",
        lambda permissions: permissions.unmatched != "ask",
        probe="cursor-desktop.permission-channels",
    ),
    Limitation(
        "cursor",
        McpServerSource,
        "tools",
        "Cursor Desktop's permission file, which the CLI reads too, has no MCP "
        "deny list.",
        lambda server: "deny" in server.tools.values(),
        probe="cursor-desktop.permission-channels",
    ),
    Limitation(
        "cursor",
        McpServerSource,
        "enabled",
        "Cursor keeps a server's on/off state per project, outside mcp.json, so "
        "a user-level server cannot start off.",
        lambda server: not server.enabled,
        probe="cursor-agent.mcp-enablement",
    ),
    Limitation(
        "opencode",
        AgentSource,
        "background",
        "OpenCode agents have no background field.",
        lambda agent: agent.background,
        probe="opencode.agent-background",
    ),
)


def required_omissions(source: Any, target: ToolName) -> dict[str, str]:
    """The omits `source` owes `target` for the portable fields it uses, each
    with the reason the target loses it."""
    return {
        limitation.field: limitation.reason
        for limitation in LIMITATIONS
        if limitation.target == target
        and isinstance(source, limitation.source)
        and limitation.used(source)
    }
