---
schema: coding-agents/v4
kind: rule
id: fixture-scoped
name: Fixture scoped rule
description: Neutral scoped rule.
targets:
  cursor:
    native:
      globs:
        - "**/*.py"
      always_apply: false
---
Apply the neutral scoped rule.
