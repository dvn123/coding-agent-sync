"""Compiled Cursor guards hold through shell-unwrap's hook.

Cursor's allowlist has no deny or ask channel, so the compiler lists every
allow and installs shell-unwrap's `beforeShellExecution` hook in `hooks.json`
with the guards. This runs Cursor CLI on exactly what `run_sync` writes; the
hook runs by its path, so a bare `PATH` serves.
"""

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    cached_scenario_fixture,
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
from coding_agents_sync import run_sync

TOOL = "unwrap-tool"
REASON = "run unwrap-tool status instead"
MODEL = "unwrap-probe"
POLICY = """\
description: permissions
wrappers: [timeout]
"""
RULES = f"""\
allow:
  - [{TOOL}]
ask:
  - [{TOOL}, push]
deny:
  - {{prefix: {TOOL} wipe, reason: {REASON}}}
"""
# Each scenario: the command, Cursor's shell outcome, whether the recorder
# ran, and whether `--force` approves prompts. The plain allow proves the hook
# ran and answered nothing: a hook that could not run would ask.
SCENARIOS = {
    "allowed": (f"{TOOL} status", "success", True, False),
    "asked": (f"{TOOL} push", "rejected", False, False),
    "asked-forced": (f"{TOOL} push", "success", True, True),
    "denied": (f"{TOOL} wipe", "rejected", False, False),
    "denied-forced": (f"{TOOL} wipe", "rejected", False, True),
    # Both commands match the `unwrap-tool` allow; the hook sees the second.
    "denied-chained": (f"{TOOL} status && {TOOL} wipe", "rejected", False, False),
}


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    state: ToolResponder
    env: dict[str, str]
    marker: Path


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, seatbelt = cursor_runtime()
    paths = Paths.create(tmp_path_factory.mktemp("cursor-unwrap").resolve())
    marker = paths.root / f"{TOOL}.executed"
    recorder = paths.bin / TOOL
    recorder.write_text(f"#!/bin/sh\n/usr/bin/touch {marker}\n")
    recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR)
    sources = paths.root / "sources" / "permissions"
    (sources / "commands").mkdir(parents=True)
    (sources / "policy.yaml").write_text(POLICY)
    (sources / "commands" / "test.yaml").write_text(RULES)
    run_sync(config_root=sources.parent, home=paths.home)
    cursor = paths.home / ".cursor"
    (cursor / "cli-config.json").write_text(
        json.dumps({"version": 1, "permissions": {"allow": [], "deny": []}})
    )
    state = ToolResponder(
        "Shell",
        {"command": TOOL, "description": "Run unwrap probe"},
        MODEL,
        "call_unwrap_probe",
        "done",
    )
    stub = recorded_server(request, state.respond)
    env = environment(
        paths,
        cursor,
        PATH=f"{paths.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
    )
    value = Runtime(executable, seatbelt, paths, stub, state, env, marker)
    require_containment(value.seatbelt, stub, paths, env)
    return value


def observe(runtime: Runtime, name: str) -> tuple[str | None, bool]:
    command, _, _, force = SCENARIOS[name]
    runtime.state.arguments["command"] = command
    runtime.marker.unlink(missing_ok=True)
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            *cursor_command(
                runtime.executable,
                runtime.stub.base_url,
                MODEL,
                f"Run {command}.",
                force=force,
            )
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
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
    return outcome, runtime.marker.exists()


observation = cached_scenario_fixture(observe)


@pytest.mark.capability_case("cursor-agent.unwrap")
@pytest.mark.capability_live
def test_compiled_allowlist_lists_the_guarded_allow(runtime: Runtime) -> None:
    desktop = json.loads((runtime.paths.home / ".cursor/permissions.json").read_text())
    assert TOOL in desktop["terminalAllowlist"]
    hooks = json.loads((runtime.paths.home / ".cursor/hooks.json").read_text())
    assert len(hooks["hooks"]["beforeShellExecution"]) == 1
    assert os.access(runtime.paths.home / ".cursor/hooks/shell-unwrap", os.X_OK)


@pytest.mark.capability_case("cursor-agent.unwrap")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "expected_outcome", "expected_ran"),
    tuple(
        pytest.param(name, outcome, ran, id=name)
        for name, (_, outcome, ran, _force) in SCENARIOS.items()
    ),
    indirect=("observation",),
    scope="module",
)
def test_hook_guards_the_allowlist(
    observation: tuple[str | None, bool], expected_outcome: str, expected_ran: bool
) -> None:
    assert observation == (expected_outcome, expected_ran)
