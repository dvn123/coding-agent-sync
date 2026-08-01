from __future__ import annotations

from pathlib import Path

import pytest

from coding_agents_sync.io import MANAGED_MANIFEST, ManagedEntryConflict
from coding_agents_sync.patches import Operation
from coding_agents_sync.plan import NativePatch, NativeValue, OwnedFile, OwnedTree, Plan
from coding_agents_sync.runtime_config import prepare_plan_native
from coding_agents_sync.sync import Transaction, reconcile
from coding_agents_sync.writer import preflight_plan


def test_plan_rejects_file_collisions(tmp_path: Path) -> None:
    path = tmp_path / "target"
    plan = Plan(files=(OwnedFile(path, b"generated"), OwnedFile(path, b"raw")))

    with pytest.raises(ManagedEntryConflict, match="Overlapping generated paths"):
        preflight_plan(plan)


def test_plan_rejects_file_tree_ancestry(tmp_path: Path) -> None:
    path = tmp_path / "target"
    plan = Plan(
        files=(OwnedFile(path, b"generated"),),
        trees=(OwnedTree(path / "tree", ((Path("child"), b"raw"),)),),
    )

    with pytest.raises(ManagedEntryConflict, match="Overlapping generated paths"):
        preflight_plan(plan)


def test_plan_rejects_generated_manifest_collision(tmp_path: Path) -> None:
    root = tmp_path / "target"
    plan = Plan(files=(OwnedFile(root / MANAGED_MANIFEST, b"not a manifest", root),))

    with pytest.raises(ManagedEntryConflict, match="overlaps managed manifest"):
        preflight_plan(plan)


@pytest.mark.parametrize(
    "relative",
    (Path("../escape"), Path("/escape"), Path("")),
)
def test_plan_rejects_unsafe_owned_tree_entries(tmp_path: Path, relative: Path) -> None:
    plan = Plan(trees=(OwnedTree(tmp_path / "target", ((relative, b"raw"),)),))

    with pytest.raises(ValueError, match="unsafe OwnedTree file path"):
        preflight_plan(plan)


def test_plan_rejects_duplicate_owned_tree_entries(tmp_path: Path) -> None:
    plan = Plan(
        trees=(
            OwnedTree(
                tmp_path / "target",
                ((Path("same"), b"first"), (Path("same"), b"second")),
            ),
        )
    )

    with pytest.raises(ValueError, match="unsafe OwnedTree file path"):
        preflight_plan(plan)


def test_plan_rejects_relative_output_path() -> None:
    plan = Plan(files=(OwnedFile(Path("relative"), b"generated"),))

    with pytest.raises(ValueError, match="OwnedFile path must be absolute"):
        preflight_plan(plan)


@pytest.mark.parametrize(
    "pointers",
    [(("native",), ("native",)), (("native",), ("native", "child"))],
)
def test_plan_rejects_duplicate_or_ancestor_native_pointers(
    tmp_path: Path, pointers: tuple[tuple[str, ...], tuple[str, ...]]
) -> None:
    plan = Plan(
        native_values=tuple(
            NativeValue("fixture", tmp_path / "target.json", pointer, "generated")
            for pointer in pointers
        )
    )

    with pytest.raises(ValueError, match="Overlapping native pointers"):
        preflight_plan(plan)


def test_plan_rejects_raw_patch_overlapping_generated_value(tmp_path: Path) -> None:
    plan = Plan(
        native_values=(
            NativeValue("fixture", tmp_path / "target.json", ("native",), "generated"),
        ),
        native_patches=(
            NativePatch(
                "fixture",
                tmp_path / "target.json",
                (Operation("set", ("native",), "raw"),),
                tmp_path / "raw.yaml",
            ),
        ),
    )

    with pytest.raises(ValueError, match="Overlapping native pointers"):
        preflight_plan(plan)


def test_plan_rejects_shared_native_path_before_writes(tmp_path: Path) -> None:
    path = tmp_path / "target.json"
    plan = Plan(
        native_values=(
            NativeValue("alpha", path, ("alpha",), 1),
            NativeValue("beta", path, ("beta",), 2),
        )
    )

    with pytest.raises(ValueError, match="Conflicting native surface target"):
        reconcile(config_root=tmp_path / "config-root", plan=plan)

    assert not path.exists()
    assert not (tmp_path / "config-root/.coding-agents-native.json").exists()


def test_plan_rejects_owned_file_overlapping_native_value(tmp_path: Path) -> None:
    path = tmp_path / "target.json"
    plan = Plan(
        files=(OwnedFile(path, b'{"raw": true}\n'),),
        native_values=(NativeValue("fixture", path, ("native",), "generated"),),
    )

    with pytest.raises(ManagedEntryConflict, match="portable and native"):
        preflight_plan(plan)


def test_plan_rejects_owned_tree_overlapping_native_patch(tmp_path: Path) -> None:
    root = tmp_path / "target"
    plan = Plan(
        trees=(OwnedTree(root, ((Path("config.json"), b'{"raw": true}\n'),)),),
        native_patches=(
            NativePatch(
                "fixture",
                root / "config.json",
                (Operation("set", ("native",), "raw"),),
                tmp_path / "raw.yaml",
            ),
        ),
    )

    with pytest.raises(ManagedEntryConflict, match="portable and native"):
        preflight_plan(plan)


def test_native_value_path_is_explicit_in_the_plan(tmp_path: Path) -> None:
    path = tmp_path / "native.json"
    plan = prepare_plan_native(
        config_root=tmp_path / "config-root",
        native_values=(NativeValue("fixture", path, ("enabled",), True),),
        native_patches=(),
    )

    assert plan.candidates[0].path == path


def test_reconcile_builds_a_write_free_transaction(tmp_path: Path) -> None:
    portable = tmp_path / "home" / "generated.md"
    native = tmp_path / "home" / "native.json"
    plan = Plan(
        files=(OwnedFile(portable, b"generated"),),
        native_values=(NativeValue("fixture", native, ("enabled",), True),),
    )

    transaction = reconcile(config_root=tmp_path / "config", plan=plan)

    assert isinstance(transaction, Transaction)
    assert transaction.plan is plan
    assert portable in transaction.drift
    assert native in transaction.drift
    assert not portable.exists()
    assert not native.exists()
