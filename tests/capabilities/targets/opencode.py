from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from capabilities.harness import Paths, sanitized_env
from capabilities.runtime import CommandResult


def supported[T, **P](
    operation: str, function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    result = function(*args, **kwargs)
    if isinstance(result, CommandResult) and result.returncode:
        pytest.skip(
            f"unavailable: installed OpenCode failed {operation}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def config(
    base_url: str, model: str, label: str, permission: dict[str, Any]
) -> dict[str, Any]:
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"test/{model}",
        "small_model": f"test/{model}",
        "autoupdate": False,
        "enabled_providers": ["test"],
        "permission": permission,
        "provider": {
            "test": {
                "name": f"Local {label} probe",
                "npm": "@ai-sdk/openai-compatible",
                "env": [],
                "models": {
                    model: {
                        "name": f"{label.title()} Probe",
                        "tool_call": True,
                        "limit": {"context": 100000, "output": 1000},
                    }
                },
                "options": {
                    "apiKey": "not-a-credential",
                    "baseURL": f"{base_url}/v1",
                },
            }
        },
    }


def environment(
    paths: Paths, config_path: Path, executable_path: str, **extra: str
) -> dict[str, str]:
    return sanitized_env(
        {
            "HOME": str(paths.home),
            "PATH": executable_path,
            "TMPDIR": str(paths.tmp),
            "XDG_CACHE_HOME": str(paths.cache),
            "XDG_CONFIG_HOME": str(paths.config),
            "XDG_DATA_HOME": str(paths.data),
            "XDG_STATE_HOME": str(paths.state),
            "OPENCODE_CONFIG": str(config_path),
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "BUN_CONFIG_REGISTRY": "http://127.0.0.1:9",
            "NPM_CONFIG_REGISTRY": "http://127.0.0.1:9",
            "npm_config_registry": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        | extra
    )
