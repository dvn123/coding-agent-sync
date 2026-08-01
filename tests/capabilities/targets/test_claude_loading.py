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
from capabilities.protocols.anthropic import (
    AnthropicRequest,
    AnthropicResponder,
)
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.claude import environment

VISIBLE_SKILL = "visible-loading-probe"
EXPLICIT_SKILL = "explicit-loading-probe"
LEGACY_COMMAND = "legacy-loading-probe"
VISIBLE_DESCRIPTION = "VISIBLE_SKILL_DESCRIPTION_SENTINEL"
VISIBLE_BODY = "VISIBLE_SKILL_BODY_SENTINEL"
EXPLICIT_DESCRIPTION = "EXPLICIT_SKILL_DESCRIPTION_SENTINEL"
EXPLICIT_BODY = "EXPLICIT_SKILL_BODY_SENTINEL"
LEGACY_DESCRIPTION = "LEGACY_COMMAND_DESCRIPTION_SENTINEL"
LEGACY_BODY = "LEGACY_COMMAND_BODY_SENTINEL"
CLAUDE_FLAGS = (
    "--print",
    "--output-format",
    "stream-json",
    "--verbose",
    "--no-session-persistence",
    "--setting-sources",
    "user",
    "--tools",
    "Skill",
)


@dataclass(frozen=True, slots=True)
class Runtime:
    claude: str
    seatbelt: Seatbelt
    stub: RecordedServer
    paths: Paths
    env: dict[str, str]


def write_fixtures(config: Path) -> None:
    visible = config / "skills" / VISIBLE_SKILL
    explicit = config / "skills" / EXPLICIT_SKILL
    commands = config / "commands"
    for directory in (visible, explicit, commands):
        directory.mkdir(parents=True)
    (visible / "SKILL.md").write_text(
        f"---\nname: {VISIBLE_SKILL}\ndescription: {VISIBLE_DESCRIPTION}\n"
        f"---\n{VISIBLE_BODY}\n"
    )
    (explicit / "SKILL.md").write_text(
        f"""---
name: {EXPLICIT_SKILL}
description: {EXPLICIT_DESCRIPTION}
arguments: [first, second]
disable-model-invocation: true
---
{EXPLICIT_BODY}
raw=<$ARGUMENTS>
indexed0=<$ARGUMENTS[0]>
short1=<$1>
named-first=<$first>
named-second=<$second>
"""
    )
    (commands / f"{LEGACY_COMMAND}.md").write_text(
        f"---\ndescription: {LEGACY_DESCRIPTION}\n---\n{LEGACY_BODY}\n"
        "raw=<$ARGUMENTS>\nindexed0=<$ARGUMENTS[0]>\nshort1=<$1>\n"
    )


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    claude, sandbox = require_command("claude"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("claude-loading").resolve())
    write_fixtures(paths.config)
    stub = recorded_server(request, AnthropicResponder().respond)
    value = Runtime(
        claude,
        loopback_seatbelt(sandbox),
        stub,
        paths,
        environment(
            paths,
            stub.base_url,
            XDG_DATA_HOME=str(paths.data),
            XDG_CACHE_HOME=str(paths.cache),
        ),
    )
    require_containment(
        value.seatbelt,
        stub,
        paths,
        value.env,
    )
    return value


def invoke(runtime: Runtime, prompt: str) -> AnthropicRequest:
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
    assert len(requests) == 1, f"Claude sent {len(requests)} requests for {prompt!r}"
    return AnthropicRequest.decode(requests[0])


SCENARIOS = {
    "discovery": (
        "DISCOVERY_CONTROL_PROMPT",
        {
            "visible-name": ("system", VISIBLE_SKILL),
            "visible-description": ("system", VISIBLE_DESCRIPTION),
            "legacy-name": ("system", LEGACY_COMMAND),
            "legacy-description": ("system", LEGACY_DESCRIPTION),
        },
        {
            "visible-body-hidden": ("request", VISIBLE_BODY),
            "explicit-name-hidden": ("request", EXPLICIT_SKILL),
            "explicit-description-hidden": ("request", EXPLICIT_DESCRIPTION),
            "explicit-body-hidden": ("request", EXPLICIT_BODY),
            "legacy-body-hidden": ("request", LEGACY_BODY),
        },
    ),
    "explicit": (
        f'/{EXPLICIT_SKILL} "alpha beta" gamma',
        {
            "body": ("user", EXPLICIT_BODY),
            "raw": ("user", 'raw=<"alpha beta" gamma>'),
            "indexed": ("user", "indexed0=<alpha beta>"),
            "numeric": ("user", "short1=<gamma>"),
            "named-first": ("user", "named-first=<alpha beta>"),
            "named-second": ("user", "named-second=<gamma>"),
        },
        {
            "no-arguments": ("request", "$ARGUMENTS"),
            "no-indexed": ("request", "$ARGUMENTS[0]"),
            "no-numeric": ("request", "$1"),
            "no-first": ("request", "$first"),
            "no-second": ("request", "$second"),
        },
    ),
    "legacy": (
        f'/{LEGACY_COMMAND} one "two words"',
        {
            "body": ("user", LEGACY_BODY),
            "raw": ("user", 'raw=<one "two words">'),
            "indexed": ("user", "indexed0=<one>"),
            "numeric": ("user", "short1=<two words>"),
        },
        {
            "no-arguments": ("request", "$ARGUMENTS"),
            "no-indexed": ("request", "$ARGUMENTS[0]"),
            "no-numeric": ("request", "$1"),
        },
    ),
}


def observe(runtime: Runtime, name: str) -> AnthropicRequest:
    return invoke(runtime, SCENARIOS[name][0])


observation = cached_scenario_fixture(observe)


EXPECTATIONS = [
    pytest.param(name, channel, sentinel, present, id=f"{name}-{label}")
    for name, (_, expected, forbidden) in SCENARIOS.items()
    for present, checks in ((True, expected), (False, forbidden))
    for label, (channel, sentinel) in checks.items()
]


@pytest.mark.capability_case("claude.loading")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "channel", "sentinel", "present"),
    EXPECTATIONS,
    indirect=("observation",),
    scope="module",
)
def test_claude_loading(
    observation: AnthropicRequest, channel: str, sentinel: str, present: bool
) -> None:
    assert (sentinel in observation.text(channel)) is present
