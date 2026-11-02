---
description: Neutral fixture agent.
effort: high
color: blue
targets:
  claude:
    native:
      tools: Read
  cursor:
    native:
      readonly: true
    omit:
      - effort
  opencode:
    native:
      permissions:
      - {action: edit, effect: deny}
      - {action: shell, effect: deny}
---
Review the neutral fixture input.
