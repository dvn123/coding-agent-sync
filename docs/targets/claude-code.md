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
| Permission hook | `<home>/.claude/hooks/shell-unwrap`, its `hooks/PreToolUse` entry, and `<home>/.claude/shell-unwrap.json` for deny reasons |

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
it emits permissions, the compiler installs shell-unwrap's hook (source:
<https://github.com/dvn123/shell-unwrap>, its Go source and dependencies
vendored in `targets/shell-unwrap/` and built at sync time, shared with Codex,
Cursor, Pi and OpenCode) at `hooks/shell-unwrap` as a `Bash` PreToolUse hook
with the `claude` argument. The hook reads the deployed `permissions` and the
reasons file, which maps every pattern a reasoned deny emits to its reason.
It denies the command when any peeled spelling of a command in it matches a
deny, in the OpenCode plugin's words and with the rule's reason, and asks
when one matches an ask. It then reads the command as zsh will run it, quoting
removed and every run-time part (a variable, a substitution, a glob) as any
text: it denies or asks when a guard matches every value, and allows only
when an allow covers every value and no guard may match any; a redirect is
judged by the Read and Edit rules. Claude still evaluates its deny and ask
rules after a hook allow. The compiler leaves `xargs` with options to the
hook, so the hook asks when it cannot parse the command or fails, and the hook
command prints an ask when it cannot run. Bypass permissions mode still shows
a hook's ask, so in that mode neither asks; denies still apply. It leaves to
Claude, by printing nothing, what it cannot bound: a guard only some value
may meet, zsh parameter flags, `tee`, `find` actions, `fd -x`, `cd` to a
run-time or outside directory or beside `git`, function definitions, and
dynamic program names. shell-unwrap's own tests and zsh oracle pin its
decisions; `tests/test_sync.py` runs the deployed hook once.

`patches/claude-settings.yaml` targets `<home>/.claude/settings.json` and
`patches/claude-mcp.yaml` targets `<home>/.claude.json`. Local counterparts
must be mode `0600`. Both preserve unnamed native fields. Raw files below
`target-config/claude/raw/` mirror below `<home>/.claude`; `settings.json` is
reserved for pointer reconciliation.

Portable files are manifest-owned by their exact generated roots. An
unmanifested sibling survives, while a modified managed output is refused.
