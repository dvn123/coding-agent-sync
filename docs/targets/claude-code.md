# Claude Target Reference

## Compiler outputs

| Source kind | Generated output |
| --- | --- |
| Global | `<home>/.claude/CLAUDE.md` |
| Rule | `<home>/.claude/rules/<source-stem>.md` |
| Skill | `<home>/.claude/skills/<source-directory>/` |
| Command | `<home>/.claude/commands/<source-file>.md` |
| Agent | `<home>/.claude/agents/<source-file>.md` |
| Permissions | named paths in `<home>/.claude/settings.json` |

The compiler emits user-scope configuration only. Project-local Claude files
and tool-owned settings remain outside its ownership.

## Global instructions and rules

The global body is written verbatim, with one trailing newline, to
`CLAUDE.md`. Rule bodies are written separately below `rules/`.

Canonical rule activation metadata does not become Claude `paths`
frontmatter. User-scope rules with `paths` are not reliably loaded, so all
compiled Claude rules are unconditional. Other target-specific rule metadata
is not emitted because the rule output is body-only.

The `rules/` directory uses a file manifest. Only unchanged, previously
manifested rule files may be replaced or removed.

## Skills

Each source skill tree is mirrored to a same-named directory. Support files are
copied byte-for-byte and the generated `SKILL.md` receives Claude-native
frontmatter:

- canonical `name` and `description`;
- canonical `paths` and `disable-model-invocation` when present;
- supported `claude:` fields, with canonical-to-native key renames.

The directory is one managed entry. Files inside it are an exact mirror, but
unmanifested sibling skill directories remain untouched.

## Commands and agents

Commands preserve their source filenames. Canonical command execution lowers
to Claude's `agent` field and `context: fork` for subtask execution.

Agents preserve their source filenames. Canonical tool inheritance, allow/deny
lists, effort, background mode, and color lower to Claude frontmatter. When an
agent inherits nothing and has no explicit allow list, write-capable tools are
denied so the output does not accidentally inherit Claude's default tool set.

## Native configuration

`config_root/patches/claude-settings.yaml` and the optional mode-`0600`
`config_root/patches.local/claude-settings.yaml` reconcile named paths in
`<home>/.claude/settings.json`.

Canonical permissions generate:

- command globs in `/permissions/allow`, `/permissions/ask`, and, when
  present, `/permissions/deny`;
- portable tool decisions using `Read(**)`, `Edit(**)`, `Write(**)`,
  `WebFetch(*)`, and `WebSearch(*)`;
- secret-name asks and `Read`/`Edit` secret-path denials;
- literal `workspace.allow` roots in `/permissions/additionalDirectories`.

Leading command options expand to attached wildcard forms. Claude-resolved
wrappers are left to Claude's strictest-match behavior; unresolved wrappers
receive a blanket wrapper allow plus wrapped copies of asks and denies.

Generated values are semantic-hash-owned in
`config_root/.coding-agents-native.json`; patch-only pointers are not added to
that manifest. Compatible `extend` operations may add Claude-native tool
entries to generated permission lists; portable command policy belongs in the
permission source.

The `claude-mcp` patch surface targets `<home>/.claude.json`. The compiler has
no built-in MCP inventory: external patches may own selected
`/mcpServers/<name>` paths while unrelated servers remain tool-owned.
