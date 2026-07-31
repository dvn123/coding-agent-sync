from __future__ import annotations

from pathlib import Path

from .model import Case, Target

TARGETS = {
    name: Target(name, command, (command, action))
    for name, command, action in (
        ("claude", "claude", "update"),
        ("codex", "codex", "update"),
        ("cursor", "cursor-agent", "update"),
        ("opencode", "opencode", "upgrade"),
    )
}

CASES = tuple(
    Case(
        case_id,
        case_id.partition(".")[0],
        f"targets/{case_id.replace('.', '_')}.py",
    )
    for case_id in (
        "claude.loading",
        "claude.permissions",
        "codex.loading",
        "codex.permissions",
        "cursor.loading",
        "cursor.permissions",
        "opencode.loading",
        "opencode.permissions",
    )
)


def validate_registry() -> None:
    ids = [case.id for case in CASES]
    duplicates = {case_id for case_id in ids if ids.count(case_id) > 1}
    if duplicates:
        raise ValueError(f"duplicate capability IDs: {sorted(duplicates)}")
    unknown = {case.target for case in CASES} - TARGETS.keys()
    if unknown:
        raise ValueError(f"unknown targets: {sorted(unknown)}")
    for case in CASES:
        if not (Path(__file__).parent / case.evidence).is_file():
            raise ValueError(f"{case.id}: missing evidence {case.evidence}")
