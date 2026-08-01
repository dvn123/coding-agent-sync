from __future__ import annotations

import hashlib
import json
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

MANIFEST_VERSION = 3
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


def _load_manifest(path: Path, targets: set[str]) -> dict[tuple[str, Pointer], str]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if parsed.get("version") != MANIFEST_VERSION or not isinstance(
            parsed.get("entries"), list
        ):
            raise ValueError
        result: dict[tuple[str, Pointer], str] = {}
        for entry in parsed["entries"]:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"target", "pointer", "hash"}
                or entry["target"] not in targets
                or not isinstance(entry["pointer"], str)
                or not isinstance(entry["hash"], str)
                or not entry["hash"].startswith("sha256:")
                or len(entry["hash"]) != 71
                or any(
                    character not in "0123456789abcdef"
                    for character in entry["hash"][7:]
                )
            ):
                raise ValueError
            key = (entry["target"], decode_pointer(entry["pointer"]))
            if key in result:
                raise ValueError
            result[key] = entry["hash"]
        return result
    except OSError, ValueError, KeyError, TypeError:
        raise NativeConfigError(f"invalid native manifest: {path}") from None


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
    hashes: dict[tuple[str, Pointer], str] = {}
    drift: set[Path] = set()
    for surface in surfaces:
        generated = values[surface.target]
        patch = Patch(tuple(patches[surface.target]))
        previous_for_target = {
            pointer: hash_
            for (target, pointer), hash_ in previous.items()
            if target == surface.target
        }
        if not patch.operations and not generated and not previous_for_target:
            continue
        document = _parse(surface)
        original = _plain(document)
        for pointer, expected in previous_for_target.items():
            present, current = get_value(document, pointer)
            desired = generated.get(pointer)
            allowed = {expected}
            if desired is not None:
                allowed.add(semantic_hash(desired))
            if present and semantic_hash(current) not in allowed:
                raise NativeConfigError(
                    f"modified generated native path: {surface.target} "
                    f"{display_pointer(pointer)}"
                )
            if pointer not in generated and present:
                apply_operations(document, Patch((Operation("delete", pointer),)))
        apply_operations(
            document,
            Patch(
                tuple(
                    Operation("set", pointer, value)
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
            hashes[(surface.target, pointer)] = semantic_hash(current)
        candidates.append(NativeCandidate(surface.path, content, changed))

    manifest = {
        "version": MANIFEST_VERSION,
        "entries": [
            {"target": target, "pointer": display_pointer(pointer), "hash": hash_}
            for (target, pointer), hash_ in sorted(hashes.items())
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


def apply_native_plan(plan: NativePlan) -> None:
    apply_native_candidates(plan)
    publish_native_manifest(plan)
