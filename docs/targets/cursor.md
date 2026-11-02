# Cursor Target Reference

## Generated surfaces

| Source | Output |
| --- | --- |
| Global | `<home>/.cursor/rules/coding-agents-global.mdc` |
| Rule | `<home>/.cursor/rules/<stem>.mdc` |
| Skill | `<home>/.agents/skills/<directory>/`, shared with Codex and OpenCode; `<home>/.cursor/skills/<directory>/` when `only` leaves out Codex, Cursor, or OpenCode |
| Command | `<home>/.cursor/skills/<stem>/SKILL.md` |
| Agent | `<home>/.cursor/agents/<file>.md` (Desktop only) |
| Permissions | owned pointers in `<home>/.cursor/permissions.json` |
| Secret paths and denied directories | `<home>/.cursorignore` |

Desktop is the primary host; the CLI is second class. Every surface here is a
native user-scope file, not a plugin: Cursor's local plugins
(`<home>/.cursor/plugins/local/<name>/`) bundle the same rules, skills,
agents, commands, and hooks, so they would add a layer without adding a
capability.

Cursor has one shared MDC rule delivery path for Desktop and Agent. Both walk
from the workspace up through its ancestors for `.cursor/rules`, so the user
rules apply to workspaces below `<home>`; the `cursor-desktop.rules` probe
pins that ancestor load. The global rule filename is reserved. Typed rule
fields are `description`, `globs`, and `alwaysApply`; a scoped rule must state
`alwaysApply: false`. Typed skill fields are `name`, `description`, `license`, `paths`,
`disable-model-invocation`, and `metadata`; Cursor ignores frontmatter it
does not read.

A command becomes a skill with `disable-model-invocation: true`, which is how
Cursor's own `migrate-to-skills` converts `.cursor/commands`: the user invokes
it from the `/` menu and the model never does by itself. Cursor skills have no
delegated execution, so `execution.agent` or `execution.subtask` needs
`omit.execution`.

Desktop loads user agents from `<home>/.cursor/agents` (the
`cursor-desktop.agents` probe pins the user provider); the CLI does not
(`cursor-agent.agents-user`). Desktop does not read agent frontmatter as
YAML: it splits each line at its first colon and keeps the rest verbatim,
quotes included. The compiler therefore writes each value bare on one line,
collapses the description's whitespace, and exempts these files from YAML
validation. Fields are `name`, `description`, `model` (a slug from
`cursor-agent models`, absent to inherit), `readonly`, `tools` (a list,
written comma-separated), and `background`.
Cursor slugs carry the effort, so the model policy's `cursor` profile takes
only `model`, and a portable `effort` needs an omission; `color` is dropped.

### Third-party extensibility

Cursor's "third-party extensibility" setting is on by default. It makes Cursor
also load `<home>/.claude/skills`, `<home>/.codex/skills`,
`<home>/.claude/agents`, `<home>/.claude/commands`, and enabled Claude Code
plugins. Skills are merged by path, not by name, so with the setting on a
skill Claude also receives appears twice, once from `<home>/.agents/skills`
and once from Claude's copy, and Claude agents load in Claude's dialect. An agent of the same name in
`<home>/.cursor/agents` wins. The setting lives in Cursor's application
storage, not a configuration file, so the compiler cannot own it: turn it off
in Cursor Settings to make this target Cursor's only source.

## Secret paths

Cursor's agent reads a `.cursorignore` in the workspace and in every directory
above it, for its file tools and for its sandboxed shell, so the compiler
writes `secret_paths` and denied workspace directories to `<home>/.cursorignore`
and every workspace below home gets them. A `**/` pattern is written as is and
a `~/` path is anchored at home (`/.aws/credentials`). An absolute path outside
home has no such ancestor and is skipped, and a workspace outside home gets
none of the file. The file's manifest is `<home>/.coding-agents-managed.json`.
The `cursor-agent.ignore` live scenario runs the compiled file against the
CLI's agent, which shares this runtime with Desktop. Desktop's
`cursor.general.globalCursorIgnoreList` setting is not read by the agent
runtime, so it does not stop the agent's tools.

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
  wrapper entry would allow any payload the wrapper carries, a `*` hole after
  the wrapper swallows a whole command, and one entry per wrapper and rule
  multiplies the allowlist. Cursor needs few wrappers: the parser leaves
  leading `VAR=value` assignments out of the matched text (the
  `assignment-prefix` scenario), and the Shell tool has its own `timeout`,
  `is_background`, and `block_until_ms`. A Cursor-only rule that steers the
  agent to those forms keeps wrapped commands rare without any entries.

A `beforeShellExecution` hook can answer `deny` or `ask`, but its `allow`
falls through to the ordinary approval flow, so a hook cannot replace the
allowlist; it could only add a hard deny, which needs a script rather than
configuration.

Hooks are not generated. `hooks.json` and its hook scripts are delivered as
raw files: `target-config/cursor/raw/hooks.json` mirrors to
`<home>/.cursor/hooks.json`, where the CLI and Desktop load user hooks, and
scripts sit beside it or anywhere else below `raw/`.

Patches target `cli-config.json`, `permissions.json`, `settings.json`, and
`mcp.json` through the corresponding `cursor-*` patch names; CLI settings such
as MCP grants live in the `cursor-cli-config` patch. They may
contribute to a generated pointer where the contribution does not clash; in
particular, a Cursor Desktop patch cannot contribute to the terminal
allowlist. Raw files below
`target-config/cursor/raw/` mirror below `<home>/.cursor`; those four native
pointer files are reserved.

The rules, skills, and agents roots use portable manifests, and unrelated
Cursor content is preserved.
