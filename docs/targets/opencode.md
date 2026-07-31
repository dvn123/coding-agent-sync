# OpenCode Target Reference

## Compiler outputs

| Source kind | Generated output |
| --- | --- |
| Global | `<home>/.config/opencode/AGENTS.md` |
| Rules | `/instructions` entries in `opencode.json` |
| Skill | `<home>/.config/opencode/skills/<source-directory>/` |
| Command | `<home>/.config/opencode/commands/<source-file>.md` |
| Agent | `<home>/.config/opencode/agents/<source-file>.md` |
| Permissions | named paths in `opencode.json` |

The compiler targets `<home>/.config/opencode` directly. The process
`XDG_CONFIG_HOME` controls discovery of the coding-agents configuration root,
not OpenCode's generated output location.

## Global instructions and rules

The global body is written verbatim to `AGENTS.md`. Rules are not copied into
OpenCode's directory. Instead, their source paths, or an explicit
`opencode:instructions` override, are registered in the generated
`/instructions` list.

Instruction entries therefore remain coupled to the external configuration
root. Moving that root requires rerunning the compiler.

## Skills, commands, and agents

Skill trees are mirrored below `skills/`. Generated frontmatter includes
OpenCode-supported identity, license, metadata, and `opencode:` fields.

Commands preserve source filenames. Canonical agent and subtask execution
lower to OpenCode frontmatter, with target-specific values taking precedence.

Agents preserve source filenames. Canonical tool restrictions derive
OpenCode's `permission.edit` and `permission.bash` values. Supported effort and
color values lower to native keys; an explicit `opencode:permission` mapping is
overlaid on the derived result.

## Native configuration

Canonical permissions generate ordered maps for:

- `/permission/bash`;
- `/permission/read`;
- `/permission/edit`;
- `/permission/write`;
- `/permission/external_directory`.

Portable `webfetch` and `websearch` decisions generate scalar permission
values. The bash map starts with `'*': ask`, then emits allows, asks,
secret-name asks, and denies. OpenCode applies the last matching key, so this
insertion order is the permission precedence contract. Leading options and
wrappers expand to text-glob variants before insertion.

The generated `/instructions` and permission pointers use semantic-hash
ownership in `config_root/.coding-agents-native.json`.

`config_root/patches/opencode.yaml` and the optional mode-`0600`
`config_root/patches.local/opencode.yaml` reconcile other named paths in
`<home>/.config/opencode/opencode.json`. Patches cannot contribute command
permissions. They may add secret denials to generated read, edit, and
external-directory maps but cannot widen those maps.

External patches may own selected `/mcp/<name>` or other native paths. The
compiler supplies no default MCP or plugin inventory and preserves every
unnamed field.
