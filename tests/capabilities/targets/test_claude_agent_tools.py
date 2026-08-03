"""How Claude resolves a subagent's `tools` frontmatter.

The compiler emits `tools` verbatim, so it needs to know which values Claude
can actually resolve. `inherit` looks like it should widen the pool, but it is
a `model` value: Claude treats it as a literal tool name, matches nothing, and
refuses to spawn the agent. Inheriting the full pool means omitting the field.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    cached_scenario_fixture,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
)
from capabilities.protocols.anthropic import AnthropicRequest, AnthropicResponder
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.claude import environment

INHERIT_AGENT = "inherit-tools-probe"
DENYLIST_AGENT = "denylist-tools-probe"
NAMED_AGENT = "named-tools-probe"
TOOL_ID = "toolu_capability_agent"
CLAUDE_FLAGS = (
    "--print",
    "--output-format",
    "stream-json",
    "--verbose",
    "--no-session-persistence",
    "--setting-sources",
    "user",
    "--tools",
    "Agent",
)


@dataclass(frozen=True, slots=True)
class Runtime:
    claude: str
    seatbelt: Seatbelt
    stub: RecordedServer
    responder: AnthropicResponder
    paths: Paths
    env: dict[str, str]


def agent(name: str, description: str, *frontmatter: str) -> str:
    lines = "\n".join(frontmatter)
    return f"---\nname: {name}\ndescription: {description}\n{lines}\n---\nProbe body\n"


def write_fixtures(config: Path) -> None:
    agents = config / "agents"
    agents.mkdir(parents=True)
    (agents / f"{INHERIT_AGENT}.md").write_text(
        agent(
            INHERIT_AGENT,
            "Probe agent whose tools field names no real tool.",
            "tools: inherit",
            "disallowedTools: [Edit, Write]",
        )
    )
    (agents / f"{DENYLIST_AGENT}.md").write_text(
        agent(
            DENYLIST_AGENT,
            "Probe agent that inherits its pool minus file writes.",
            "disallowedTools: [Edit, Write]",
        )
    )
    (agents / f"{NAMED_AGENT}.md").write_text(
        agent(
            NAMED_AGENT,
            "Probe agent restricted to an explicit tool list.",
            "tools: [Read, Grep]",
        )
    )


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    claude, sandbox = require_command("claude"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("claude-agent-tools").resolve())
    write_fixtures(paths.config)
    responder = AnthropicResponder()
    stub = recorded_server(request, responder.respond)
    value = Runtime(
        claude,
        loopback_seatbelt(sandbox),
        stub,
        responder,
        paths,
        environment(
            paths,
            stub.base_url,
            XDG_DATA_HOME=str(paths.data),
            XDG_CACHE_HOME=str(paths.cache),
        ),
    )
    require_containment(value.seatbelt, stub, paths, value.env)
    return value


def invoke(runtime: Runtime, prompt: str, expected: int) -> list[AnthropicRequest]:
    start = len(runtime.stub.requests)
    result = run_probe(
        run,
        *runtime.seatbelt.command(runtime.claude, prompt, *CLAUDE_FLAGS),
        cwd=runtime.paths.work,
        env=runtime.env,
    )
    requests = runtime.stub.requests[start:]
    assert result.returncode == 0, (
        f"Claude invocation failed for {prompt!r}\nexit={result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert len(requests) == expected, (
        f"Claude sent {len(requests)} requests for {prompt!r}, wanted {expected}"
    )
    return [AnthropicRequest.decode(item) for item in requests]


def observe(runtime: Runtime, _name: str) -> AnthropicRequest:
    return invoke(runtime, "ROSTER_CONTROL_PROMPT", 1)[0]


observation = cached_scenario_fixture(observe)

ROSTER = {
    # `inherit` reaches the roster as a literal tool name rather than widening
    # the pool, which is why the compiler must reject it.
    "inherit-is-literal": f"{INHERIT_AGENT}: Probe agent whose tools field names "
    "no real tool. (Tools: inherit)",
    # Omitting `tools` is what actually inherits the pool.
    "omitted-inherits": f"{DENYLIST_AGENT}: Probe agent that inherits its pool "
    "minus file writes. (Tools: All tools except Edit, Write)",
    # An explicit list is reported verbatim.
    "named-list": f"{NAMED_AGENT}: Probe agent restricted to an explicit tool "
    "list. (Tools: Read, Grep)",
}


@pytest.mark.capability_case("claude.agent-tools")
@pytest.mark.capability_live
@pytest.mark.parametrize("sentinel", ROSTER.values(), ids=ROSTER.keys())
@pytest.mark.parametrize("observation", ["roster"], indirect=True, scope="module")
def test_claude_agent_tools_roster(
    observation: AnthropicRequest, sentinel: str
) -> None:
    assert sentinel in observation.text("system")


@pytest.mark.capability_case("claude.agent-tools")
@pytest.mark.capability_live
def test_claude_refuses_agent_whose_tools_resolve_to_nothing(
    runtime: Runtime,
) -> None:
    runtime.responder.tool_call = (
        "Agent",
        {
            "description": "probe",
            "prompt": "Reply with OK.",
            "subagent_type": INHERIT_AGENT,
            # Background is the default, and it reports a successful launch and
            # defers the failure to a later notification. Foreground surfaces
            # the refusal in the tool result itself.
            "run_in_background": False,
        },
        TOOL_ID,
    )
    try:
        requests = invoke(runtime, "SPAWN_CONTROL_PROMPT", 2)
    finally:
        runtime.responder.tool_call = None

    result = requests[1].tool_result(TOOL_ID)
    assert result is not None, "Claude never returned a tool result for the Agent call"
    assert result.get("is_error") is True, f"expected an error result, got {result}"
    assert "zero tools" in requests[1].text("request")
