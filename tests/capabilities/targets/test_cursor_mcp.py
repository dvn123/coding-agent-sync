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


@pytest.mark.capability_case("cursor-agent.mcp-oauth")
@pytest.mark.capability_live
def test_cursor_agent_mcp_oauth_requires_external_authority() -> None:
    pytest.skip("unavailable: MCP OAuth requires external authorization")


# The Desktop permission file's mcpAllowlist and the CLI's own Mcp(...)
# rules, on the probe tool. Without --force the CLI cannot answer an ask.
MCP_PERMISSION_SCENARIOS: dict[str, tuple[list[str], dict[str, list[str]]]] = {
    "unlisted": ([], {}),
    "allowlist-server": (["probe:*"], {}),
    "allowlist-tool": (["probe:echo"], {}),
    "allowlist-any-server": (["*:echo"], {}),
    "allowlist-other-tool": (["probe:other"], {}),
    "cli-allow-tool": ([], {"allow": ["Mcp(probe:echo)"]}),
    "cli-allow-server": ([], {"allow": ["Mcp(probe:*)"]}),
    "cli-deny-over-allowlist": (["probe:*"], {"deny": ["Mcp(probe:echo)"]}),
}


@pytest.fixture(scope="module")
def mcp_permissions(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, CheckResult]:
    executable, seatbelt = cursor_runtime()
    observed = {}
    for name, (allowlist, rules) in MCP_PERMISSION_SCENARIOS.items():
        paths = Paths.create(tmp_path_factory.mktemp(f"cursor-mcp-{name}").resolve())
        cursor = paths.home / ".cursor"
        cursor.mkdir()
        (cursor / "cli-config.json").write_text(
            json.dumps(
                {"version": 1, "approvalMode": "allowlist"}
                | ({"permissions": {"allow": [], "deny": []} | rules} if rules else {})
            )
        )
        (cursor / "permissions.json").write_text(
            json.dumps({"approvalMode": "allowlist", "mcpAllowlist": allowlist})
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
            timeout=40,
        )
        methods = {item.get("method") for item in read_stdio_requests(log)}
        observed[name] = CheckResult(
            {
                "exit-zero": result.returncode == 0,
                "listed": "tools/list" in methods,
                "called": "tools/call" in methods,
            },
            f"exit={result.returncode} stdout={result.stdout[-800:]} "
            f"stderr={result.stderr[-500:]}",
        )
    return observed


# Listed by the server, then called. The shared allowlist approves a server,
# a tool, or a tool on any server; a call nothing approves waits for an approval
# the CLI cannot give; the CLI's own Mcp(...) rules allow too, and its deny
# outranks the shared allowlist.
MCP_PERMISSION_EXPECTED = {
    "unlisted": False,
    "allowlist-server": True,
    "allowlist-tool": True,
    "allowlist-any-server": True,
    "allowlist-other-tool": False,
    "cli-allow-tool": True,
    "cli-allow-server": True,
    "cli-deny-over-allowlist": False,
}


@pytest.mark.capability_case("cursor-agent.mcp-allowlist")
@pytest.mark.capability_live
@pytest.mark.parametrize(("scenario", "called"), MCP_PERMISSION_EXPECTED.items())
def test_cursor_mcp_tool_permissions(
    mcp_permissions: dict[str, CheckResult], scenario: str, called: bool
) -> None:
    observation = mcp_permissions[scenario]
    checks = observation.checks

    # The server always starts and lists its tool, so a dead runtime cannot
    # pass a scenario that expects no call.
    assert checks["exit-zero"] and checks["listed"], observation.detail
    assert checks["called"] is called, observation.detail
