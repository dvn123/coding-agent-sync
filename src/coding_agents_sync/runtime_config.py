from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import tomlkit

from .artifacts import CodexAgentRegistration, NativeConfigArtifact
from .io import write_bytes
from .patches import (
    Operation,
    Patch,
    Pointer,
    apply_operations,
    decode_pointer,
    display_pointer,
    get_value,
    load_patch,
    merge_patches,
    validate_generated_conflicts,
)

MANIFEST_VERSION = 3
MANIFEST_NAME = ".coding-agents-native.json"
LEGACY_RETIREMENTS: dict[int, dict[str, tuple[Pointer, ...]]] = {
    1: {
        "codex": (("mcp_servers", "context7"),),
        "opencode": (("mcp", "context7"), ("mcp", "notion")),
    },
    2: {
        "claude-mcp": (("mcpServers", "github"),),
        "codex": (("mcp_servers", "github"),),
        "cursor-mcp": (("mcpServers", "github"),),
        "opencode": (("mcp", "github"),),
    },
}
NATIVE_TARGETS = {
    "claude",
    "claude-mcp",
    "cursor-cli",
    "cursor-desktop",
    "cursor-mcp",
    "opencode",
    "codex",
}


class NativeConfigError(ValueError):
    pass


@dataclass(frozen=True)
class GeneratedRuntimeConfig:
    opencode_instructions: tuple[str, ...] = ()
    codex_skill_paths: tuple[str, ...] = ()
    codex_agents: tuple[CodexAgentRegistration, ...] = ()
    native: tuple[NativeConfigArtifact, ...] = ()


@dataclass(frozen=True)
class _Surface:
    name: str
    patch_name: str
    path: Path
    format: Literal["json", "toml"]


@dataclass
class NativeCandidate:
    surface: _Surface
    content: bytes
    changed: bool


@dataclass
class NativePlan:
    candidates: tuple[NativeCandidate, ...]
    manifest_path: Path
    manifest_content: bytes
    manifest_changed: bool
    drift: tuple[Path, ...]


def _surfaces(home: Path) -> tuple[_Surface, ...]:
    return (
        _Surface("claude", "claude-settings", home / ".claude/settings.json", "json"),
        _Surface("claude-mcp", "claude-mcp", home / ".claude.json", "json"),
        _Surface(
            "cursor-cli", "cursor-cli-config", home / ".cursor/cli-config.json", "json"
        ),
        _Surface(
            "cursor-desktop",
            "cursor-permissions",
            home / ".cursor/permissions.json",
            "json",
        ),
        _Surface("cursor", "cursor-settings", home / ".cursor/settings.json", "json"),
        _Surface("cursor-mcp", "cursor-mcp", home / ".cursor/mcp.json", "json"),
        _Surface(
            "opencode", "opencode", home / ".config/opencode/opencode.json", "json"
        ),
        _Surface("codex", "codex", home / ".codex/config.toml", "toml"),
    )


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


def _generated(config: GeneratedRuntimeConfig) -> dict[str, dict[Pointer, Any]]:
    codex: dict[Pointer, Any] = {
        ("skills", "config"): [
            {"path": path, "enabled": True} for path in sorted(config.codex_skill_paths)
        ]
    }
    for agent in config.codex_agents:
        codex[("agents", agent.slug, "description")] = agent.description
        codex[("agents", agent.slug, "config_file")] = agent.config_file
    generated = {
        "opencode": {("instructions",): list(config.opencode_instructions)},
        "codex": codex,
    }
    for artifact in config.native:
        target = generated.setdefault(artifact.target, {})
        if artifact.target not in NATIVE_TARGETS:
            raise NativeConfigError(
                f"unknown generated native target: {artifact.target}"
            )
        for pointer, value in artifact.values:
            if pointer in target and target[pointer] != value:
                raise NativeConfigError(
                    f"duplicate generated native path: {artifact.target} "
                    f"{display_pointer(pointer)}"
                )
            target[pointer] = value
    return generated


def _load_manifest(path: Path) -> tuple[int, dict[tuple[str, Pointer], str]]:
    if not path.exists():
        return MANIFEST_VERSION, {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        version = parsed.get("version")
        if version not in {*LEGACY_RETIREMENTS, MANIFEST_VERSION} or not isinstance(
            parsed.get("entries"), list
        ):
            raise ValueError
        result: dict[tuple[str, Pointer], str] = {}
        for entry in parsed["entries"]:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"target", "pointer", "hash"}
                or entry["target"] not in NATIVE_TARGETS
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
        return version, result
    except OSError, ValueError, KeyError, TypeError:
        raise NativeConfigError(f"invalid native manifest: {path}") from None


