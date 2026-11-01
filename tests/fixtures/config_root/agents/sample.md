---
schema: coding-agents/v1
kind: agent
id: fixture-agent
name: fixture-agent
description: Neutral fixture agent.
effort: high
color: blue
targets:
  claude:
    native:
      tools: Read
  codex:
    omit:
      color: Codex agents have no color field.
  cursor:
    native:
      readonly: true
    omit:
      effort: Cursor agents have no effort field.
      color: Cursor agents have no color field.
  opencode:
    native:
      permissions:
      - {action: edit, effect: deny}
      - {action: shell, effect: deny}
---
Review the neutral fixture input.
