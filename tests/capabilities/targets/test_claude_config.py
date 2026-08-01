from __future__ import annotations

import json
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
)
from capabilities.protocols.anthropic import AnthropicRequest, AnthropicResponder
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.claude import environment

MODEL = "claude-settings-resolution-probe"
TOOL_ID = "toolu_sandbox_config_probe"


@dataclass(frozen=True, slots=True)
class Runtime:
    claude: str
    seatbelt: Seatbelt
    stub: RecordedServer
    responder: AnthropicResponder
    paths: Paths


@pytest.fixture(scope="module")
def runtime(request: pytest.FixtureRequest) -> Iterator[Runtime]:
    # Claude's macOS sandbox expands its rules onto the exec command line, so
    # keep this path short enough to stay below the platform argument limit.
    with TemporaryDirectory(prefix="claude-config-", dir="/tmp") as root:
        paths = Paths.create(Path(root).resolve())
        responder = AnthropicResponder()
        stub = recorded_server(request, responder.respond)
        seatbelt = loopback_seatbelt(require_command("sandbox-exec"))
        value = Runtime(
            require_command("claude"),
            Seatbelt(
                seatbelt.executable,
                f"{seatbelt.profile}\n(allow network* (local unix-socket))",
            ),
            stub,
            responder,
            paths,
        )
        require_containment(
            value.seatbelt,
            stub,
            paths,
            environment(paths, stub.base_url),
        )
        yield value


def write_settings(runtime: Runtime, value: object) -> Path:
    settings = runtime.paths.config / "settings.json"
    settings.write_text(json.dumps(value))
    return settings


def invoke(
    runtime: Runtime, *, tool: str | None = None, outer_seatbelt: bool = True
) -> list[AnthropicRequest]:
    runtime.responder.tool_call = (
        ("Bash", {"command": tool}, TOOL_ID) if tool is not None else None
    )
    start = len(runtime.stub.requests)
    command = (
        runtime.claude,
        "Run the requested Bash command exactly once."
        if tool
        else "Reply with configuration probe complete.",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--setting-sources",
        "user",
        "--permission-mode",
        "dontAsk",
        *(("--tools", "Bash") if tool else ("--tools", "")),
    )
    process = run_probe(
        run,
        *(runtime.seatbelt.command(*command) if outer_seatbelt else command),
        cwd=runtime.paths.work,
        env=environment(runtime.paths, runtime.stub.base_url),
        timeout=20,
    )
    assert process.returncode == 0, process.stderr
    return [
        AnthropicRequest.decode(request) for request in runtime.stub.requests[start:]
    ]


@pytest.mark.capability_case("claude.settings")
@pytest.mark.capability_live
def test_claude_resolves_model_from_isolated_user_settings(runtime: Runtime) -> None:
    write_settings(runtime, {"model": MODEL})

    requests = invoke(runtime)

    assert len(requests) == 1
    assert requests[0].raw["model"] == MODEL


@pytest.mark.capability_case("claude.sandbox")
@pytest.mark.capability_live
def test_claude_sandbox_enforces_deny_write_from_user_settings(
    runtime: Runtime,
) -> None:
    allowed = runtime.paths.work / "allowed"
    blocked_dir = runtime.paths.work / "blocked"
    blocked_dir.mkdir()
    denied = blocked_dir / "denied"
    write_settings(
        runtime,
        {
            "model": MODEL,
            "permissions": {"allow": ["Bash(*)"]},
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": True,
                "allowUnsandboxedCommands": False,
                "filesystem": {"denyWrite": [str(blocked_dir)]},
            },
        },
    )
    command = " && ".join(
        f"/usr/bin/touch {shlex.quote(str(path))}" for path in (allowed, denied)
    )

    # Claude's macOS sandbox cannot initialize within a second Seatbelt profile.
    requests = invoke(runtime, tool=command, outer_seatbelt=False)

    assert len(requests) == 2
    result = requests[1].tool_result(TOOL_ID)
    assert result is not None
    assert allowed.exists(), result
    assert not denied.exists()
