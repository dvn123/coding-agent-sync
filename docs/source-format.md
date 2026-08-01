# Canonical Source Format (schema: coding-agents/v4)

Coding Agents Sync is a lossless, target-first compiler. A source shares a
field only when its user-visible meaning is the same on every target that
receives it. Target-specific capabilities stay in that target's compiler.

Every source uses this support ladder:

1. Canonical portable intent.
2. Strict, typed target-native fields under `targets.<target>.native`.
3. Visibly unvalidated target-native fields under `targets.<target>.raw`, or
   byte-for-byte raw target files.

Unknown canonical and typed-native fields are errors. An unsupported or lossy
non-default portable value is an error until the source acknowledges it with a
reasoned `omit` entry.

## Source tree

```text
global/AGENTS.md
rules/*.md
skills/*/SKILL.md
commands/*.md
agents/*.md
permissions/policy.yaml
permissions/commands/*.yaml
permissions.local/*.yaml
patches/*.yaml
patches.local/*.yaml
target-config/<target>/raw/*
```

`config_root` owns all of these inputs and the native pointer/hash manifest.
`home` owns generated target files only. `permissions.local/` is optional and
is merged with committed permission fragments before validation. Local patches
must be mode `0600`.

## Common Markdown shape

`global`, `rule`, `skill`, `command`, and `agent` documents have YAML
frontmatter and a Markdown body:

```yaml
---
schema: coding-agents/v4
kind: skill
id: inspect-project
name: inspect-project
description: Inspect a project and report concrete findings.
targets: {}
---

Skill body.
```

`schema`, `kind`, `id`, and `name` are required. `description` defaults to an
empty string. `kind` must match the source directory. Unknown top-level fields
are errors except the documented canonical fields below and `targets`.
`internal` is ignored for source-author bookkeeping; it is never compiled.
Duplicate YAML keys are errors, including nested target block keys.

Permission documents use the same common fields in YAML, without frontmatter.

## Canonical portable fields

| Kind | Fields |
| --- | --- |
| Global | No additional fields. |
| Rule | No additional fields. |
| Skill | `paths`, `disable_model_invocation`, `license`, `metadata`. |
| Command | `execution.agent`, `execution.subtask`. |
| Agent | `effort`, `background`, `color`. |
| Permission policy | `wrappers`, `tools`, `workspace`, `secret_paths`, `secret_names`. |
| Permission rules | `options`, `allow`, `ask`, `deny`. |

The compiler projects a canonical field only where its semantics match. For
example, skill `paths` and `disable_model_invocation` require omissions for
Codex and OpenCode; agent `background` requires omissions for Codex and
OpenCode; agent `color` requires an omission for Codex; and `effort: max`
requires one for Codex. Cursor and Codex have no portable command-policy
projection, so every non-empty permission-rule fragment must acknowledge
`targets.cursor.omit.commands` and `targets.codex.omit.commands`.

### Skills

`paths` is a list of file globs; `disable_model_invocation` is boolean;
`license` is a string; `metadata` is a map. These are canonical only for the
targets whose native behavior is equivalent. Do not substitute a similar field
from another target for one of these values.

### Commands

`execution.agent` asks Claude and OpenCode to invoke a named subagent.
`execution.subtask: true` asks each to run the command as an isolated subtask.
Cursor and Codex require `omit.command`, because neither has this generated
user-command surface.

### Agents

`effort` is one of `low`, `medium`, `high`, `xhigh`, or `max`. `background`
and `color` are portable only as described above. Tool allow/deny/inheritance
is deliberately not portable agent intent: write strict target-native settings
instead.

### Permissions

`tools` maps `read`, `edit`, `write`, `webfetch`, or `websearch` to `allow`,
`ask`, or `deny`. `workspace.allow` and `.ask` contain `/`-absolute or `~/`
roots. Command rules use portable literal argv tokens:

```yaml
schema: coding-agents/v4
kind: permission-rules
id: version-control
name: Version control
description: Narrow command permissions
options:
  git: [-C, --no-pager]
allow:
  - [git, status]
ask:
  - command: git
    subcommand: [push]
    tail: [[--force-with-lease]]
deny:
  - command: git
    subcommand: [push]
    tail: [[--force]]
targets:
  cursor:
    omit:
      commands: Cursor has no lossless portable command policy.
  codex:
    omit:
      commands: Codex has no portable command policy.
```

Fragments are merged before validation. Duplicate rules, contradictory
containment, duplicate option vocabularies, shell syntax, wildcard tokens, and
lossy predicate combinations are errors.

## Target blocks

`targets` is a mapping whose only keys are `claude`, `cursor`, `codex`, and
`opencode`. A target block has exactly these optional mappings:

```yaml
targets:
  claude:
    native:
      model: sonnet
    raw:
      future-native-key: future-native-value
    omit:
      some.portable.path: Concrete reason this target cannot preserve it.
```

