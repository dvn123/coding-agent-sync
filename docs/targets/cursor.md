# Cursor Target Reference

## Compiler outputs

| Source kind | Generated output |
| --- | --- |
| Global | `<home>/.cursor/rules/coding-agents-global.mdc` |
| Rule | `<home>/.cursor/rules/<source-stem>.mdc` |
| Skill | `<home>/.cursor/skills/<source-directory>/` |
| Command | unsupported |
| Agent | `<home>/.cursor/agents/<source-file>.md` |
| CLI permissions | named paths in `<home>/.cursor/cli-config.json` |
| Desktop permissions | named paths in `<home>/.cursor/permissions.json` |

Cursor Agent and Cursor Desktop share the ancestor-discovered MDC rule
channel. The compiler does not generate or depend on a Cursor plugin.

## Global instructions and rules

The global body is emitted as the reserved always-on rule
`coding-agents-global.mdc`. Canonical rules become MDC files:

- `activation.always: true` lowers to `alwaysApply: true`;
- `activation.globs` lowers to Cursor `globs`;
- no activation metadata emits an ancestor-discovered rule with no
  `alwaysApply` or `globs`.

The output filename `coding-agents-global.mdc` is reserved for the global
source. The rules root is manifest-owned; unmanifested sibling files survive.
The writer also retires the legacy plugin only when its prior managed hash
proves ownership.

## Skills and agents

Skill trees are mirrored and receive Cursor-supported frontmatter. Canonical
license is omitted because Cursor does not support it; supported `cursor:`
fields pass through.

Agents preserve their source filenames. Canonical write capability derives the
native `readonly` flag, and canonical background mode lowers to
`is_background`. Target-specific values override derived values.

## Native configuration

Canonical command permissions generate separate values for:

- Cursor Agent `/approvalMode`, expressible command and tool allows in
  `/permissions/allow`, command/tool/secret-path denies in
  `/permissions/deny`, and `autoAcceptWebSearch` when `websearch` is declared;
- Cursor Desktop `/approvalMode` and expressible command allows in
  `/terminalAllowlist`.

Cursor has no ask channel, so portable asks are absent rather than converted
to denies. Cursor Desktop also has no deny channel. Free-text command rules
and exact zero-argument rules are omitted because the verified native matcher
cannot represent them. Leading options expand to both valued and valueless
token shapes; wrapper guards are emitted around asks and denies where
possible.

Cursor Agent maps portable `read` and `write` to `Read(**)` and `Write(**)`.
It does not map `edit` or `webfetch`, whose closest native channels would widen
the authored decision. The compiler deliberately does not synthesize Desktop
secret-path or non-shell tool entries when that surface cannot express them.

External patches resolve from `config_root`:

| Patch name | Native target |
| --- | --- |
| `cursor-cli-config` | `<home>/.cursor/cli-config.json` |
| `cursor-permissions` | `<home>/.cursor/permissions.json` |
| `cursor-settings` | `<home>/.cursor/settings.json` |
| `cursor-mcp` | `<home>/.cursor/mcp.json` |

Optional same-named files under `patches.local/` must have mode `0600`.
Generated pointers use native semantic-hash ownership. Patch-only settings and
MCP pointers own only their exact named paths; unrelated native fields remain
tool-owned. Compatible Cursor Agent list extensions may add native tool
entries. Cursor Desktop's generated terminal allowlist is reserved from patch
contributions.

`coding-agents-cursor-desktop-probe` provides an opt-in macOS capability check.
It compiles an isolated ancestor rule into a temporary home and inspects only
the Cursor instance it launches.
