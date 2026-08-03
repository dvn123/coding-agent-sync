from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Literal

MANAGED_MANIFEST = ".coding-agents-managed.json"
MANIFEST_VERSION = 1

type Manifest = dict[str, str | None]


class ManagedEntryConflict(RuntimeError):
    pass


def write_text(path: Path, content: str) -> None:
    if path.exists() and path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def owner_execute_mode(path: Path, executable: bool) -> int:
    """Deployed mode for `path`, tracking the source's owner-execute bit only.

    Group and world bits are never widened: an executable script deploys 0700.
    """
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    return mode | stat.S_IXUSR if executable else mode & ~stat.S_IXUSR


def write_bytes(path: Path, content: bytes, *, executable: bool = False) -> None:
    if path.exists() and path.is_file() and path.read_bytes() == content:
        # Content is already correct, but the mode may still be stale.
        if (desired := owner_execute_mode(path, executable)) != (
            path.stat().st_mode & 0o777
        ):
            os.chmod(path, desired)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = owner_execute_mode(path, executable)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_optional_text(path: Path, content: str | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        write_text(path, content)


def _managed_path(root: Path, name: str) -> Path:
    relative = Path(name)
    if (
        not name
        or relative.is_absolute()
        or ".." in relative.parts
        or name == MANAGED_MANIFEST
    ):
        raise ValueError(f"Invalid managed entry: {name!r}")
    return root / relative


def entry_hash(path: Path, mode: Literal["dir", "file"]) -> str | None:
    if path.is_symlink():
        return None
    digest = hashlib.sha256()
    if mode == "file":
        if not path.is_file():
            return None
        digest.update(path.read_bytes())
    else:
        if not path.is_dir():
            return None
        for child in sorted(path.rglob("*"), key=lambda item: item.relative_to(path)):
            relative = child.relative_to(path).as_posix().encode()
            if child.is_symlink():
                return None
            digest.update(b"d\0" if child.is_dir() else b"f\0")
            digest.update(relative)
            digest.update(b"\0")
            if child.is_file():
                digest.update(child.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def load_manifest(path: Path) -> Manifest:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        manifest: Manifest
        if isinstance(parsed, list):
            manifest = {str(item): None for item in parsed}
        elif (
            isinstance(parsed, dict)
            and parsed.get("version") == MANIFEST_VERSION
            and isinstance(parsed.get("entries"), dict)
            and all(
                isinstance(name, str)
                and isinstance(value, str)
                and value.startswith("sha256:")
                for name, value in parsed["entries"].items()
            )
        ):
            manifest = {
                str(name): str(value) for name, value in parsed["entries"].items()
            }
        else:
            raise ValueError(f"Invalid managed manifest: {path}")
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid managed manifest: {path}") from error
    for name in manifest:
        _managed_path(path.parent, name)
    return manifest


def write_manifest(path: Path, entries: dict[str, str]) -> None:
    content = {"version": MANIFEST_VERSION, "entries": dict(sorted(entries.items()))}
    write_text(path, json.dumps(content, indent=2, ensure_ascii=False) + "\n")


def prune_stale_entries(
    *,
    target_root: Path,
    previous: Manifest,
    current: set[str],
    mode: Literal["dir", "file"],
) -> None:
    stale = previous.keys() - current
    for name in stale:
        target = _managed_path(target_root, name)
        if target.is_symlink():
            continue
        expected = previous[name]
        actual = entry_hash(target, mode)
        if expected is None and actual is not None:
            raise ManagedEntryConflict(
                f"Refusing to prune legacy generated {mode} without a hash: {target}"
            )
        if actual is not None and actual != expected:
            raise ManagedEntryConflict(
                f"Refusing to prune modified generated {mode}: {target}"
            )
        if mode == "dir":
            if target.is_dir():
                shutil.rmtree(target)
        elif target.is_file():
            target.unlink()


def sync_manifested_entries(
    target_root: Path, current: set[str], mode: Literal["dir", "file"]
) -> None:
    previous = load_manifest(target_root / MANAGED_MANIFEST)
    prune_stale_entries(
        target_root=target_root, previous=previous, current=current, mode=mode
    )
    entries: dict[str, str] = {}
    for name in current:
        target = _managed_path(target_root, name)
        if (hash_ := entry_hash(target, mode)) is None:
            raise ManagedEntryConflict(f"Managed {mode} is missing or unsafe: {target}")
        entries[name] = hash_
    write_manifest(target_root / MANAGED_MANIFEST, entries)


def retire_manifested_entries(target_root: Path, mode: Literal["dir", "file"]) -> None:
    manifest_path = target_root / MANAGED_MANIFEST
    prune_stale_entries(
        target_root=target_root,
        previous=load_manifest(manifest_path),
        current=set(),
        mode=mode,
    )
    manifest_path.unlink(missing_ok=True)
