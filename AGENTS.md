# Repository Instructions

- Keep the Python package under `src/coding_agents_sync/` and user-neutral tests
  under `tests/`.
- This repository owns compiler behavior and target knowledge, not authored
  user configuration or machine orchestration. Configuration used by tests
  must be neutral and isolated below pytest temporary directories or
  `tests/fixtures/`.
- Put source-schema behavior in `sources.py`, target lowering in the matching
  translator, artifact definitions in `artifacts.py`, and write/ownership
  behavior in `writer.py` or `runtime_config.py`. Keep `sync.py`
  orchestration-only.
- Preserve manifested portable ownership and native pointer/hash ownership.
  Never prune unmanifested siblings or unnamed native fields.
- `config_root` owns sources, patches, target fragments, and the native
  manifest. `home` owns generated target files. Lower layers must not infer one
  from the other.
- Add tests for schema, translation, manifests, native reconciliation, or
  ownership changes. Prefer `run_sync()` integration tests for cross-layer
  behavior.
- Run `uv run --locked coding-agents-check` and `uv build` before finishing a
  change.
