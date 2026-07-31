# Codex Target Reference

## Compiler outputs

| Source kind | Generated output |
| --- | --- |
| Global and rules | `<home>/.codex/AGENTS.md` |
| Rule execution policy | `<home>/.codex/rules/coding-agents.rules` |
| Native rule fragments | `<home>/.codex/rules/<fragment>.rules` |
| Skill | `<home>/.codex/skills/<source-directory>/` |
| Command | generated skill below `<home>/.codex/skills/` |
| Agent | `<home>/.codex/agents/<source-stem>.toml` |
| Registrations | named paths in `<home>/.codex/config.toml` |

`<home>/.codex` is the compiler's Codex home even when the process running the
CLI has another `CODEX_HOME`.

## Instructions and rules

Codex has one guideline channel. The compiler concatenates the global body,
then rule bodies in source-path order, with blank lines between sections.

Codex-specific `rules` metadata is translated to native execution-policy DSL.
Unsupported or lossy values produce warnings. Canonical user permissions do
not enter this DSL: Codex's matcher cannot safely represent their predicates,
and its rules govern host execution rather than the ordinary sandboxed path.

External native fragments from
`config_root/target-config/codex/rules/*.rules` are copied verbatim beside the
generated `coding-agents.rules`. All files share one managed-root manifest, so
an existing unmanifested filename cannot be claimed.

## Skills and commands

Canonical skills are mirrored into same-named directories. Codex frontmatter
contains only supported fields.

Commands are wrapped as generated skills. A command's derived directory name
is resolved against existing skill names and earlier commands; collisions use
the source-prefixed fallback. The same name-resolution result drives both file
generation and config registration.

## Agents

Canonical agents become TOML documents containing identity, description,
developer instructions, supported reasoning effort, and a derived sandbox
mode. An agent that cannot write lowers to `read-only`; other agents lower to
`workspace-write`. `codex:` fields override derived native values.

Agent descriptions and config paths are registered under
`/agents/<slug>/description` and `/agents/<slug>/config_file`.

## Native configuration

The compiler reconciles generated skill registrations at `/skills/config` and
agent registrations at the pointers above. Their semantic hashes are recorded
in `config_root/.coding-agents-native.json`.

`config_root/patches/codex.yaml` and the optional mode-`0600`
`config_root/patches.local/codex.yaml` reconcile other named paths in
`<home>/.codex/config.toml`. TOML comments and unrelated fields are preserved.
Patch operations cannot replace a generated pointer with conflicting content.

The `codex` patch surface may own selected `/mcp_servers/<name>` paths. The
compiler supplies no default servers and preserves every unnamed MCP entry.
