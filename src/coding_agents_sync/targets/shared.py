from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ..models import SyncContext
from ..output.plan import Diagnostic, OwnedTree, Plan
from ..sources import SkillSource, SourceBundle
from .codex import codex_skill
from .cursor import cursor_skill
from .opencode import opencode_skill
from .support import declared_trees, markdown, shared_skill, shared_skills_root

type SkillLowering = Callable[
    [SkillSource, Path], tuple[tuple[OwnedTree, ...], list[Diagnostic]]
]

READERS: tuple[tuple[str, SkillLowering], ...] = (
    ("Codex", codex_skill),
    ("Cursor", cursor_skill),
    ("OpenCode", opencode_skill),
)
SKILL_FILE = Path("SKILL.md")


def _split(content: bytes) -> tuple[dict[str, Any], str]:
    text = content.decode()
    end = text.index("\n---\n", 4)
    return yaml.safe_load(text[4:end]), text[end + 5 :]


def _merge(
    skill: SkillSource, root: Path, trees: list[tuple[str, OwnedTree]]
) -> tuple[tuple[OwnedTree, ...], list[Diagnostic]]:
    """One tree carrying every reader's frontmatter and files.

    Each reader ignores the frontmatter keys and files it does not know, so
    the union serves all three; a key they need to differ cannot be shared,
    and the skill must be narrowed with `only`. Other files are the bundled
    ones, which only Codex rewrites (its `agents/openai.yaml`); Codex comes
    first, so its copy wins.
    """
    meta: dict[str, Any] = {}
    owners: dict[str, str] = {}
    files: dict[Path, bytes] = {}
    body = ""
    for reader, tree in trees:
        for path, content in tree.files:
            if path == SKILL_FILE:
                fields, body = _split(content)
                for key, value in fields.items():
                    if key in meta and meta[key] != value:
                        return (), [
                            Diagnostic(
                                "error",
                                f"{owners[key]} and {reader} need different "
                                f"`{key}` in the shared skill; narrow it with "
                                "`only` to give each its own copy",
                                skill.path,
                            )
                        ]
                    meta.setdefault(key, value)
                    owners.setdefault(key, reader)
            else:
                files.setdefault(path, content)
    files[SKILL_FILE] = markdown(meta, body)
    executables = frozenset().union(*(tree.executables for _, tree in trees))
    return (
        OwnedTree(
            root / skill.source_dir.name,
            tuple(sorted(files.items())),
            root,
            executables=executables,
        ),
    ), []


def compile_shared_skills(ctx: SyncContext, sources: SourceBundle) -> Plan:
    """Skills Codex, Cursor, and OpenCode all receive, once in `~/.agents/skills`."""
    root = shared_skills_root(ctx.home)
    trees = declared_trees(root.parent, (("skills", "dir"),))
    diagnostics: list[Diagnostic] = []
    for skill in sources.skills:
        if not shared_skill(skill):
            continue
        lowered: list[tuple[str, OwnedTree]] = []
        for reader, lower in READERS:
            tree, issues = lower(skill, root)
            diagnostics.extend(issues)
            lowered.extend((reader, item) for item in tree)
        if len(lowered) == len(READERS):
            merged, issues = _merge(skill, root, lowered)
            diagnostics.extend(issues)
            trees.extend(merged)
    return Plan((), tuple(trees), (), (), tuple(diagnostics))
