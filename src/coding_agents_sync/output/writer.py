import json
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

import tomlkit
import yaml

from ..io import (
    MANAGED_MANIFEST,
    ManagedEntryConflict,
    entry_hash,
    is_incidental,
    load_manifest,
    split_frontmatter,
    sync_manifested_entries,
    write_bytes,
)
from .patches import contributes_to_generated, touches
from .plan import ManifestMode, OwnedFile, OwnedTree, Plan


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
        elif path.suffix == ".md":
            header, _ = split_frontmatter(content)
            if header is not None and not isinstance(yaml.safe_load(header), dict):
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
        if is_incidental(rel):
            continue
        if child.is_file() or child.is_symlink():
            if rel not in expected_files:
                child.unlink()
        elif child.is_dir():
            with suppress(OSError):
                child.rmdir()


def _write_owned_tree(tree: OwnedTree, destination: Path | None = None) -> None:
    target = destination or tree.root
    if target.is_symlink():
        return
    target.mkdir(parents=True, exist_ok=True)
    files = dict(tree.files)
    for relative, content in files.items():
        write_bytes(target / relative, content, executable=relative in tree.executables)
    _prune_tree(target, set(files))


def _generated_trees(plan: Plan) -> list[OwnedTree]:
    return [tree for tree in plan.trees if not tree.declaration]


def _managed_groups(plan: Plan) -> dict[tuple[Path, ManifestMode], set[str]]:
    """Each manifest root and mode, with the entry names generated below it."""
    groups: dict[tuple[Path, ManifestMode], set[str]] = {}
    for tree in plan.trees:
        names = groups.setdefault((tree.manifest_root, tree.manifest_mode), set())
        if not tree.declaration:
            names.add(tree.root.relative_to(tree.manifest_root).as_posix())
    for file in plan.files:
        groups.setdefault((file.manifest_root, "file"), set()).add(
            file.path.relative_to(file.manifest_root).as_posix()
        )
    return groups


def _output_paths(plan: Plan) -> list[Path]:
    return [
        *(file.path for file in plan.files),
        *(tree.root for tree in _generated_trees(plan)),
    ]


def _content_paths(plan: Plan) -> list[Path]:
    return [
        *(file.path for file in plan.files),
        *(
            tree.root / relative
            for tree in _generated_trees(plan)
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
        _absolute(file.manifest_root, "OwnedFile manifest root")
        _managed_name(file.path, file.manifest_root, "OwnedFile path")
    for tree in plan.trees:
        _absolute(tree.root, "OwnedTree root")
        _absolute(tree.manifest_root, "OwnedTree manifest root")
        if not tree.declaration:
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

    for root, _ in _managed_groups(plan):
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
            if path == other_path and touches(pointer, other_pointer):
                raise ValueError(f"Overlapping native pointers: {target} {pointer}")
    for index, (target, path, operation) in enumerate(operations):
        for _, other_path, other in operations[:index]:
            if path == other_path and touches(operation.pointer, other.pointer):
                raise ValueError(
                    f"Overlapping native pointers: {target} {operation.pointer}"
                )
        for _, other_path, pointer, value in generated:
            if path != other_path or not touches(operation.pointer, pointer):
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
    producers: dict[Path, OwnedFile | OwnedTree] = {
        **{file.path: file for file in plan.files},
        **{tree.root: tree for tree in _generated_trees(plan)},
    }
    for (root, mode), current in _managed_groups(plan).items():
        previous = load_manifest(root / MANAGED_MANIFEST)
        desired: dict[str, str] = {}
        with TemporaryDirectory() as temporary:
            stage = Path(temporary)
            for name in current:
                target = root / name
                producer = producers.get(target)
                if producer is None:
                    raise ManagedEntryConflict(f"Missing generated {mode}: {target}")
                if isinstance(producer, OwnedFile):
                    write_bytes(stage / name, producer.content)
                else:
                    _write_owned_tree(producer, stage / name)
                hash_ = entry_hash(stage / name, mode)
                if hash_ is None:
                    raise ManagedEntryConflict(f"Missing generated {mode}: {target}")
                desired[name] = hash_
            unstructured = {
                stage / name
                for name in current
                if isinstance(producer := producers[root / name], OwnedFile)
                and not producer.structured
            }
            for path in stage.rglob("*"):
                if path.is_file() and path not in unstructured:
                    _validate_structured(path)
        for name in current - set(previous):
            target = root / name
            actual = entry_hash(target, mode)
            if (target.exists() or target.is_symlink()) and actual != desired[name]:
                raise ManagedEntryConflict(
                    f"Refusing to claim unmanifested generated {mode}: {target}"
                )
        for name, expected in previous.items():
            actual = entry_hash(root / name, mode)
            if actual is not None and actual not in {expected, desired.get(name)}:
                raise ManagedEntryConflict(
                    f"Refusing to replace modified generated {mode}: {root / name}"
                )


def plan_drift(plan: Plan) -> tuple[Path, ...]:
    drift: set[Path] = set()
    for file in plan.files:
        if not file.path.is_file() or file.path.read_bytes() != file.content:
            drift.add(file.path)
    for tree in _generated_trees(plan):
        actual = (
            {
                path.relative_to(tree.root)
                for path in tree.root.rglob("*")
                if path.is_file() and not is_incidental(path.relative_to(tree.root))
            }
            if tree.root.is_dir()
            else set()
        )
        if actual != {relative for relative, _ in tree.files} or any(
            (tree.root / relative).read_bytes() != content
            for relative, content in tree.files
            if (tree.root / relative).is_file()
        ):
            drift.add(tree.root)
    for (root, mode), current in _managed_groups(plan).items():
        manifest_path = root / MANAGED_MANIFEST
        manifest = load_manifest(manifest_path)
        if set(manifest) != current or any(
            expected != entry_hash(root / name, mode)
            for name, expected in manifest.items()
        ):
            drift.add(manifest_path)
    return tuple(sorted(drift))


def write_plan(plan: Plan) -> None:
    for file in plan.files:
        write_bytes(file.path, file.content)
    for tree in _generated_trees(plan):
        _write_owned_tree(tree)


def publish_plan_manifests(plan: Plan) -> None:
    for (root, mode), current in _managed_groups(plan).items():
        sync_manifested_entries(root, current, mode)
