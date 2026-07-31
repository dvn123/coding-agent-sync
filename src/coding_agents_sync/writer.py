from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path
from tempfile import TemporaryDirectory

import tomlkit
import yaml

from .artifacts import (
    Artifact,
    FileTreeArtifact,
    ManagedRootArtifact,
    OptionalTextArtifact,
    RetiredRootArtifact,
    RetiredTextArtifact,
    TextArtifact,
    TreeArtifact,
)
from .io import (
    ManagedEntryConflict,
    entry_hash,
    load_manifest,
    retire_manifested_entries,
    sync_manifested_entries,
    write_bytes,
    write_optional_text,
    write_text,
)


def _validate_structured(path: Path) -> None:
    content = path.read_text()
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


def write_tree_artifact(artifact: TreeArtifact) -> None:
    if artifact.target.is_symlink():
        return
    artifact.target.mkdir(parents=True, exist_ok=True)

    expected_files: set[Path] = set()
    overrides = {
        Path(path): content for path, content in artifact.text_overrides.items()
    }

    for source_file in sorted(
        path for path in artifact.source.rglob("*") if path.is_file()
    ):
        rel = source_file.relative_to(artifact.source)
        expected_files.add(rel)
        if rel in overrides:
            continue
        write_bytes(artifact.target / rel, source_file.read_bytes())

    for rel, content in sorted(overrides.items(), key=lambda item: str(item[0])):
        expected_files.add(rel)
        write_text(artifact.target / rel, content)

    _prune_tree(artifact.target, expected_files)


def write_file_tree_artifact(artifact: FileTreeArtifact) -> None:
    if artifact.target.is_symlink():
        return
    artifact.target.mkdir(parents=True, exist_ok=True)
    expected_files = set(artifact.files)
    for rel, content in sorted(artifact.files.items(), key=lambda item: str(item[0])):
        write_text(artifact.target / rel, content)
    _prune_tree(artifact.target, expected_files)


def _destination(artifact: Artifact) -> Path | None:
    if isinstance(artifact, (TextArtifact, OptionalTextArtifact)):
        return artifact.path
    if isinstance(artifact, (FileTreeArtifact, TreeArtifact)):
        return artifact.target
    return None


def _write_artifact(artifact: Artifact, destination: Path | None = None) -> None:
    if isinstance(artifact, TextArtifact):
        write_text(destination or artifact.path, artifact.content)
    elif isinstance(artifact, FileTreeArtifact):
        write_file_tree_artifact(
            FileTreeArtifact(destination or artifact.target, artifact.files)
        )
    elif isinstance(artifact, OptionalTextArtifact):
        write_optional_text(destination or artifact.path, artifact.content)
    elif isinstance(artifact, TreeArtifact):
        write_tree_artifact(
            TreeArtifact(
                artifact.source,
                destination or artifact.target,
                artifact.text_overrides,
            )
        )
    elif isinstance(artifact, RetiredTextArtifact):
        path = destination or artifact.path
        if path.is_file() and path.read_text() == artifact.generated_content:
            path.unlink()
    else:
        raise TypeError(f"Unsupported concrete artifact: {artifact!r}")


