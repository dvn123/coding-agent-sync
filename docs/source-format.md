# Canonical Source Format (schema: coding-agents/v3)

This is the canonical source schema, implemented in `sources.py`,
`target_fields.py`, and the translators.

Per-tool native field meanings, defaults, and discovery rules live in
`docs/targets/claude-code.md`, `codex.md`, `opencode.md`, and `cursor.md`. This file only
documents the canonical _source_ shape and the translation/prefix rules that
connect it to those four targets. When in doubt about what a field does on a
given tool, read that tool's doc.

## Common metadata

```yaml
schema: coding-agents/v3
kind: global | rule | skill | command | agent | permission-policy | permission-rules
id: stable-id
name: display-or-trigger-name
description: human/tool description
```

Markdown kinds carry this metadata in frontmatter. Permission sources under
`permissions/` carry it in their YAML documents. `schema`, `kind`, `id`, and
`name` are required; `description` defaults to empty. No `targets:` wrapper key
exists in v3. See "The prefix convention" below.

## The prefix convention

Three, and only three, categories of field exist:

1. **Canonical fields** — unprefixed, top-level. Reserved for a concept that
   is genuinely translated into two or more tools' native equivalents (same
   underlying intent, tool-appropriate shape). Adding a tool that has no
   equivalent for a canonical field is fine — that tool's translator just
   no-ops on it.
2. **Tool-only fields** — always prefixed with `claude:`, `codex:`,
   `opencode:`, or `cursor:`. Reserved for anything that exists natively on
   exactly one tool, or that overrides a canonical field's derived value for
   one tool specifically. There are no unprefixed tool-specific escape
   hatches in v3.
3. **Internal fields** — always prefixed with `internal:`. Reserved for
   fields that exist purely for the source authors' own bookkeeping and are
   **never rendered into any tool's output** — no tool consumes them
   natively, unlike category 2 which always reaches exactly one tool's
   generated file. The loader strips these before translation; no translator
   ever sees them. Currently: `internal:version` (skill semver, checked
   against no tool's documented frontmatter — Claude, Codex, OpenCode, and
   Cursor skill formats were all checked and none recognize a `version`
   field).

