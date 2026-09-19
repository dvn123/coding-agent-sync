# Codex Target Reference

## Generated surfaces

| Source | Output |
| --- | --- |
| Global and rules | `<home>/.codex/AGENTS.md` |
| Typed rule policy | `<home>/.codex/rules/coding-agents.rules` |
| Skill | `<home>/.codex/skills/<directory>/` |
| Command | no generated surface |
| Agent | `<home>/.codex/agents/<stem>.toml` |
| Registrations | owned pointers in `<home>/.codex/config.toml` |

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
projection.

Typed Codex skill fields are `name`, `description`, `license`, `metadata`, and
`allowed-tools`. Typed agent fields are `name`, `description`,
`developer_instructions`, `model`, `model_reasoning_effort`, `sandbox_mode`,
and `nickname_candidates`. `targets.codex.raw` is supported for agent TOML
fields and is visibly unvalidated. Non-equivalent portable skill or agent
fields require a reasoned omit acknowledgement.

`patches/codex.yaml` targets `<home>/.codex/config.toml`; it may only use
pointers disjoint from generated skill and agent registrations. Raw files below
`target-config/codex/raw/` mirror below `<home>/.codex`; `config.toml` and the
generated rules file are reserved. Raw files are byte-preserved and
manifest-owned.

Codex ownership is pointer plus semantic hash for registrations and portable
manifests for files. Unnamed TOML keys and unmanifested siblings remain
Codex-owned.
