from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path

import pytest

from .model import Target
from .runtime import CommandTimeout, Seatbelt, prove_loopback_only
from .server import RecordedServer, Responder

PASSTHROUGH = {
    "LANG",
    "LC_ALL",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "CURSOR_AGENT_LOCAL",
}


def sanitized_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key in PASSTHROUGH}
    env.update(
        {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DO_NOT_TRACK": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        }
    )
    env.update(extra or {})
    return env


def require_command(name: str, reason: str | None = None) -> str:
    if executable := shutil.which(name):
        return executable
    pytest.skip(f"unavailable: {reason or f'{name} executable is not installed'}")


def recorded_server(
    request: pytest.FixtureRequest, responder: Responder
) -> RecordedServer:
    server = RecordedServer.start(0, responder)
    request.addfinalizer(server.close)
    return server


def cached_scenario_fixture[T, R](loader: Callable[[R, str], T]):
    values: dict[str, T] = {}

    @pytest.fixture(scope="module")
    def observation(request: pytest.FixtureRequest, runtime: R) -> T:
        name = str(request.param)
        if name not in values:
            values[name] = loader(runtime, name)
        return values[name]

    return observation


def run_probe[T, **P](
    runner: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs
) -> T:
    try:
        return runner(*args, **kwargs)
    except CommandTimeout as error:
        pytest.fail(str(error))


def version(target: Target) -> str:
    if target.name == "cursor-desktop":
        from coding_agents_sync.probes.cursor_desktop import (
            CURSOR_APP,
            ProbeUnavailable,
            _cursor_version,
        )

        try:
            return _cursor_version(CURSOR_APP)
        except ProbeUnavailable:
            return ""
    executable = shutil.which(target.command)
    if not executable:
        return ""
    try:
        result = subprocess.run(
            [executable, *target.version_args],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
    except OSError, subprocess.TimeoutExpired:
        return ""
    return (result.stdout or result.stderr).strip().splitlines()[0]


def update(target: Target) -> tuple[bool, str]:
    if target.updater is None:
        return False, "target is not updated by the harness"
    if not (executable := shutil.which(target.updater[0])):
        return False, f"{target.updater[0]} updater is unavailable"
    try:
        result = subprocess.run(
            [executable, *target.updater[1:]],
            capture_output=True,
            check=False,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    detail = (result.stdout or result.stderr).strip()
    return result.returncode == 0, detail or f"exit {result.returncode}"


def _command_output(*command: str, timeout: int = 15) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode:
        raise RuntimeError((result.stdout or result.stderr).strip())
    return (result.stdout or result.stderr).strip()


def resolve_cursor_local(*, allow_download: bool = False) -> tuple[Path | None, str]:
    executable = shutil.which("cursor-agent")
    if not executable:
        return None, "cursor-agent executable is unavailable"
    try:
        version_id = _command_output(executable, "--version").rsplit(" ", 1)[-1]
        system = {"Darwin": "darwin", "Linux": "linux"}.get(platform.system())
        machine = {"arm64": "arm64", "x86_64": "x64"}.get(platform.machine())
        if not version_id or not system or not machine:
            raise RuntimeError("unsupported Cursor local-runtime platform")
        cache = (
            Path.home()
            / "Library/Caches/coding-agents-capabilities/cursor"
            / version_id
        )
        candidates = list(cache.rglob("cursor-agent-local")) if cache.exists() else []
        if not candidates:
            if not allow_download:
                return (
                    None,
                    "cached Cursor local runtime is unavailable; download disabled",
                )
            cache.mkdir(parents=True, exist_ok=True)
            url = (
                f"https://downloads.cursor.com/lab/{version_id}/{system}/{machine}/"
                "agent-cli-local-package.tar.gz"
            )
            with tempfile.TemporaryDirectory() as temporary:
                archive = Path(temporary) / "cursor-local.tar.gz"
                urllib.request.urlretrieve(url, archive)
                with tarfile.open(archive) as bundle:
                    bundle.extractall(cache, filter="data")
            candidates = list(cache.rglob("cursor-agent-local"))
        if not candidates or version_id not in _command_output(
            str(candidates[0]), "--version"
        ):
            raise RuntimeError("matching Cursor local runtime is unavailable")
        return candidates[0], str(candidates[0])
    except (
        OSError,
        tarfile.TarError,
        subprocess.TimeoutExpired,
        RuntimeError,
    ) as error:
        return None, str(error)


@dataclass(frozen=True, slots=True)
class Paths:
    root: Path
    home: Path
    config: Path
    cache: Path
    data: Path
    state: Path
    work: Path
    tmp: Path
    bin: Path

    @classmethod
    def create(cls, root: Path) -> Paths:
        children = (root / name for name in cls.__annotations__ if name != "root")
        value = cls(root, *children)
        for field in fields(value):
            getattr(value, field.name).mkdir(exist_ok=True)
        return value


def require_containment(
    seatbelt: Seatbelt, server: RecordedServer, paths: Paths, env: dict[str, str]
) -> None:
    try:
        port = int(server.base_url.rsplit(":", 1)[1])
        control = prove_loopback_only(seatbelt, port, paths.work, env)
    except CommandTimeout as error:
        pytest.fail(f"containment harness timed out: {error}")
    if control.returncode:
        pytest.fail(
            "containment failed closed: loopback access and non-loopback denial "
            f"were not both proven\nstdout={control.stdout}\nstderr={control.stderr}"
        )