def _string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


COMMAND_POINTERS: frozenset[Pointer] = frozenset(
    {("permission", "bash"), ("terminalAllowlist",)}
)
TIGHTEN_ONLY_POINTERS: frozenset[Pointer] = frozenset(
    {
        ("permission", "read"),
        ("permission", "edit"),
        # The portable `workspace` block owns the roots the agent may reach, so
        # a patch may not widen them; machine-local credential paths still need
        # somewhere to deny, and they cannot be authored portably.
        ("permission", "external_directory"),
    }
)


def _touches(left: Pointer, right: Pointer) -> bool:
    """Whether either pointer is the other or an ancestor of it."""
    shortest = min(len(left), len(right))
    return left[:shortest] == right[:shortest]


def _adds_only_denials(operation: Operation, pointer: Pointer) -> bool:
    """Whether an operation at or under `pointer` can only tighten it."""
    if operation.kind == "delete":
        return False
    value = _plain(operation.value)
    if operation.pointer == pointer:
        return isinstance(value, Mapping) and all(
            decision == "deny" for decision in value.values()
        )
    return value == "deny"


def _validate_patch_scope(surface: _Surface, patch: Patch) -> None:
    """Reserve generated command policy and permit only added secret denials."""
    for operation in patch.operations:
        for pointer in COMMAND_POINTERS:
            if _touches(operation.pointer, pointer):
                raise NativeConfigError(
                    f"{surface.name} patch cannot contribute command "
                    f"permissions at {display_pointer(operation.pointer)}; "
                    "author them in permissions/commands/"
                )
        for pointer in TIGHTEN_ONLY_POINTERS:
            if not _touches(operation.pointer, pointer):
                continue
            # An ancestor carries the map as nested data, which this cannot
            # inspect pointer-wise, so only at-or-under operations are read.
            under = operation.pointer[: len(pointer)] == pointer
            if not under or not _adds_only_denials(operation, pointer):
                raise NativeConfigError(
                    f"{surface.name} patch may only add deny entries at "
                    f"{display_pointer(pointer)}"
                )


def _validate_managed_value(surface: _Surface, pointer: Pointer, value: Any) -> bool:
    if pointer[-1] in {
        "allow",
        "deny",
        "ask",
        "additionalDirectories",
        "terminalAllowlist",
    }:
        return _string_list(value)
    if surface.name == "opencode":
        if pointer in {("instructions",), ("plugin",), ("enabled_providers",)}:
            return _string_list(value)
        if pointer[:1] == ("permission",):
            return isinstance(value, str) or (
                isinstance(value, Mapping)
                and all(
                    isinstance(pattern, str)
                    and isinstance(decision, str)
                    and decision in {"allow", "ask", "deny"}
                    for pattern, decision in value.items()
                )
            )
    if surface.name in {"cursor-cli", "cursor-desktop"} and pointer == (
        "approvalMode",
    ):
        return value in {"allowlist", "manual", "unrestricted"}
    if surface.name == "codex":
        if pointer in {
            ("notify",),
            ("sandbox_workspace_write", "writable_roots"),
            ("shell_environment_policy", "include"),
            ("shell_environment_policy", "exclude"),
        }:
            return _string_list(value)
        if len(pointer) == 3 and pointer[:1] == ("agents",):
            return isinstance(value, str)
    return True


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
                f"{surface.name} failed validation at "
                f"{display_pointer(operation.pointer)}"
            )
        if operation.kind in {"replace", "overlay"} and not isinstance(value, Mapping):
            raise NativeConfigError(
                f"{surface.name} failed validation at "
                f"{display_pointer(operation.pointer)}"
            )
        if operation.kind == "extend" and not isinstance(value, list):
            raise NativeConfigError(
                f"{surface.name} failed validation at "
                f"{display_pointer(operation.pointer)}"
            )
        if present and not _validate_managed_value(
            surface, operation.pointer, _plain(value)
        ):
            raise NativeConfigError(
                f"{surface.name} failed validation at "
                f"{display_pointer(operation.pointer)}"
            )
    for pointer in generated:
        present, value = get_value(document, pointer)
        if not present or not _validate_managed_value(surface, pointer, _plain(value)):
            raise NativeConfigError(
                f"{surface.name} failed validation at {display_pointer(pointer)}"
            )
    if surface.name == "opencode":
        present, value = get_value(document, ("instructions",))
        if present and not _string_list(value):
            raise NativeConfigError("opencode failed validation at /instructions")
    if surface.name == "codex":
        present, value = get_value(document, ("skills", "config"))
        if present and (
            not isinstance(value, list)
            or not all(
                isinstance(item, Mapping)
                and isinstance(item.get("path"), str)
                and isinstance(item.get("enabled"), bool)
                for item in value
            )
        ):
            raise NativeConfigError("codex failed validation at /skills/config")


