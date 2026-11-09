# Pi Target Reference

The compiler targets the latest Pi release (`@earendil-works/pi-coding-agent`)
and writes below `<home>/.pi/agent`. Pi has no permission system and no named
agents of its own, so the compiler installs two Pi packages and writes the
configuration they read. It pins both, because each lowering below was checked
against that version:

| Package | Carries |
| --- | --- |
| `@gotgenes/pi-permission-system@42.0.0` | command, tool, directory, secret, and MCP decisions; agent `deny` |
| `@gotgenes/pi-subagents@25.0.5` | named agents |

## Generated surfaces

| Source | Output |
| --- | --- |
| Global and rules | `<home>/.pi/agent/AGENTS.md`, the global body followed by each rule |
| Skill | `<home>/.agents/skills/<directory>/`, shared with Codex, Cursor, and OpenCode; `<home>/.pi/agent/skills/<directory>/` when `only` leaves out a shared reader |
| Command | `<home>/.pi/agent/prompts/<source-file>.md`, a prompt template |
| Agent | `<home>/.pi/agent/agents/<source-file>.md` |
| MCP server | owned `mcpServers/<id>` fields in `<home>/.pi/agent/mcp.json` |
| Permissions | owned `permission/<surface>` maps in `<home>/.pi/agent/extensions/pi-permission-system/config.json` |
| Packages | owned entries of `packages` in `<home>/.pi/agent/settings.json` |

Pi reads one context file per directory and has no rules directory, so rules
join the global instructions, as on Codex. Rules have no Pi-native fields.
Typed skills use `name`, `description`, `license`, `metadata`, and
`compatibility`; portable `disable_model_invocation` lowers to
`disable-model-invocation: true`. Commands use `description` and
`argument-hint`. A prompt template expands into the current session, so a
command's `execution` needs `omit` (`pi.prompt-execution`). Pi serves skills as
`/skill:<name>`, so a command may share a skill's name.

## Agents

Agents follow the `@gotgenes/pi-subagents` file format. Typed native fields are
`description`, `display_name`, `tools`, `model` (`provider/modelId` or a fuzzy
name), `thinking` (`off` through `max`), `max_turns`, `prompt_mode` (`append`
or `replace`), `inherit_context`, `run_in_background`, `enabled`, `locked`, and
the permission system's `permission` map. Portable `effort` lands as
`thinking`, `background` as `run_in_background: true`; `color` is dropped.

A child receives only the tools its file names, and no extension tool unless
named. An agent without native `tools` therefore names Pi's built-ins,
`codemode`, `tool_search`, and `mcp__*`, which is what an agent inherits on the
other targets. A native `tools` list replaces it.

`deny` lowers to `permission:` entries the permission system enforces per
child: `shell` to `bash: deny`, `edit` to `edit: deny` and `write: deny`. Pi
has no built-in web tool and the allowlist above admits no extension tool, and
the subagents package strips its own tools from every child, so `webfetch`,
`websearch`, and `delegate` need no entry. The permission system reads agent
frontmatter with a line parser that keeps only the `permission` block.

Named agents resolve `model_policy` before rendering. A profile with a model
emits `model` and its effort as `thinking`; one without emits neither, so the
child inherits the parent's model and thinking level.

## Permissions

The permission map's surfaces apply last-match-wins within each map, and the
most restrictive decision across surfaces wins:

| Surface | Contents |
| --- | --- |
| `*` | `allow`, Pi's own default, for every tool no portable field decides |
| `bash` | `*` with the policy's `unmatched`, then allows, asks, and denies, each deny's `reason` attached, then secret-name denies |
| `read`, `grep`, `find`, `ls` | the portable `read` decision |
| `edit`, `write` | the portable `edit` and `write` decisions, each on its own gate; with `workspace.edit_ask`, a map of that decision as `*` and an ask for each directory's `/*` |
| `path` | `*: allow`, then every secret path and denied workspace directory as a deny; it gates every file tool and every path a command names |
| `external_directory` | `*` with `workspace.unmatched`, then each workspace root and its `/*` |
| `mcp` | `<server>:<tool>` with each MCP tool decision |

