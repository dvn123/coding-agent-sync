# Claude Code Target Reference

## Generated surfaces

| Source | Output |
| --- | --- |
| Global | `<home>/.claude/CLAUDE.md` |
| Rule | `<home>/.claude/rules/<stem>.md` |
| Skill | `<home>/.claude/skills/<directory>/` |
| Command | `<home>/.claude/skills/<source-stem>/SKILL.md`; Claude merged commands into skills, so a command may not share a skill's name |
| Agent | `<home>/.claude/agents/<source-file>.md` |
| Permissions | owned pointers in `<home>/.claude/settings.json` |

Global and rule bodies are Markdown-only. Skills, commands, and agents use
canonical values plus strict `targets.claude.native` frontmatter. Unknown
native fields are errors; accepted `targets.claude.raw` frontmatter remains
unvalidated and produces a source-located warning.

Named agents resolve `model_policy` before rendering. A profile with a model
emits it and its `effort`; one without emits `model: inherit` and no effort,
so the subagent follows the parent conversation deliberately.

Claude receives portable command, tool, workspace, secret-path, and
secret-name permissions in `settings.json`. It is one of the two targets with
an exact portable command-policy projection. `unmatched: allow` lands as a
bare `Bash` allow, which every deny rule outranks; the permission mode stays a
patch's to set. Claude anchors a bare path rule at the cwd and a `/` one at
the settings file, so `**/` secret paths and absolute directories take the
`//` root form, workspace directories receive `Edit(<dir>/**)` when edits are
allowed, and `workspace.unmatched: allow` becomes `Read(//**)` and
`Edit(//**)`. Denied workspace directories become Read and Edit denies. An
ask directory stays out of `additionalDirectories` and becomes `Read(<dir>/**)`
and `Edit(<dir>/**)` ask rules, which outrank those broader allows (the
`claude.permissions` probe pins both). A secret name becomes a
`Bash(*NAME*)` deny and a `sandbox.credentials.envVars` deny, which unsets the
variable for sandboxed commands so `env` cannot print it; that second half
binds only while a patch enables `sandbox`. A Read deny also stops a shell
read of the same literal path (`cat`, `cp`) with the sandbox off, which the
`read-deny-shell` live scenarios pin. Claude strips an absolute
program path, then peels leading assignments, `env` in every form, `sudo`,
and `timeout`, `time`, `nice`, `nohup`, `stdbuf`, `command`, `builtin`, and
`noglob` before it matches a deny or ask rule, and retries the rule behind a
bare `xargs`. So `/usr/bin/git push -f` and `/usr/bin/env rm -rf x` need no
copies, and neither do those wrappers; `xargs` with options still does. An
allow strips only the assignments Claude lists as safe, so a declared
`assignments` token lands in front of allow rules alone. Claude
does not look past a leading redirect (`2>&1 rm -rf x`), so whenever the
policy has a deny, every command of that shape is denied outright. The
`deny-reach`, `ask-reach`, `allow-assign`, and `compiled-guard` live scenarios
pin this. The compiler targets the latest Claude Code release and the copy the
Claude app bundles for its Code tab (under
`~/Library/Application Support/Claude/claude-code/`), which can trail the CLI;
put that copy first on `PATH` to probe it. Generated pointers are semantic
hash-owned; a Claude settings patch may use a different pointer, or contribute
to a generated one where the contribution does not clash.

`patches/claude-settings.yaml` targets `<home>/.claude/settings.json` and
`patches/claude-mcp.yaml` targets `<home>/.claude.json`. Local counterparts
must be mode `0600`. Both preserve unnamed native fields. Raw files below
`target-config/claude/raw/` mirror below `<home>/.claude`; `settings.json` is
reserved for pointer reconciliation.

Portable files are manifest-owned by their exact generated roots. An
unmanifested sibling survives, while a modified managed output is refused.
