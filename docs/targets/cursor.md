# Cursor Target Reference

## Generated surfaces

| Source | Output |
| --- | --- |
| Global | `<home>/.cursor/rules/coding-agents-global.mdc` |
| Rule | `<home>/.cursor/rules/<stem>.mdc` |
| Skill | `<home>/.cursor/skills/<directory>/` |
| Command | no generated surface |
| Agent | no generated surface |
| Permissions | owned pointers in `<home>/.cursor/cli-config.json` when portable policy is exact |

Cursor has one shared MDC rule delivery path for Desktop and Agent. The global
rule filename is reserved. Typed rule fields are `description`, `globs`, and
`always_apply`; a scoped rule must state `always_apply: false`. Typed skill
fields are `name`, `description`, `paths`, `disable-model-invocation`, and
`metadata`.

Cursor does not receive portable user commands, user agents, or command
permissions. Sources must acknowledge `omit.command`, `omit.agent`, and
non-empty command-policy `omit.commands` as applicable. The compiler creates
no generated shell-command allowlist for this omitted policy.

Patches target `cli-config.json`, `permissions.json`, `settings.json`, and
`mcp.json` through the corresponding `cursor-*` patch names. They must not
overlap generated pointers; in particular, a Cursor Desktop patch cannot
contribute to the terminal allowlist. Raw files below
`target-config/cursor/raw/` mirror below `<home>/.cursor`; those four native
pointer files are reserved.

The rules and skills roots use portable manifests. The compiler retires only
its previously manifested legacy agent/plugin roots and preserves unrelated
Cursor content.
