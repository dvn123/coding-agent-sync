import stat
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from ..io import MANAGED_MANIFEST, is_incidental
from ..ir import applies_to, resolve
from ..output.patches import load_patch, merge_patches
from ..output.plan import Diagnostic, ManifestMode, NativePatch, OwnedFile, OwnedTree
from ..sources import (
    CommandPermission,
    Decision,
    McpServerSource,
    SourceBundle,
    StrictModel,
    ToolName,
)


class McpNative(StrictModel):
    # The command one target runs instead, such as an absolute path for an
    # agent that starts servers without the login shell's PATH.
    command: str | None = None


def mcp_servers(
    sources: SourceBundle, target: ToolName
) -> tuple[list[tuple[McpServerSource, dict[str, Any]]], list[Diagnostic]]:
    """Each server `target` receives, with its fields with empty ones left
    out: `command`, `args`, and `env` for a local server, `url` for a remote
    one."""
    resolved, diagnostics = resolve(target, sources.mcp_servers, McpNative, raw=False)
    return [
        (server.source, _mcp_fields(server.source, server.native.command))
        for server in resolved
    ], diagnostics


def mcp_tool_decisions(
    servers: list[tuple[McpServerSource, dict[str, Any]]],
) -> Iterator[tuple[str, str, Decision]]:
    """Each decided tool as its server id, its name, and its decision."""
    for server, _ in servers:
        for tool, decision in server.tools.items():
            yield server.id, tool, decision


def _mcp_fields(server: McpServerSource, command: str | None) -> dict[str, Any]:
    if server.url is not None:
        return {"url": server.url}
    return {
        "command": command or server.command,
        **({"args": list(server.args)} if server.args else {}),
        **({"env": dict(server.env)} if server.env else {}),
    }


def one_of(field: str, value: str | None, allowed: frozenset[str]) -> str | None:
    """Reject a value outside a target's closed vocabulary for `field`."""
    if value is not None and value not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}, got {value!r}")
    return value


def bundled_files(source_dir: Path) -> tuple[dict[Path, bytes], frozenset[Path]]:
    """Bundled skill files by relative path, plus the owner-executable subset."""
    paths = [
        path
        for path in source_dir.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not is_incidental(path.relative_to(source_dir))
    ]
    return (
        {path.relative_to(source_dir): path.read_bytes() for path in paths},
        frozenset(
            path.relative_to(source_dir)
            for path in paths
            if path.stat().st_mode & stat.S_IXUSR
        ),
    )


def with_skill_scripts(
    sources: SourceBundle, target: str, root: Path, home: Path
) -> SourceBundle:
    """Allow each skill's bundled executables where `target` deploys them.

    A skill's scripts are canonical source, so the agent that loads the skill
    may run them. Matchers compare the command as typed, so both the
    home-relative and the absolute spelling are allowed; a spelling that is not
    a portable token (a Windows drive, a space in the home path) is left out.
    """
    if not (permissions := sources.permissions):
        return sources
    seen = {rule.predicates for rule in permissions.commands.allow}
    added: list[CommandPermission] = []
    for skill in sources.skills:
        if not applies_to(skill, target):
            continue
        skills_root = (
            shared_skills_root(home)
            if target in SHARED_SKILL_READERS and shared_skill(skill)
            else root / "skills"
        )
        bases = (
            f"~/{skills_root.relative_to(home).as_posix()}",
            skills_root.as_posix(),
        )
        for relative in sorted(bundled_files(skill.source_dir)[1]):
            for base in bases:
                path = f"{base}/{skill.source_dir.name}/{relative.as_posix()}"
                try:
                    rule = CommandPermission(command=path)
                except ValidationError:
                    continue
                if rule.predicates not in seen:
                    seen.add(rule.predicates)
                    added.append(rule)
    if not added:
        return sources
    commands = permissions.commands.model_copy(
        update={"allow": (*permissions.commands.allow, *added)}
    )
    return sources.model_copy(
        update={"permissions": permissions.model_copy(update={"commands": commands})}
    )


# Codex, Cursor, OpenCode, and Pi all read `~/.agents/skills`; Claude does not.
SHARED_SKILL_READERS = ("codex", "cursor", "opencode", "pi")


def shared_skills_root(home: Path) -> Path:
    return home / ".agents" / "skills"


def shared_skill(skill: Any) -> bool:
    """A skill every shared reader receives lands once, in the shared root;
    one that `only` narrows keeps each reader's own skills directory."""
    return all(applies_to(skill, target) for target in SHARED_SKILL_READERS)


def skill_collisions(sources: SourceBundle, target: ToolName) -> set[str]:
    """Commands this target lowers to skills that a skill already names.

    The command and the skill would share one skill directory and one `/name`.
    """
    skill_names = {
        skill.source_dir.name for skill in sources.skills if applies_to(skill, target)
    }
    return {
        command.stem
        for command in sources.commands
        if applies_to(command, target) and command.stem in skill_names
    }


def collision_error(command: Any) -> Diagnostic:
    return Diagnostic(
        "error",
        f"command {command.stem} collides with the skill of that name; both "
        f"would answer /{command.stem}",
        command.path,
    )


def declared_trees(
    root: Path, roots: Iterable[tuple[str, ManifestMode]]
) -> list[OwnedTree]:
    """One manifest-owned tree per managed directory below `root`."""
    return [
        OwnedTree(root / name, (), root / name, manifest_mode=mode, declaration=True)
        for name, mode in roots
    ]


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


def skill_tree(skill: Any, root: Path, meta: Mapping[str, Any]) -> OwnedTree:
    """A skill's bundled files below `root`, with `meta` as its frontmatter."""
    files, executables = bundled_files(skill.source_dir)
    files[Path("SKILL.md")] = markdown(meta, skill.body)
    return OwnedTree(
        root / skill.source_dir.name,
        tuple(sorted(files.items(), key=lambda item: item[0].as_posix())),
        root,
        executables=executables,
    )


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
    declaration = OwnedTree(root, (), root, manifest_mode="file", declaration=True)
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
    if files:
        diagnostics.append(
            Diagnostic(
                "warning",
                f"using {len(files)} unvalidated raw target file(s)",
                source_root,
            )
        )
    return tuple(files), declaration, tuple(diagnostics)
