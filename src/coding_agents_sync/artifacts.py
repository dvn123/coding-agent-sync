from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .models import OpenCodeInstruction


@dataclass(frozen=True)
class TextArtifact:
    path: Path
    content: str


@dataclass(frozen=True)
class FileTreeArtifact:
    target: Path
    files: dict[Path, str] = field(default_factory=dict)


@dataclass(frozen=True)
class OptionalTextArtifact:
    path: Path
    content: str | None


@dataclass(frozen=True)
class TreeArtifact:
    source: Path
    target: Path
    text_overrides: dict[Path, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ManagedRootArtifact:
    root: Path
    current: set[str]
    mode: Literal["dir", "file"]


@dataclass(frozen=True)
class RetiredRootArtifact:
    root: Path
    mode: Literal["dir", "file"]


@dataclass(frozen=True)
class RetiredTextArtifact:
    path: Path
    generated_content: str


@dataclass(frozen=True)
class OpenCodeConfigArtifact:
    instructions: tuple[OpenCodeInstruction, ...]


@dataclass(frozen=True)
class NativeConfigArtifact:
    target: str
    values: tuple[tuple[tuple[str, ...], Any], ...]


@dataclass(frozen=True)
class CodexAgentRegistration:
    slug: str
    description: str
    config_file: str


@dataclass(frozen=True)
class CodexConfigArtifact:
    skill_paths: tuple[str, ...]
    agents: tuple[CodexAgentRegistration, ...]


type Artifact = (
    TextArtifact
    | FileTreeArtifact
    | OptionalTextArtifact
    | TreeArtifact
    | ManagedRootArtifact
    | RetiredRootArtifact
    | RetiredTextArtifact
    | OpenCodeConfigArtifact
    | NativeConfigArtifact
    | CodexConfigArtifact
)
