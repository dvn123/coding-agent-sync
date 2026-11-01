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
Tool dependencies require a non-empty `value`; optional fields are
`description`, `transport` (`stdio` or `streamable_http`), and `url`. They
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

Legacy `sandbox_mode` remains readable for older clients but emits a warning:
current custom agents inherit the parent sandbox and cannot override it.
An instruction to avoid edits is not an enforced read-only sandbox. Configure
the sandbox and approval policy on the parent session. See the official
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
| Skill | `<home>/.codex/skills/<directory>/` |
| Command | no generated surface |
| Agent | `<home>/.codex/agents/<stem>.toml` |
| Workspace directories | owned `sandbox_workspace_write.writable_roots` in `<home>/.codex/config.toml` |

Codex discovers `skills/` and `agents/*.toml` below `<home>/.codex` itself, so
`config.toml` carries no skill or agent registration.

Global and rule bodies concatenate into `AGENTS.md`. A rule may separately use
strict `targets.codex.native.rules` entries containing a non-empty literal
`pattern`, `decision` (`allow`, `prompt`, or `forbidden`), and optional
`justification`. Unknown values are errors. Codex does not receive portable
commands; sources must explicitly omit them.

## Command permission projection

Portable deny rules land in the same rules file as `forbidden` prefix rules.
Codex matches one flat argv by literal prefix and has no wildcard, so a rule
projects as its head and each tail appended directly, bare and once per
declared wrapper (`security find-generic-password`, `env security
find-generic-password`). The option-hole heads and the tail-after-arguments
spellings have no prefix form and fall to Codex's own approval flow; a deny
with a text predicate or an exact match is dropped with a warning. Allow and
ask rules never project: an allow would skip the sandbox approval Codex
already applies, and an ask would add prompts the sandbox does not need, so
fragments carrying them acknowledge `targets.codex.omit.commands.allow` and
`targets.codex.omit.commands.ask`. Secret paths and names still have no
projection, and neither does the policy's `unmatched` decision: Codex gates an
unmatched command by sandbox and approval policy, not by an allowlist, so a
non-default value needs an omit here too. Literal `workspace.allow`
directories land as absolute `sandbox_workspace_write.writable_roots`, which
Codex applies in `workspace-write` mode; `workspace.ask`, `.deny`, and
`.unmatched` have no channel and each need an omit.

Typed Codex skill fields are `name`, `description`, `license`, `metadata`, and
`allowed-tools`. Typed agent fields are `name`, `description`,
`developer_instructions`, `model`, `model_reasoning_effort`, `sandbox_mode`,
and `nickname_candidates`. `targets.codex.raw` is supported for agent TOML
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
