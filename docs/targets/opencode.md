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

OpenCode's ordered `/permission/bash` map preserves allow, ask, and deny
precedence, while other portable permission values lower only to semantically
matching V1 surfaces. Its leading `*` entry carries the policy's `unmatched`
decision; every rule is written after it, so the guards still outrank it.

The one departure from the portable policy is that the deny bucket lands as
`ask`, so the map never contains a bash `deny`. OpenCode answers a denial with
`PermissionDeniedError`, whose message embeds the serialized ruleset filtered
only by permission *type* — every bash rule reaches the model on every denial.
Against a real corpus that is roughly 1.3 MB, or ~324k tokens, per denial, and
it has ended sessions outright with `ContextOverflowError`. Guards still bind
and still sit last, so they beat both the allow bucket and the blanket wrapper
allows; they prompt instead of blocking. Every other target keeps its deny
channel. Secret-path denies on `/permission/read` and `/permission/edit` are
unaffected: those denials serialize only rules matching their own permission
type, which is a handful of entries.

`patches/opencode.yaml` targets
`<home>/.config/opencode/opencode.json`. Patches preserve unnamed fields and
may contribute to a generated pointer where the contribution does not clash,
including overlaying read, edit, or external-directory maps. They cannot
contribute command policy. Raw files below `target-config/opencode/raw/`
mirror below the OpenCode root; `opencode.json` is reserved for pointer
reconciliation.

Generated files use portable manifests; generated native values use pointer
plus semantic-hash ownership in `config_root/.coding-agents-native.json`.
