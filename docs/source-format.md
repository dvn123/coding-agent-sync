# Canonical Source Format

Coding Agents Sync is a lossless, target-first compiler. A source shares a
field only when its user-visible meaning is the same on every target that
receives it. Target-specific capabilities stay in that target's compiler.

Every source uses this support ladder:

1. Canonical portable intent.
2. Strict, typed target-native fields under `targets.<target>.native`.
3. Visibly unvalidated target-native fields under `targets.<target>.raw`, or
   byte-for-byte raw target files.

Unknown canonical and typed-native fields are errors. An unsupported or lossy
non-default portable value is an error until the source lists it under
`omit`.

## Source tree

```text
model-policy.yaml
global/AGENTS.md
rules/*.md
skills/*/SKILL.md
commands/*.md
agents/*.md
permissions/policy.yaml
permissions/commands/*.yaml
permissions.local/*.yaml
mcp/*.yaml
mcp.local/*.yaml
patches/*.yaml
patches.local/*.yaml
target-config/<target>/raw/*
```

`config_root` owns all of these inputs and the native pointer/hash manifest.
`home` owns generated target files only. `permissions.local/` is optional and
is merged with committed permission fragments before validation.
`mcp.local/` holds machine-local servers beside `mcp/`; a name may appear in
only one of them. Local patches and local MCP servers must be mode `0600`.

## Common Markdown shape

`global`, `rule`, `skill`, `command`, and `agent` documents are Markdown with
optional YAML frontmatter:

```yaml
---
description: Inspect a project and report concrete findings.
targets: {}
---

Skill body.
```

A source's directory gives its kind and its file name its name: a skill is
named by its directory, every other kind by its file stem. A skill may also
carry the Agent Skills `name` key, which must equal its directory, so a
published `SKILL.md` drops in unchanged. `description` defaults to an empty
string. Unknown top-level fields are errors except the documented canonical
fields below and `targets`. `internal` is ignored for source-author
bookkeeping; it is never compiled. Duplicate YAML keys are errors, including
nested target block keys.

Permission and MCP documents are plain YAML with the same field rules. An
empty file is an empty source.

## Canonical portable fields

| Kind | Fields |
| --- | --- |
| Global | `enabled`, `only`. |
| Rule | `enabled`, `only`. |
| Skill | `enabled`, `only`, `paths`, `disable_model_invocation`, `license`, `metadata`. |
| Command | `enabled`, `only`, `execution.agent`, `execution.subtask`. |
| Agent | `enabled`, `only`, `model_policy`, `effort`, `background`, `color`, `deny`. |
| Permission policy | `wrappers`, `unmatched`, `tools`, `workspace`, `secret_paths`, `secret_names`. |
| Permission rules | `enabled`, `options`, `allow`, `ask`, `deny`. |
| MCP server | `enabled`, `only`, `command`, `args`, `env`, `url`, `tools`. |

`model-policy.yaml` is a strict compiler input. It names a `default` profile and defines
named target profiles; a profile without a `model` makes the agent inherit
the invoking session's model. Each profile may set only `model` and `effort` for
`claude`, `opencode`, `pi`, and optionally `codex`, and only `model` for `cursor`,
whose model slugs carry the effort (`claude-sonnet-5-5-medium`). Codex effort
values are model-defined strings; the other profiles use the portable effort
vocabulary.
An absent `codex` entry preserves existing native agent model settings. An
explicit `codex: {}` selects inheritance and owns the model fields, so it
cannot be combined with native or raw model overrides. A named agent selects a profile with
`model_policy`; an omitted selector uses `default`. The `inherit` profile emits
no model or effort pin, except Claude emits its explicit native `model: inherit`
marker so it follows the parent conversation rather than Claude's model-order
fallback.

