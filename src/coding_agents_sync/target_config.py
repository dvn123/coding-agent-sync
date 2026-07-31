from __future__ import annotations

from pathlib import Path


def target_config_path(config_root: Path, target: str, filename: str) -> Path:
    return config_root / "target-config" / target / filename


def codex_rule_fragments(config_root: Path) -> dict[str, str]:
    root = target_config_path(config_root, "codex", "rules")
    if not root.exists():
        return {}
    return {
        path.name: path.read_text(encoding="utf-8").rstrip() + "\n"
        for path in sorted(root.glob("*.rules"))
        if path.is_file()
    }
