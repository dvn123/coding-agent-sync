from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

import tomlkit
import yaml

from .io import (
    MANAGED_MANIFEST,
    ManagedEntryConflict,
    entry_hash,
    load_manifest,
    retire_manifested_entries,
    sync_manifested_entries,
    write_bytes,
)
from .patches import Pointer, contributes_to_generated
from .plan import ManifestMode, OwnedFile, OwnedTree, Plan


def _pointers_overlap(pointer: Pointer, other: Pointer) -> bool:
    length = min(len(pointer), len(other))
    return pointer[:length] == other[:length]


def _validate_structured(path: Path) -> None:
    try:
        content = path.read_text()
    except UnicodeDecodeError as exc:
        raise ManagedEntryConflict(
            f"Invalid generated file encoding: {path}: {exc}"
        ) from None
    try:
        if path.suffix == ".json":
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError
        elif path.suffix == ".toml":
            parsed = tomlkit.parse(content)
            if not isinstance(parsed, Mapping):
                raise ValueError
        elif path.suffix in {".yaml", ".yml"}:
            yaml.safe_load(content)
        elif path.suffix == ".md" and content.startswith("---\n"):
            _, frontmatter, _ = content.split("---", 2)
            parsed = yaml.safe_load(frontmatter)
            if not isinstance(parsed, dict):
                raise ValueError
    except OSError, ValueError, yaml.YAMLError:
        raise ManagedEntryConflict(
            f"Invalid generated structured file: {path}"
        ) from None


def _prune_tree(target: Path, expected_files: set[Path]) -> None:
    if not target.exists() or target.is_symlink():
        return
    for child in sorted(
        target.rglob("*"), key=lambda path: len(path.parts), reverse=True
    ):
        rel = child.relative_to(target)
        if child.is_file() or child.is_symlink():
            if rel not in expected_files:
                child.unlink()
        elif child.is_dir():
            with suppress(OSError):
                child.rmdir()


def _tree_files(tree: OwnedTree) -> dict[Path, bytes]:
    return dict(tree.files)


def _write_owned_file(file: OwnedFile, destination: Path | None = None) -> None:
    path = destination or file.path
    if file.content is not None:
        write_bytes(path, file.content)
    elif file.retire_if is None:
        path.unlink(missing_ok=True)
    elif path.is_file() and path.read_bytes() == file.retire_if:
        path.unlink()


def _write_owned_tree(tree: OwnedTree, destination: Path | None = None) -> None:
    if tree.declaration or tree.retired:
        return
    target = destination or tree.root
    if target.is_symlink():
        return
    target.mkdir(parents=True, exist_ok=True)
    files = _tree_files(tree)
    for relative, content in files.items():
        write_bytes(target / relative, content, executable=relative in tree.executables)
    _prune_tree(target, set(files))


def _managed_groups(
    plan: Plan,
) -> tuple[tuple[Path, ManifestMode, set[str], bool], ...]:
    groups: dict[tuple[Path, ManifestMode], tuple[set[str], bool]] = {}

    def add(root: Path, mode: ManifestMode, name: str | None, retired: bool) -> None:
        current, was_retired = groups.setdefault((root, mode), (set(), retired))
        if was_retired != retired:
            raise ManagedEntryConflict(f"Conflicting managed root: {root}")
        if name is not None:
            current.add(name)

    for tree in plan.trees:
        if tree.declaration or tree.retired:
            if tree.manifest_root is None:
                raise ManagedEntryConflict(f"Missing managed root: {tree.root}")
            add(tree.manifest_root, tree.manifest_mode, None, tree.retired)
        elif tree.manifest_root is not None:
            add(
                tree.manifest_root,
                tree.manifest_mode,
                tree.root.relative_to(tree.manifest_root).as_posix(),
                False,
            )
    for file in plan.files:
        if file.manifest_root is not None:
            add(
                file.manifest_root,
                "file",
                (
                    file.path.relative_to(file.manifest_root).as_posix()
                    if file.content is not None
                    else None
                ),
                False,
            )
    return tuple(
        (root, mode, current, retired)
        for (root, mode), (current, retired) in groups.items()
    )


def _plan_producers(plan: Plan) -> dict[Path, OwnedFile | OwnedTree]:
    producers: dict[Path, OwnedFile | OwnedTree] = {}
    for file in plan.files:
        if file.content is not None:
            producers[file.path] = file
    for tree in plan.trees:
        if not tree.declaration and not tree.retired:
            producers[tree.root] = tree
    return producers


