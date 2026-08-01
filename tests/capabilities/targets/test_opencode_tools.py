from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    cached_scenario_fixture,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
)
from capabilities.model import CheckResult
from capabilities.protocols.openai import ToolResponder
from capabilities.protocols.opencode import OpenCodeEvent
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.opencode import config as opencode_config
from capabilities.targets.opencode import environment

OUTPUT_COMMAND = "blackbox-output"
OUTPUT_LAST_LINE = "OPENCODE_TOOL_OUTPUT_LAST_LINE_0bf79e"
OUTPUT_PATH = re.compile(r"Full output saved to: (\S+)")


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    state: ToolResponder
    env: dict[str, str]
    shell_marker: Path


def executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    opencode, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-tools").resolve())
    (paths.work / ".git").mkdir()
    shell_marker = paths.root / "configured-shell-used"
    shell = paths.bin / "probe-shell"
    executable(
        shell,
        f'/usr/bin/touch "{shell_marker}"\nexec /bin/sh "$@"\n',
    )
    executable(
        paths.bin / OUTPUT_COMMAND,
        'i=0\nwhile [ "$i" -lt 12 ]; do echo "probe-output-$i"; i=$((i + 1)); done\n'
        f'echo "{OUTPUT_LAST_LINE}"\n',
    )
    state = ToolResponder(
        "bash",
        {"command": OUTPUT_COMMAND, "description": "tool-output probe"},
        "tool-probe",
        "call_tool_probe",
        "tool probe complete",
    )
    stub = recorded_server(request, state.respond)
    config = opencode_config(
        stub.base_url,
        "tool-probe",
        "tool output",
        {"bash": "allow"},
    )
    config.update(
        {
            "shell": str(shell),
            "tool_output": {"max_lines": 4, "max_bytes": 96},
        }
    )
    config_path = paths.root / "opencode.json"
    config_path.write_text(json.dumps(config))
    env = environment(
        paths,
        config_path,
        f"{paths.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
    )
    value = Runtime(
        opencode,
        loopback_seatbelt(sandbox),
        paths,
        stub,
        state,
        env,
        shell_marker,
    )
    require_containment(value.seatbelt, stub, paths, env)
    return value


def observe(runtime: Runtime, _name: str) -> CheckResult:
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.executable,
            "run",
            "Call the bash tool exactly as instructed by the model.",
            "--title",
            "capability tool-output probe",
            "--pure",
            "--format",
            "json",
            "--model",
            "test/tool-probe",
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
    )
    assert process.returncode == 0, (
        f"OpenCode tool probe failed\nstdout={process.stdout}\nstderr={process.stderr}"
    )
    observation = next(
        (
            tool
            for event in OpenCodeEvent.decode_lines(process.stdout)
            if (tool := event.tool_use()) is not None and tool.tool == "bash"
        ),
        None,
    )
    output = str(observation.output) if observation else ""
    match = OUTPUT_PATH.search(output)
    path = Path(match.group(1)) if match else None
    return CheckResult(
        {
            "configured-shell-used": runtime.shell_marker.is_file(),
            "tool-output-truncated": observation is not None
            and observation.status == "completed"
            and "...output truncated..." in output,
            "tool-output-path": path is not None and path.is_file(),
            "tool-output-preserved": path is not None
            and path.is_file()
            and OUTPUT_LAST_LINE in path.read_text(),
        },
        str(observation),
    )


observation = cached_scenario_fixture(observe)


@pytest.mark.capability_case("opencode.shell-runtime")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check"),
    (pytest.param("tool", "configured-shell-used"),),
    indirect=("observation",),
    scope="module",
)
def test_opencode_shell_runtime(observation: CheckResult, check: str) -> None:
    assert observation.checks[check], observation.detail


@pytest.mark.capability_case("opencode.tool-output-runtime")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check"),
    tuple(
        pytest.param("tool", check, id=check)
        for check in (
            "tool-output-truncated",
            "tool-output-path",
            "tool-output-preserved",
        )
    ),
    indirect=("observation",),
    scope="module",
)
def test_opencode_tool_output(observation: CheckResult, check: str) -> None:
    assert observation.checks[check], observation.detail
