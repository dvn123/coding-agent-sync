from __future__ import annotations

import json
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import pytest

from capabilities.harness import Paths, run_probe, sanitized_env
from capabilities.runtime import CommandResult, Seatbelt, run


def supported[T, **P](
    operation: str, function: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    result = function(*args, **kwargs)
    if isinstance(result, CommandResult) and result.returncode:
        pytest.fail(
            f"installed OpenCode failed {operation}\n"
            f"stdout={result.stdout.strip()}\nstderr={result.stderr.strip()}"
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
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "BUN_CONFIG_REGISTRY": "http://127.0.0.1:9",
            "NPM_CONFIG_REGISTRY": "http://127.0.0.1:9",
            "npm_config_registry": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
        | extra
    )


def free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


class Runtime(Protocol):
    @property
    def executable(self) -> str: ...
    @property
    def seatbelt(self) -> Seatbelt: ...
    @property
    def paths(self) -> Paths: ...
    @property
    def env(self) -> dict[str, str]: ...


def isolate_service(request: pytest.FixtureRequest, runtime: Runtime) -> None:
    """Give `debug` and `api` a private managed service.

    They reach the background service, whose default port is fixed per
    channel, so an isolated HOME would otherwise contend with the owner's
    running service for that port and hang.
    """
    config_dir = Path(
        runtime.env.get("OPENCODE_CONFIG_DIR", runtime.paths.config / "opencode")
    )
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "service.json").write_text(json.dumps({"port": free_port()}))
    request.addfinalizer(
        lambda: run(
            *runtime.seatbelt.command(runtime.executable, "service", "stop"),
            cwd=runtime.paths.work,
            env=runtime.env,
            timeout=30,
        )
    )


def api(
    method: str, path: str, directory: Path | None = None, data: Any = None
) -> tuple[str, ...]:
    """`opencode api` arguments; a raw path sends no caller directory itself."""
    if directory is not None:
        path += "?" + urlencode({"location[directory]": str(directory)})
    body = () if data is None else ("--data", json.dumps(data))
    return ("api", method, path, *body)


def inspect(runtime: Runtime, *arguments: str) -> Any:
    """Run a JSON inspector against the managed service from the work tree."""
    result = supported(
        f"the `{' '.join(arguments[:3])}` inspector",
        run_probe,
        run,
        *runtime.seatbelt.command(runtime.executable, *arguments),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=60,
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def settled(
    runtime: Runtime,
    arguments: tuple[str, ...],
    ready: Callable[[Any], bool],
    timeout: float = 20,
) -> Any:
    """Poll an inspector until `ready` accepts its value or time runs out.

    Catalog routes such as `agent.list` and `model.list` answer without
    awaiting plugin activation, which registers agents, skills, and models, so
    a cold location first reports a partial or empty catalog; MCP servers
    likewise connect in the background.
    """
    deadline = time.monotonic() + timeout
    while not ready(value := inspect(runtime, *arguments)):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    return value
