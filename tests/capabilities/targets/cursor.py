from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from capabilities.harness import Paths, require_command, sanitized_env
from capabilities.runtime import Seatbelt, loopback_seatbelt


def local_executable() -> str | None:
    configured = os.environ.get("CURSOR_AGENT_LOCAL")
    if configured and os.access(configured, os.X_OK):
        return configured
    if executable := shutil.which("cursor-agent-local"):
        return executable
    installed = shutil.which("cursor-agent")
    if not installed:
        return None
    version_id = version(installed).rsplit(" ", 1)[-1]
    cache = (
        Path.home() / "Library/Caches/coding-agents-capabilities/cursor" / version_id
    )
    return next(
        (
            str(path)
            for path in cache.rglob("cursor-agent-local")
            if os.access(path, os.X_OK)
        ),
        None,
    )


def version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except OSError, subprocess.TimeoutExpired:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def runtime() -> tuple[str, Seatbelt]:
    installed = require_command("cursor-agent")
    executable = local_executable()
    if not executable:
        pytest.skip("unavailable: matching Cursor local runtime is unavailable")
    installed_version, local_version = version(installed), version(executable)
    if not installed_version or not local_version:
        pytest.skip("unavailable: Cursor runtime version could not be determined")
    if local_version != installed_version:
        pytest.skip("unavailable: Cursor local and installed versions differ")
    return executable, loopback_seatbelt(require_command("sandbox-exec"))


def command(
    executable: str,
    base_url: str,
    model: str,
    prompt: str,
    *,
    force: bool = False,
    disable_project_configs: bool = True,
    approve_mcps: bool = False,
) -> tuple[str, ...]:
    return (
        executable,
        "--authless",
        "--base-url",
        f"{base_url}/v1",
        "--local-agent-api-key",
        "dummy-not-a-credential",
        "--print",
        "--output-format",
        "stream-json",
        "--model",
        model,
        *(("--disable-project-configs",) if disable_project_configs else ()),
        "--disable-indexing",
        "--disable-codebase-ref",
        "--single-turn",
        "--trust",
        *(("--force",) if force else ()),
        *(("--approve-mcps",) if approve_mcps else ()),
        prompt,
    )


def environment(paths: Paths, config: Path, **extra: str) -> dict[str, str]:
    return sanitized_env(
        {
            "HOME": str(paths.home),
            "CURSOR_CONFIG_DIR": str(config),
            "TMPDIR": str(paths.tmp),
            "AGENT_CLI_CREDENTIAL_STORE": "file",
            "CURSOR_AGENT_DISABLE_DEBUG_LOG": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        | extra
    )
