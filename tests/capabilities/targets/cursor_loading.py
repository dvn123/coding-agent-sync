from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    cached_scenario_fixture,
    recorded_server,
    require_containment,
    run_probe,
)
from capabilities.protocols.cursor import CursorRequest
from capabilities.protocols.openai import openai_sse
from capabilities.runtime import Seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.cursor import (
    command as cursor_command,
)
from capabilities.targets.cursor import (
    environment,
)
from capabilities.targets.cursor import (
    runtime as cursor_runtime,
)

MODEL = "loading-probe"
SKILL_DESCRIPTION = "CURSOR_AUTO_SKILL_DESCRIPTION_MARKER"
SKILL_BODY = "CURSOR_AUTO_SKILL_BODY_MARKER"
MANUAL_DESCRIPTION = "CURSOR_MANUAL_SKILL_DESCRIPTION_MARKER"
MANUAL_BODY = "CURSOR_MANUAL_SKILL_BODY_MARKER"
COMMAND_BODY = "CURSOR_LEGACY_COMMAND_BODY_MARKER"
COLLIDING_COMMAND_BODY = "CURSOR_COLLIDING_COMMAND_BODY_MARKER"
USER_RULE_BODY = "CURSOR_USER_RULE_BODY_MARKER"


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    env: dict[str, str]


def write_skill(
    path: Path, name: str, description: str, body: str, *, manual: bool = False
) -> None:
    path.mkdir(parents=True)
    extra = "disable-model-invocation: true\n" if manual else ""
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n"
    )


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, seatbelt = cursor_runtime()
    paths = Paths.create(tmp_path_factory.mktemp("cursor-loading").resolve())
    work = paths.home / "work"
    work.mkdir()
    paths = replace(paths, work=work)
    cursor = paths.home / ".cursor"
    cursor.mkdir()
    (cursor / "cli-config.json").write_text(
        json.dumps({"version": 1, "approvalMode": "allowlist"})
    )
    write_skill(
        cursor / "skills" / "probe-skill",
        "probe-skill",
        SKILL_DESCRIPTION,
        f"{SKILL_BODY}\nArguments: $ARGUMENTS",
    )
    write_skill(
        cursor / "skills" / "manual-skill",
        "manual-skill",
        MANUAL_DESCRIPTION,
        f"{MANUAL_BODY}\nArguments: $ARGUMENTS",
        manual=True,
    )
    commands = cursor / "commands"
    commands.mkdir()
    (commands / "probe-command.md").write_text(
        f"---\ndescription: legacy command probe\n---\n{COMMAND_BODY}\n$ARGUMENTS\n"
    )
    (commands / "probe-skill.md").write_text(
        f"---\ndescription: collision probe\n---\n{COLLIDING_COMMAND_BODY}\n"
    )
    (cursor / "rules").mkdir()
    (cursor / "rules" / "always.mdc").write_text(
        "---\n"
        "alwaysApply: true\n"
        "description: Cursor user rule probe.\n"
        "globs: ''\n"
        "---\n"
        f"{USER_RULE_BODY}\n"
    )
    stub = recorded_server(
        request,
        lambda _request, _count: (
            openai_sse(
                [
                    ({"role": "assistant", "content": "done"}, None),
                    ({}, "stop"),
                ],
                MODEL,
            ),
            "text/event-stream",
        ),
    )
    env = environment(paths, cursor)
    value = Runtime(executable, seatbelt, paths, stub, env)
    require_containment(
        value.seatbelt,
        stub,
        paths,
        env,
    )
    return value


def invoke(runtime: Runtime, prompt: str) -> CursorRequest:
    runtime.stub.requests.clear()
    result = run_probe(
        run,
        *runtime.seatbelt.command(
            *cursor_command(runtime.executable, runtime.stub.base_url, MODEL, prompt)
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert len(runtime.stub.requests) == 1
    return CursorRequest.decode(runtime.stub.requests[0])


PROMPTS = {
    "discovery": "Inspect available capabilities.",
    "skill": "/probe-skill alpha beta",
    "manual": "/manual-skill gamma delta",
    "command": "/probe-command epsilon zeta",
}


def observe(runtime: Runtime, name: str) -> CursorRequest:
    return invoke(runtime, PROMPTS[name])


observation = cached_scenario_fixture(observe)


EXPECTATIONS = [
    pytest.param(scenario, "user", value, present, id=f"{scenario}-{label}")
    for scenario, checks in {
        "discovery": [
            ("description", SKILL_DESCRIPTION, True),
            ("body-progressive", SKILL_BODY, False),
            ("manual-hidden", MANUAL_DESCRIPTION, False),
            ("command-progressive", COMMAND_BODY, False),
        ],
        "skill": [
            ("body", SKILL_BODY, True),
            ("arguments", "alpha beta", True),
            ("collision", COLLIDING_COMMAND_BODY, True),
        ],
        "manual": [
            ("body", MANUAL_BODY, True),
            ("arguments", "gamma delta", True),
        ],
        "command": [
            ("body", COMMAND_BODY, True),
            ("arguments", "epsilon zeta", True),
        ],
    }.items()
    for label, value, present in checks
]
EXPECTATIONS.append(
    pytest.param(
        "discovery",
        "request",
        USER_RULE_BODY,
        True,
        id="discovery-user-rule",
    )
)


@pytest.mark.capability_case("cursor.loading")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "channel", "sentinel", "present"),
    EXPECTATIONS,
    indirect=("observation",),
    scope="module",
)
def test_cursor_loading(
    observation: CursorRequest, channel: str, sentinel: str, present: bool
) -> None:
    text = (
        json.dumps(observation.raw)
        if channel == "request"
        else observation.text(channel)
    )
    assert (sentinel in text) is present
