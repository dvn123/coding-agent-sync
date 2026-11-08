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
| Permission hook | `<home>/.claude/hooks/shell-unwrap.js`, its `hooks/PreToolUse` entry, and `<home>/.claude/shell-unwrap.json` for deny reasons |

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
bare `Bash` allow, which every deny rule outranks; `unmatched: sandbox` lands
as `sandbox.enabled` and `sandbox.autoAllowBashIfSandboxed`, with no bare
allow, so a patch must not set those two keys; the permission mode stays a
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
copies, and neither do those wrappers. `xargs` with options gets none either:
the shell-unwrap hook peels it, and asks when it cannot run. An allow strips
only the assignments Claude lists as safe; the hook carries an allow over the
other harmless ones (`GIT_EDITOR=true`). Claude also
looks past a leading redirect (`2>&1 rm -rf x`), so that shape needs no
copies either. The `deny-reach`, `ask-reach`, `allow-assign`, and `compiled-guard` live scenarios
pin this. The compiler targets the latest Claude Code release and the copy the
Claude app bundles for its Code tab (under
`~/Library/Application Support/Claude/claude-code/`), which can trail the CLI;
put that copy first on `PATH` to probe it. Generated pointers are semantic
hash-owned; a Claude settings patch may use a different pointer, or contribute
to a generated one where the contribution does not clash.

Claude 2.1.319 splits `&&` chains, pipes, and `if` bodies, and peels `timeout`,
`nice`, and bare `xargs` with their options, but a loop, a command
substitution, a subshell, or a wrapper it does not peel (`env -i`, `env -u`,
`time -p`, `xargs -n1`) prompts even when every program in it is allowed. Its
deny rules already catch peeled spellings except behind `xargs` with options,
and it never shows the model a deny rule's `reason`. Whenever
it emits permissions, the compiler installs shell-unwrap's hook bundle
(source: <https://github.com/dvn123/shell-unwrap>, vendored as
`targets/shell-unwrap/hook.js` and shared with Cursor) as a `Bash` PreToolUse
hook with the `claude` argument, run by Bun with
tree-sitter's bash grammar embedded. The hook reads the deployed `permissions`
and the reasons file, which maps every pattern a reasoned deny emits to its
reason, and judges each command node with the same peel as the OpenCode
target's shell-unwrap plugin: it denies the command when any peeled spelling of
a node matches a deny, in the plugin's words and with the rule's reason, asks
when one matches an ask, and allows it when every node reaches an allow through
transparent wrappers and harmless assignments alone. Claude still evaluates its
deny and ask rules after a hook allow, so the hook removes prompts and never
lifts a guard. The compiler leaves `xargs` with options to the hook, so the
hook asks when it cannot parse the command or fails, and the hook command
prints an ask when Bun cannot run it. Bypass permissions mode still shows a
hook's ask, so in that mode neither asks; denies still apply. Bun starts in
the bundle's directory, since a mise `bun` shim exits in a project whose mise
config is untrusted. For an allow it stays silent, leaving
Claude's own flow, on anything it does not model: file redirects, `tee`,
`find` actions, `fd -x`, `cd` outside the working directories or beside `git`,
shell variable assignments, function definitions, and dynamic program names.
shell-unwrap's own tests pin its decisions; `tests/test_sync.py` runs the
deployed hook once.

`patches/claude-settings.yaml` targets `<home>/.claude/settings.json` and
`patches/claude-mcp.yaml` targets `<home>/.claude.json`. Local counterparts
must be mode `0600`. Both preserve unnamed native fields. Raw files below
`target-config/claude/raw/` mirror below `<home>/.claude`; `settings.json` is
reserved for pointer reconciliation.

Portable files are manifest-owned by their exact generated roots. An
unmanifested sibling survives, while a modified managed output is refused.
