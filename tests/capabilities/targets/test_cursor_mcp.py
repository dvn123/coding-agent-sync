from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import Paths, recorded_server, require_containment, run_probe
from capabilities.mcp import (
    TOOL_DESCRIPTION,
    TOOL_RESULT,
    CursorMCPResponder,
    mcp_checks,
    read_stdio_requests,
    stdio_command,
)
from capabilities.model import CheckResult
from capabilities.protocols.cursor import CursorRequest
from capabilities.runtime import Seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.cursor import command as cursor_command
from capabilities.targets.cursor import environment
from capabilities.targets.cursor import runtime as cursor_runtime

MODEL = "mcp-probe"


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    responder: CursorMCPResponder
    env: dict[str, str]
    log: Path


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, seatbelt = cursor_runtime()
    paths = Paths.create(tmp_path_factory.mktemp("cursor-mcp").resolve())
    cursor = paths.home / ".cursor"
    cursor.mkdir()
    (cursor / "cli-config.json").write_text(
        json.dumps({"version": 1, "approvalMode": "force"})
    )
    project = paths.work / ".cursor"
    project.mkdir()
    log = paths.root / "mcp.jsonl"
    command, *args = stdio_command(log)
    (project / "mcp.json").write_text(
        json.dumps({"mcpServers": {"probe": {"command": command, "args": args}}})
    )
    responder = CursorMCPResponder(MODEL)
    stub = recorded_server(request, responder.respond)
    env = environment(paths, cursor)
    value = Runtime(executable, seatbelt, paths, stub, responder, env, log)
    require_containment(value.seatbelt, stub, paths, env)
    return value


@pytest.mark.capability_case("cursor-agent.mcp-runtime")
@pytest.mark.capability_live
def test_cursor_agent_executes_a_project_stdio_mcp_tool(runtime: Runtime) -> None:
    result = run_probe(
        run,
        *runtime.seatbelt.command(
            *cursor_command(
                runtime.executable,
                runtime.stub.base_url,
                MODEL,
                "Call the MCP probe tool exactly as instructed by the model.",
                force=True,
                disable_project_configs=False,
                approve_mcps=True,
            )
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
    )
    requests = tuple(CursorRequest.decode(item) for item in runtime.stub.requests)
    advertised = next(
        (request for request in requests[1:] if TOOL_DESCRIPTION in request.text()),
        None,
    )
    checks = {
        **mcp_checks(read_stdio_requests(runtime.log)),
        "tool-advertised": advertised is not None and "echo" in advertised.text(),
        "tool-description-advertised": advertised is not None,
        "tool-result-returned": any(
            TOOL_RESULT in request.text() for request in requests[1:]
        ),
    }
    observation = CheckResult(
        checks,
        f"exit={result.returncode} discovered={runtime.responder.discovered} "
        f"called={runtime.responder.called_tool} stderr={result.stderr}",
    )
    assert result.returncode == 0, observation.detail
    assert all(observation.checks.values()), observation.detail


@pytest.fixture(scope="module")
def allowlist_runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, seatbelt = cursor_runtime()
    paths = Paths.create(tmp_path_factory.mktemp("cursor-mcp-allowlist").resolve())
    cursor = paths.home / ".cursor"
    cursor.mkdir()
    (cursor / "cli-config.json").write_text(
        json.dumps({"version": 1, "approvalMode": "allowlist"})
    )
    # The Desktop permissions file's mcpAllowlist maps to Mcp(...) allows in
    # the CLI's shared permission provider. --approve-mcps still starts the
    # server; the allowlist is what approves the tool call without --force.
    (cursor / "permissions.json").write_text(
        json.dumps({"approvalMode": "allowlist", "mcpAllowlist": ["probe:*"]})
    )
    project = paths.work / ".cursor"
    project.mkdir()
    log = paths.root / "mcp.jsonl"
    command, *args = stdio_command(log)
    (project / "mcp.json").write_text(
        json.dumps({"mcpServers": {"probe": {"command": command, "args": args}}})
    )
    responder = CursorMCPResponder(MODEL)
    stub = recorded_server(request, responder.respond)
    env = environment(paths, cursor)
    value = Runtime(executable, seatbelt, paths, stub, responder, env, log)
    require_containment(value.seatbelt, stub, paths, env)
    return value


@pytest.mark.capability_case("cursor-agent.mcp-allowlist")
@pytest.mark.capability_live
def test_cursor_agent_mcp_allowlist_approves_the_listed_server(
    allowlist_runtime: Runtime,
) -> None:
    result = run_probe(
        run,
        *allowlist_runtime.seatbelt.command(
            *cursor_command(
                allowlist_runtime.executable,
                allowlist_runtime.stub.base_url,
                MODEL,
                "Call the MCP probe tool exactly as instructed by the model.",
                disable_project_configs=False,
                approve_mcps=True,
            )
        ),
        cwd=allowlist_runtime.paths.work,
        env=allowlist_runtime.env,
        timeout=30,
    )
    checks = mcp_checks(read_stdio_requests(allowlist_runtime.log))
    assert result.returncode == 0, (
        f"exit={result.returncode} checks={checks} stderr={result.stderr}"
    )
    assert all(checks.values()), checks


@pytest.mark.capability_case("cursor-agent.mcp-allowlist")
@pytest.mark.capability_live
def test_cursor_agent_mcp_allowlist_ignores_an_unlisted_server(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> None:
    executable, seatbelt = cursor_runtime()
    paths = Paths.create(tmp_path_factory.mktemp("cursor-mcp-unlisted").resolve())
    cursor = paths.home / ".cursor"
    cursor.mkdir()
    (cursor / "cli-config.json").write_text(
        json.dumps({"version": 1, "approvalMode": "allowlist"})
    )
    (cursor / "permissions.json").write_text(
        json.dumps({"approvalMode": "allowlist", "mcpAllowlist": ["other:*"]})
    )
    project = paths.work / ".cursor"
    project.mkdir()
    log = paths.root / "mcp.jsonl"
    command, *args = stdio_command(log)
    (project / "mcp.json").write_text(
        json.dumps({"mcpServers": {"probe": {"command": command, "args": args}}})
    )
    responder = CursorMCPResponder(MODEL)
    stub = recorded_server(request, responder.respond)
    env = environment(paths, cursor)
    require_containment(seatbelt, stub, paths, env)

    result = run_probe(
        run,
        *seatbelt.command(
            *cursor_command(
                executable,
                stub.base_url,
                MODEL,
                "Call the MCP probe tool exactly as instructed by the model.",
                disable_project_configs=False,
                approve_mcps=True,
            )
        ),
        cwd=paths.work,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, f"exit={result.returncode} stderr={result.stderr}"
    methods = {request.get("method") for request in read_stdio_requests(log)}
    # The server started and advertised its tool; only the call was blocked
    # at the allowlist layer. Without this gate a dead runtime would pass
    # vacuously.
    assert {"initialize", "tools/list"} <= methods, methods
    assert "tools/call" not in methods, methods


@pytest.mark.capability_case("cursor-agent.mcp-oauth")
@pytest.mark.capability_live
def test_cursor_agent_mcp_oauth_requires_external_authority() -> None:
    pytest.skip("unavailable: MCP OAuth requires external authorization")
