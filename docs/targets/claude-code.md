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

Named agents resolve `model_policy` before rendering. The `sweet-spot` profile
emits its model and `effort`; `inherit` emits `model: inherit` and no effort,
so the subagent follows the parent conversation deliberately.

Claude receives portable command, tool, workspace, secret-path, and
secret-name permissions in `settings.json`. It is one of the two targets with
an exact portable command-policy projection. `unmatched: allow` lands as a
bare `Bash` allow, which every deny rule outranks; the permission mode stays a
patch's to set. Claude anchors a bare path rule at the cwd and a `/` one at
the settings file, so `**/` secret paths and absolute directories take the
`//` root form, workspace directories receive `Edit(<dir>/**)` when edits are
allowed, and `workspace.unmatched: allow` becomes `Read(//**)` and
`Edit(//**)`. Denied workspace directories become Read and Edit denies.
`workspace.ask` has no channel and needs an omit. Claude peels leading
assignments, `env` in every form, `sudo`, and `timeout`, `time`, `nice`,
`nohup`, `stdbuf`, `command`, `builtin`, and `noglob` before it matches a deny
or ask rule, and retries the rule behind a bare `xargs`, so those wrappers get
no copies; `xargs` with options still does. Claude resolves no program path,
so every ask and deny also lands behind `/*/` (`/*/git push *`), which
matches any absolute directory but not `ls foo/rm`. It looks past neither a
leading redirect (`2>&1 rm -rf x`) nor a peeled wrapper run by path
(`/usr/bin/env rm -rf x`), so whenever the policy has a deny, every command
of either shape is denied outright. The `deny-reach` and `compiled-guard` live
scenarios pin this. Generated pointers are semantic
hash-owned; a Claude settings patch may use a different pointer, or contribute
to a generated one where the contribution does not clash.

`patches/claude-settings.yaml` targets `<home>/.claude/settings.json` and
`patches/claude-mcp.yaml` targets `<home>/.claude.json`. Local counterparts
must be mode `0600`. Both preserve unnamed native fields. Raw files below
`target-config/claude/raw/` mirror below `<home>/.claude`; `settings.json` is
reserved for pointer reconciliation.

Portable files are manifest-owned by their exact generated roots. An
unmanifested sibling survives, while a modified managed output is refused.
