from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
    sanitized_env,
)
from capabilities.mcp import (
    TOOL_DESCRIPTION,
    TOOL_RESULT,
    ResponsesMCPResponder,
    find_tool_name,
    mcp_checks,
    read_stdio_requests,
    stdio_command,
)
from capabilities.model import CheckResult
from capabilities.protocols.responses import ResponsesRequest
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.codex import write_config


@dataclass(frozen=True, slots=True)
class Runtime:
    codex: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    responder: ResponsesMCPResponder
    env: dict[str, str]
    log: Path


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    codex, sandbox = require_command("codex"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("codex-mcp").resolve())
    codex_home = paths.home / ".codex"
    codex_home.mkdir()
    responder = ResponsesMCPResponder(search_deferred=True)
    stub = recorded_server(request, responder.respond)
    write_config(codex_home, paths.work, stub.base_url, approval="never")
    log = paths.root / "mcp.jsonl"
    command, *args = stdio_command(log)
    config = codex_home / "config.toml"
    config.write_text(
        config.read_text()
        + "\n\n[mcp_servers.probe]\n"
        + f"command = {json.dumps(command)}\n"
        + f"args = {json.dumps(args)}\n"
        + "startup_timeout_sec = 10\n"
        + "tool_timeout_sec = 10\n"
    )
    env = sanitized_env(
        {
            "CODEX_HOME": str(codex_home),
            "CODEX_CAPABILITY_KEY": "local-placeholder",
            "HOME": str(paths.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": str(paths.tmp),
        }
    )
    value = Runtime(
        codex,
        loopback_seatbelt(sandbox),
        paths,
        stub,
        responder,
        env,
        log,
    )
    require_containment(value.seatbelt, stub, paths, env)
    return value


@pytest.mark.capability_case("codex.mcp-runtime")
@pytest.mark.capability_live
def test_codex_executes_a_stdio_mcp_tool(runtime: Runtime) -> None:
    result = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.codex,
            "exec",
            "--skip-git-repo-check",
            "--strict-config",
            "--ephemeral",
            "--json",
            "-C",
            str(runtime.paths.work),
            "Call the MCP probe tool exactly as instructed by the model.",
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
        stdin=-3,
    )
    requests = tuple(ResponsesRequest.decode(item) for item in runtime.stub.requests)
    advertised = next(
        (request for request in requests if find_tool_name(request.raw) is not None),
        None,
    )
    checks = {
        **mcp_checks(read_stdio_requests(runtime.log)),
        "tool-advertised": runtime.responder.tool_name == "mcp__probe__echo",
        "tool-description-advertised": advertised is not None
        and TOOL_DESCRIPTION
        in advertised.text("tools") + advertised.text("tool_search_output"),
        "tool-result-returned": any(
            TOOL_RESULT in output.text
            for request in requests[1:]
            for output in request.function_outputs()
        ),
    }
    observation = CheckResult(
        checks,
        f"exit={result.returncode} tool={runtime.responder.tool_name} "
        f"stderr={result.stderr}",
    )
    assert result.returncode == 0, observation.detail
    assert all(observation.checks.values()), observation.detail


@pytest.mark.capability_case("codex.mcp-oauth")
@pytest.mark.capability_live
def test_codex_mcp_oauth_requires_external_authority() -> None:
    pytest.skip("unavailable: MCP OAuth requires external authorization")