This is a hard rule, not a style preference: if a field has no cross-tool
translation, it is prefixed, even if it is a single scalar Claude and only
Claude understands (`claude:model: sonnet`), and even if the exact same key
name happens to appear on two tools with different enforcement semantics
(e.g. `allowed-tools`, kept prefixed per-tool below — see the Skills section
for why sharing a name isn't sharing a meaning).

Two equivalent forms for tool-only fields, both valid YAML (`key:value` with
no space after the first `:` is one scalar key, not a nested map):

```yaml
# Flat scalar sugar — for a single field
codex:model: gpt-5
codex:model_reasoning_effort: high

# Nested block — for structured data
codex:
  rules:
    - pattern: ["gh", "pr", "view"]
      decision: prompt
      justification: "Viewing PRs requires approval."
```

Both forms populate the same internal per-tool bucket; mix freely on one
source. The loader strips the `<tool>:` prefix and routes the remainder
verbatim into that tool's model: no compiler allowlist drops unknown keys. An
unrecognized key is emitted and logged as a build-time warning, so typos
surface without silently discarding a legitimately supported field. Whether a
target accepts or ignores that field is target-version-specific and must not be
assumed by the compiler.

**Model is never canonical.** Model IDs and aliases don't port across
vendors (Claude's `sonnet`/`opus`/`haiku`/`fable`, Codex's `gpt-5`,
OpenCode's provider+model pair, Cursor's own IDs) — there is nothing to
translate. Every kind that has a model concept sets it as four independent
properties: `claude:model`, `codex:model`, `opencode:model`, `cursor:model`.
Prefer a model _family_ name (`opus`, `gpt-5`) over a dated/versioned release
(`claude-opus-4-8`, `gpt-5.5`) so the source doesn't go stale as vendors ship
point releases.
Setting one has no effect on the others.

## Global

`global/AGENTS.md` requires only the common v3 frontmatter. It has no additional
kind-specific fields: `name` and `description` satisfy the schema but are never
rendered. Only the body compiles into each supported tool's native
global-instructions channel.
Claude's `CLAUDE.md` does not support frontmatter, so v3 emits none.

```yaml
---
schema: coding-agents/v3
kind: global
id: global
name: Shared coding-agent instructions
description: ''
---

# Shared Instructions

Body compiles verbatim to: ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md
(concatenated with rule bodies — see Rules), and ~/.config/opencode/AGENTS.md.
Cursor `AGENTS.md` is project-scoped, so the same body compiles to the reserved
always-on rule ~/.cursor/rules/coding-agents-global.mdc.
```

## Rules

`rules/<id>.md`.

### Canonical fields

| Field               | Type           | Default | Translates to                                                             |
| :------------------ | :------------- | :------ | :------------------------------------------------------------------------ |
| `activation.always` | bool           | `true`  | Claude: unconditional file; Cursor: MDC `alwaysApply`.                    |
| `activation.globs`  | list\[string\] | `[]`    | Cursor: MDC `globs`; other target caveats are described below.            |

### Per-tool delivery (structural, not a field — for context)

- **Claude**: one file per rule at `~/.claude/rules/<id>.md`. **Caveat:**
  [anthropics/claude-code#21858](https://github.com/anthropics/claude-code/issues/21858)
  — `paths:` on user-scope (`~/.claude/rules/`) rules is currently silently
  never loaded, confirmed live, unfixed, no maintainer response as of the
  issue's last activity. Until that closes, v3 **never emits `paths:`** for
  Claude regardless of `activation.globs` — a globs-scoped rule loads
  unconditionally on Claude only (same ceiling OpenCode already has), rather
  than silently not loading at all.
- **Cursor Desktop and Cursor Agent**: one shared MDC file per rule at
  `~/.cursor/rules/<id>.mdc`. Both surfaces use the ancestor-discovered rule
  channel.
  `activation.always` maps to `alwaysApply`; when false,
  `activation.globs` maps to Cursor's comma-delimited `globs` scalar. The
  compiler emits no Desktop rules plugin; that delivery path overlapped with
  ancestor discovery and has been retired.
  `coding-agents-global.mdc` is reserved for the canonical global body and is
  always on; a canonical rule cannot claim that filename.
- **OpenCode**: one entry per rule in `opencode.json`'s `instructions` array
  (a file path string). No per-file glob scoping exists in OpenCode's
  `instructions` mechanism at all — `activation.globs` is a structural no-op
  there, not just an unimplemented translation.
- **Codex**: rule bodies concatenate into `~/.codex/AGENTS.md`, appended after
  the global body. No per-rule scoping exists in Codex's `AGENTS.md` mechanism.

### Tool-only fields

| Field                   | Tool     | Description                                                                                                                                           |
| :---------------------- | :------- | :---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `opencode:instructions` | OpenCode | Override the generated `instructions` array item (defaults to the canonical source path; set to a different local path or a remote URL).              |
| `codex:rules`           | Codex    | Exec-policy list, see below. Unrelated to guideline-rule delivery above — this is Codex's Starlark command-permission DSL, not coding-guideline text. |

`codex:rules` items match Codex's native `prefix_rule(...)` fields one-to-one:

| Field           | Required | Description                                                                                    |
| :-------------- | :------- | :--------------------------------------------------------------------------------------------- |
| `pattern`       | Yes      | Non-empty list of literal command tokens.                                                      |
| `decision`      | No       | `allow`, `prompt`, or `forbidden`. Default `allow`.                                            |
| `justification` | No       | Human-readable reason shown in Codex's approval prompt.                                        |
| `match`         | No       | Inline example commands that must match `pattern` — a validation fixture, not a runtime input. |
| `not_match`     | No       | Inline example commands that must NOT match — a validation fixture.                            |

### Full example

```yaml
---
schema: coding-agents/v3
kind: rule
id: quality
name: quality
description: Shared implementation quality guidance
activation:
  always: true
---
# Implementation Quality
...body...
```

```yaml
---
schema: coding-agents/v3
kind: rule
id: shell-safety
name: shell-safety
description: Shell safety rules
activation:
  globs:
    - "**/*.sh"
codex:
  rules:
    - pattern: ["tool", "publish", "--unsafe"]
      decision: forbidden
      justification: "Unsafe publication requires an explicit safe mode."
      not_match: ["tool publish --safe package"]
---
# Shell Safety
...body...
```

## Skills

`skills/<id>/SKILL.md` plus supporting files, copied as a tree.

### Canonical fields

Shared by exactly the tools that document identical name+shape+semantics
(present on 1 doesn't disqualify a field — an unsupported tool just no-ops):

| Field                      | Type   | Shared by               | Notes                                                                     |
| :------------------------- | :----- | :---------------------- | :------------------------------------------------------------------------ |
| `paths`                    | list   | Claude, Cursor          | Glob-scopes the skill to matching files. No equivalent on Codex/OpenCode. |
| `disable_model_invocation` | bool   | Claude, Cursor          | Blocks auto-invocation; manual `/`-invoke still works.                    |
| `license`                  | string | Codex, OpenCode         | License string/terms reference.                                           |
| `metadata`                 | map    | Codex, OpenCode, Cursor | Arbitrary string-keyed metadata, identical shape on all three.            |

`allowed-tools` is **not** canonical despite appearing on both Claude and
Codex — same name, different enforcement model (Claude: skip permission
prompts; Codex: approval-list gating). Kept as `claude:allowed_tools` /
`codex:allowed-tools` (see below).

### Internal fields

| Field              | Description                                                                                   |
| :----------------- | :-------------------------------------------------------------------------------------------- |
| `internal:version` | Compiler-internal semver for this skill source. Stripped before translation; no tool sees it. |

### Tool-only fields

| Field                      | Tool     | Description                                                                                                                                                                                        |
| :------------------------- | :------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claude:when_to_use`       | Claude   | Extra trigger context appended to `description`.                                                                                                                                                   |
| `claude:argument_hint`     | Claude   | Autocomplete hint, e.g. `[issue-number]`.                                                                                                                                                          |
| `claude:arguments`         | Claude   | Named positional args for `$name` substitution.                                                                                                                                                    |
| `claude:user_invocable`    | Claude   | `false` hides from `/` menu (auto-invoke still allowed).                                                                                                                                           |
| `claude:allowed_tools`     | Claude   | Tools usable without permission prompts while the skill is active.                                                                                                                                 |
| `claude:disallowed_tools`  | Claude   | Tools removed from the available pool while the skill is active.                                                                                                                                   |
| `claude:model`             | Claude   | Model override for this skill's active turn.                                                                                                                                                       |
| `claude:effort`            | Claude   | Reasoning effort override for this skill's active turn.                                                                                                                                            |
| `claude:context`           | Claude   | `fork` runs the skill in an isolated subagent context.                                                                                                                                             |
| `claude:agent`             | Claude   | Named subagent to run this skill's turn as.                                                                                                                                                        |
| `claude:shell`             | Claude   | Shell used for `!command` execution inside the skill body.                                                                                                                                         |
| `codex:allowed-tools`      | Codex    | Codex's approval-list gating (see canonical-vs-not note above).                                                                                                                                    |
| `codex:license`            | Codex    | Redundant alias if you want a Codex-only license string different from the canonical one; normally omit and rely on the canonical `license`.                                                       |
| `opencode:compatibility`   | OpenCode | Compatibility marker string.                                                                                                                                                                       |
| `cursor:` (none currently) | Cursor   | Cursor's documented skill fields (`name`, `description`, `paths`, `disable-model-invocation`, `metadata`) are all covered by common/canonical fields above; no Cursor-only skill field exists today. |

### Full example

```yaml
---
schema: coding-agents/v3
kind: skill
id: inspect-project
name: inspect-project
description: Inspect a project and report concrete findings.
paths:
  - "**/*"
license: Complete terms in LICENSE.txt
metadata:
  owner: platform
claude:
  allowed_tools: [Bash, Read]
  context: fork
codex:
  allowed-tools: [Bash]
---
Skill body...
```

## Commands

`commands/<id>.md`. Structural note: Cursor has no command mechanism at all
(the doc is explicit: author a command intended for Cursor as a `skill` or
`agent` instead — there is no `.cursor/commands/`). Codex has no native
slash-command file format; a canonical command compiles to a generated Codex
skill wrapper (a structural transform, not a field translation).

### Canonical fields

| Field               | Type   | Translates to                                                                                                             |
| :------------------ | :----- | :------------------------------------------------------------------------------------------------------------------------ |
| `execution.agent`   | string | Claude: `agent` invocable key. OpenCode: `agent` field. Same "delegate to this named subagent" intent both places.        |
| `execution.subtask` | bool   | Claude: `context: fork`. OpenCode: native `subtask: true`. Same "run as an isolated turn" intent, different native shape. |

### Tool-only fields

| Field                  | Tool     | Description                                                |
| :--------------------- | :------- | :--------------------------------------------------------- |
| `claude:model`         | Claude   | Model for this command's turn.                             |
| `claude:argument_hint` | Claude   | Autocomplete hint.                                         |
| `claude:allowed_tools` | Claude   | Tools usable without prompts during this command.          |
| `claude:context`       | Claude   | Explicit fork override (rare; prefer `execution.subtask`). |
| `opencode:model`       | OpenCode | Model for this command's turn.                             |

### Full example

```yaml
---
schema: coding-agents/v3
kind: command
id: analyze-changes
name: analyze-changes
description: Analyze changed code for correctness and maintainability.
execution:
  subtask: true
claude:
  allowed_tools: [Read, Edit, Grep]
opencode:model: provider/example-model
---
Command body...
```

## Agents

`agents/<id>.md`. The richest, most divergent kind — every tool models
subagents differently. This is the exhaustive example: every canonical field
plus every documented tool-only field for all four targets, set at once.

### Canonical fields

| Field           | Type                            | Translates to                                                                                                                                     |
| :-------------- | :------------------------------ | :------------------------------------------------------------------------------------------------------------------------------------------------ |
| `tools.inherit` | bool                            | Claude: `tools: inherit`. Derives Cursor/Codex/OpenCode read-only signals below when `false` + empty `allow`.                                     |
| `tools.allow`   | list\[string\]                  | Claude: `tools:` allow-list.                                                                                                                      |
| `tools.deny`    | list\[string\]                  | Claude: `disallowedTools:`. **Derived** (not authored) on other tools — see "Derived permission lowering".                                        |
| `effort`        | `low\|medium\|high\|xhigh\|max` | Claude: `effort`. Codex: `model_reasoning_effort` (clamps/drops values Codex doesn't have). OpenCode: `reasoningEffort`. Cursor: no-op.           |
| `background`    | bool                            | Claude: `background`. Cursor: `is_background`. Codex/OpenCode: no-op.                                                                             |
| `color`         | string                          | Claude: `color`. OpenCode: `color`. Same "UI color" shape and meaning on both — canonical, not two separate prefixed fields. Codex/Cursor: no-op. |

### Derived permission lowering

`tools.allow`/`tools.deny` is Claude's native shape and the only field you
author for permissions. Every other translator lowers it automatically using
a shared "write-capable tools" set (`Edit`, `Write`, `MultiEdit`,
`NotebookEdit`, `Bash`):

- **Cursor**: `readonly: true` if none of the write-capable tools are in
  `allow` (or all are in `deny`).
- **Codex**: `sandbox_mode: read-only` under the same condition, else
  `workspace-write`.
- **OpenCode**: `permission.edit`/`permission.bash`/etc. set to `deny` for
  each denied write-capable tool, `allow` for each explicitly allowed one.

This is a best-effort heuristic, not a guarantee — override it per tool with
the tool-only fields below when it gets a specific agent wrong.

### Tool-only fields

| Field                          | Tool     | Description                                                                                           |
| :----------------------------- | :------- | :---------------------------------------------------------------------------------------------------- |
| `claude:model`                 | Claude   | Model alias, full ID, or `inherit`.                                                                   |
| `claude:permission_mode`       | Claude   | `default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, or `plan`.                          |
| `claude:max_turns`             | Claude   | Maximum agentic turns before stopping.                                                                |
| `claude:isolation`             | Claude   | Context isolation mode.                                                                               |
| `claude:memory`                | Claude   | Persistent-memory mode for this subagent.                                                             |
| `claude:initial_prompt`        | Claude   | Prompt injected at subagent startup.                                                                  |
| `claude:skills`                | Claude   | Skill names to preload (full content, not just description).                                          |
| `codex:model`                  | Codex    | Model ID.                                                                                             |
| `codex:model_reasoning_effort` | Codex    | Override the value derived from canonical `effort`.                                                   |
| `codex:sandbox_mode`           | Codex    | Override the value derived from canonical `tools`.                                                    |
| `codex:nickname_candidates`    | Codex    | List of candidate display nicknames.                                                                  |
| `opencode:model`               | OpenCode | Model ID.                                                                                             |
| `opencode:provider`            | OpenCode | Provider ID paired with `model`.                                                                      |
| `opencode:reasoning_effort`    | OpenCode | Override the value derived from canonical `effort`.                                                   |
| `opencode:permission`          | OpenCode | Fine-grained permission map; merges over (doesn't replace) the values derived from canonical `tools`. |
| `opencode:mode`                | OpenCode | `primary`, `subagent`, or `all`.                                                                      |
| `opencode:steps`               | OpenCode | Maximum agentic iterations.                                                                           |
| `opencode:temperature`         | OpenCode | Sampling temperature.                                                                                 |
| `opencode:top_p`               | OpenCode | Nucleus sampling value.                                                                               |
| `opencode:disable`             | OpenCode | `true` disables the agent.                                                                            |
| `opencode:hidden`              | OpenCode | `true` hides from autocomplete.                                                                       |
| `cursor:model`                 | Cursor   | Model ID or `inherit`.                                                                                |
| `cursor:readonly`              | Cursor   | Override the value derived from canonical `tools`.                                                    |
| `cursor:is_background`         | Cursor   | Override the value derived from canonical `background`.                                               |

MCP servers and hooks are entirely out of scope for every tool (no
`mcp_servers`, no `hooks` field, canonical or tool-only, on any kind) — each
tool configures both natively, outside this compiler.

### Full example — every field set

```yaml
---
schema: coding-agents/v3
kind: agent
id: sample-reviewer
name: sample-reviewer
description: >-
  Review a completed change against its requirements and report concrete
  concerns.

tools:
  inherit: false
  allow: [Read, Grep, Glob, Bash]
  deny: [Edit, Write, NotebookEdit]
effort: high
background: false
color: yellow

claude:
  model: sonnet
  permission_mode: default
  max_turns: 12
  isolation: fork
  memory: none
  initial_prompt: "Review the change and report findings only."
  skills: [sample-skill]

codex:
  model: gpt-5
  model_reasoning_effort: high
  sandbox_mode: read-only
  nickname_candidates: [Atlas, Delta, Echo]

opencode:
  model: example-model
  provider: example-provider
  reasoning_effort: high
  permission:
    bash:
      "tool publish*": deny
  mode: subagent
  steps: 20
  temperature: 0.2
  top_p: 0.9
  disable: false
  hidden: false

cursor:
  model: inherit
  readonly: true
  is_background: false
---
Subagent system prompt in Markdown.
```

Every canonical field here (`tools`, `effort`, `background`, `color`) drives all four
outputs from one authored value each; every `claude:`/`codex:`/`opencode:`/
`cursor:` block is that tool's own concept with no equivalent anywhere else,
prefixed so it's unambiguous at a glance which fields are portable and which
are one tool's alone.

## User permissions

Permissions combine one policy with command fragments:

```text
permissions/
├── policy.yaml
└── commands/*.yaml
permissions.local/*.yaml
```

`permissions.local/*.yaml` is optional and resolves from `config_root`, just
like the committed source tree. Local and committed command fragments use the
same schema and merge before validation. File placement and lexical order do
not provide precedence.

### Policy

```yaml
schema: coding-agents/v3
kind: permission-policy
id: user
name: User permissions
description: Portable user-scoped permissions.
wrappers: [env, timeout]
tools:
  read: allow
  edit: ask
  write: ask
  webfetch: allow
  websearch: ask
workspace:
  allow: [~/shared]
  ask: [/opt/review]
secret_paths: ['**/.env']
secret_names: [SERVICE_TOKEN]
```

- `wrappers` lists literal command tokens that can hide a payload from a
  target matcher. Translators emit wrapper grants and repeat stricter guards
  where the target cannot peel the wrapper itself.
- `tools` maps `read`, `edit`, `write`, `webfetch`, or `websearch` to `allow`,
  `ask`, or `deny`. Undeclared classes keep the target's default.
- `workspace.allow` and `workspace.ask` contain unique absolute or
  `~`-relative directory roots.
- `secret_paths` contains native path patterns denied on targets with a path
  permission channel.
- `secret_names` contains literal environment-variable names. Claude Code and
  OpenCode lower each name to a whole-command ask pattern; other targets omit
  it when no equivalent ask channel exists.

### Command rules

Each `commands/*.yaml` or `permissions.local/*.yaml` document owns one domain:

```yaml
schema: coding-agents/v3
kind: permission-rules
id: version-control
name: Version control
description: Portable command permissions.
options:
  git: [-C, -c, --no-pager]
allow:
  - [git, status]
ask:
  - command: git
    subcommand: push
    tail: [[--force-with-lease]]
deny:
  - command: git
    subcommand: push
    tail: [[--force]]
```

A list entry is shorthand for `command` plus `subcommand`. The expanded form
supports these independent predicates:

| Field | Meaning |
| --- | --- |
| `command` | Required portable literal argv token. |
| `subcommand` | Literal tokens immediately following the command, apart from declared leading options. |
| `tail` | Alternative token sequences matched after the head. |
| `text` | Alternative literal strings matched anywhere after the head. |
| `exact` | Require the command to end after the subcommand. |

`tail` and `text` may narrow any decision, but cannot appear together on one
rule. `exact` cannot combine with either. Tokens reject whitespace, shell
syntax, and native wildcard syntax so the same source retains meaning across
targets.

`options` declares leading option tokens that may appear between a command and
its subcommand. Every token must begin with `-`. A command's vocabulary may be
declared in only one fragment and must be used by at least one rule.

Validation rejects duplicate predicate sets and redundant or unreachable
containment. A stricter decision may narrow a looser one: `ask` may narrow
`allow`, and `deny` may narrow either. The inverse and same-bucket containment
are invalid.

### Lowering

- Claude Code emits command globs in `permissions.allow`, `.ask`, and `.deny`,
  plus native tool, workspace, secret-name, and secret-path entries.
- Codex receives no portable command permissions. Its exec-rule matcher cannot
  represent the portable predicates safely. Tool, workspace, and secret policy
  also have no equivalent generated channel.
- Cursor Agent emits expressible allows and denies. Portable asks are omitted,
  never converted into denies. Free-text rules and exact zero-argument rules
  have no verified native representation and are omitted.
- Cursor Desktop receives only expressible allows in `terminalAllowlist`; its
  shipped schema has no ask or deny channel.
- OpenCode emits an ordered bash map. The broad fallback comes first, followed
  by `allow`, `ask`, secret-name asks, and `deny`, so its last-match semantics
  make the strictest applicable decision win.

Leading options expand to target-native matcher variants. Wrapper guards are
repeated only where required by the target. Unsupported projections are
omitted rather than approximated with a stricter or looser decision.

Native patches remain available for target-only settings and target-native
tool entries. OpenCode's bash map and Cursor Desktop's terminal allowlist are
reserved generated pointers. Patches may add only `deny` entries to generated
OpenCode read, edit, and external-directory maps. Portable command grants and
review rules belong in the permission sources above.
