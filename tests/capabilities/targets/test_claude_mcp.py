import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
)
from capabilities.mcp import (
    TOOL_ARGUMENT,
    TOOL_DESCRIPTION,
    TOOL_RESULT,
    AnthropicMCPResponder,
    find_tool_name,
    mcp_checks,
    read_stdio_requests,
    stdio_command,
)
from capabilities.model import CheckResult
from capabilities.protocols.anthropic import AnthropicRequest, AnthropicResponder
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.claude import environment


@dataclass(frozen=True, slots=True)
class Runtime:
    claude: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    responder: AnthropicMCPResponder
    env: dict[str, str]
    config: Path
    log: Path


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    claude, sandbox = require_command("claude"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("claude-mcp").resolve())
    log = paths.root / "mcp.jsonl"
    command, *args = stdio_command(log)
    config = paths.root / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "probe": {"type": "stdio", "command": command, "args": args}
                }
            }
        )
    )
    responder = AnthropicMCPResponder()
    stub = recorded_server(request, responder.respond)
    env = environment(
        paths,
        stub.base_url,
        XDG_DATA_HOME=str(paths.data),
        XDG_CACHE_HOME=str(paths.cache),
        # Tool search would defer the MCP tool out of the first request.
        ENABLE_TOOL_SEARCH="false",
    )
    value = Runtime(
        claude,
        loopback_seatbelt(sandbox),
        paths,
        stub,
        responder,
        env,
        config,
        log,
    )
    require_containment(value.seatbelt, stub, paths, env)
    return value


@pytest.mark.capability_case("claude.mcp-runtime")
@pytest.mark.capability_live
def test_claude_executes_a_stdio_mcp_tool(runtime: Runtime) -> None:
    result = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.claude,
            "Call the MCP probe tool exactly as instructed by the model.",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--model",
            "claude-sonnet-4-6",
            "--mcp-config",
            str(runtime.config),
            "--strict-mcp-config",
            "--allowedTools",
            "mcp__probe__echo",
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
    )
    requests = tuple(AnthropicRequest.decode(item) for item in runtime.stub.requests)
    checks = {
        **mcp_checks(read_stdio_requests(runtime.log)),
        "tool-advertised": runtime.responder.tool_name == "mcp__probe__echo",
        "tool-description-advertised": bool(requests)
        and TOOL_DESCRIPTION in requests[0].text("request"),
        "tool-result-returned": any(
            TOOL_RESULT in request.text("user") for request in requests[1:]
        ),
    }
    observation = CheckResult(
        checks,
        f"exit={result.returncode} tool={runtime.responder.tool_name} "
        f"stderr={result.stderr}",
    )
    assert result.returncode == 0, observation.detail
    assert all(observation.checks.values()), observation.detail


@pytest.mark.capability_case("claude.mcp-oauth")
@pytest.mark.capability_live
def test_claude_mcp_oauth_requires_external_authority() -> None:
    pytest.skip("unavailable: MCP OAuth requires external authorization")


# Permission rules on the MCP probe tool, under `dontAsk`, where an ask becomes
# a denial, so allow, ask, and deny are told apart without a prompt.
MCP_PERMISSION_SCENARIOS: dict[str, dict[str, list[str]]] = {
    "no-rule": {},
    **{
        f"{decision}-{name}": {decision: [rule]}
        for decision in ("allow", "ask", "deny")
        for name, rule in (
            ("tool", "mcp__probe__echo"),
            ("server", "mcp__probe"),
            ("wildcard", "mcp__probe__*"),
        )
    },
    "ask-over-allow": {"allow": ["mcp__probe"], "ask": ["mcp__probe__echo"]},
    "deny-over-allow": {"allow": ["mcp__probe"], "deny": ["mcp__probe__echo"]},
}


@pytest.fixture(scope="module")
def mcp_permissions(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, CheckResult]:
    claude, sandbox = require_command("claude"), require_command("sandbox-exec")
    seatbelt = loopback_seatbelt(sandbox)
    observed = {}
    for name, permissions in MCP_PERMISSION_SCENARIOS.items():
        paths = Paths.create(tmp_path_factory.mktemp(f"claude-mcp-{name}").resolve())
        log = paths.root / "mcp.jsonl"
        command, *args = stdio_command(log)
        config = paths.root / "mcp.json"
        config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "probe": {"type": "stdio", "command": command, "args": args}
                    }
                }
            )
        )
        settings = paths.root / "settings.json"
        settings.write_text(json.dumps({"permissions": permissions}))
        # Calls the tool once and stops at any result, so a denial ends the run.
        responder = AnthropicResponder(
            ("mcp__probe__echo", {"value": TOOL_ARGUMENT}, "mcp_probe_call")
        )
        stub = recorded_server(request, responder.respond)
        env = environment(
            paths,
            stub.base_url,
            XDG_DATA_HOME=str(paths.data),
            XDG_CACHE_HOME=str(paths.cache),
            ENABLE_TOOL_SEARCH="false",
        )
        result = run_probe(
            run,
            *seatbelt.command(
                claude,
                "Call the MCP probe tool exactly as instructed by the model.",
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--no-session-persistence",
                "--mcp-config",
                str(config),
                "--strict-mcp-config",
                "--setting-sources",
                "",
                "--settings",
                str(settings),
                "--permission-mode",
                "dontAsk",
            ),
            cwd=paths.work,
            env=env,
            timeout=30,
        )
        observed[name] = CheckResult(
            {
                "exit-zero": result.returncode == 0,
                "advertised": bool(stub.requests)
                and find_tool_name(stub.requests[0]) == "mcp__probe__echo",
                "called": mcp_checks(read_stdio_requests(log))["mcp-tool-called"],
            },
            f"exit={result.returncode} stdout={result.stdout[-1200:]} "
            f"stderr={result.stderr[-800:]}",
        )
    return observed


# Offered to the model, then called. An ask keeps the tool offered but never
# runs it here; a deny removes it from the request; the tool, its server, and a
# `__*` wildcard all match; and a rule on the tool outranks one on its server.
MCP_PERMISSION_EXPECTED = {
    "no-rule": (True, False),
    **{f"allow-{name}": (True, True) for name in ("tool", "server", "wildcard")},
    **{f"ask-{name}": (True, False) for name in ("tool", "server", "wildcard")},
    **{f"deny-{name}": (False, False) for name in ("tool", "server", "wildcard")},
    "ask-over-allow": (True, False),
    "deny-over-allow": (False, False),
}


@pytest.mark.capability_case("claude.mcp-permissions")
@pytest.mark.capability_live
@pytest.mark.parametrize(("scenario", "expected"), MCP_PERMISSION_EXPECTED.items())
def test_claude_mcp_tool_permissions(
    mcp_permissions: dict[str, CheckResult],
    scenario: str,
    expected: tuple[bool, bool],
) -> None:
    observation = mcp_permissions[scenario]
    checks = observation.checks

    assert checks["exit-zero"], observation.detail
    assert (checks["advertised"], checks["called"]) == expected, observation.detail
