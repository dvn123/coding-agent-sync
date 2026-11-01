from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import tomlkit

from .io import write_bytes
from .patches import (
    Operation,
    Patch,
    Pointer,
    apply_operations,
    decode_pointer,
    display_pointer,
    get_value,
)
from .plan import NativePatch, NativeValue

MANIFEST_NAME = ".coding-agents-native.json"


class NativeConfigError(ValueError):
    pass


@dataclass(frozen=True)
class _Surface:
    target: str
    path: Path
    format: Literal["json", "toml"]


@dataclass
class NativeCandidate:
    path: Path
    content: bytes
    changed: bool


@dataclass
class NativePlan:
    candidates: tuple[NativeCandidate, ...]
    manifest_path: Path
    manifest_content: bytes
    manifest_changed: bool
    drift: tuple[Path, ...]


def _plain(value: Any) -> Any:
    if hasattr(value, "unwrap"):
        return value.unwrap()
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_plain(child) for child in value]
    return value


def semantic_hash(value: Any) -> str:
    payload = json.dumps(
        _plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _format(path: Path) -> Literal["json", "toml"]:
    if path.suffix == ".json":
        return "json"
    if path.suffix == ".toml":
        return "toml"
    raise NativeConfigError(f"unsupported native config format: {path}")


def _parse(surface: _Surface) -> MutableMapping[str, Any]:
    if not surface.path.exists():
        return tomlkit.document() if surface.format == "toml" else {}
    try:
        parsed = (
            tomlkit.parse(surface.path.read_text(encoding="utf-8"))
            if surface.format == "toml"
            else json.loads(surface.path.read_text(encoding="utf-8"))
        )
    except OSError, ValueError:
        raise NativeConfigError(f"invalid native config: {surface.path}") from None
    if not isinstance(parsed, MutableMapping):
        raise NativeConfigError(f"native config is not a mapping: {surface.path}")
    return parsed


@dataclass(frozen=True)
class OwnedItems:
    """The hashes of the entries the compiler owns in a generated list."""

    hashes: frozenset[str]


# A whole owned value is recorded by its hash; a generated list by its items.
type Ownership = str | OwnedItems

HASH = re.compile(r"sha256:[0-9a-f]{64}")


def _load_manifest(
    path: Path, targets: set[str]
) -> dict[tuple[str, Pointer], Ownership]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed.get("entries"), list):
            raise ValueError
        result: dict[tuple[str, Pointer], Ownership] = {}
        for entry in parsed["entries"]:
            fields = set(entry) - {"target", "pointer"}
            if (
                entry["target"] not in targets
                or not isinstance(entry["pointer"], str)
                or fields not in ({"hash"}, {"items"})
            ):
                raise ValueError
            ownership: Ownership
            if "hash" in entry:
                ownership = entry["hash"]
                if not isinstance(ownership, str) or not HASH.fullmatch(ownership):
                    raise ValueError
            else:
                items = entry["items"]
                if not isinstance(items, list) or not all(
                    isinstance(item, str) and HASH.fullmatch(item) for item in items
                ):
                    raise ValueError
                ownership = OwnedItems(frozenset(items))
            key = (entry["target"], decode_pointer(entry["pointer"]))
            if key in result:
                raise ValueError
            result[key] = ownership
        return result
    except OSError, ValueError, KeyError, TypeError:
        raise NativeConfigError(f"invalid native manifest: {path}") from None


def _foreign_items(current: list[Any], ownership: Ownership | None) -> list[Any]:
    """Entries of an existing list that the compiler does not own.

    An unclaimed list, or a wholly owned one that was edited, keeps every entry:
    the generated ones are deduplicated when the list is rebuilt.
    """
    if isinstance(ownership, OwnedItems):
        return [item for item in current if semantic_hash(item) not in ownership.hashes]
    if ownership is not None and semantic_hash(current) == ownership:
        return []
    return list(current)


