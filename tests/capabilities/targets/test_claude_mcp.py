from __future__ import annotations

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
    TOOL_DESCRIPTION,
    TOOL_RESULT,
    AnthropicMCPResponder,
    mcp_checks,
    read_stdio_requests,
    stdio_command,
)
from capabilities.model import CheckResult
from capabilities.protocols.anthropic import AnthropicRequest
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
