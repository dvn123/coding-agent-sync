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
    ChatMCPResponder,
    RemoteMCP,
    find_tool_name,
    mcp_checks,
    read_stdio_requests,
    stdio_command,
)
from capabilities.model import CheckResult
from capabilities.protocols.opencode import OpenCodeRequest
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.opencode import config as opencode_config
from capabilities.targets.opencode import environment

MODEL = "mcp-probe"


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    rg: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    responder: ChatMCPResponder
    remote: RemoteMCP


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, rg, sandbox = (
        require_command("opencode"),
        require_command("rg", "OpenCode requires rg to discover MCP tools"),
        require_command("sandbox-exec"),
    )
    paths = Paths.create(tmp_path_factory.mktemp("opencode-mcp").resolve())
    responder = ChatMCPResponder(MODEL)
    stub = recorded_server(request, responder.respond)
    remote = RemoteMCP.start()
    request.addfinalizer(remote.close)
    value = Runtime(
        executable,
        rg,
        loopback_seatbelt(sandbox),
        paths,
        stub,
        responder,
        remote,
    )
    require_containment(
        value.seatbelt,
        stub,
        paths,
        environment(
            paths,
            paths.root / "unused.json",
            f"{Path(rg).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        ),
    )
    return value


def invoke(runtime: Runtime, kind: str) -> CheckResult:
    runtime.stub.requests.clear()
    runtime.responder.tool_name = None
    log = runtime.paths.root / f"{kind}.jsonl"
    mcp = (
        {
            "probe": {
                "type": "local",
                "command": list(stdio_command(log)),
                "enabled": True,
            }
        }
        if kind == "local"
        else {
            "probe": {
                "type": "remote",
                "url": runtime.remote.url,
                "enabled": True,
            }
        }
    )
    config = opencode_config(
        runtime.stub.base_url,
        MODEL,
        "mcp",
        {"*": "allow"},
    )
    config["mcp"] = mcp
    config_path = runtime.paths.root / f"{kind}.json"
    config_path.write_text(json.dumps(config))
    env = environment(
        runtime.paths,
        config_path,
        f"{Path(runtime.rg).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
    )
    result = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.executable,
            "run",
            "Call the MCP probe tool exactly as instructed by the model.",
            "--pure",
            "--format",
            "json",
            "--model",
            f"test/{MODEL}",
        ),
        cwd=runtime.paths.work,
        env=env,
        timeout=30,
    )
    requests = tuple(OpenCodeRequest.decode(item) for item in runtime.stub.requests)
    advertised = next(
        (request for request in requests if find_tool_name(request.raw) is not None),
        None,
    )
    mcp_requests = (
        read_stdio_requests(log)
        if kind == "local"
        else tuple(runtime.remote.protocol.requests)
    )
    checks = {
        **mcp_checks(mcp_requests),
        "tool-advertised": runtime.responder.tool_name is not None,
        "tool-description-advertised": advertised is not None
        and TOOL_DESCRIPTION in advertised.text("tools"),
        "tool-result-returned": any(
            TOOL_RESULT in request.text() for request in requests[1:]
        ),
    }
    return CheckResult(
        checks,
        f"exit={result.returncode} tool={runtime.responder.tool_name} "
        f"stderr={result.stderr}",
    )


@pytest.mark.capability_case("opencode.mcp-local-runtime")
@pytest.mark.capability_live
def test_opencode_executes_a_local_stdio_mcp_tool(runtime: Runtime) -> None:
    observation = invoke(runtime, "local")
    assert all(observation.checks.values()), observation.detail


@pytest.mark.capability_case("opencode.mcp-remote-runtime")
@pytest.mark.capability_live
def test_opencode_executes_a_remote_http_mcp_tool(runtime: Runtime) -> None:
    observation = invoke(runtime, "remote")
    assert all(observation.checks.values()), observation.detail


@pytest.mark.capability_case("opencode.mcp-oauth")
@pytest.mark.capability_live
def test_opencode_mcp_oauth_requires_external_authority() -> None:
    pytest.skip("unavailable: MCP OAuth requires external authorization")