def _validate_candidate(
    surface: _Surface,
    document: Mapping[str, Any],
    patch: Patch,
    generated: Mapping[Pointer, Any],
) -> None:
    for operation in patch.operations:
        present, value = get_value(document, operation.pointer)
        if operation.kind != "delete" and not present:
            raise NativeConfigError(
                f"native patch failed at {surface.path} "
                f"{display_pointer(operation.pointer)}"
            )
        if operation.kind in {"replace", "overlay"} and not isinstance(value, Mapping):
            raise NativeConfigError(
                f"native patch failed at {surface.path} "
                f"{display_pointer(operation.pointer)}"
            )
        if operation.kind == "extend" and not isinstance(value, list):
            raise NativeConfigError(
                f"native patch failed at {surface.path} "
                f"{display_pointer(operation.pointer)}"
            )
    for pointer in generated:
        present, _ = get_value(document, pointer)
        if not present:
            raise NativeConfigError(
                f"generated native value missing at {surface.path} "
                f"{display_pointer(pointer)}"
            )


def _serialize(surface: _Surface, document: MutableMapping[str, Any]) -> bytes:
    content = (
        tomlkit.dumps(document)
        if surface.format == "toml"
        else json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    ).encode()
    try:
        parsed = (
            tomlkit.parse(content.decode())
            if surface.format == "toml"
            else json.loads(content)
        )
    except ValueError:
        raise NativeConfigError(
            f"candidate failed serialization: {surface.path}"
        ) from None
    if not isinstance(parsed, Mapping):
        raise NativeConfigError(f"candidate is not a mapping: {surface.path}")
    return content


def _with_foreign(value: Any, foreign: list[Any] | None) -> Any:
    """Foreign entries first, so generated rules win where the last match does."""
    return [*foreign, *value] if foreign else value


def _delete_with_empty_parents(
    document: MutableMapping[str, Any], pointer: Pointer
) -> None:
    """Delete a stale owned pointer and the parent tables it leaves empty."""
    apply_operations(document, Patch((Operation("delete", pointer),)))
    for depth in range(len(pointer) - 1, 0, -1):
        _, parent = get_value(document, pointer[:depth])
        if parent != {}:
            break
        apply_operations(document, Patch((Operation("delete", pointer[:depth]),)))


def _surfaces(
    native_values: tuple[NativeValue, ...], native_patches: tuple[NativePatch, ...]
) -> tuple[_Surface, ...]:
    paths: dict[str, Path] = {}
    targets: dict[Path, str] = {}
    for target, path in (
        *((value.target, value.path) for value in native_values),
        *((patch.target, patch.path) for patch in native_patches),
    ):
        previous = paths.setdefault(target, path)
        if previous != path:
            raise NativeConfigError(f"conflicting native surface path: {target}")
        previous_target = targets.setdefault(path, target)
        if previous_target != target:
            raise NativeConfigError(f"conflicting native surface target: {path}")
    return tuple(
        _Surface(target, path, _format(path))
        for target, path in sorted(
            paths.items(), key=lambda item: (item[0], str(item[1]))
        )
    )


