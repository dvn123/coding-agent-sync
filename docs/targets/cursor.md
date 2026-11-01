# Cursor Target Reference

## Generated surfaces

| Source | Output |
| --- | --- |
| Global | `<home>/.cursor/rules/coding-agents-global.mdc` |
| Rule | `<home>/.cursor/rules/<stem>.mdc` |
| Skill | `<home>/.cursor/skills/<directory>/` |
| Command | no generated surface |
| Agent | no generated surface |
| Permissions | owned pointers in `<home>/.cursor/cli-config.json` and `<home>/.cursor/permissions.json` |

Cursor has one shared MDC rule delivery path for Desktop and Agent. The global
rule filename is reserved. Typed rule fields are `description`, `globs`, and
`alwaysApply`; a scoped rule must state `alwaysApply: false`. Typed skill
fields are `name`, `description`, `paths`, `disable-model-invocation`, and
`metadata`.

Cursor does not receive portable user commands or user agents. Sources must
acknowledge `omit.command` and `omit.agent` as applicable.

Cursor validates `model_policy` metadata but emits no agent configuration. A
skill fallback may use the symbolic policy as guidance only; the target has no
compiler-owned per-subagent model or effort surface.

## Command permission projection

Command rules project to one surface: Desktop's `terminalAllowlist` and
`approvalMode: allowlist` in `permissions.json`, merged with hand-authored
native content under pointer ownership. Cursor CLI reads the same file (the
`cursor-agent.permissions` probe pins it), so it gets the same allowlist and
no generated `cli-config.json` shell entries. The CLI is second class: it has
no hard deny of its own, so a guarded command prompts there as on Desktop,
and `--force` approves it. Desktop stays on `allowlist` whatever the policy
says, so `unmatched: allow` requires `targets.cursor.omit.unmatched`.

- Allows lower to allowlist entries. Cursor matches a plain entry as the whole
  command or a prefix of it at a word boundary, so a rule without a hole is
  one `git status`. A rule with a hole uses the `prog:args` glob form, bare
  and with a trailing `*`, which is not optional. Both shapes are pinned by
  the `cursor-agent.permissions` probe. A bare exact rule (`command: fd,
  exact: true`) lowers to the exact-bare form `fd:`, which matches the program
  with no arguments. A rule with a text predicate has no token-bounded form
  and does not project.
- An allow whose command is an interpreter (`sh`, `bash`, `zsh`, `eval`)
  never projects: `sh` would allow every payload the interpreter runs. The
  rule is skipped with a warning.
- The allowlist has no deny channel, so asks and denies guard by withholding:
  every allow an ask or deny narrows, prefix-overlaps, or whose option-hole
  heads can carry the guarded variant leaves the allowlist, and the guarded
  command prompts. A guard that overlaps an allow without provably narrowing
  it withholds it too, with a warning. A standalone ask or deny leaves nothing
  to emit, since an unlisted command already prompts.
- Wrapped spellings are not listed: `timeout 30 git status` prompts. A bare
  wrapper entry would allow any payload the wrapper carries.

Hooks are not generated. `hooks.json` and its hook scripts are delivered as
raw files: `target-config/cursor/raw/hooks.json` mirrors to
`<home>/.cursor/hooks.json`, where the CLI loads user hooks, and scripts sit
beside it or anywhere else below `raw/`.

Patches target `cli-config.json`, `permissions.json`, `settings.json`, and
`mcp.json` through the corresponding `cursor-*` patch names; CLI settings such
as MCP grants live in the `cursor-cli-config` patch. They may
contribute to a generated pointer where the contribution does not clash; in
particular, a Cursor Desktop patch cannot contribute to the terminal
allowlist. Raw files below
`target-config/cursor/raw/` mirror below `<home>/.cursor`; those four native
pointer files are reserved.

The rules and skills roots use portable manifests, and unrelated Cursor content
is preserved.
