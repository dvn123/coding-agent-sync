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
commands or command permissions; sources must explicitly omit them.

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
