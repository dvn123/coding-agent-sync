from __future__ import annotations

from functools import cache
from pathlib import Path

import pytest

from coding_agents_sync.probes.cursor_desktop import CURSOR_APP

RESOURCES = CURSOR_APP / "Contents/Resources/app"
EXTENSIONS = RESOURCES / "extensions"
AGENT_EXEC_BUNDLE = EXTENSIONS / "cursor-agent-exec/dist/main.js"
MCP_BUNDLE = EXTENSIONS / "cursor-mcp/dist/main.js"
DESKTOP_BUNDLE = RESOURCES / "out/vs/workbench/workbench.desktop.main.js"
ENVIRONMENT_SCHEMA = EXTENSIONS / "cursor-always-local/schemas/environment.schema.json"
PERMISSIONS_SCHEMA = EXTENSIONS / "cursor-always-local/schemas/permissions.schema.json"


def require_installed(*paths: Path) -> None:
    if missing := [str(path) for path in paths if not path.is_file()]:
        pytest.skip(
            f"unavailable: Cursor Desktop bundle is missing {', '.join(missing)}"
        )


@cache
def read(path: Path) -> str:
    return path.read_text(errors="replace")
