from __future__ import annotations

from collections.abc import Iterable

# Unknown target fields warn but still pass through.

CLAUDE_SKILL_FIELDS: frozenset[str] = frozenset(
    {
        "when_to_use",
        "argument_hint",
        "arguments",
        "user_invocable",
        "allowed_tools",
        "disallowed_tools",
        "model",
        "effort",
        "context",
        "agent",
        "shell",
    }
)
CLAUDE_COMMAND_FIELDS: frozenset[str] = frozenset(
    {"model", "argument_hint", "allowed_tools", "context", "agent"}
)
CLAUDE_AGENT_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "permission_mode",
        "max_turns",
        "isolation",
        "memory",
        "initial_prompt",
        "skills",
    }
)
CLAUDE_RULE_FIELDS: frozenset[str] = frozenset()

CODEX_SKILL_FIELDS: frozenset[str] = frozenset({"allowed-tools", "license", "metadata"})
CODEX_COMMAND_FIELDS: frozenset[str] = frozenset()
CODEX_AGENT_FIELDS: frozenset[str] = frozenset(
    {"model", "model_reasoning_effort", "sandbox_mode", "nickname_candidates"}
)
CODEX_RULE_FIELDS: frozenset[str] = frozenset({"rules"})

OPENCODE_SKILL_FIELDS: frozenset[str] = frozenset({"compatibility"})
OPENCODE_COMMAND_FIELDS: frozenset[str] = frozenset({"model"})
OPENCODE_AGENT_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "provider",
        "color",
        "reasoning_effort",
        "permission",
        "mode",
        "steps",
        "temperature",
        "top_p",
        "disable",
        "hidden",
    }
)
OPENCODE_RULE_FIELDS: frozenset[str] = frozenset({"instructions"})

CURSOR_SKILL_FIELDS: frozenset[str] = frozenset()
CURSOR_COMMAND_FIELDS: frozenset[str] = frozenset()
CURSOR_AGENT_FIELDS: frozenset[str] = frozenset({"model", "readonly", "is_background"})
CURSOR_RULE_FIELDS: frozenset[str] = frozenset()

KNOWN_FIELDS: dict[str, dict[str, frozenset[str]]] = {
    "claude": {
        "skill": CLAUDE_SKILL_FIELDS,
        "command": CLAUDE_COMMAND_FIELDS,
        "agent": CLAUDE_AGENT_FIELDS,
        "rule": CLAUDE_RULE_FIELDS,
    },
    "codex": {
        "skill": CODEX_SKILL_FIELDS,
        "command": CODEX_COMMAND_FIELDS,
        "agent": CODEX_AGENT_FIELDS,
        "rule": CODEX_RULE_FIELDS,
    },
    "opencode": {
        "skill": OPENCODE_SKILL_FIELDS,
        "command": OPENCODE_COMMAND_FIELDS,
        "agent": OPENCODE_AGENT_FIELDS,
        "rule": OPENCODE_RULE_FIELDS,
    },
    "cursor": {
        "skill": CURSOR_SKILL_FIELDS,
        "command": CURSOR_COMMAND_FIELDS,
        "agent": CURSOR_AGENT_FIELDS,
        "rule": CURSOR_RULE_FIELDS,
    },
}

CLAUDE_SKILL_RENAME: dict[str, str] = {
    "allowed_tools": "allowed-tools",
    "argument_hint": "argument-hint",
    "disable_model_invocation": "disable-model-invocation",
    "user_invocable": "user-invocable",
    "disallowed_tools": "disallowed-tools",
}
CLAUDE_COMMAND_RENAME: dict[str, str] = {
    "allowed_tools": "allowed-tools",
    "argument_hint": "argument-hint",
}
CLAUDE_AGENT_RENAME: dict[str, str] = {
    "disallowed_tools": "disallowedTools",
    "permission_mode": "permissionMode",
    "max_turns": "maxTurns",
    "initial_prompt": "initialPrompt",
}
OPENCODE_AGENT_RENAME: dict[str, str] = {"reasoning_effort": "reasoningEffort"}


def unknown_fields(tool: str, kind: str, keys: Iterable[str]) -> list[str]:
    known = KNOWN_FIELDS.get(tool, {}).get(kind, frozenset())
    return [key for key in keys if key not in known]
