# Coding Agents Sync

Coding Agents Sync is an installable Python CLI that compiles one external
Markdown and YAML configuration tree into user-level configuration for Claude
Code, Codex, Cursor, and OpenCode. It preserves target-native fields, writes
only declared outputs, and refuses to replace modified managed content.

This repository contains the compiler, target knowledge, documentation, and
tests. It intentionally contains no user configuration or machine
orchestration.

## Installation

Install from a local checkout with uv:

```sh
uv tool install /path/to/coding-agents-sync
```

The installed command runs directly:

```sh
coding-agents-sync
coding-agents-sync --check
```

`--check` reports drift and exits nonzero without writing. The additional
`coding-agents-cursor-desktop-probe` command runs the isolated Cursor Desktop
capability probe, and `coding-agents-check` runs this repository's development
validation suite.

## Configuration

The default configuration root is:

1. `$XDG_CONFIG_HOME/coding-agents` when `XDG_CONFIG_HOME` is non-empty.
2. `~/.config/coding-agents` otherwise.

Override the configuration and generated-target roots independently:

```sh
coding-agents-sync \
  --config-root /path/to/coding-agents \
  --home /path/to/isolated-home
```

`--home` defaults to the current user's home. It controls only generated tool
targets. The source tree, committed patches, local patches, target fragments,
and native ownership manifest always resolve from `config_root`.

A complete external configuration may use:

```text
global/AGENTS.md
rules/*.md
skills/*/SKILL.md
commands/*.md
agents/*.md
permissions/policy.yaml
permissions/commands/*.yaml
permissions.local/*.yaml
patches/*.yaml
patches.local/*.yaml
target-config/codex/rules/*.rules
```

All Markdown sources use `schema: coding-agents/v3`. Native patches use
`schema: coding-agents/patch/v1`; files under `patches.local/` must have mode
`0600`. The CLI consumes plain YAML patches and does not render templates.
Local permission fragments use the normal `permission-rules` schema and merge
with committed fragments before validation.
See [the source schema](docs/source-format.md) and the
[target references](docs/targets/) for the supported fields and lowering
behavior.

## Ownership

Portable generated roots use `.coding-agents-managed.json` manifests. The
compiler prunes only previously manifested entries and rejects modified managed
content. Unmanifested siblings remain untouched.

Native configuration is reconciled at named JSON/TOML pointers. Patch
operations own only their named paths. Generated permission paths, OpenCode
instructions, and Codex skill/agent registrations use semantic hashes recorded
in `config_root/.coding-agents-native.json`. Unnamed native fields remain
tool-owned.

Generated targets remain rooted below `home`:

```text
.claude/
.claude.json
.codex/
.cursor/
.config/opencode/
```

## Development

The project requires Python 3.14 and uses uv:

```sh
uv sync --locked
uv run --locked coding-agents-check
uv build
```

The validation command runs Ruff formatting, Ruff linting, BasedPyright, and
pytest. The ordinary test run does not launch installed coding agents.

The opt-in capability harness can exercise selected installed targets in
isolated temporary homes:

```sh
uv run --locked pytest --capabilities-live \
  --capability-report /tmp/capabilities.json tests/capabilities/
```

The v2 report (`coding-agents/capability-report/v2`) includes every registered
case and every declared target surface. A
surface's `management` is `generated`, `patch_only`, or `unmanaged`; an
unobserved surface is explicitly `unverified` / `not_probed`. Its
`evidence_kind` distinguishes target-native evidence from installed-static
evidence; only `compiler_e2e` proves the compiler lowering path. Repeat
`--target` or `--case` to narrow the live suite.

`actual` is a conclusion at the stated `evidence_kind`. In particular,
`installed_static` / `supported` means the capability is declared or present in
installed artifacts; it does not claim runtime execution.

Some cases intentionally establish that a surface is unsupported. Their
`support_when_checks_pass` value records whether passing checks prove
`supported` or `unsupported`. A failed check is `contradicted` / `regression`,
never proof of the opposite binary capability. Surface rows retain singular
fields when one case supplies evidence; rows with multiple cases expose every
case in `cases` and every full observation in `observations`. Their aggregate
is `contradicted` if selected evidence regresses or conflicts, and their
`check_count` is the sum of all associated observations.

Static Cursor Desktop bundle evidence is included by `--capabilities-live`; its
behavioral rule-loading probe also requires the explicit GUI opt-in:

```sh
uv run --locked pytest --capabilities-live --capabilities-desktop \
  --case cursor-desktop.rules tests/capabilities/
```

The harness strips credential environment variables and treats missing target
executables or Desktop applications as unavailable.

Live probes use the installed target versions by default. Updating targets is
explicit with `--update-targets`; it fails the run if an update fails or no
post-update version can be observed. `--skip-update` remains a deprecated
no-op and cannot be combined with `--update-targets`. Cursor local-runtime
cache lookup is read-only by default; use `--bootstrap-cursor-runtime` to
download a matching runtime into the managed cache.
