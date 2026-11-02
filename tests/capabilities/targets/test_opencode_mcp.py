import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_command,
    require_containment,
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
from capabilities.runtime import CommandTimeout, Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.opencode import api, environment, isolate_service, settled
from capabilities.targets.opencode import config as opencode_config

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


@dataclass(frozen=True, slots=True)
class Probe:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    env: dict[str, str]


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


def status(servers: dict[str, Any]) -> str:
    return next(
        (
            item["status"]["status"]
            for item in servers["data"]
            if item["name"] == "probe"
        ),
        "unlisted",
    )


def invoke(
    request: pytest.FixtureRequest,
    runtime: Runtime,
    kind: str,
    rules: tuple[dict[str, str], ...] = (),
    name: str | None = None,
) -> CheckResult:
    runtime.stub.requests.clear()
    runtime.responder.tool_name = None
    paths = Paths.create(runtime.paths.root / (name or kind))
    log = paths.root / "mcp.jsonl"
    server = (
        {"type": "local", "command": list(stdio_command(log))}
        if kind == "local"
        else {"type": "remote", "url": runtime.remote.url, "oauth": False}
    )
    config = opencode_config(
        runtime.stub.base_url,
        MODEL,
        "mcp",
        {"*": "allow"},
    )
    # MCP tools default to Code Mode, reachable only through `execute`; the
    # probe calls the server's tool directly.
    config["mcp"] = {"servers": {"probe": server | {"codemode": False}}}
    if rules:
        # Native rules apply after the V1 `permission` map; the last match wins.
        config["permissions"] = list(rules)
    config_path = paths.root / "opencode.json"
    config_path.write_text(json.dumps(config))
    probe = Probe(
        runtime.executable,
        runtime.seatbelt,
        paths,
        environment(
            paths,
            config_path,
            f"{Path(runtime.rg).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        ),
    )
    isolate_service(request, probe)
    # A turn lists only MCP servers already connected when it starts, so the
    # server connects on the managed service before the prompt reuses it.
    connected = status(
        settled(
            probe,
            api("get", "/api/mcp", paths.work),
            lambda value: status(value) == "connected",
        )
    )
    # A turn that waits on an answer `run` cannot give never ends.
    try:
        result = run(
            *runtime.seatbelt.command(
                runtime.executable,
                "run",
                "Call the MCP probe tool exactly as instructed by the model.",
                "--format",
                "json",
                "--model",
                f"test/{MODEL}",
            ),
            cwd=paths.work,
            env=probe.env,
            timeout=30,
        )
    except CommandTimeout:
        result = None
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
        "finished": result is not None,
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
        f"status={connected} exit={result and result.returncode} "
        f"tool={runtime.responder.tool_name} "
        f"stdout={result and result.stdout[-600:]} stderr={result and result.stderr}",
    )


@pytest.mark.capability_case("opencode.mcp-local-runtime")
@pytest.mark.capability_live
def test_opencode_executes_a_local_stdio_mcp_tool(
    request: pytest.FixtureRequest, runtime: Runtime
) -> None:
    observation = invoke(request, runtime, "local")
    assert all(observation.checks.values()), observation.detail


@pytest.mark.capability_case("opencode.mcp-remote-runtime")
@pytest.mark.capability_live
def test_opencode_executes_a_remote_http_mcp_tool(
    request: pytest.FixtureRequest, runtime: Runtime
) -> None:
    observation = invoke(request, runtime, "remote")
    assert all(observation.checks.values()), observation.detail


@pytest.mark.capability_case("opencode.mcp-oauth")
@pytest.mark.capability_live
def test_opencode_mcp_oauth_requires_external_authority() -> None:
    pytest.skip("unavailable: MCP OAuth requires external authorization")


def mcp_rule(action: str, effect: str) -> dict[str, str]:
    return {"action": action, "resource": "*", "effect": effect}


# Native rules on the probe tool, over a blanket allow. `run` cannot answer an
# ask.
MCP_PERMISSION_SCENARIOS = {
    "no-rule": (),
    **{
        f"{effect}-{name}": (mcp_rule(action, effect),)
        for effect in ("allow", "ask", "deny")
        for name, action in (("tool", "probe_echo"), ("wildcard", "probe_*"))
    },
    "allow-after-deny": (mcp_rule("probe_*", "deny"), mcp_rule("probe_echo", "allow")),
}


# Offered to the model, then called. An ask keeps the tool offered and `run`
# ends the turn without calling it; a deny removes it from the request; the
# action is `<server>_<tool>`, `<server>_*` matches it, and the last match wins.
MCP_PERMISSION_EXPECTED = {
    "no-rule": (True, True),
    "allow-tool": (True, True),
    "allow-wildcard": (True, True),
    "ask-tool": (True, False),
    "ask-wildcard": (True, False),
    "deny-tool": (False, False),
    "deny-wildcard": (False, False),
    "allow-after-deny": (True, True),
}


@pytest.fixture(scope="module")
def mcp_permissions(
    request: pytest.FixtureRequest, runtime: Runtime
) -> dict[str, CheckResult]:
    return {
        name: invoke(request, runtime, "local", rules, f"permission-{name}")
        for name, rules in MCP_PERMISSION_SCENARIOS.items()
    }


@pytest.mark.capability_case("opencode.mcp-permissions")
@pytest.mark.capability_live
@pytest.mark.parametrize(("scenario", "expected"), MCP_PERMISSION_EXPECTED.items())
def test_opencode_mcp_tool_permissions(
    mcp_permissions: dict[str, CheckResult],
    scenario: str,
    expected: tuple[bool, bool],
) -> None:
    observation = mcp_permissions[scenario]
    checks = observation.checks

    assert checks["finished"], observation.detail
    assert (
        checks["tool-advertised"],
        checks["mcp-tool-called"],
    ) == expected, observation.detail
