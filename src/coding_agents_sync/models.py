from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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
