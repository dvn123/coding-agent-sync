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
`always_apply`; a scoped rule must state `always_apply: false`. Typed skill
fields are `name`, `description`, `paths`, `disable-model-invocation`, and
`metadata`.

Cursor does not receive portable user commands or user agents. Sources must
acknowledge `omit.command` and `omit.agent` as applicable.

## Command permission projection

Command rules project to two native surfaces. The CLI receives
`approvalMode`, `permissions.allow`, and `permissions.deny` in
`cli-config.json`; Desktop receives `approvalMode` and `terminalAllowlist` in
`permissions.json`. Both files are merged with hand-authored native content
under pointer ownership.

The CLI's `approvalMode` carries the policy's `unmatched` decision:
`allowlist` for `ask`, `unrestricted` for `allow`. The third enum value,
`manual`, prompts for everything and has no portable spelling. `unrestricted`
waives the allowlist but not `permissions.deny`, which the
`cursor-agent.config` probe pins, so the CLI's guards keep binding.

Desktop stays on `allowlist` whatever the policy says, so `unmatched: allow`
requires `targets.cursor.omit.unmatched`. Desktop has no deny channel: its
only answer to a guarded command is to withhold it from `terminalAllowlist`
and prompt. `unrestricted` there returns true from `getModeFullAutoRun`
unconditionally, which would run every guarded command without asking, so the
mode the CLI takes is not one Desktop can be given.

- Allow and deny rules lower to `Shell(...)` entries with the program before
  the colon and the token pattern after it. A bare exact rule (`command:
  fd, exact: true`) lowers to the exact-bare form `fd:`, which matches the
  program with no arguments. A rule with a text predicate has no
  token-bounded form and does not project.
- An allow whose command is an interpreter (`sh`, `bash`, `zsh`, `eval`)
  never projects: `Shell(sh)` would allow every payload the interpreter
  runs. The rule is skipped with a warning. The set is exactly these four,
  the interpreters with live-probe evidence in the capability suite; other
  interpreters (`python`, `node`, ...) are not filtered today.
- A standalone ask never leaves the source: unlisted commands prompt, which
  is already ask-equivalent.
- An ask that narrows an allow claws the guarded variant back through the
  CLI deny channel. The clawback is absolute (a CLI deny holds even under
  `--force`), but the alternative is letting the dangerous variant ride the
  allow. Desktop has no deny channel, so the allow rules the guarded
  variant rides leave its allowlist and prompt per invocation, which is
  ask-equivalent, while the rest of the family stays allowlisted. The
  colliding rules are the ones the ask provably narrows, the ones it
  prefix-overlaps, and the ones whose option-hole heads can carry the
  variant (an allow whose subcommand embeds declared options ahead of the
  ask's). A narrowing ask the token matcher cannot express (a text
  predicate) drops the same rules from the CLI allowlist too.
- An ask that shares an allow's subcommand prefix without provably narrowing
  it (for example, the ask re-expresses the allow's tail as subcommand
  tokens) is a guarded overlap: it is clawed back and excludes the colliding
  rules from Desktop the same way, and the compile emits a warning, because
  containment cannot be proven and the residual overlap would otherwise ride
  the allow.
  An ask that embeds the allow's declared option vocabulary (`git -C foo
  status` against allow `[git, status]` with `options: {git: [-C]}`) is a
  guarded overlap for the same reason: it rides the emitted option-hole
  head. An ask on a distinct subcommand or against an exact allow stays
  standalone.
- A deny rule whose text predicate has no token-bounded form degrades to a
  prompt (allowlist mode), with a warning.
- Wrappers never produce a bare allow: `Shell(env)` would match any payload
  the wrapper carries, so each rule is emitted bare and once per declared
  wrapper with the wrapper attached (`env:git status`, `env:* git status`,
  and their trailing-star forms).
- Desktop has no deny channel, so a fragment containing deny rules must
  acknowledge `targets.cursor.omit.commands.deny`. The CLI still projects
  the deny; the omission records the Desktop-side loss.

Hooks are not generated. `hooks.json` and its hook scripts are delivered as
raw files: `target-config/cursor/raw/hooks.json` mirrors to
`<home>/.cursor/hooks.json`, where the CLI loads user hooks, and scripts sit
beside it or anywhere else below `raw/`.

Patches target `cli-config.json`, `permissions.json`, `settings.json`, and
`mcp.json` through the corresponding `cursor-*` patch names. They may
contribute to a generated pointer where the contribution does not clash; in
particular, a Cursor Desktop patch cannot contribute to the terminal
allowlist. Raw files below
`target-config/cursor/raw/` mirror below `<home>/.cursor`; those four native
pointer files are reserved.

The rules and skills roots use portable manifests. The compiler retires only
its previously manifested legacy agent/plugin roots and preserves unrelated
Cursor content.
