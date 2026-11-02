from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from capabilities.harness import (
    Paths,
    cached_scenario_fixture,
    recorded_server,
    require_containment,
    run_probe,
)
from capabilities.model import CheckResult
from capabilities.protocols.cursor import CursorRequest
from capabilities.protocols.openai import ToolResponder
from capabilities.runtime import Seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.cursor import (
    command as cursor_command,
)
from capabilities.targets.cursor import (
    environment,
)
from capabilities.targets.cursor import (
    runtime as cursor_runtime,
)
from coding_agents_sync import run_sync

MODEL = "ignore-probe"
# One marker per file, so a read shows up in the next model request.
FILES = {
    "workspace-secret": ("work/.env", "IGNORE_PROBE_WORKSPACE_7f3a"),
    "home-secret": (".aws/credentials", "IGNORE_PROBE_HOME_91c2"),
    "control": ("work/notes.txt", "IGNORE_PROBE_CONTROL_b52e"),
}
POLICY = """\
schema: coding-agents/v1
kind: permission-policy
id: user
name: user
description: permissions
secret_paths: ["**/.env", "~/.aws/credentials"]
"""


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    state: ToolResponder
    env: dict[str, str]


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, seatbelt = cursor_runtime()
    paths = Paths.create(tmp_path_factory.mktemp("cursor-ignore").resolve())
    for relative, marker in FILES.values():
        path = paths.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{marker}\n")
    # The compiled `~/.cursorignore` sits above the workspace, and the runtime
    # reads one in every ancestor.
    sources = paths.root / "sources"
    (sources / "permissions").mkdir(parents=True)
    (sources / "permissions" / "policy.yaml").write_text(POLICY)
    run_sync(config_root=sources, home=paths.home)
    cursor = paths.home / ".cursor"
    cursor.mkdir(exist_ok=True)
    (cursor / "cli-config.json").write_text(
        json.dumps({"version": 1, "approvalMode": "allowlist"})
    )
    state = ToolResponder("Read", {}, MODEL, "call_ignore_probe", "done")
    stub = recorded_server(request, state.respond)
    env = environment(paths, cursor)
    value = Runtime(executable, seatbelt, paths, stub, state, env)
    require_containment(value.seatbelt, stub, paths, env)
    return value


def observe(runtime: Runtime, name: str) -> CheckResult:
    relative, marker = FILES[name]
    runtime.state.arguments = {"path": str(runtime.paths.home / relative)}
    runtime.stub.requests.clear()
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            *cursor_command(
                runtime.executable,
                runtime.stub.base_url,
                MODEL,
                f"Read {relative}.",
                force=True,
            )
        ),
        cwd=runtime.paths.home / "work",
        env=runtime.env,
        timeout=20,
    )
    seen = "\n".join(
        CursorRequest.decode(request).text() for request in runtime.stub.requests
    )
    return CheckResult(
        {
            "exit-zero": process.returncode == 0,
            "tool-called": len(runtime.stub.requests) >= 2,
            "content-read": marker in seen,
        },
        f"stdout={process.stdout[-2000:]}\nstderr={process.stderr[-2000:]}",
    )


observation = cached_scenario_fixture(observe)


@pytest.mark.capability_case("cursor-agent.ignore")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "read"),
    (
        pytest.param("workspace-secret", False, id="workspace-secret"),
        pytest.param("home-secret", False, id="home-secret"),
        pytest.param("control", True, id="control"),
    ),
    indirect=("observation",),
    scope="module",
)
def test_cursor_home_cursorignore_hides_files_from_the_agent(
    observation: CheckResult, read: bool
) -> None:
    assert observation.checks["exit-zero"], observation.detail
    assert observation.checks["tool-called"], observation.detail
    assert observation.checks["content-read"] is read, observation.detail
