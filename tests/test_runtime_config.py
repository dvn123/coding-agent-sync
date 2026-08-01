from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from coding_agents_sync.io import write_bytes
from coding_agents_sync.patches import (
    Operation,
    Patch,
    PatchError,
    apply_operations,
    decode_pointer,
    load_patch,
    merge_patches,
)
from coding_agents_sync.plan import NativePatch, NativeValue
from coding_agents_sync.runtime_config import (
    NativeConfigError,
    apply_native_plan,
    prepare_plan_native,
)


def write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)


def test_pointer_decoding_and_patch_schema_validation(tmp_path: Path) -> None:
    assert decode_pointer("/a~1b/c~0d") == ("a/b", "c~d")
    for value in ("", "plain", "/bad~", "/bad~2"):
        with pytest.raises(PatchError):
            decode_pointer(value)
    for name, body in {
        "mapping-set": "set:\n  /x: {a: 1}\n",
        "scalar-replace": "replace:\n  /x: 1\n",
        "scalar-extend": "extend:\n  /x: 1\n",
        "list-overlay": "overlay:\n  /x: [1]\n",
        "overlap": "set:\n  /x: 1\n  /x/y: 2\n",
    }.items():
        write(tmp_path / name, f"schema: coding-agents/patch/v1\n{body}")
        with pytest.raises(PatchError):
            load_patch(tmp_path / name)


def test_operations_are_structural_and_local_overrides_replace_exact_pointers() -> None:
    document = {"unknown": {"keep": True}, "gone": 1, "table": {"old": 1}}
    apply_operations(
        document,
        Patch(
            (
                Operation("set", ("new", "leaf"), [1, 2]),
                Operation("replace", ("table",), {"fresh": 2}),
                Operation("extend", ("items",), ["a", "b", "a"]),
                Operation("delete", ("gone",)),
            )
        ),
    )
    assert document == {
        "unknown": {"keep": True},
        "new": {"leaf": [1, 2]},
        "table": {"fresh": 2},
        "items": ["a", "b"],
    }
    committed = Patch((Operation("set", ("a",), 1),))
    assert merge_patches(
        committed, Patch((Operation("set", ("a",), 2),))
    ).operations == (Operation("set", ("a",), 2),)
    with pytest.raises(PatchError):
        merge_patches(committed, Patch((Operation("set", ("a", "b"), 2),)))


def test_native_plan_preserves_unknown_values_and_file_mode(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    path = tmp_path / "home/settings.json"
    write(path, json.dumps({"unknown": {"nested": True}, "managed": "old"}), 0o640)
    plan = prepare_plan_native(
        config_root=config_root,
        native_values=(NativeValue("fixture", path, ("managed",), "new"),),
        native_patches=(),
    )
    assert path in plan.drift
    apply_native_plan(plan)
    assert json.loads(path.read_text()) == {
        "unknown": {"nested": True},
        "managed": "new",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_native_plan_refuses_modified_owned_pointer(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    path = tmp_path / "home/settings.json"
    values = (NativeValue("fixture", path, ("managed",), "generated"),)
    apply_native_plan(
        prepare_plan_native(
            config_root=config_root,
            native_values=values,
            native_patches=(),
        )
    )
    write(path, json.dumps({"managed": "user"}))
    with pytest.raises(NativeConfigError, match="modified generated native path"):
        prepare_plan_native(
            config_root=config_root,
            native_values=values,
            native_patches=(),
        )


def test_native_plan_removes_stale_owned_pointer_but_not_siblings(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    path = tmp_path / "home/settings.json"
    apply_native_plan(
        prepare_plan_native(
            config_root=config_root,
            native_values=(NativeValue("fixture", path, ("managed",), True),),
            native_patches=(),
        )
    )
    document = json.loads(path.read_text())
    document["unknown"] = "keep"
    write(path, json.dumps(document))
    apply_native_plan(
        prepare_plan_native(
            config_root=config_root,
            native_values=(),
            native_patches=(NativePatch("fixture", path, (), config_root / "patch"),),
        )
    )
    assert json.loads(path.read_text()) == {"unknown": "keep"}


def test_native_patch_is_applied_without_target_schema_validation(
    tmp_path: Path,
) -> None:
    config_root = tmp_path / "config"
    path = tmp_path / "home/settings.json"
    plan = prepare_plan_native(
        config_root=config_root,
        native_values=(),
        native_patches=(
            NativePatch(
                "fixture",
                path,
                (Operation("set", ("opaque",), ["unvalidated"]),),
                config_root / "patch",
            ),
        ),
    )
    apply_native_plan(plan)
    assert json.loads(path.read_text()) == {"opaque": ["unvalidated"]}


def test_manifest_is_strict_for_current_plan_targets(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    write(
        config_root / ".coding-agents-native.json",
        json.dumps(
            {
                "version": 3,
                "entries": [
                    {"target": "other", "pointer": "/x", "hash": "sha256:" + "0" * 64}
                ],
            }
        ),
    )
    with pytest.raises(NativeConfigError, match="manifest"):
        prepare_plan_native(
            config_root=config_root,
            native_values=(
                NativeValue("fixture", tmp_path / "home/settings.json", ("x",), 1),
            ),
            native_patches=(),
        )


def test_manifest_publishes_after_native_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import coding_agents_sync.runtime_config as runtime_config

    plan = prepare_plan_native(
        config_root=tmp_path / "config",
        native_values=(
            NativeValue("fixture", tmp_path / "home/settings.json", ("x",), 1),
        ),
        native_patches=(),
    )
    writes: list[Path] = []
    original = runtime_config.write_bytes

    def record(path: Path, content: bytes) -> None:
        writes.append(path)
        original(path, content)

    monkeypatch.setattr(runtime_config, "write_bytes", record)
    apply_native_plan(plan)

    assert writes[-1] == plan.manifest_path


def test_native_plan_rejects_ambiguous_targets_and_formats(tmp_path: Path) -> None:
    with pytest.raises(NativeConfigError, match="conflicting native surface"):
        prepare_plan_native(
            config_root=tmp_path / "config",
            native_values=(
                NativeValue("fixture", tmp_path / "one.json", ("x",), 1),
                NativeValue("fixture", tmp_path / "two.json", ("y",), 2),
            ),
            native_patches=(),
        )
    with pytest.raises(NativeConfigError, match="conflicting native surface target"):
        prepare_plan_native(
            config_root=tmp_path / "config",
            native_values=(
                NativeValue("alpha", tmp_path / "shared.json", ("x",), 1),
                NativeValue("beta", tmp_path / "shared.json", ("y",), 2),
            ),
            native_patches=(),
        )
    with pytest.raises(NativeConfigError, match="unsupported native config format"):
        prepare_plan_native(
            config_root=tmp_path / "config",
            native_values=(NativeValue("fixture", tmp_path / "native.txt", ("x",), 1),),
            native_patches=(),
        )


def test_atomic_native_write_uses_replace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "generated.bin"
    write(path, "old")
    replaced: list[tuple[str | os.PathLike[str], str | os.PathLike[str]]] = []
    real_replace = os.replace

    def replace(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        replaced.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", replace)
    write_bytes(path, b"new")
    assert replaced and Path(replaced[0][1]) == path
