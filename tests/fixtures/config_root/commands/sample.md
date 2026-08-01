---
schema: coding-agents/v4
kind: command
id: fixture-command
name: fixture-command
description: Neutral fixture command.
execution:
  subtask: true
targets:
  codex:
    omit:
      command: Codex has no native command delivery.
  cursor:
    omit:
      command: Cursor has no user command delivery.
---
Run the neutral fixture command.
