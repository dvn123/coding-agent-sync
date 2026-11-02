---
name: project-review
description: Review a project and report actionable findings with file references.
targets:
  codex:
    native:
      openai:
        interface:
          display_name: Project review
          short_description: Review a project for actionable defects
          default_prompt: Review this project and report actionable findings.
---

Read the project instructions. Trace the relevant code before judging it.
Report actionable defects with file references and reproduction steps.
Do not change files unless the user asks for fixes.
