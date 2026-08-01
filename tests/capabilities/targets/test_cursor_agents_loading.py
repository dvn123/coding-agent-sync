from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from capabilities.harness import Paths, recorded_server, require_containment, run_probe
from capabilities.protocols.cursor import CursorRequest
from capabilities.protocols.openai import openai_sse
from capabilities.runtime import Seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.cursor import command, environment
from capabilities.targets.cursor import runtime as cursor_runtime

MODEL = "agents-probe"
USER_AGENT_NAME = "user-agent-probe"
PROJECT_AGENT_NAME = "project-agent-probe"
USER_AGENT_DESCRIPTION = "CURSOR_USER_AGENT_DESCRIPTION_MARKER"
PROJECT_AGENT_DESCRIPTION = "CURSOR_PROJECT_AGENT_DESCRIPTION_MARKER"
USER_AGENT_BODY = "CURSOR_USER_AGENT_BODY_MARKER"
PROJECT_AGENT_BODY = "CURSOR_PROJECT_AGENT_BODY_MARKER"


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    env: dict[str, str]


def write_agent(path: Path, name: str, description: str, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "tools: Read, Grep\n"
        "---\n"
        f"{body}\n"
    )


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, seatbelt = cursor_runtime()
    paths = Paths.create(tmp_path_factory.mktemp("cursor-agents").resolve())
    work = paths.home / "work"
    work.mkdir()
    paths = replace(paths, work=work)
    cursor = paths.home / ".cursor"
    cursor.mkdir()
    (cursor / "cli-config.json").write_text(
        json.dumps({"version": 1, "approvalMode": "allowlist"})
    )
    write_agent(
        cursor / "agents" / "user-agent-probe.md",
        USER_AGENT_NAME,
        USER_AGENT_DESCRIPTION,
        USER_AGENT_BODY,
    )
    write_agent(
        work / ".cursor" / "agents" / "project-agent-probe.md",
        PROJECT_AGENT_NAME,
        PROJECT_AGENT_DESCRIPTION,
        PROJECT_AGENT_BODY,
    )
    stub = recorded_server(
        request,
        lambda _request, _count: (
            openai_sse(
                [
                    ({"role": "assistant", "content": "done"}, None),
                    ({}, "stop"),
                ],
                MODEL,
            ),
            "text/event-stream",
        ),
    )
    env = environment(paths, cursor)
    value = Runtime(executable, seatbelt, paths, stub, env)
    require_containment(value.seatbelt, stub, paths, env)
    return value


def discover(runtime: Runtime) -> CursorRequest:
    runtime.stub.requests.clear()
    result = run_probe(
        run,
        *runtime.seatbelt.command(
            *command(
                runtime.executable,
                runtime.stub.base_url,
                MODEL,
                "Inspect available custom agents.",
                disable_project_configs=False,
            )
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert len(runtime.stub.requests) == 1
    return CursorRequest.decode(runtime.stub.requests[0])


@pytest.mark.capability_case("cursor-agent.agents-project")
@pytest.mark.capability_live
def test_cursor_agent_exposes_project_agent_discovery_metadata(
    runtime: Runtime,
) -> None:
    request = discover(runtime)
    tools = request.text("tools")

    assert PROJECT_AGENT_NAME in tools
    assert PROJECT_AGENT_DESCRIPTION in tools
    assert PROJECT_AGENT_BODY not in tools


@pytest.mark.capability_case("cursor-agent.agents-user")
@pytest.mark.capability_live
def test_cursor_agent_omits_user_agent_discovery_metadata(runtime: Runtime) -> None:
    request = discover(runtime)
    tools = request.text("tools")

    assert USER_AGENT_NAME not in tools
    assert USER_AGENT_DESCRIPTION not in tools
    assert USER_AGENT_BODY not in tools