def _output_paths(plan: Plan) -> list[Path]:
    return [
        *(file.path for file in plan.files if file.content is not None),
        *(
            tree.root
            for tree in plan.trees
            if not tree.declaration and not tree.retired
        ),
    ]


def _content_paths(plan: Plan) -> list[Path]:
    return [
        *(file.path for file in plan.files if file.content is not None),
        *(
            tree.root / relative
            for tree in plan.trees
            if not tree.declaration and not tree.retired
            for relative, _ in tree.files
        ),
    ]


def _paths_overlap(path: Path, other: Path) -> bool:
    return path == other or path in other.parents or other in path.parents


def _absolute(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute: {path}")


def _managed_name(path: Path, root: Path, label: str) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError(f"{label} must be inside its manifest root: {path}") from None
    if not relative.parts:
        raise ValueError(f"{label} cannot equal its manifest root: {path}")


def _validate_paths(plan: Plan) -> None:
    for file in plan.files:
        _absolute(file.path, "OwnedFile path")
        if file.manifest_root is not None:
            _absolute(file.manifest_root, "OwnedFile manifest root")
            _managed_name(file.path, file.manifest_root, "OwnedFile path")
    for tree in plan.trees:
        _absolute(tree.root, "OwnedTree root")
        if tree.manifest_root is not None:
            _absolute(tree.manifest_root, "OwnedTree manifest root")
            if not tree.declaration and not tree.retired:
                _managed_name(tree.root, tree.manifest_root, "OwnedTree root")
        seen: set[Path] = set()
        for relative, _ in tree.files:
            if (
                not relative.parts
                or relative.is_absolute()
                or ".." in relative.parts
                or relative in seen
            ):
                raise ValueError(f"unsafe OwnedTree file path: {relative}")
            seen.add(relative)
    for value in plan.native_values:
        _absolute(value.path, "NativeValue path")
    for patch in plan.native_patches:
        _absolute(patch.path, "NativePatch path")


def _validate_plan(plan: Plan) -> None:
    _validate_paths(plan)
    for diagnostic in plan.diagnostics:
        if diagnostic.level == "error":
            source = f": {diagnostic.source}" if diagnostic.source else ""
            raise ValueError(f"{diagnostic.message}{source}")

    paths = _output_paths(plan)
    for index, path in enumerate(paths):
        for other in paths[:index]:
            if _paths_overlap(path, other):
                raise ManagedEntryConflict(
                    f"Overlapping generated paths: {path} and {other}"
                )

    for root, _, _, _ in _managed_groups(plan):
        manifest = root / MANAGED_MANIFEST
        for path in _content_paths(plan):
            if _paths_overlap(path, manifest):
                raise ManagedEntryConflict(
                    f"Generated output overlaps managed manifest: {path} and {manifest}"
                )

    surfaces = [
        *((value.target, value.path) for value in plan.native_values),
        *((patch.target, patch.path) for patch in plan.native_patches),
    ]
    target_paths: dict[str, Path] = {}
    path_targets: dict[Path, str] = {}
    for target, path in surfaces:
        previous_path = target_paths.setdefault(target, path)
        if previous_path != path:
            raise ValueError(f"Conflicting native surface path: {target}")
        previous_target = path_targets.setdefault(path, target)
        if previous_target != target:
            raise ValueError(f"Conflicting native surface target: {path}")

    # Generated pointers own their output outright, so any overlap between two
    # of them is ambiguous. A patch may additionally contribute to a generated
    # pointer, but only by agreeing with it or adding to a container the
    # compiler owns; see patches.contributes_to_generated.
    generated = [
        (value.target, value.path, value.pointer, value.value)
        for value in plan.native_values
    ]
    operations = [
        (patch.target, patch.path, operation)
        for patch in plan.native_patches
        for operation in patch.operations
    ]
    for index, (target, path, pointer, _) in enumerate(generated):
        for _, other_path, other_pointer, _ in generated[:index]:
            if path == other_path and _pointers_overlap(pointer, other_pointer):
                raise ValueError(f"Overlapping native pointers: {target} {pointer}")
    for index, (target, path, operation) in enumerate(operations):
        for _, other_path, other in operations[:index]:
            if path == other_path and _pointers_overlap(
                operation.pointer, other.pointer
            ):
                raise ValueError(
                    f"Overlapping native pointers: {target} {operation.pointer}"
                )
        for _, other_path, pointer, value in generated:
            if path != other_path or not _pointers_overlap(operation.pointer, pointer):
                continue
            if not contributes_to_generated(operation, pointer, value):
                raise ValueError(
                    f"Overlapping native pointers: {target} {operation.pointer}"
                )
    for path in [
        *(value.path for value in plan.native_values),
        *(patch.path for patch in plan.native_patches),
    ]:
        for output in paths:
            if _paths_overlap(path, output):
                raise ManagedEntryConflict(
                    f"Overlapping portable and native paths: {output} and {path}"
                )


def preflight_plan(plan: Plan) -> None:
    _validate_plan(plan)
    producers = _plan_producers(plan)
    for root, mode, current, retired in _managed_groups(plan):
        previous = load_manifest(root / ".coding-agents-managed.json")
        desired: dict[str, str] = {}
        with TemporaryDirectory() as temporary:
            stage = Path(temporary)
            for name in current:
                target = root / name
                producer = producers.get(target)
                if producer is None:
                    raise ManagedEntryConflict(f"Missing generated {mode}: {target}")
                if isinstance(producer, OwnedFile):
                    _write_owned_file(producer, stage / name)
                else:
                    _write_owned_tree(producer, stage / name)
                hash_ = entry_hash(stage / name, mode)
                if hash_ is None:
                    raise ManagedEntryConflict(f"Missing generated {mode}: {target}")
                desired[name] = hash_
            for path in stage.rglob("*"):
                if path.is_file():
                    _validate_structured(path)
        if retired:
            current = set()
        previous_names = set(previous)
        for name in current - previous_names:
            target = root / name
            actual = entry_hash(target, mode)
            if (target.exists() or target.is_symlink()) and actual != desired[name]:
                raise ManagedEntryConflict(
                    f"Refusing to claim unmanifested generated {mode}: {target}"
                )
        for name, expected in previous.items():
            actual = entry_hash(root / name, mode)
            if expected is None and name not in current and actual is not None:
                raise ManagedEntryConflict(
                    f"Refusing to prune legacy generated {mode} without a hash: "
                    f"{root / name}"
                )
            if (
                expected is not None
                and actual is not None
                and actual
                not in {
                    expected,
                    desired.get(name),
                }
            ):
                raise ManagedEntryConflict(
                    f"Refusing to replace modified generated {mode}: {root / name}"
                )


def plan_drift(plan: Plan) -> tuple[Path, ...]:
    drift: set[Path] = set()
    for file in plan.files:
        if file.content is None:
            if (file.retire_if is None and file.path.exists()) or (
                file.retire_if is not None
                and file.path.is_file()
                and file.path.read_bytes() == file.retire_if
            ):
                drift.add(file.path)
        elif not file.path.is_file() or file.path.read_bytes() != file.content:
            drift.add(file.path)
    for tree in plan.trees:
        if tree.declaration or tree.retired:
            continue
        actual = (
            {
                path.relative_to(tree.root)
                for path in tree.root.rglob("*")
                if path.is_file()
            }
            if tree.root.is_dir()
            else set()
        )
        expected = set(_tree_files(tree))
        if actual != expected or any(
            (tree.root / relative).read_bytes() != content
            for relative, content in tree.files
            if (tree.root / relative).is_file()
        ):
            drift.add(tree.root)
    for root, mode, current, retired in _managed_groups(plan):
        manifest_path = root / ".coding-agents-managed.json"
        if retired:
            if manifest_path.exists():
                drift.add(manifest_path)
            continue
        manifest = load_manifest(manifest_path)
        if set(manifest) != current or any(
            expected != entry_hash(root / name, mode)
            for name, expected in manifest.items()
        ):
            drift.add(manifest_path)
    return tuple(sorted(drift))


def write_plan(plan: Plan) -> None:
    for file in plan.files:
        _write_owned_file(file)
    for tree in plan.trees:
        _write_owned_tree(tree)


def publish_plan_manifests(plan: Plan) -> None:
    for root, mode, current, retired in _managed_groups(plan):
        if retired:
            retire_manifested_entries(root, mode)
        else:
            sync_manifested_entries(root, current, mode)