The permission system matches each command of a chain, and commands nested in
substitutions and subshells, against `bash`. It strips leading assignments
before matching, so guards get no assignment copies. It decides a command
behind `time`, `timeout`, `nice`, `stdbuf`, or `setsid` with literal, known
options as the command it runs, and raises one behind `env`, `xargs`, `nohup`,
`sudo`, `doas`, `flock`, `parallel`, or `watch`, or inside an inline shell
payload (`bash -c`, `eval`), to at least `ask`, so asks get no copies behind
those wrappers. It does not know `command`, `builtin`, or `exec`, so an ask
keeps its copies behind a declared one. It does not strip a program path:
`/usr/bin/git push` misses a `git push` ask. A `?` is a one-character
wildcard there; portable tokens never hold one.

shell-unwrap's extension carries the denies through every spelling, so a deny
gets no wrapper copy. It lands in `extensions/shell-unwrap/`, where Pi
discovers its `index.js`, beside the hook binary and a `shell-unwrap.json`
that holds every deny as a Claude `Bash(...)` pattern. On each `bash` call it
runs the binary's Codex host and blocks the call with the deny's reason when
any peeled spelling of a command inside it is denied (a command behind any
wrapper or a program path, a bundled or joined flag, or a command in a
substitution), or when a deny matches every value zsh may run a command with
(`for f in --force; do git push $f; done`). It blocks when the hook cannot
run, so the guards fail closed. It never asks; the permission system's own ask would
repeat its prompt.

Without a permission source, an MCP tool decision or an agent `deny` still
installs the permission system, with `*: allow` and `bash: {"*": "allow"}`,
because a config the extension cannot find asks for everything. The system
auto-allows reads below Pi's own directories only, so the compiler adds
`~/.agents/skills` to `piInfrastructureReadPaths` when it writes shared skills.
Pi has no built-in web tool, so a `webfetch` or `websearch` decision other than
`allow` needs `omit: [tools]` (`pi.web-tools`).

## MCP

Servers land in Pi's `mcpServers` shape: `command`, `args`, and `env` for a
local server, `url` for a remote one, plus the source `description`, which Pi
lists the server under and ranks its tools by. The compiler leaves `exposure`
at Pi's default, `codemode`; a `pi-mcp` patch may set it per server.

## Patches and raw files

| Patch name | Native file |
| --- | --- |
| `pi-settings` | `<home>/.pi/agent/settings.json` |
| `pi-mcp` | `<home>/.pi/agent/mcp.json` |
| `pi-permissions` | `<home>/.pi/agent/extensions/pi-permission-system/config.json` |
| `pi-autoformat` | `<home>/.pi/agent/extensions/pi-autoformat/config.json` |

Pi writes `settings.json` and `mcp.json` itself, and the permission system
writes its runtime switches into its config, so the compiler owns pointers, not
files. A `pi-permissions` patch may set runtime switches such as
`promptNotifications` and other surfaces, but never `/permission/bash`, since
command policy has one writer. The compiler generates nothing for
`pi-autoformat`: a `pi-settings` patch that extends `/packages` installs the
package, and the `pi-autoformat` patch configures its formatters. Raw files
below `target-config/pi/raw/` mirror below `<home>/.pi/agent`; the four patch
surfaces are reserved.

## Updating the packages

Bump `PERMISSION_PACKAGE` or `SUBAGENTS_PACKAGE` in `targets/pi.py` to a
release at least three days old, read its changelog for changes to the config
or agent format, and run the `pi` live cases. Pi installs a pinned package on
its next start.

Generated files use portable manifests; generated native values use pointer
plus semantic-hash ownership in `config_root/.coding-agents-native.json`.
