from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from .models import SyncContext
from .plan import Plan, merge_plans
from .runtime_config import (
    NativePlan,
    apply_native_candidates,
    prepare_plan_native,
    publish_native_manifest,
)
from .sources import load_sources
from .targets import compile_claude, compile_codex, compile_cursor, compile_opencode
from .writer import (
    plan_drift,
    preflight_plan,
    publish_plan_manifests,
    write_plan,
)


@dataclass(frozen=True)
class Transaction:
    plan: Plan
    native_plan: NativePlan
    drift: tuple[Path, ...]


def reconcile(*, config_root: Path, plan: Plan) -> Transaction:
    preflight_plan(plan)
    native_plan = prepare_plan_native(
        config_root=config_root,
        native_values=plan.native_values,
        native_patches=plan.native_patches,
    )
    return Transaction(plan, native_plan, (*plan_drift(plan), *native_plan.drift))


def apply_transaction(transaction: Transaction) -> None:
    write_plan(transaction.plan)
    apply_native_candidates(transaction.native_plan)
    publish_plan_manifests(transaction.plan)
    publish_native_manifest(transaction.native_plan)


def run_sync(*, config_root: Path, home: Path, check: bool = False) -> tuple[Path, ...]:
    ctx = SyncContext(config_root=config_root, home=home)
    sources = load_sources(ctx.config_root)
    plan = merge_plans(
        compile_claude(ctx, sources),
        compile_cursor(ctx, sources),
        compile_opencode(ctx, sources),
        compile_codex(ctx, sources),
    )
    for diagnostic in plan.diagnostics:
        if diagnostic.level == "warning":
            source = f": {diagnostic.source}" if diagnostic.source else ""
            print(f"Warning: {diagnostic.message}{source}", file=sys.stderr)
    transaction = reconcile(config_root=config_root, plan=plan)
    if check:
        return transaction.drift
    apply_transaction(transaction)
    return ()
