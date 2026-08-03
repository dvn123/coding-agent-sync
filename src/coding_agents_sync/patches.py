from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

type Pointer = tuple[str, ...]
type OperationKind = Literal["set", "replace", "delete", "extend", "overlay"]


class PatchError(ValueError):
    pass


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(
    loader: yaml.Loader, node: yaml.Node, deep: bool = False
) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise PatchError("patch contains a duplicate mapping key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def decode_pointer(value: str) -> Pointer:
    if not value or not value.startswith("/"):
        raise PatchError("patch operation has an invalid pointer")
    parts: list[str] = []
    for raw in value[1:].split("/"):
        decoded = ""
        index = 0
        while index < len(raw):
            if raw[index] != "~":
                decoded += raw[index]
                index += 1
                continue
            if index + 1 == len(raw) or raw[index + 1] not in "01":
                raise PatchError("patch operation has an invalid pointer escape")
            decoded += "~" if raw[index + 1] == "0" else "/"
            index += 2
        parts.append(decoded)
    return tuple(parts)


def display_pointer(pointer: Pointer) -> str:
    return "/" + "/".join(
        part.replace("~", "~0").replace("/", "~1") for part in pointer
    )


@dataclass(frozen=True)
class Operation:
    kind: OperationKind
    pointer: Pointer
    value: Any = None


@dataclass(frozen=True)
class Patch:
    operations: tuple[Operation, ...] = ()


def _validate_conflicts(operations: list[Operation]) -> None:
    for index, operation in enumerate(operations):
        for other in operations[:index]:
            if operation.pointer == other.pointer:
                raise PatchError(
                    f"duplicate operation at {display_pointer(operation.pointer)}"
                )
            shortest = min(len(operation.pointer), len(other.pointer))
            if operation.pointer[:shortest] == other.pointer[:shortest]:
                raise PatchError(
                    f"overlapping operation at {display_pointer(operation.pointer)}"
                )


def load_patch(path: Path) -> Patch:
    if not path.exists():
        return Patch()
    try:
        parsed = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueLoader)
    except yaml.YAMLError, PatchError:
        raise PatchError(f"invalid patch: {path}") from None
    if not isinstance(parsed, dict) or parsed.get("schema") != "coding-agents/patch/v1":
        raise PatchError(f"invalid patch schema: {path}")
    if set(parsed) - {"schema", "set", "replace", "delete", "extend", "overlay"}:
        raise PatchError(f"invalid patch section: {path}")
    operations: list[Operation] = []
    for kind in ("set", "replace", "extend", "overlay"):
        values = parsed.get(kind, {})
        if not isinstance(values, dict):
            raise PatchError(f"invalid {kind} section: {path}")
        for raw_pointer, value in values.items():
            if not isinstance(raw_pointer, str):
                raise PatchError(f"invalid {kind} pointer: {path}")
            if kind == "set" and isinstance(value, Mapping):
                raise PatchError(f"mapping-valued set at {raw_pointer}")
            if kind == "replace" and not isinstance(value, Mapping):
                raise PatchError(f"non-mapping replace at {raw_pointer}")
            if kind == "extend" and not isinstance(value, list):
                raise PatchError(f"non-list extend at {raw_pointer}")
            if kind == "overlay" and not isinstance(value, Mapping):
                raise PatchError(f"non-mapping overlay at {raw_pointer}")
            operations.append(Operation(kind, decode_pointer(raw_pointer), value))
    deletes = parsed.get("delete", [])
    if not isinstance(deletes, list) or not all(
        isinstance(item, str) for item in deletes
    ):
        raise PatchError(f"invalid delete section: {path}")
    operations.extend(Operation("delete", decode_pointer(item)) for item in deletes)
    _validate_conflicts(operations)
    return Patch(tuple(operations))


def merge_patches(committed: Patch, local: Patch) -> Patch:
    local_pointers = {operation.pointer for operation in local.operations}
    operations = [
        operation
        for operation in committed.operations
        if operation.pointer not in local_pointers
    ] + list(local.operations)
    _validate_conflicts(operations)
    return Patch(tuple(operations))


def contributes_to_generated(
    operation: Operation, pointer: Pointer, value: Any
) -> bool:
    """Report whether an operation may co-own a generated pointer.

    A patch contributes to generated output only by agreeing with it exactly or
    by adding to a container the compiler owns. Any other overlap, including an
    ancestor or descendant pointer, is ambiguous ownership.
    """

    if operation.pointer != pointer:
        return False
    return (
        (operation.kind == "set" and operation.value == value)
        or (operation.kind == "extend" and isinstance(value, list))
        or (operation.kind == "overlay" and isinstance(value, Mapping))
    )


def validate_generated_conflicts(
    patch: Patch, generated: Mapping[Pointer, Any]
) -> None:
    for operation in patch.operations:
        for pointer, value in generated.items():
            if contributes_to_generated(operation, pointer, value):
                continue
            if (
                pointer[: len(operation.pointer)] == operation.pointer
                or operation.pointer[: len(pointer)] == pointer
            ):
                raise PatchError(f"reserved generated path {display_pointer(pointer)}")


def get_value(document: Mapping[str, Any], pointer: Pointer) -> tuple[bool, Any]:
    current: Any = document
    for key in pointer:
        if not isinstance(current, Mapping) or key not in current:
            return False, None
        current = current[key]
    return True, current


def apply_operations(document: MutableMapping[str, Any], patch: Patch) -> None:
    for operation in patch.operations:
        current: MutableMapping[str, Any] = document
        missing_parent = False
        for key in operation.pointer[:-1]:
            if key not in current:
                if operation.kind == "delete":
                    missing_parent = True
                    break
                current[key] = {}
            child = current[key]
            if not isinstance(child, MutableMapping):
                raise PatchError(
                    f"{operation.kind} cannot traverse "
                    f"{display_pointer(operation.pointer)}"
                )
            current = child
        if missing_parent:
            continue
        key = operation.pointer[-1]
        if operation.kind == "delete":
            current.pop(key, None)
        elif operation.kind == "extend":
            existing = current.get(key, [])
            if not isinstance(existing, list):
                raise PatchError(
                    f"extend requires a list at {display_pointer(operation.pointer)}"
                )
            combined = deepcopy(existing)
            combined.extend(
                item for item in deepcopy(operation.value) if item not in combined
            )
            current[key] = combined
        elif operation.kind == "overlay":
            existing = current.get(key, {})
            if not isinstance(existing, MutableMapping):
                raise PatchError(
                    "overlay requires a mapping at "
                    f"{display_pointer(operation.pointer)}"
                )
            current[key] = {**existing, **deepcopy(operation.value)}
        else:
            current[key] = deepcopy(operation.value)
