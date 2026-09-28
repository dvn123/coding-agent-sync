# OpenCode Target Reference

The compiler targets the latest OpenCode 2 release only. OpenCode migrates a
v1-shaped config on every load; the compiler emits native v2 shapes wherever
it has moved to one, and the rest still passes through that migration.

## Generated surfaces

| Source | Output |
| --- | --- |
| Global and rules | `<home>/.config/opencode/AGENTS.md`, the global body followed by each rule |
| Skill | `<home>/.config/opencode/skills/<directory>/` |
| Command | `<home>/.config/opencode/commands/<source-file>.md` |
| Agent | `<home>/.config/opencode/agents/<source-file>.md` |
| Permissions | owned native `/permissions` rule list in `opencode.json` |
| Permissions | `<home>/.config/opencode/plugins/opencode-unwrap.js`, the bundled wrapper plugin |

OpenCode 2 loads only AGENTS.md files and never resolves the `instructions`
config list, so rules have no OpenCode-native fields. Typed skills use
`name`, `description`, `license`, `metadata`, and `compatibility`; commands use
`name`, `description`, `agent`, `subagent`, and `model`; agents use the native
`ConfigAgent.Info` fields `description`, `model` (`provider/model#variant`),
`request`, `mode`, `hidden`, `color` (hex), `steps`, `disabled`, and a
`permissions` rule list, since any other key sends the file through OpenCode's
v1 migration. Effort and sampling go in `request.body`, where the portable
effort also lands. Unknown fields are errors.
Accepted raw frontmatter is visibly unvalidated and cannot shadow typed or
canonical output.

Permissions land as one ordered `/permissions` list of `{action, resource,
effect}` rules. OpenCode appends it after every agent's built-in rules and
applies the last match, so each action's catch-all precedes its narrower
rules: `shell *` carries the policy's `unmatched` decision, then the allow,
ask, and deny buckets follow in that order. The `edit` action also gates the
write and patch tools. A patch may retire the legacy `/permission` map and may
extend `/permissions` with any rule except one whose action matches `shell`,
since command policy has one writer.

Named agents resolve `model_policy` before rendering. The `sweet-spot` profile
emits its model and `reasoningEffort`; `inherit` emits neither, so the agent
inherits the invoking primary agent's model and effort.

Command denies and secret names land as bash `deny` entries after every allow
and ask. This requires OpenCode 2.0 or later: 1.x answered a denial with
`PermissionDeniedError`, whose message embedded every bash rule, roughly
1.3 MB per denial against a real corpus. 2.0 answers with
`Permission.BlockedError`, whose model-facing message names only the
permission. A deny rule's `reason` lands in `<home>/.config/opencode/
opencode-unwrap.json`, keyed by every pattern the rule emits, and the plugin
names it on the denial; the file is removed when no deny carries a reason.

OpenCode matches a shell rule against a command node's raw text, so a bare
deny misses `timeout 30 rm`, `FOO=1 rm`, and `/bin/rm`. The compiler installs
the opencode-unwrap plugin (source: <https://github.com/dvn123/opencode-unwrap>, vendored as
`targets/opencode-unwrap.js`) whenever it emits permissions. The plugin
hooks `permission.evaluate` and denies a command when the agent's and
session's rules deny it with its assignments, program path, or wrappers
peeled, so denies land bare, with no directory copies and no copies behind
the wrappers the plugin peels. It carries over a deny, so ask rules and any
declared wrapper it does not peel keep their copies. The one allow it carries
is through transparent wrappers (`timeout`, `time -p`, `nice`, `nohup`,
`noglob`, `nocorrect`) for a command that fell to the catch-all; the audit does
not model that allow.
The
`opencode.unwrap` live scenarios run OpenCode on the compiled output; the
opt-in oracle scenario checks the audit's model against OpenCode on a real
policy. To update the plugin, run `bun run build` in its repository, copy
`dist/opencode-unwrap.js` over the vendored file, and copy its
`test/spellings.json` to `tests/fixtures/opencode-unwrap/`, which the audit's
port must match exactly.

File permissions are checked by path relative to the session directory for
files inside it and by absolute path otherwise, and the matcher's `**/` needs
a `/`, so each denied path also lands in its relative spellings: `**/.env`
adds `.env`, and `~/.aws/credentials` adds `**/.aws/credentials` and
`.aws/credentials`. `external_directory` receives `workspace.allow`, `.ask`,
and `.deny`, and a `*` entry only for `workspace.unmatched: allow`: OpenCode's
own `*: ask` default sits before its built-in allows for tool output, tmp,
and config, and a written `*` would land after them.

`patches/opencode.yaml` targets
`<home>/.config/opencode/opencode.json`. Patches preserve unnamed fields and
may contribute to a generated pointer where the contribution does not clash,
including overlaying read, edit, or external-directory maps. They cannot
contribute command policy. Raw files below `target-config/opencode/raw/`
mirror below the OpenCode root; `opencode.json` is reserved for pointer
reconciliation.

Generated files use portable manifests; generated native values use pointer
plus semantic-hash ownership in `config_root/.coding-agents-native.json`.
