from __future__ import annotations

from typing import Any

from .metadata import markdown_document

CODEX_SKILL_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "allowed-tools",
    "metadata",
}


def codex_skill_meta(meta: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(meta)
    if "allowed_tools" in normalized and "allowed-tools" not in normalized:
        normalized["allowed-tools"] = normalized["allowed_tools"]
    out = {}
    for key in CODEX_SKILL_FRONTMATTER_KEYS:
        if key in normalized:
            out[key] = normalized[key]
    for key, value in normalized.items():
        if key != "allowed_tools" and key not in out:
            out[key] = value
    description = out.get("description")
    if isinstance(description, str):
        out["description"] = description.replace("<", "").replace(">", "")
    return out


def codex_command_skill_name(command_name: str, existing_skill_names: set[str]) -> str:
    if command_name not in existing_skill_names:
        return command_name
    return f"source-command-{command_name}"


def codex_command_skill_document(
    command_name: str,
    meta: dict[str, Any],
    body: str,
    skill_name: str | None = None,
) -> str:
    frontmatter_name = skill_name or command_name
    description = str(
        meta.get("description") or f"Run the {command_name} shared command."
    )
    skill_meta = codex_skill_meta(
        {
            "name": frontmatter_name,
            "description": (
                f"Command wrapper for {command_name}. Do not auto-invoke this skill. "
                f"Use it only when the user explicitly asks to run the {command_name} "
                f"command. Mentions, questions, audits, lists, or config/debugging "
                f"requests about {command_name} are not triggers. This is not a "
                f"Codex slash command. Command purpose: {description}"
            ),
        }
    )

    notes: list[str] = []
    if "agent" in meta:
        notes.append(f"- Original command agent: `{meta['agent']}`")
    if "model" in meta:
        notes.append(f"- Original command model: `{meta['model']}`")
    if meta.get("subtask"):
        notes.append("- Original command requested a forked subtask context.")

    notes_block = "\n".join(notes)
    if notes_block:
        notes_block = f"## Original Command Metadata\n\n{notes_block}\n\n"

    skill_body = (
        f"# {command_name}\n\n"
        "This wrapper runs only on explicit request and is not a Codex slash "
        "command.\n\n"
        f"{notes_block}"
        "## Command Instructions\n\n"
        f"{body.strip()}\n"
    )
    return markdown_document(skill_meta, skill_body)
