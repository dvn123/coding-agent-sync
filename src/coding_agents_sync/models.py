from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

type OpenCodeInstruction = str


@dataclass(frozen=True)
class SyncContext:
    config_root: Path
    home: Path

    @property
    def claude(self) -> Path:
        return self.home / ".claude"

    @property
    def cursor(self) -> Path:
        return self.home / ".cursor"

    @property
    def codex(self) -> Path:
        return self.home / ".codex"

    @property
    def opencode(self) -> Path:
        return self.home / ".config" / "opencode"

    @property
    def opencode_json(self) -> Path:
        return self.opencode / "opencode.json"

    @property
    def codex_config(self) -> Path:
        return self.codex / "config.toml"
