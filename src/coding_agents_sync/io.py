import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Literal

import yaml

MANAGED_MANIFEST = ".coding-agents-managed.json"

type Manifest = dict[str, str]


class ManagedEntryConflict(RuntimeError):
    pass


class _StrictLoader(yaml.SafeLoader):
    """A SafeLoader that rejects duplicate and non-scalar mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> Any:
        seen: set[str] = set()
        for key, _ in node.value:
            if key.tag == "tag:yaml.org,2002:merge":
                continue
            if not isinstance(key, yaml.ScalarNode):
                raise yaml.constructor.ConstructorError(
                    None, None, "mapping key must be scalar", key.start_mark
                )
            if key.value in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate YAML key `{key.value}`", key.start_mark
                )
            seen.add(key.value)
        return super().construct_mapping(node, deep=deep)


def load_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_StrictLoader)


def split_frontmatter(text: str) -> tuple[str | None, str]:
    """A Markdown document's YAML frontmatter, if any, and its body."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != "---":
        return None, text
    for index, line in enumerate(lines[1:], 1):
        if line.rstrip() in {"---", "..."}:
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return None, text


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


def is_incidental(relative: Path) -> bool:
    """Files a tool or the OS writes into a generated tree by using it.

    Running a skill's Python leaves bytecode caches and opening a folder in
    Finder leaves `.DS_Store`; neither is generated content or a user edit.
    """
    return (
        "__pycache__" in relative.parts
        or relative.suffix in {".pyc", ".pyo"}
        or relative.name == ".DS_Store"
    )


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
            if is_incidental(child.relative_to(path)):
                continue
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
        entries = json.loads(path.read_text(encoding="utf-8"))["entries"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError(f"Invalid managed manifest: {path}") from error
    if not isinstance(entries, dict) or not all(
        isinstance(value, str) and value.startswith("sha256:")
        for value in entries.values()
    ):
        raise ValueError(f"Invalid managed manifest: {path}")
    for name in entries:
        _managed_path(path.parent, name)
    return entries


def write_manifest(path: Path, entries: Manifest) -> None:
    content = json.dumps(
        {"entries": dict(sorted(entries.items()))}, indent=2, ensure_ascii=False
    )
    write_bytes(path, (content + "\n").encode())


def prune_stale_entries(
    *,
    target_root: Path,
    previous: Manifest,
    current: set[str],
    mode: Literal["dir", "file"],
    released: frozenset[Path] = frozenset(),
) -> None:
    """Remove each previously managed entry the plan no longer has. A
    `released` path changed owner, such as a raw file that became a native
    pointer surface, so it leaves the manifest and stays."""
    stale = previous.keys() - current
    for name in stale:
        target = _managed_path(target_root, name)
        if target.is_symlink() or target in released:
            continue
        actual = entry_hash(target, mode)
        if actual is not None and actual != previous[name]:
            raise ManagedEntryConflict(
                f"Refusing to prune modified generated {mode}: {target}"
            )
        if mode == "dir":
            if target.is_dir():
                shutil.rmtree(target)
        elif target.is_file():
            target.unlink()
        _remove_empty_parents(target, target_root)


def _remove_empty_parents(path: Path, root: Path) -> None:
    """Remove the directories between `path` and `root` that pruning emptied."""
    for parent in path.parents:
        if parent == root:
            return
        try:
            parent.rmdir()
        except OSError:
            return


def sync_manifested_entries(
    target_root: Path,
    current: set[str],
    mode: Literal["dir", "file"],
    released: frozenset[Path] = frozenset(),
) -> None:
    previous = load_manifest(target_root / MANAGED_MANIFEST)
    prune_stale_entries(
        target_root=target_root,
        previous=previous,
        current=current,
        mode=mode,
        released=released,
    )
    entries: Manifest = {}
    for name in current:
        target = _managed_path(target_root, name)
        if (hash_ := entry_hash(target, mode)) is None:
            raise ManagedEntryConflict(f"Managed {mode} is missing or unsafe: {target}")
        entries[name] = hash_
    write_manifest(target_root / MANAGED_MANIFEST, entries)