`native` is validated by the relevant target compiler for that source kind.
The keys are exact target-native spellings accepted by that compiler. An
unknown key, invalid value, or native block on an unsupported source kind is a
build error.

`raw` is an escape hatch for a documented source surface that can preserve the
field without interpretation. It is never normalized or silently hidden. A
raw field that shadows canonical or typed-native output is an error; an
accepted raw field produces a warning naming its source. Raw is not available
for rule surfaces that have no safe field-level representation.

`omit` maps a dotted portable field path to a non-empty, trimmed reason. Every
entry must be consumed by that target compiler. Stale, misspelled, unsupported,
or unnecessary omissions are errors. This forces the source to record why a
non-default portable value is intentionally absent from one target.

## Strict target-native fields

Target compilers own their native schemas. The supported typed fields are:

| Target | Source kinds and typed fields |
| --- | --- |
| Claude | Skills: native Claude skill frontmatter. Commands: `description`, `agent`, `context`, `model`, `argument-hint`, `allowed-tools`. Agents: native Claude agent frontmatter including `model`, `tools`, `disallowedTools`, permission mode, turn limits, isolation, memory, initial prompt, and skills. |
| Cursor | Rules: `description`, `globs`, `always_apply`; `globs` requires `always_apply: false`. Skills: `name`, `description`, `paths`, `disable-model-invocation`, `metadata`. |
| Codex | Rules: `rules` containing `pattern`, `decision`, and optional `justification`. Skills: `name`, `description`, `license`, `metadata`, `allowed-tools`. Agents: `name`, `description`, `developer_instructions`, `model`, `model_reasoning_effort`, `sandbox_mode`, `nickname_candidates`. |
| OpenCode V1 | Rules: `instructions`. Skills: `name`, `description`, `license`, `metadata`, `compatibility`. Commands: `name`, `description`, `agent`, `subtask`, `model`. Agents: `name`, `description`, `model`, `provider`, `color`, `reasoningEffort`, `permission`, `mode`, `steps`, `temperature`, `top_p`, `disable`, `hidden`. |

The table is intentionally concrete, not a target extension API. OpenCode V1
is the only active OpenCode adapter. V2 remains out of scope until it is
stable; the compiler does not carry a permanent version framework.

## Raw target files

Place raw files below `target-config/<target>/raw/`. They are copied
byte-for-byte below that target's home root and are included in one file
manifest rooted at that target directory. This declaration gives raw files the
same stale-output cleanup and modified-output refusal as generated files.

Raw inputs may not be symlinks. The compiler rejects unsafe paths, files that
collide with generated output, and files that collide with a native pointer
surface. Current native-pointer exclusions are:

| Target | Raw root | Excluded paths |
| --- | --- | --- |
| Claude | `<home>/.claude` | `settings.json` |
| Cursor | `<home>/.cursor` | `cli-config.json`, `permissions.json`, `settings.json`, `mcp.json` |
| Codex | `<home>/.codex` | `config.toml`, `rules/coding-agents.rules` |
| OpenCode V1 | `<home>/.config/opencode` | `opencode.json` |

Use a raw file when a complete target-native artifact cannot be represented by
typed fields. It produces an explicit warning, not an implicit compatibility
promise.

## Native pointer patches

Patch files use `schema: coding-agents/patch/v1`. Committed files live below
`patches/`; local files of the same name live below `patches.local/` and must
be mode `0600`. The named patch surfaces are:

| Patch name | Native file |
| --- | --- |
| `claude-settings` | `<home>/.claude/settings.json` |
| `claude-mcp` | `<home>/.claude.json` |
| `cursor-cli-config` | `<home>/.cursor/cli-config.json` |
| `cursor-permissions` | `<home>/.cursor/permissions.json` |
| `cursor-settings` | `<home>/.cursor/settings.json` |
| `cursor-mcp` | `<home>/.cursor/mcp.json` |
| `codex` | `<home>/.codex/config.toml` |
| `opencode` | `<home>/.config/opencode/opencode.json` |

Patches operate on named JSON/TOML pointers and preserve unnamed native
fields. A patch cannot overlap a generated pointer, portable file, or raw
file. Target compilers additionally enforce their own safety rules, such as
OpenCode's command-policy exclusion and Cursor Desktop terminal-allowlist
exclusion.

## Ownership and publication

Each compiler returns a `Plan` of `OwnedFile`, `OwnedTree`, `NativeValue`,
`NativePatch`, and `Diagnostic`. The shared lower layer does not know target
schemas. It performs global preflight, validates path and pointer collisions,
reconciles manifests and native semantic hashes, writes portable and native
content atomically, then publishes manifests last.

Portable manifests preserve unmanifested siblings and refuse to claim or
replace modified output. Native ownership is pointer plus semantic hash in
`config_root/.coding-agents-native.json`; modified generated pointers are
refused, while unnamed pointers remain native-tool-owned. A failed preflight
makes no generated writes.
