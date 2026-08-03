from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .patches import Operation, Pointer

type ManifestMode = Literal["dir", "file"]
type DiagnosticLevel = Literal["error", "warning"]


@dataclass(frozen=True)
class OwnedFile:
    path: Path
    content: bytes | None
    manifest_root: Path | None = None
    retire_if: bytes | None = None


@dataclass(frozen=True)
class OwnedTree:
    root: Path
    files: tuple[tuple[Path, bytes], ...] = ()
    manifest_root: Path | None = None
    manifest_mode: ManifestMode = "dir"
    declaration: bool = False
    retired: bool = False
    executables: frozenset[Path] = frozenset()


@dataclass(frozen=True)
class NativeValue:
    target: str
    path: Path
    pointer: Pointer
    value: Any


@dataclass(frozen=True)
class NativePatch:
    target: str
    path: Path
    operations: tuple[Operation, ...]
    source: Path


@dataclass(frozen=True)
class Diagnostic:
    level: DiagnosticLevel
    message: str
    source: Path | None = None


@dataclass(frozen=True)
class Plan:
    files: tuple[OwnedFile, ...] = ()
    trees: tuple[OwnedTree, ...] = ()
    native_values: tuple[NativeValue, ...] = ()
    native_patches: tuple[NativePatch, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()


def merge_plans(*plans: Plan) -> Plan:
    return Plan(
        tuple(file for plan in plans for file in plan.files),
        tuple(tree for plan in plans for tree in plan.trees),
        tuple(value for plan in plans for value in plan.native_values),
        tuple(patch for plan in plans for patch in plan.native_patches),
        tuple(diagnostic for plan in plans for diagnostic in plan.diagnostics),
    )
