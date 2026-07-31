from __future__ import annotations

import json
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
from capabilities.model import CheckResult
from capabilities.protocols.openai import ToolResponder
from capabilities.protocols.opencode import (
    OpenCodeEvent,
    OpenCodeRequest,
    ToolUseObservation,
    decode_skill_catalog,
)
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.opencode import config as opencode_config
from capabilities.targets.opencode import environment, supported

SKILL_NAME = "blackbox-loading-probe"
SKILL_DESCRIPTION = "Discover the black-box OpenCode loading marker."
SKILL_BODY = "OPEN_CODE_SKILL_BODY_7f43c1"
DENIED_SKILL = "blackbox-denied-probe"
DENIED_BODY = "OPEN_CODE_DENIED_SKILL_BODY_2d95a8"
COMMAND_NAME = "blackbox-command-probe"
COMMAND_BODY = "OPEN_CODE_COMMAND_BODY_63a70e"


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    state: ToolResponder
    env: dict[str, str]


def write_fixtures(config_home: Path) -> None:
    skills = config_home / "opencode" / "skills"
    commands = config_home / "opencode" / "commands"
    (skills / SKILL_NAME).mkdir(parents=True)
    (skills / DENIED_SKILL).mkdir()
    commands.mkdir()
    (skills / SKILL_NAME / "SKILL.md").write_text(
        f"---\nname: {SKILL_NAME}\ndescription: {SKILL_DESCRIPTION}\n---\n"
        f"{SKILL_BODY}\n"
    )
    (skills / DENIED_SKILL / "SKILL.md").write_text(
        f"---\nname: {DENIED_SKILL}\ndescription: denied fixture\n---\n{DENIED_BODY}\n"
    )
    (commands / f"{COMMAND_NAME}.md").write_text(
        f"---\ndescription: command fixture\n---\n{COMMAND_BODY}\n"
        "all-arguments=[$ARGUMENTS]\nfirst-argument=[$1]\n"
    )


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, rg, sandbox = (
        require_command("opencode"),
        require_command("rg", "OpenCode native skill loader requires rg"),
        require_command("sandbox-exec"),
    )
    paths = Paths.create(tmp_path_factory.mktemp("opencode-loading").resolve())
    state = ToolResponder(
        "skill",
        {},
        "loading-probe",
        "call_loading_probe",
        "loading probe complete",
        result_role="assistant",
        split_role=True,
        enabled=False,
    )
    stub = recorded_server(request, state.respond)
    config = paths.root / "opencode.json"
    config.write_text(
        json.dumps(
            opencode_config(
                stub.base_url,
                "loading-probe",
                "loading",
                {"skill": {"*": "allow", DENIED_SKILL: "deny"}},
            )
        )
    )
    write_fixtures(paths.config)
    env = environment(
        paths,
        config,
        f"{Path(rg).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
    )
    value = Runtime(executable, loopback_seatbelt(sandbox), paths, stub, state, env)
    require_containment(
        value.seatbelt,
        stub,
        paths,
        env,
    )
    return value


def execute(runtime: Runtime, *arguments: str):
    process = run_probe(
        run,
        *runtime.seatbelt.command(runtime.executable, *arguments),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
    )
    assert process.returncode == 0, (
        f"OpenCode loading probe failed\nstdout={process.stdout}\n"
        f"stderr={process.stderr}"
    )
    return process


def find_event(
    events: tuple[OpenCodeEvent, ...], skill: str
) -> ToolUseObservation | None:
    return next(
        (
            observation
            for event in events
            if (observation := event.tool_use()) is not None
            and observation.tool == "skill"
            and observation.input.get("name") == skill
        ),
        None,
    )


def model_case(
    runtime: Runtime,
    requested_skill: str | None = None,
    *,
    command: bool = False,
) -> tuple[tuple[OpenCodeRequest, ...], tuple[OpenCodeEvent, ...]]:
    runtime.state.enabled = requested_skill is not None
    runtime.state.arguments = {"name": requested_skill}
    runtime.stub.requests.clear()
    args = ["run"]
    if command:
        args += ["--command", COMMAND_NAME, "alpha"]
    else:
        args.append("Call the skill tool exactly as instructed by the model.")
    args += [
        "--title",
        "capability loading probe",
        "--pure",
        "--format",
        "json",
        "--model",
        "test/loading-probe",
    ]
    process = execute(runtime, *args)
    return (
        tuple(OpenCodeRequest.decode(request) for request in runtime.stub.requests),
        OpenCodeEvent.decode_lines(process.stdout),
    )


def observe(runtime: Runtime, name: str) -> CheckResult:
    if name == "catalog":
        process = supported(
            "the `debug skill` inspector",
            run,
            *runtime.seatbelt.command(runtime.executable, "debug", "skill", "--pure"),
            cwd=runtime.paths.work,
            env=runtime.env,
            timeout=30,
        )
        catalog = decode_skill_catalog(process.stdout)
        return CheckResult(
            {
                "catalog-name": SKILL_NAME in catalog.text("skill_catalog"),
                "catalog-description": SKILL_DESCRIPTION
                in catalog.text("skill_catalog"),
            },
            catalog.text("skill_catalog"),
        )
    if name == "command":
        requests, _ = model_case(runtime, command=True)
        command_user = requests[0].text("user")
        return CheckResult(
            {
                "command-body": COMMAND_BODY in command_user,
                "command-all-arguments": "all-arguments=[alpha]" in command_user,
                "command-first-argument": "first-argument=[alpha]" in command_user,
                "command-no-placeholder": all(
                    placeholder not in command_user
                    for placeholder in ("$ARGUMENTS", "$1")
                ),
            },
            command_user,
        )

    skill = SKILL_NAME if name == "allowed" else DENIED_SKILL
    requests, events = model_case(runtime, skill)
    event = find_event(events, skill)
    initial = requests[0]
    checks = (
        {
            "tool-advertised": "skill" in initial.text("tools"),
            "allowed-metadata-visible": all(
                value in initial.text("system")
                for value in (SKILL_NAME, SKILL_DESCRIPTION)
            ),
            "denied-metadata-hidden": DENIED_SKILL not in initial.text(),
            "loaded-status": event is not None and event.status == "completed",
            "loaded-body-event": event is not None and SKILL_BODY in str(event.output),
            "loaded-body-model": any(
                SKILL_BODY in request.text("tool") for request in requests[1:]
            ),
        }
        if name == "allowed"
        else {
            "denied-status": event is not None and event.status == "error",
            "denied-body-hidden": all(
                DENIED_BODY not in request.text() for request in requests
            ),
        }
    )
    return CheckResult(checks, str(event))


observation = cached_scenario_fixture(observe)


EXPECTATIONS = (
    ("catalog", "catalog-name"),
    ("catalog", "catalog-description"),
    ("allowed", "tool-advertised"),
    ("allowed", "allowed-metadata-visible"),
    ("allowed", "denied-metadata-hidden"),
    ("allowed", "loaded-status"),
    ("allowed", "loaded-body-event"),
    ("allowed", "loaded-body-model"),
    ("denied", "denied-status"),
    ("denied", "denied-body-hidden"),
    ("command", "command-body"),
    ("command", "command-all-arguments"),
    ("command", "command-first-argument"),
    ("command", "command-no-placeholder"),
)


@pytest.mark.capability_case("opencode.loading")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check"),
    tuple(pytest.param(scenario, check, id=check) for scenario, check in EXPECTATIONS),
    indirect=("observation",),
    scope="module",
)
def test_opencode_loading(observation: CheckResult, check: str) -> None:
    assert observation.checks[check], observation.detail
