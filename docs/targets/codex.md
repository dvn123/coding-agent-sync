# Codex Target Reference

This target also delivers local configuration for the ChatGPT desktop app.
Keep the `codex` target name and `.codex` paths: Desktop and the CLI share
these configuration surfaces. Desktop-only options may use precise
`/desktop/...` native patch pointers; `/tui/...` settings affect the CLI.
Account connections, OAuth state, and hook trust remain host-owned.

Skills with `disable_model_invocation: true` receive
`policy.allow_implicit_invocation: false` in `agents/openai.yaml`.
The canonical value wins over a bundled policy value; other bundled UI,
dependency, and policy fields are preserved. Without the canonical flag,
the bundled file is copied unchanged. See the official
[skill metadata reference](https://learn.chatgpt.com/docs/build-skills).

## Typed skill metadata

Use `targets.codex.native.openai` to author `agents/openai.yaml` without
maintaining a separate target file:

```yaml
targets:
  codex:
    native:
      openai:
        interface:
          display_name: Project review
          short_description: Review a project for actionable defects
          default_prompt: Review this project.
        policy:
          allow_implicit_invocation: false
        dependencies:
          tools:
            - type: mcp
              value: docs
              transport: streamable_http
              url: https://example.com/mcp
```

Interface fields also include `icon_small`, `icon_large`, and `brand_color`.
`policy.products` limits a skill to `chatgpt`, `codex`, or `atlas`.
Tool dependencies require a non-empty `value`; optional fields are
`description`, `transport` (`stdio` or `streamable_http`), `command`, and
`url`. They
declare dependencies, not credentials or authorization to install a server.
Register executable MCP configuration in the parent native patch.

Typed fields merge by section into bundled metadata. Named fields win while
unmentioned fields survive; dependency tool lists replace the bundled list.
The portable explicit-invocation flag wins over the native invocation policy.
These fields never appear in `SKILL.md` frontmatter. Other targets continue
to receive the portable skill and its bundled files.

## Custom agents

Current ChatGPT Desktop and Codex support agent model behavior through
`model`, `model_reasoning_effort`, `model_reasoning_summary`
(`auto`, `concise`, `detailed`, `none`), and `model_verbosity`
(`low`, `medium`, `high`). Optional `codex` entries in `model-policy.yaml`
select model and effort through the same symbolic profiles as other targets.
Do not combine a selected Codex profile with native or raw model fields.

Use typed `features` to disable `shell_tool`, `apps`, `plugins`, `memories`,
or `request_permissions_tool`. Only `false` is accepted: these controls reduce
the parent's tool surface; they do not grant new capabilities.

Use `skills.config: [{path: /path/to/SKILL.md, enabled: false}]` to disable
individual skills, `skills.bundled.enabled: false` to disable bundled skills,
and `skills.include_instructions: false` to omit the automatic skills block.
Skills otherwise inherit from the parent. File paths must exist on the target
machine; keep machine-specific paths in local-only source artifacts.

Custom agents inherit the parent sandbox and approval policy and cannot
override them, so agent `sandbox_mode` is rejected: the `codex.agent-tool-gates`
probe shows a role's `sandbox_mode`, `web_search`, and `features.multi_agent`
leave the spawned agent unchanged, while `features.shell_tool = false` removes
its shell tools. A portable agent `deny: [shell]` lowers to that feature. An instruction to avoid
edits is not an enforced read-only sandbox. Configure the sandbox and approval
policy on the parent session. See the official
[custom agent schema](https://learn.chatgpt.com/docs/agent-configuration/subagents).

These are local runtime configuration surfaces. They do not provision cloud
Work hooks, account connectors, OAuth grants, or app trust decisions. Use
native patches for parent MCP servers, plugin registrations, hooks, config
profiles, and documented desktop settings; those surfaces are already
pointer-owned and preserve unnamed siblings.

## Generated surfaces

| Source | Output |
| --- | --- |
| Global and rules | `<home>/.codex/AGENTS.md` |
| Typed rule policy | `<home>/.codex/rules/coding-agents.rules` |
| Skill | `<home>/.agents/skills/<directory>/`, shared with Cursor and OpenCode; `<home>/.codex/skills/<directory>/` when `only` leaves out Codex, Cursor, or OpenCode |
| Command | `<home>/.codex/skills/<stem>/` with implicit invocation off |
| Agent | `<home>/.codex/agents/<stem>.toml` |
| Permissions | owned `default_permissions` and `permissions.coding-agents` in `<home>/.codex/config.toml` |

Codex discovers `skills/` and `agents/*.toml` below `<home>/.codex` itself, so
`config.toml` carries no skill or agent registration.

Global and rule bodies concatenate into `AGENTS.md`. A rule may separately use
strict `targets.codex.native.rules` entries containing a non-empty literal
`pattern`, `decision` (`allow`, `prompt`, or `forbidden`), and optional
`justification`. A pattern token may be a list of alternatives for that
position. Unknown values are errors.

Codex 0.166 has no custom prompts, and its own command migration turns a
command into a skill, so a command lowers to `skills/<stem>/` with
`policy.allow_implicit_invocation: false`: the user invokes it, the model
never does by itself. Codex skills do not expand `$ARGUMENTS` or `$1`, so such
a placeholder stays literal and the user's text after the skill mention
carries the arguments. `execution.agent` and `execution.subtask` have no
skill equivalent and need `omit: [execution]`.

## Command permission projection

Portable ask and deny rules land in the same rules file as `prompt` and
`forbidden` prefix rules.
Codex matches one flat argv by literal prefix and has no wildcard, so a rule
projects as its head and each tail appended directly, bare and behind any
declared wrapper. Codex reads a list token as alternatives for one position,
so all wrappers share one lead token (`[["env", "nohup"], "security",
"find-generic-password"]`), and patterns that differ only in their last token
with the same decision and reason merge (`["git", "push", ["--force", "-f"]]`).
A wrapper that takes arguments of its own (`timeout 5 …`) still falls through,
because no prefix can skip them. Codex resolves an absolute program path to
its basename itself. The option-hole heads and the tail-after-arguments
spellings have no prefix form, and an ask with a text predicate or an exact
match is dropped with a warning; these fall to Codex's own approval flow.
Allow rules never project and owe no omission: a Codex `allow` rule runs the
command outside the sandbox, while an unlisted command already runs inside
it.

shell-unwrap's hook bundle carries the denies the prefix rules miss. It lands
in `hooks/shell-unwrap.js` beside a `shell-unwrap.json` that holds every deny
as a Claude `Bash(...)` pattern, text and exact rules included, and runs as a
`Bash` `PreToolUse` command in `config.toml`. It denies a call when any peeled
spelling of a command inside it matches a deny: a wrapper with arguments
(`timeout 5 …`), an option before the subcommand, a bundled or joined flag
(`-fu`, `--method=POST`), or a command in a substitution. Codex rejects an
`ask` from a `PreToolUse` hook, so asks stay prefix rules only. When Bun or
the hook cannot run, the hook command prints a deny, so the guards fail closed.
Codex runs the hook only once the user trusts it in `/hooks`; the trust covers
the hook's declaration, so a new bundle needs no new trust.

## Permission profile

The policy lowers to a `coding-agents` permissions profile in `config.toml`,
selected by `default_permissions`:

- `extends = ":workspace"` inherits Codex's workspace sandbox: the project and
  temp directories are writable and the rest of the disk readable.
- Literal `workspace.allow` directories become absolute `write` entries, or,
  when `workspace.unmatched` is `allow`, one `":root" = "write"` entry.
- `secret_paths` and `workspace.deny` directories become `deny` entries, which
  block reads as well as writes inside the sandbox. A `**/` path becomes the
  absolute glob `/**/…`, which macOS enforces natively; Linux warns that an
  unbounded `**` needs `glob_scan_max_depth`.
- `tools` decisions of `allow` are Codex's own behaviour and owe nothing. A
  `websearch: deny` becomes `web_search = "disabled"`, which removes the tool
  from the request (the `codex.config` probe pins both modes); any other `ask`
  or `deny` has no channel and needs `omit: [tools]`.

`targets.codex.native.default_permissions` on the policy selects a built-in
profile instead (`:read-only`, `:workspace`, or `:danger-full-access`).
`coding-agents` stays defined for a session to switch to, but its `deny` entries
do not bind in a session that starts on a built-in profile, so
`:danger-full-access` leaves a secret path readable by commands.

The profile carries no network settings: set
`/permissions/coding-agents/network/enabled` in the `codex` patch. Legacy
`sandbox_mode` and `sandbox_workspace_write` keys do not apply while a profile
is selected. The ChatGPT app lists the profile, with the policy's description,
in its permission picker and starts threads on it unless the user picks
another mode; Codex keeps the read denials when a session switches to another
profile. `secret_names` land in `shell_environment_policy.exclude`, owned by
entry, so no command sees those variables; a patch broadens the list with
`extend`. A command that only names one is not refused, since prefix rules
cannot match text. `workspace.ask` has no channel and needs an omit, and so
does `unmatched: allow`: Codex
0.166 retired the `untrusted` approval policy, and `on-request` already runs
an unlisted command inside the sandbox, so the approval policy stays with the
user's patch.

Typed Codex skill fields are `name`, `description`, `license`, `metadata`, and
`allowed-tools`. Typed agent fields are `name`, `description`,
`developer_instructions`, `model`, `model_reasoning_effort`,
`model_reasoning_summary`, `model_verbosity`, `personality` (`none`,
`friendly`, `pragmatic`), `service_tier` (a catalog tier id such as `fast` or
`flex`), `features`, `skills`, and `nickname_candidates`. Codex rejects a role
file with any other key, so portable `color` is dropped. `targets.codex.raw` is supported for agent TOML
fields and is visibly unvalidated. Non-equivalent portable skill or agent
fields require a reasoned omit acknowledgement.

`patches/codex.yaml` targets `<home>/.codex/config.toml`; it may only use
pointers disjoint from generated pointers. Raw files below
`target-config/codex/raw/` mirror below `<home>/.codex`; `config.toml` and the
generated rules file are reserved. Raw files are byte-preserved and
manifest-owned.

Codex ownership is pointer plus semantic hash for generated pointers and portable
manifests for files. Unnamed TOML keys and unmanifested siblings remain
Codex-owned.