`only` is an optional list of target names (`claude`, `cursor`, `codex`,
`opencode`, `pi`). Absent or empty means every target that supports the kind receives
the artifact. A non-empty list restricts emission to those targets: compilers
skip emit and omit checks for every other target, and any `targets.<name>`
block outside the list is an error. Prefer `only` when guidance is
intentionally host-specific; keep `omit` for unavoidable field loss on a
target that still receives the artifact. Host files (`CLAUDE.md`,
`AGENTS.md`, Cursor's global MDC) are manifested like every other generated
file, so a global excluded by `only` is pruned, and a hand-written host file
is refused rather than overwritten.

`enabled: false` keeps a source in the tree but delivers it nowhere, so the
next sync removes what it generated; it defaults to `true`. A global, rule,
skill, command, or agent then reaches no target, and a permission rules
fragment, committed or local, contributes none of its rules. An MCP server is
the exception: `enabled: false` still registers it, switched off, as
[MCP servers](#mcp-servers) describes. A native patch takes the same key, below.

The compiler projects a canonical field only where its semantics match, and
a field that changes behavior needs an omission where a target cannot carry
it: agent `background` on Codex and OpenCode, agent `effort` on Cursor. Fields
that only describe or narrow an artifact are dropped silently where a target
has no counterpart: skill `license` and `paths` (Codex, OpenCode, and Pi offer
the skill everywhere), and agent `color` on Codex, Cursor, and Pi. Codex projects every
portable effort, including `max`.
Cursor projects allows to the allowlist its Desktop app and CLI share, and
a generated hook carries the asks and denies. Codex projects ask and deny rules as
`prompt` and `forbidden` prefix rules, a generated hook carries every deny, and
allows stay with its sandbox, so it owes no command omission. Commands and agents that intentionally never
reach Cursor or Codex can set `only: [claude, opencode]` instead of repeating
those surface omissions.

### Skills

`paths` is a list of file globs; `disable_model_invocation` is boolean;
`license` is a string; `metadata` is a map. These are canonical only for the
targets whose native behavior is equivalent. Do not substitute a similar field
from another target for one of these values.

Codex, Cursor, OpenCode, and Pi all read `<home>/.agents/skills`, Cursor even
with its third-party extensibility off, so a skill all four receive is written
there once. Its `SKILL.md` carries every reader's frontmatter, since each
ignores keys it does not know, and Codex's `agents/openai.yaml` beside it. A
skill whose `only` leaves out any of the four is written to each receiving
target's own skills directory instead, and so is a command, since OpenCode
serves commands natively. Readers that need a frontmatter key to differ, such
as a target-native `description`, cannot share a copy: the compiler rejects
the skill until `only` splits it. Claude reads only `<home>/.claude/skills`.

### Commands

`execution.agent` asks Claude and OpenCode to invoke a named subagent.
`execution.subtask: true` asks each to run the command as an isolated subtask.
Codex and Cursor receive a command as a skill the model cannot invoke by
itself, and Pi as a prompt template; all three require `omit: [execution]` for
either field. A command and a skill
of the same name are rejected on every target that serves commands as skills.
Prefer `only: [claude, opencode]` when delegation matters more than reaching
the other hosts.

### Agents

`effort` is one of `low`, `medium`, `high`, `xhigh`, or `max`. `background`
and `color` are portable only as described above.

`deny` lists the tool classes the agent may not use: `shell`, `edit` (every
file-writing tool), `webfetch`, `websearch`, and `delegate` (spawning another
agent). Claude adds the matching tools to `disallowedTools` ahead of any
native ones, and OpenCode adds a deny rule per entry ahead of its native
`permissions`. Pi lowers `shell` and `edit` into the agent's `permission:` map
and needs nothing for the rest, as the Pi target reference describes. Cursor carries `edit` as `readonly: true`, which also blocks
other write operations, and Codex carries `shell` as `features.shell_tool =
false`; each still applies what it can and needs `omit: [deny]` for the rest,
because a Cursor agent lists only the tools it may use and a Codex role keeps
its parent's sandbox, web search, and spawn depth (`codex.agent-tool-gates`
pins that). Tool allowlists and anything else stay target-native.

Because those settings are target-native, they are checked against each host's
closed vocabulary rather than passed through: Claude `permissionMode`,
`memory`, `isolation`, and `effort`; OpenCode `mode` and
every leaf of `permission`. Claude `tools` and `disallowedTools` must name real
tools or MCP patterns — omit `tools` to inherit the pool, because a list that
resolves to nothing makes Claude refuse to spawn the agent, which
`claude.agent-tools` pins against the installed binary.

A portable `color` reaches OpenCode as hex, Claude's color names mapped to
it. OpenCode silently drops any other value, so the compiler rejects it; a
native override replaces it. That value is checked after the merge.

Cursor agents have no `effort` field, so it needs `omit: [effort]`.
`background` lowers natively.

Fields whose accepted set is genuinely open stay unchecked: any `model` ID and
Codex `model_reasoning_effort`, whose enum ends in a catch-all for
model-defined values. OpenCode agent frontmatter also hoists every key it does
not know into `options`, which is how a top-level `reasoningEffort` reaches the
provider, so unknown keys there are passthrough by design.

### Permissions

`unmatched` is the decision for a shell command no rule matches: `ask`, the
default, `allow`, or `sandbox`. `allow` lands only where the guard rules still
outrank it: OpenCode's and Pi's `*` bash entry, which every rule is written
after, Claude's bare `Bash` allow, which every deny outranks. Cursor and Codex
each need an omit for `unmatched: allow`: Cursor's shared allowlist has no deny
channel to keep the guards binding, and Codex gates an unmatched command by
sandbox. `sandbox` runs an unmatched command in the target's own sandbox
without asking, and asks where the target has none: Claude gets no bare `Bash`
allow but `sandbox.enabled` and `sandbox.autoAllowBashIfSandboxed`, so leaving
the sandbox needs an allow rule or a prompt; Codex's `on-request` already runs
an unlisted command in its sandboxed profile, and a native
`default_permissions: :danger-full-access` is an error under `sandbox`; the
Cursor CLI gets `sandbox.mode: enabled` beside the shared allowlist, while
Desktop's sandbox setting lives in the editor's own storage; OpenCode's and
Pi's `*` entry asks. Under `sandbox` an allow rule means a command may also run outside
the sandbox unasked. There is no `deny`
value: a blanket command denial is not a policy any target can run under.
`wrappers`, by default the programs that run their argument as a command
(`command`, `env`, `nice`, `nohup`, `stdbuf`, `time`, `timeout`, `xargs`),
get wrapped copies of every ask and deny rule, never a
blanket allow, so a wrapped allowed command falls to `unmatched`; a target
that peels a wrapper itself gets no copies for it, and Cursor's hook peels
them as Claude's does. An allowed whole program (`- rg`) also lands behind
`command` and `xargs`, which run it as given, where the target cannot see
through them: Cursor, and Pi for `command`. Claude strips a program
path itself, and OpenCode's bundled plugin does, as the OpenCode target
reference describes.

`tools` maps `read`, `edit`, `write`, `webfetch`, or `websearch` to `allow`,
`ask`, or `deny`. Only Cursor gates writing separately from editing, and it
has no file channel for any tool: its file tools run unasked in the workspace,
which `read`, `edit`, and `write` set to `allow` match, and any other decision
or any web tool decision needs `omit: [tools]`
(`cursor-desktop.tool-defaults`). Pi gates `write` apart from `edit` and
takes each as written, but has no web tool, so a web decision other than
`allow` needs `omit: [tools]` there (`pi.web-tools`). On Claude and OpenCode
`edit` and `write` fold onto a single gate: Claude's Edit rules cover every
file-editing tool, and OpenCode's write tool asks for its `edit` permission. A
split decision narrows to the stricter side there with a warning. `workspace.allow`,
`.ask`, and `.deny` contain `/`-absolute or `~/` roots; `workspace.unmatched`
(`ask`, the default, or `allow`) decides every other directory outside the
project, and file tools honour it only where `tools` allows them. A denied
directory's files are denied like secret paths. `secret_paths` are `**/`
patterns, matching at any depth, or `~/` home paths; each target receives
them in every anchor it checks a file by. `secret_names` deny any command
that mentions them.

A command rule is a token sequence: a string, split on whitespace, or a YAML
list of literal tokens. A bare entry (`- git status`) allows, asks, or denies
that prefix. A mapping sets `prefix` or `exact`, where an exact rule matches
the sequence with nothing after it, and may add `tail`, token sequences that
match after the head, or `text`, literal substrings, plus a deny's `reason`.
In every rule string, `{a,b}` expands to its alternatives as a shell expands
braces, so `gh pr {list,view}` is two rules and
`tail: ['{-X,--method} {POST,PUT}']` is four sequences. Whitespace around an
alternative is dropped, so a long group may wrap onto the next line. Tokens are portable
literal argv tokens. A token may start with `~` to match the
home-relative command text agents type; every target matches it literally and
never expands it. Every target also receives a `~/` command under its absolute
path, and the glob targets under its `$HOME/` spelling and a subcommand token
holding a `/` single- and double-quoted.

An ask or deny with a subcommand also matches any options between the program
and the subcommand (`git -C repo push` for `git push`). An allow carries over
only the options that pick where or how a command runs: the compiler's table
for well-known CLIs (`git -C`, `kubectl --context`, `gcloud --project`), plus
the `options` a fragment declares for its own tools. A command's `options` have
one home. Spellings that change nothing a rule decides are not written as
rules: shell-unwrap reads a bundled or joined flag (`-fu`, `--method=POST`,
`-XPOST`) in each of its readings, peels wrappers and program paths, and carries
an allow over a harmless assignment such as `GIT_EDITOR=true`. Every target
runs it for denies, and Claude, Cursor, and OpenCode for asks too. A `text` predicate matches as a substring anywhere after the command, so
a leading or trailing space in it anchors the match to a token boundary:
`' -T'` matches `curl -T f` but not `-H Content-Type`. A deny rule
may carry a one-line `reason`, what the agent should run instead; it is not a
predicate. Claude delivers it through the shell-unwrap hook, OpenCode through
the shell-unwrap plugin, Pi through the permission system's deny `reason` and
the shell-unwrap extension, and Codex as the forbidden rule's `justification`
and through the shell-unwrap hook.

```yaml
options:
  ./gradlew: [--offline]
allow:
  - git {status,log}
  - exact: node --version
ask:
  - prefix: git push
    tail: --force-with-lease
deny:
  - prefix: git push
    tail: ['{--force,-f}']
    reason: use --force-with-lease
```

Fragments are merged before validation. Duplicate rules, contradictory
containment, duplicate option vocabularies, shell syntax, wildcard tokens, and
lossy predicate combinations are errors.

### MCP servers

Each `mcp/<id>.yaml` or `mcp.local/<id>.yaml` declares one server, registered
under its file name by every target it applies to. A local server sets `command` with optional `args` and
`env`; a remote one sets `url`. `targets.<target>.native.command` replaces the
command for one target, such as an absolute path for Cursor, which starts
servers without the login shell's `PATH`.

`tools` maps an exact tool name to `allow`, `ask`, or `deny`. A tool it does
not name follows the target's own default and any patch rule.

```yaml
# mcp/browser.yaml
command: browser-mcp
args: [--headless]
env: {MODE: quiet}
tools:
  browser_snapshot: allow
  browser_click: ask
  browser_run_code: deny
targets:
  cursor:
    omit: [tools]
```

| Target | Generated pointers |
| --- | --- |
| Claude | `type` (`stdio` or `http`) and the fields under `mcpServers/<id>` in `<home>/.claude.json` |
| Codex | the fields under `mcp_servers/<id>` in `config.toml` |
| Cursor | the fields under `mcpServers/<id>` in `<home>/.cursor/mcp.json` |
| OpenCode | `mcp/servers/<id>` as `type: local` with one `command` array and `environment`, or `type: remote` with `url` |
| Pi | the fields under `mcpServers/<id>` in `<home>/.pi/agent/mcp.json`, plus the source `description` |

| Target | `tools` lowering |
| --- | --- |
| Claude | `mcp__<id>__<tool>` in `permissions.allow`, `ask`, or `deny` of `settings.json` |
| Codex | `mcp_servers/<id>/tools/<tool>/approval_mode` as `approve` or `prompt`; a deny joins `mcp_servers/<id>/disabled_tools`, which hides the tool |
| Cursor | an allow joins `mcpAllowlist` as `<id>:<tool>` in `permissions.json`; an unlisted tool asks; a deny needs `tools` in `targets.cursor.omit` |
| OpenCode | `{action: <id>_<tool>, resource: "*", effect}` after the policy's rules in `permissions` |
| Pi | `<id>:<tool>` in the permission system's `permission/mcp` map; a deny also hides the tool |

`enabled: false` registers a server that starts off, for a session to turn on.
Its `tools` decisions still lower, so that session keeps them.

| Target | Off by default | On for one session |
| --- | --- | --- |
| Claude | not in `.claude.json`; `<home>/.claude/mcp/<id>.json` holds its `mcpServers/<id>` entry, with any `claude-mcp` patch operation inside that entry applied there, and the compiler deletes `mcpServers/<id>` from `.claude.json` | `claude --mcp-config ~/.claude/mcp/<id>.json` |
| Codex | `mcp_servers/<id>/enabled = false` | `codex -c mcp_servers.<id>.enabled=true` |
| Cursor | unsupported: the on/off state is per project, outside `mcp.json`; needs `enabled` in `targets.cursor.omit`, and the server stays on | the MCP toggle in Cursor, saved for that project |
| OpenCode | `mcp/servers/<id>/disabled: true` | the MCP servers dialog, space on the server |
| Pi | `mcpServers/<id>/enabled: false` | none per session: `/mcp`, "Enable in this project" in a trusted project, which writes a `.pi/mcp.json` override; a plain enable rewrites the owned `enabled` in `~/.pi/agent/mcp.json`, and the next sync refuses it |

The compiler owns only these leaf pointers, so a patch keeps the settings
beside them: server default approval modes, enablement, and OAuth. A value a
patch already wrote is taken over when it matches; one that differs is
refused until it does. Credentials stay out of the source: reference them
through the target's environment interpolation or native login.

## Target blocks

`targets` is a mapping whose only keys are `claude`, `cursor`, `codex`,
`opencode`, and `pi`. A target block has exactly these optional mappings:

```yaml
targets:
  claude:
    native:
      model: sonnet
    raw:
      future-native-key: future-native-value
    omit: [some.portable.path]
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

`omit` lists dotted portable field paths this target loses. Every entry must
be consumed by that target compiler. Stale, misspelled, unsupported, or
unnecessary omissions are errors. This forces the source to acknowledge that a
non-default portable value is intentionally absent from one target. The fields
each target owes an omit for, and why, are the `LIMITATIONS` table in
`src/coding_agents_sync/limits.py`; a limitation with a probe fails the live
capability run once the target starts supporting the field.

## Strict target-native fields

Target compilers own their native schemas. The supported typed fields are:

| Target | Source kinds and typed fields |
| --- | --- |
| Claude | Skills and commands: native Claude skill frontmatter, including `hooks` and fork `background`. Agents: native Claude agent frontmatter including `model`, `tools`, `disallowedTools`, permission mode, turn limits, isolation, memory, initial prompt, skills, `mcpServers`, `hooks`, `autoCompactWindow`, `omitClaudeMd`, and the `observer` fields. |
| Cursor | Rules: `description`, `globs`, `alwaysApply`; `globs` requires `alwaysApply: false`. Skills and commands: `name`, `description`, `license`, `paths`, `disable-model-invocation`, `metadata`. Agents: `model`, `readonly`, `tools`. |
| Codex / ChatGPT Desktop | Rules: `rules` containing `pattern` (a token may list alternatives), `decision`, and optional `justification`. Skills and commands: `name`, `description`, `license`, `metadata`, `allowed-tools`, `openai` (typed UI, policy and dependencies). Agents: `name`, `description`, `developer_instructions`, `model`, `model_reasoning_effort`, `model_reasoning_summary`, `model_verbosity`, `features`, `skills`, `nickname_candidates`, `personality`, `service_tier`. See the target reference for nested fields. |
| OpenCode | Skills: `name`, `description`, `license`, `metadata`, `compatibility`. Commands: `name`, `description`, `agent`, `subagent`, `model`. Agents: `description`, `model`, `request`, `mode`, `hidden`, `color`, `steps`, `disabled`, `permissions`. |
| Pi | Skills: `name`, `description`, `license`, `metadata`, `compatibility`. Commands: `description`, `argument-hint`. Agents: `description`, `display_name`, `tools`, `model`, `thinking`, `max_turns`, `prompt_mode`, `inherit_context`, `run_in_background`, `enabled`, `locked`, `permission`. |

The table is intentionally concrete, not a target extension API. Each target
is compiled for its latest release only; the compiler carries no version
framework.

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
| Pi | `<home>/.pi/agent` | `settings.json`, `mcp.json`, `extensions/pi-permission-system/config.json`, `extensions/pi-autoformat/config.json` |

Use a raw file when a complete target-native artifact cannot be represented by
typed fields. It produces an explicit warning, not an implicit compatibility
promise.

## Native pointer patches

Committed patch files live below
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
| `cursor-hooks` | `<home>/.cursor/hooks.json` |
| `codex` | `<home>/.codex/config.toml` |
| `opencode` | `<home>/.config/opencode/opencode.json` |
| `pi-settings` | `<home>/.pi/agent/settings.json` |
| `pi-mcp` | `<home>/.pi/agent/mcp.json` |
| `pi-permissions` | `<home>/.pi/agent/extensions/pi-permission-system/config.json` |
| `pi-autoformat` | `<home>/.pi/agent/extensions/pi-autoformat/config.json` |

Patches operate on named JSON/TOML pointers and preserve unnamed native
fields. A top-level `enabled: false` makes a patch write nothing, so the
values it recorded retire as if its file were gone. A patch cannot overlap a portable file or a raw file. It may
contribute to a generated pointer only where the contribution is unambiguous:
a `set` that agrees with the generated value, an `extend` onto a generated
list, or an `overlay` onto a generated map. Every other overlap, including an
ancestor or descendant pointer, is a clash and is rejected. Target compilers
additionally enforce their own exclusions, such as OpenCode's command-policy
exclusion and Cursor Desktop's terminal-allowlist exclusion.

## Ownership and publication

Each compiler returns a `Plan` of `OwnedFile`, `OwnedTree`, `NativeValue`,
`NativePatch`, and `Diagnostic`. The shared lower layer does not know target
schemas. It performs global preflight, validates path and pointer collisions,
reconciles manifests and native semantic hashes, writes portable and native
content atomically, then publishes manifests last.

Portable manifests preserve unmanifested siblings and refuse to claim or
replace modified output. Native ownership is recorded per pointer in
`config_root/.coding-agents-native.json`: a generated list owns its own entries
and keeps foreign ones ahead of them; any other value is owned whole by
semantic hash, and a modified or differing existing value is refused. A value a
patch's `set` or `replace` writes is recorded as `patched` by its hash: the
patch overwrites it on every sync, and once no patch writes the pointer, the
next sync deletes the value if it still holds that hash and leaves an edited
one to the native tool. Unnamed pointers remain native-tool-owned. A failed preflight
makes no generated writes.