def preflight_artifacts(artifacts: Sequence[Artifact]) -> None:
    producers = {
        destination: artifact
        for artifact in artifacts
        if (destination := _destination(artifact)) is not None
    }
    for managed in (
        item
        for item in artifacts
        if isinstance(item, (ManagedRootArtifact, RetiredRootArtifact))
    ):
        previous = load_manifest(managed.root / ".coding-agents-managed.json")
        current = managed.current if isinstance(managed, ManagedRootArtifact) else set()
        desired: dict[str, str] = {}
        with TemporaryDirectory() as temporary:
            stage = Path(temporary)
            for name in current:
                target = managed.root / name
                producer = producers.get(target)
                if producer is not None:
                    _write_artifact(producer, stage / name)
                hash_ = entry_hash(stage / name, managed.mode)
                if hash_ is None:
                    raise ManagedEntryConflict(
                        f"Missing generated {managed.mode}: {target}"
                    )
                desired[name] = hash_
            for path in stage.rglob("*"):
                if path.is_file():
                    _validate_structured(path)
        previous_names = set(previous)
        for name in current - previous_names:
            target = managed.root / name
            actual = entry_hash(target, managed.mode)
            if (target.exists() or target.is_symlink()) and actual != desired[name]:
                raise ManagedEntryConflict(
                    f"Refusing to claim unmanifested generated {managed.mode}: {target}"
                )
        for name, expected in previous.items():
            actual = entry_hash(managed.root / name, managed.mode)
            if expected is None and name not in current and actual is not None:
                raise ManagedEntryConflict(
                    f"Refusing to prune legacy generated {managed.mode} "
                    "without a hash: "
                    f"{managed.root / name}"
                )
            if expected is None:
                continue
            if actual is not None and actual not in {expected, desired.get(name)}:
                raise ManagedEntryConflict(
                    f"Refusing to replace modified generated {managed.mode}: "
                    f"{managed.root / name}"
                )


def artifact_drift(artifacts: Sequence[Artifact]) -> tuple[Path, ...]:
    drift: set[Path] = set()
    for item in artifacts:
        if isinstance(item, TextArtifact):
            if not item.path.is_file() or item.path.read_text() != item.content:
                drift.add(item.path)
        elif isinstance(item, OptionalTextArtifact):
            if item.content is None:
                if item.path.exists():
                    drift.add(item.path)
            elif not item.path.is_file() or item.path.read_text() != item.content:
                drift.add(item.path)
        elif isinstance(item, FileTreeArtifact):
            actual = (
                {
                    path.relative_to(item.target)
                    for path in item.target.rglob("*")
                    if path.is_file()
                }
                if item.target.is_dir()
                else set()
            )
            if actual != set(item.files) or any(
                (item.target / path).read_text() != content
                for path, content in item.files.items()
                if (item.target / path).is_file()
            ):
                drift.add(item.target)
        elif isinstance(item, TreeArtifact):
            expected = {
                path.relative_to(item.source)
                for path in item.source.rglob("*")
                if path.is_file()
            } | set(item.text_overrides)
            actual = (
                {
                    path.relative_to(item.target)
                    for path in item.target.rglob("*")
                    if path.is_file()
                }
                if item.target.is_dir()
                else set()
            )
            if expected != actual:
                drift.add(item.target)
                continue
            for relative in expected:
                desired = item.text_overrides.get(relative)
                if desired is None:
                    if (item.target / relative).read_bytes() != (
                        item.source / relative
                    ).read_bytes():
                        drift.add(item.target)
                        break
                elif (item.target / relative).read_text() != desired:
                    drift.add(item.target)
                    break
        elif isinstance(item, ManagedRootArtifact):
            manifest_path = item.root / ".coding-agents-managed.json"
            manifest = load_manifest(manifest_path)
            hashes_match = set(manifest) == item.current and all(
                expected == entry_hash(item.root / name, item.mode)
                for name, expected in manifest.items()
            )
            if not hashes_match:
                drift.add(manifest_path)
        elif isinstance(item, RetiredRootArtifact):
            manifest_path = item.root / ".coding-agents-managed.json"
            if manifest_path.exists():
                drift.add(manifest_path)
        elif (
            isinstance(item, RetiredTextArtifact)
            and item.path.is_file()
            and item.path.read_text() == item.generated_content
        ):
            drift.add(item.path)
    return tuple(sorted(drift))


def write_artifacts(artifacts: Sequence[Artifact]) -> None:
    for artifact in artifacts:
        if not isinstance(artifact, (ManagedRootArtifact, RetiredRootArtifact)):
            _write_artifact(artifact)


def publish_manifests(artifacts: Sequence[Artifact]) -> None:
    for artifact in artifacts:
        if isinstance(artifact, ManagedRootArtifact):
            sync_manifested_entries(artifact.root, artifact.current, artifact.mode)
        elif isinstance(artifact, RetiredRootArtifact):
            retire_manifested_entries(artifact.root, artifact.mode)
