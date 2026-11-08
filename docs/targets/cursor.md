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
`omit: [execution]`.

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
no generated `cli-config.json` shell entries. Both load the shell-unwrap hook
plugin that carries the guards; `--force` approves its asks but not its
denies (the `hook-denied-forced` scenario). Desktop stays on `allowlist` whatever the policy
says, so `unmatched: allow` requires `unmatched` in `targets.cursor.omit`.
Desktop's agent runs file tools unasked inside the workspace: a write asks
only outside it, for a protected config file, or when `promptWorkspaceWrites`
is set, which Desktop never sets. A web fetch asks unless the host is in the
in-app `webFetchDomainAllowlist`, and a web search asks unless the in-app
toggle or run-everything mode is on; both live in Cursor's state store, not
in a file, and a subagent approves both itself.

- Allows lower to allowlist entries. Cursor matches a plain entry as the whole
  command or a prefix of it at a word boundary, so a rule without a hole is
  one `git status`. A rule with a hole uses the `prog:args` glob form, bare
  and with a trailing `*`, which is not optional. Both shapes are pinned by
  the `cursor-agent.permissions` probe. A bare exact rule (`exact: fd`)
  lowers to the exact-bare form `fd:`, which matches the program
  with no arguments. A rule with a text predicate has no token-bounded form
  and does not project.
- An allow whose command is an interpreter (`sh`, `bash`, `zsh`, `eval`)
  never projects: `sh` would allow every payload the interpreter runs. The
  rule is skipped with a warning.
- The allowlist has no deny channel, so every allow lands as written and a
  `beforeShellExecution` hook carries the asks and denies. It is shell-unwrap's
  hook bundle, shared with Claude and run with the `cursor` argument, at
  `<home>/.cursor/hooks/shell-unwrap.js`, registered in the
  user `hooks.json` that Desktop and the CLI both load, beside
  `shell-unwrap.json`: Claude's `Bash(...)` ask and deny patterns and the deny
  reasons. It is not a local plugin, because Desktop skips user-local plugins
  when a team turns off `allowUserLocalPluginImports`. It parses each call, peels every command inside it as
  the Claude hook does, and denies or asks when any spelling matches. Cursor
  ignores an `allow` from the hook, so it answers `{}` otherwise and the
  allowlist decides. It asks when it cannot parse the command or bun cannot
  run, and the hook is `failClosed`, so the guards never silently lapse.
  Desktop runs `beforeShellExecution` before every shell call
  (`runPreExecutionHooks` in `cursor-agent-exec`): `deny` refuses and `ask`
  prompts.
- Wrapped spellings are mostly not listed: `timeout 30 git status` prompts. A
  bare wrapper entry would allow any payload the wrapper carries, a `*` hole
  after the wrapper swallows a whole command, and one entry per wrapper and
  rule multiplies the allowlist. The exception is an allowed whole program
  (`rg`), which also lands as `command rg` and `xargs rg`: both run it as
  given, and agents pipe into `xargs` often. Cursor needs few wrappers: the parser leaves
  leading `VAR=value` assignments out of the matched text (the
  `assignment-prefix` scenario), and the Shell tool has its own `timeout`,
  `is_background`, and `block_until_ms`. A Cursor-only rule that steers the
  agent to those forms keeps wrapped commands rare without any entries.

Other hooks are not generated. The `cursor-hooks` patch adds them to
`<home>/.cursor/hooks.json`, extending the generated `beforeShellExecution`
list or setting other events; their scripts are raw files below `raw/`.

Patches target `cli-config.json`, `permissions.json`, `settings.json`,
`mcp.json`, and `hooks.json` through the corresponding `cursor-*` patch
names. A source's MCP
tool allows land in Desktop's `mcpAllowlist`; CLI-only grants, such as an
`Mcp(<server>:get_*)` glob, live in the `cursor-cli-config` patch. They may
contribute to a generated pointer where the contribution does not clash; in
particular, a Cursor Desktop patch cannot contribute to the terminal
allowlist. Raw files below
`target-config/cursor/raw/` mirror below `<home>/.cursor`; those five native
pointer files are reserved.

The rules, skills, and agents roots use portable manifests, and unrelated
Cursor content is preserved.