def _serialize(surface: _Surface, document: MutableMapping[str, Any]) -> bytes:
    content = (
        tomlkit.dumps(document)
        if surface.format == "toml"
        else json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    ).encode()
    try:
        reparsed = (
            tomlkit.parse(content.decode())
            if surface.format == "toml"
            else json.loads(content)
        )
    except ValueError:
        raise NativeConfigError(
            f"candidate failed serialization: {surface.path}"
        ) from None
    if not isinstance(reparsed, Mapping):
        raise NativeConfigError(f"candidate is not a mapping: {surface.path}")
    return content


def prepare_native_plan(
    *, config_root: Path, home: Path, generated: GeneratedRuntimeConfig
) -> NativePlan:
    generated_by_target = _generated(generated)
    manifest_path = config_root / MANIFEST_NAME
    previous_version, previous = _load_manifest(manifest_path)
    candidates: list[NativeCandidate] = []
    hashes: dict[tuple[str, Pointer], str] = {}
    drift: set[Path] = set()
    for surface in _surfaces(home):
        committed = load_patch(config_root / "patches" / f"{surface.patch_name}.yaml")
        local_path = config_root / "patches.local" / f"{surface.patch_name}.yaml"
        if local_path.exists() and stat.S_IMODE(local_path.stat().st_mode) != 0o600:
            raise NativeConfigError(f"local patch must use mode 0600: {local_path}")
        patch = merge_patches(committed, load_patch(local_path))
        current_generated = generated_by_target.get(surface.name, {})
        _validate_patch_scope(surface, patch)
        validate_generated_conflicts(patch, current_generated)
        relevant_previous = {
            pointer: hash_
            for (target, pointer), hash_ in previous.items()
            if target == surface.name
        }
        retirements = tuple(
            pointer
            for version in range(previous_version, MANIFEST_VERSION)
            for pointer in LEGACY_RETIREMENTS.get(version, {}).get(surface.name, ())
        )
        if (
            not patch.operations
            and not current_generated
            and not relevant_previous
            and not retirements
        ):
            continue
        document = _parse(surface)
        original = _plain(document)
        apply_operations(
            document,
            Patch(tuple(Operation("delete", pointer) for pointer in retirements)),
        )
        for pointer, expected in relevant_previous.items():
            present, value = get_value(document, pointer)
            desired = current_generated.get(pointer)
            allowed = {expected}
            if desired is not None:
                allowed.add(semantic_hash(desired))
            if present and semantic_hash(value) not in allowed:
                raise NativeConfigError(
                    f"modified generated native path: {surface.name} "
                    f"{display_pointer(pointer)}"
                )
            if pointer not in current_generated and present:
                apply_operations(document, Patch((Operation("delete", pointer),)))
        apply_operations(
            document,
            Patch(
                tuple(
                    Operation("set", pointer, value)
                    for pointer, value in current_generated.items()
                )
            ),
        )
        apply_operations(document, patch)
        _validate_candidate(surface, document, patch, current_generated)
        content = _serialize(surface, document)
        changed = _plain(document) != original
        if changed:
            drift.add(surface.path)
        for pointer in current_generated:
            present, value = get_value(document, pointer)
            if not present:
                raise NativeConfigError(
                    f"missing generated native path: {surface.name} "
                    f"{display_pointer(pointer)}"
                )
            hashes[(surface.name, pointer)] = semantic_hash(value)
        candidates.append(NativeCandidate(surface, content, changed))
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


def apply_native_plan(plan: NativePlan) -> None:
    for candidate in plan.candidates:
        if candidate.changed:
            write_bytes(candidate.surface.path, candidate.content)
    if plan.manifest_changed:
        write_bytes(plan.manifest_path, plan.manifest_content)
