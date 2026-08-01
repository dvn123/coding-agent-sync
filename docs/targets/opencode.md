# OpenCode V1 Target Reference

OpenCode V1 is the only active OpenCode adapter. V2 remains outside the
compiler until it leaves beta; there is no general version-adapter framework.

## Generated surfaces

| Source | Output |
| --- | --- |
| Global | `<home>/.config/opencode/AGENTS.md` |
| Rules | owned `/instructions` pointer in `opencode.json` |
| Skill | `<home>/.config/opencode/skills/<directory>/` |
| Command | `<home>/.config/opencode/commands/<source-file>.md` |
| Agent | `<home>/.config/opencode/agents/<source-file>.md` |
| Permissions | owned `/permission/*` pointers in `opencode.json` |

Rules may use strict `targets.opencode.native.instructions`. Typed skills use
`name`, `description`, `license`, `metadata`, and `compatibility`; commands use
`name`, `description`, `agent`, `subtask`, and `model`; agents use the concrete
OpenCode V1 agent fields, including `reasoningEffort`, `permission`, model,
provider, mode, sampling, and visibility settings. Unknown fields are errors.
Accepted raw frontmatter is visibly unvalidated and cannot shadow typed or
canonical output.

OpenCode receives the exact portable permission policy. Its ordered
`/permission/bash` map preserves allow, ask, and deny precedence, while other
portable permission values lower only to semantically matching V1 surfaces.

`patches/opencode.yaml` targets
`<home>/.config/opencode/opencode.json`. Patches preserve unnamed fields but
must not overlap generated pointers. They cannot contribute command policy and
may only add deny entries to generated read, edit, or external-directory
maps. Raw files below `target-config/opencode/raw/` mirror below the OpenCode
root; `opencode.json` is reserved for pointer reconciliation.

Generated files use portable manifests; generated native values use pointer
plus semantic-hash ownership in `config_root/.coding-agents-native.json`.
