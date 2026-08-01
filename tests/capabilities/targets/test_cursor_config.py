from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_containment,
    run_probe,
)
from capabilities.protocols.cursor import CursorEvent
from capabilities.protocols.openai import ToolResponder
from capabilities.runtime import Seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.cursor import command as cursor_command
from capabilities.targets.cursor import environment
from capabilities.targets.cursor import runtime as cursor_runtime

MODEL = "cursor-cli-config-probe"
COMMAND = "cursor-cli-config-command"


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    config: Path
    marker: Path
    env: dict[str, str]


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, seatbelt = cursor_runtime()
    paths = Paths.create(tmp_path_factory.mktemp("cursor-config").resolve())
    config = paths.home / ".cursor"
    config.mkdir()
    marker = paths.root / "executed"
    recorder = paths.bin / COMMAND
    recorder.write_text(f'#!/bin/sh\n/usr/bin/touch "{marker}"\n')
    recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR)
    responder = ToolResponder(
        "Shell",
        {"command": COMMAND, "description": "Run CLI config probe"},
        MODEL,
        "call_cursor_config_probe",
        "done",
    )
    stub = recorded_server(request, responder.respond)
    env = environment(
        paths,
        config,
        PATH=f"{paths.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
    )
    value = Runtime(executable, seatbelt, paths, stub, config, marker, env)
    require_containment(value.seatbelt, stub, paths, env)
    return value


def invoke(runtime: Runtime, approval_mode: str) -> tuple[str, bool]:
    runtime.marker.unlink(missing_ok=True)
    (runtime.config / "cli-config.json").write_text(
        json.dumps({"version": 1, "approvalMode": approval_mode})
    )
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            *cursor_command(
                runtime.executable,
                runtime.stub.base_url,
                MODEL,
                f"Run {COMMAND} exactly once.",
            )
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    outcome = next(
        (
            observation.text
            for event in CursorEvent.decode_lines(process.stdout)
            if (observation := event.shell_outcome()) is not None
        ),
        None,
    )
    assert outcome is not None
    return outcome, runtime.marker.exists()


@pytest.mark.capability_case("cursor-agent.config")
@pytest.mark.capability_live
def test_cursor_cli_config_approval_mode_changes_shell_behavior(
    runtime: Runtime,
) -> None:
    allowlist = invoke(runtime, "allowlist")
    unrestricted = invoke(runtime, "unrestricted")

    assert allowlist == ("rejected", False)
    assert unrestricted == ("success", True)
