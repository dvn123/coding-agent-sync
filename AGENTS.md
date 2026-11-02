# Repository Instructions

- Keep the Python package under `src/coding_agents_sync/` and user-neutral tests
  under `tests/`.
- This repository owns compiler behavior and target knowledge, not authored
  user configuration or machine orchestration. Configuration used by tests
  must be neutral and isolated below pytest temporary directories or
  `tests/fixtures/`.
- Put source-schema behavior in `sources.py`, portable fields a target cannot
  represent in `limits.py`, per-target source resolution in `ir.py`, target
  lowering in the matching translator under `targets/`, plan types in
  `output/plan.py`, and write/ownership behavior in `output/writer.py` or
  `output/native.py`. Keep `sync.py` orchestration-only.
- A translator lowers what `ir.resolve` hands it and never re-checks `only`,
  native fields, or omits itself. A new omission is a `Limitation`, and its
  `probe` must name a negative capability case: a refusal the target reports
  for the field, or a pin of the installed build's vocabulary (its schema or
  its parser) that a new channel would change. Probe before declaring a
  limitation; it may turn out lowerable.
- After an intended output change, run
  `uv run pytest tests/test_golden.py --inline-snapshot=fix` and review the
  diff under `tests/golden/`.
- Preserve manifested portable ownership and native pointer ownership: whole
  values by hash, generated lists by entry. Never prune unmanifested siblings,
  foreign list entries, or unnamed native fields.
- `config_root` owns sources, patches, target fragments, and the native
  manifest. `home` owns generated target files. Lower layers must not infer one
  from the other.
- Add tests for schema, translation, manifests, native reconciliation, or
  ownership changes. Prefer `run_sync()` integration tests for cross-layer
  behavior.
- Run `uv run --locked coding-agents-check` and `uv build` before finishing a
  change.