def prepare_plan_native(
    *,
    config_root: Path,
    native_values: tuple[NativeValue, ...],
    native_patches: tuple[NativePatch, ...],
) -> NativePlan:
    """Reconcile a validated Plan without discovering target-specific surfaces."""

    surfaces = _surfaces(native_values, native_patches)
    values: dict[str, dict[Pointer, Any]] = {surface.target: {} for surface in surfaces}
    patches: dict[str, list[Operation]] = {surface.target: [] for surface in surfaces}
    for value in native_values:
        generated = values[value.target]
        if value.pointer in generated and generated[value.pointer] != value.value:
            raise NativeConfigError(
                f"duplicate generated native path: {value.target} "
                f"{display_pointer(value.pointer)}"
            )
        generated[value.pointer] = value.value
    for patch in native_patches:
        patches[patch.target].extend(patch.operations)

    manifest_path = config_root / MANIFEST_NAME
    previous = _load_manifest(manifest_path, set(values))
    candidates: list[NativeCandidate] = []
    owned: dict[tuple[str, Pointer], Ownership] = {}
    drift: set[Path] = set()
    for surface in surfaces:
        generated = values[surface.target]
        patch = Patch(tuple(patches[surface.target]))
        previous_for_target = {
            pointer: ownership
            for (target, pointer), ownership in previous.items()
            if target == surface.target
        }
        if not patch.operations and not generated and not previous_for_target:
            continue
        document = _parse(surface)
        original = _plain(document)
        foreign: dict[Pointer, list[Any]] = {}
        for pointer in previous_for_target.keys() | generated.keys():
            present, current = get_value(document, pointer)
            if not present:
                continue
            ownership = previous_for_target.get(pointer)
            current = _plain(current)
            if isinstance(current, list) and (
                isinstance(ownership, OwnedItems)
                or isinstance(generated.get(pointer), list)
            ):
                foreign[pointer] = _foreign_items(current, ownership)
            elif ownership is None:
                if current != _plain(generated[pointer]):
                    raise NativeConfigError(
                        f"existing native value differs: {surface.target} "
                        f"{display_pointer(pointer)}"
                    )
            elif semantic_hash(current) not in {
                ownership,
                *((semantic_hash(generated[pointer]),) if pointer in generated else ()),
            }:
                raise NativeConfigError(
                    f"modified generated native path: {surface.target} "
                    f"{display_pointer(pointer)}"
                )
        for pointer, value in generated.items():
            if pointer in foreign:
                # An entry the compiler also generates is owned from now on.
                generated_items = _plain(value)
                foreign[pointer] = [
                    item for item in foreign[pointer] if item not in generated_items
                ]
        for pointer in previous_for_target.keys() - generated.keys():
            if foreign.get(pointer):
                apply_operations(
                    document, Patch((Operation("set", pointer, foreign[pointer]),))
                )
            elif get_value(document, pointer)[0]:
                _delete_with_empty_parents(document, pointer)
        apply_operations(
            document,
            Patch(
                tuple(
                    Operation(
                        "set", pointer, _with_foreign(value, foreign.get(pointer))
                    )
                    for pointer, value in generated.items()
                )
            ),
        )
        apply_operations(document, patch)
        _validate_candidate(surface, document, patch, generated)
        content = _serialize(surface, document)
        changed = _plain(document) != original
        if changed:
            drift.add(surface.path)
        for pointer in generated:
            present, current = get_value(document, pointer)
            if not present:
                raise NativeConfigError(
                    f"generated native value missing at {surface.path} "
                    f"{display_pointer(pointer)}"
                )
            current = _plain(current)
            owned[(surface.target, pointer)] = (
                OwnedItems(
                    frozenset(semantic_hash(item) for item in current)
                    - frozenset(
                        semantic_hash(item) for item in foreign.get(pointer, [])
                    )
                )
                if isinstance(generated[pointer], list)
                else semantic_hash(current)
            )
        candidates.append(NativeCandidate(surface.path, content, changed))

    manifest = {
        "entries": [
            {
                "target": target,
                "pointer": display_pointer(pointer),
                **(
                    {"items": sorted(ownership.hashes)}
                    if isinstance(ownership, OwnedItems)
                    else {"hash": ownership}
                ),
            }
            for (target, pointer), ownership in sorted(
                owned.items(), key=lambda item: item[0]
            )
        ],
    }
    manifest_content = (json.dumps(manifest, indent=2) + "\n").encode()
    manifest_changed = (
        not manifest_path.exists() or manifest_path.read_bytes() != manifest_content
    )
    if manifest_changed:
        drift.add(manifest_path)
    return NativePlan(
        tuple(candidates),
        manifest_path,
        manifest_content,
        manifest_changed,
        tuple(sorted(drift)),
    )


def apply_native_candidates(plan: NativePlan) -> None:
    for candidate in plan.candidates:
        if candidate.changed:
            write_bytes(candidate.path, candidate.content)


def publish_native_manifest(plan: NativePlan) -> None:
    if plan.manifest_changed:
        write_bytes(plan.manifest_path, plan.manifest_content)
