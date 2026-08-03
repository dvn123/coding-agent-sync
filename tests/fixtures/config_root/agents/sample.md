---
schema: coding-agents/v4
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
    native:
      sandbox_mode: read-only
    omit:
      color: Codex agents have no color field.
  cursor:
    omit:
      agent: Cursor Agent does not load user-scope agents.
  opencode:
    native:
      # OpenCode takes #RRGGBB or a theme name, so the portable `blue` needs a
      # native override here.
      color: info
      permission:
        edit: deny
        bash: deny
---
Review the neutral fixture input.
