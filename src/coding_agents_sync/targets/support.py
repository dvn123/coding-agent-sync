from __future__ import annotations

import stat
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from ..io import MANAGED_MANIFEST
from ..patches import load_patch, merge_patches
from ..plan import Diagnostic, NativePatch, OwnedFile, OwnedTree
from ..sources import TargetBlock


def bundled_files(source_dir: Path) -> tuple[dict[Path, bytes], frozenset[Path]]:
    """Bundled skill files by relative path, plus the owner-executable subset."""
    paths = [
        path
        for path in source_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
    ]
    return (
        {path.relative_to(source_dir): path.read_bytes() for path in paths},
        frozenset(
            path.relative_to(source_dir)
            for path in paths
            if path.stat().st_mode & stat.S_IXUSR
        ),
    )


def applies_to(source: Any, target: str) -> bool:
    """Whether this portable artifact should emit (and owe omits) on `target`."""
    only = getattr(source, "only", ())
    return not only or target in only


def block(source: Any, target: str) -> TargetBlock:
    return source.targets.root.get(target, TargetBlock())


def omissions(
    source: Path, target: str, value: TargetBlock, required: set[str]
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    for path in sorted(required - set(value.omit)):
        diagnostics.append(
            Diagnostic(
                "error",
                f"{path} is unsupported on {target}; add targets.{target}.omit.{path}",
                source,
            )
        )
    for path, reason in value.omit.items():
        if path not in required:
            diagnostics.append(
                Diagnostic(
                    "error",
                    f"{path} is not an omitted {target} capability",
                    source,
                )
            )
        else:
            diagnostics.append(
                Diagnostic("warning", f"omitted {path}: {reason}", source)
            )
    return tuple(diagnostics)


def unhandled_target_block(
    source: Path, target: str, value: TargetBlock
) -> tuple[Diagnostic, ...]:
    diagnostics: list[Diagnostic] = []
    if value.native:
        diagnostics.append(
            Diagnostic("error", f"{target} native fields are not valid here", source)
        )
    if value.raw:
        diagnostics.append(
            Diagnostic("error", f"{target} raw fields are not valid here", source)
        )
    return tuple(diagnostics)


def strict_native[T: BaseModel](
    source: Path, target: str, value: TargetBlock, model: type[T]
) -> tuple[T | None, tuple[Diagnostic, ...]]:
    try:
        return model.model_validate(value.native), ()
    except ValidationError as exc:
        return None, (
            Diagnostic("error", f"invalid {target} native fields: {exc}", source),
        )


def frontmatter(
    source: Path,
    canonical: Mapping[str, Any],
    native: BaseModel,
    raw: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[Diagnostic, ...]]:
    typed = native.model_dump(exclude_none=True, by_alias=True)
    rendered = {**canonical, **typed}
    overlap = sorted(set(raw) & set(rendered))
    if overlap:
        return rendered, (
            Diagnostic(
                "error",
                "raw target fields shadow canonical or typed fields: "
                f"{', '.join(overlap)}",
                source,
            ),
        )
    diagnostics = (
        (Diagnostic("warning", "using unvalidated raw target fields", source),)
        if raw
        else ()
    )
    return {**rendered, **raw}, diagnostics


def markdown(meta: Mapping[str, Any], body: str) -> bytes:
    header = yaml.safe_dump(dict(meta), sort_keys=False, allow_unicode=True).rstrip()
    return f"---\n{header}\n---\n\n{body.rstrip()}\n".encode()


def native_patch(
    *,
    config_root: Path,
    target: str,
    path: Path,
    patch_name: str,
) -> tuple[NativePatch, tuple[Diagnostic, ...]]:
    committed = config_root / "patches" / f"{patch_name}.yaml"
    local = config_root / "patches.local" / f"{patch_name}.yaml"
    try:
        if local.exists() and stat.S_IMODE(local.stat().st_mode) != 0o600:
            raise ValueError(f"local patch must use mode 0600: {local}")
        patch = merge_patches(load_patch(committed), load_patch(local))
    except ValueError as exc:
        return NativePatch(target, path, (), committed), (
            Diagnostic("error", str(exc), local if local.exists() else committed),
        )
    return NativePatch(target, path, patch.operations, committed), ()


def raw_files(
    *,
    config_root: Path,
    target: str,
    root: Path,
    excluded: Iterable[Path],
) -> tuple[tuple[OwnedFile, ...], OwnedTree, tuple[Diagnostic, ...]]:
    source_root = config_root / "target-config" / target / "raw"
    declaration = OwnedTree(
        root,
        manifest_root=root,
        manifest_mode="file",
        declaration=True,
    )
    if not source_root.exists():
        return (), declaration, ()
    if source_root.is_symlink() or not source_root.is_dir():
        return (
            (),
            declaration,
            (
                Diagnostic(
                    "error",
                    "raw target root must be a directory, not a symlink",
                    source_root,
                ),
            ),
        )
    forbidden = set(excluded)
    files: list[OwnedFile] = []
    diagnostics: list[Diagnostic] = []
    for source in sorted(source_root.rglob("*")):
        relative = source.relative_to(source_root)
        if source.is_symlink():
            diagnostics.append(
                Diagnostic("error", "raw target input may not be a symlink", source)
            )
        elif source.is_file():
            if relative.is_absolute() or ".." in relative.parts:
                diagnostics.append(
                    Diagnostic("error", "unsafe raw target path", source)
                )
            elif relative.name == MANAGED_MANIFEST:
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "raw target input may not be a managed manifest",
                        source,
                    )
                )
            elif relative in forbidden:
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "raw target input conflicts with native pointer surface",
                        source,
                    )
                )
            else:
                files.append(OwnedFile(root / relative, source.read_bytes(), root))
                diagnostics.append(
                    Diagnostic("warning", "using unvalidated raw target file", source)
                )
    return tuple(files), declaration, tuple(diagnostics)
