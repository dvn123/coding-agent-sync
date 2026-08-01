---
schema: coding-agents/v4
kind: skill
id: fixture-skill
name: fixture-skill
description: Neutral fixture skill.
paths:
  - "**/*.py"
license: See asset.txt
metadata:
  fixture: "true"
targets:
  claude:
    omit:
      license: Claude skill frontmatter has no license field.
      metadata: Claude skill frontmatter has no metadata field.
  cursor:
    omit:
      license: Cursor skill frontmatter has no license field.
  codex:
    omit:
      paths: Codex skills have no path activation.
  opencode:
    omit:
      paths: OpenCode skills have no path activation.
---
Run the neutral fixture skill.
