from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    cached_scenario_fixture,
    require_command,
    require_containment,
    run_probe,
    sanitized_env,
)
from capabilities.protocols.responses import ResponsesRequest, responses_done
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.codex import write_config

SKILL_NAME = "managed-loading-probe"
SKILL_DESCRIPTION = "Load the managed Codex black-box marker on explicit request."
SKILL_BODY = "MANAGED_SKILL_BODY_MARKER_7D37"
SKILL_ARGUMENT = "SKILL_ARGUMENT_MARKER_294A"
COMMAND_NAME = "managed-command-probe"
COMMAND_DESCRIPTION = (
    "Command wrapper for managed-command-probe. Do not auto-invoke this skill."
)
COMMAND_BODY = "COMMAND_WRAPPER_BODY_MARKER_91CF"
COMMAND_ARGUMENT = "COMMAND_ARGUMENT_MARKER_C61B"


@dataclass(frozen=True, slots=True)
class Scenario:
    prompt: str
    registration: str = "directory"
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Runtime:
    codex: str
    seatbelt: Seatbelt
    root: Path


SCENARIOS = {
    "listed": Scenario("Reply with probe complete."),
    "skill-explicit": Scenario(f"${SKILL_NAME} {SKILL_ARGUMENT}"),
    "command-unrelated": Scenario(
        "Reply with probe complete without invoking any skill."
    ),
    "command-explicit": Scenario(f"${COMMAND_NAME} {COMMAND_ARGUMENT}"),
    "skill-file-path": Scenario("Reply with probe complete.", "file"),
    "directory-disabled": Scenario("Reply with probe complete.", enabled=False),
    "skill-file-disabled": Scenario(
        "Reply with probe complete.", "file", enabled=False
    ),
}


def skill_document(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Runtime:
    codex, sandbox = require_command("codex"), require_command("sandbox-exec")
    root = tmp_path_factory.mktemp("codex-loading").resolve()
    paths = Paths.create(root / "containment")
    with RecordedServer.start(
        0,
        lambda _request, _count: (responses_done("containment"), "text/event-stream"),
    ) as stub:
        require_containment(
            loopback_seatbelt(sandbox),
            stub,
            paths,
            sanitized_env({"HOME": str(paths.home), "PATH": "/usr/bin:/bin"}),
        )
    return Runtime(codex, loopback_seatbelt(sandbox), root)


def invoke(runtime: Runtime, name: str) -> ResponsesRequest:
    scenario = SCENARIOS[name]
    paths = Paths.create(runtime.root / name)
    codex_home = paths.home / ".codex"
    codex_home.mkdir()
    skill_dir = codex_home / "skills" / SKILL_NAME
    command_dir = codex_home / "skills" / COMMAND_NAME
    skill_dir.mkdir(parents=True)
    command_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        skill_document(SKILL_NAME, SKILL_DESCRIPTION, SKILL_BODY)
    )
    (command_dir / "SKILL.md").write_text(
        skill_document(
            COMMAND_NAME,
            COMMAND_DESCRIPTION,
            f"{COMMAND_BODY}\nRun only when explicitly invoked.",
        )
    )
    stub = RecordedServer.start(
        0, lambda _request, _count: (responses_done(name), "text/event-stream")
    )
    registrations = [
        (
            directory
            if scenario.registration == "directory"
            else directory / "SKILL.md",
            scenario.enabled,
        )
        for directory in (skill_dir, command_dir)
    ]
    write_config(
        codex_home,
        paths.work,
        stub.base_url,
        approval="never",
        skills=registrations,
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
    try:
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
                str(paths.work),
                scenario.prompt,
            ),
            cwd=paths.work,
            env=env,
            timeout=20,
            stdin=-3,
        )
        assert result.returncode == 0, (
            f"Codex {name} failed\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        assert len(stub.requests) == 1
        return ResponsesRequest.decode(stub.requests[0])
    finally:
        stub.close()


observation = cached_scenario_fixture(invoke)


EXPECTATIONS = [
    pytest.param("listed", "developer", value, True, id=f"listed-{label}")
    for label, value in (
        ("skill-name", SKILL_NAME),
        ("skill-description", SKILL_DESCRIPTION),
        ("command-name", COMMAND_NAME),
        ("command-description", COMMAND_DESCRIPTION),
    )
] + [
    pytest.param(scenario, channel, value, present, id=f"{scenario}-{label}")
    for scenario, checks in {
        "listed": [
            ("skill-body-progressive", "request", SKILL_BODY, False),
            ("command-body-progressive", "request", COMMAND_BODY, False),
        ],
        "skill-explicit": [
            ("skill-body", "user", SKILL_BODY, True),
            ("skill-argument", "user", SKILL_ARGUMENT, True),
        ],
        "command-unrelated": [
            ("command-listed", "developer", COMMAND_NAME, True),
            ("command-body-progressive", "request", COMMAND_BODY, False),
        ],
        "command-explicit": [
            ("command-body", "user", COMMAND_BODY, True),
            ("command-argument", "user", COMMAND_ARGUMENT, True),
        ],
        "skill-file-path": [
            ("skill-name", "developer", SKILL_NAME, True),
            ("skill-description", "developer", SKILL_DESCRIPTION, True),
        ],
        "directory-disabled": [
            ("skill-name", "developer", SKILL_NAME, True),
            ("command-name", "developer", COMMAND_NAME, True),
        ],
        "skill-file-disabled": [
            ("skill-name", "request", SKILL_NAME, False),
            ("skill-description", "request", SKILL_DESCRIPTION, False),
            ("skill-body", "request", SKILL_BODY, False),
            ("command-name", "request", COMMAND_NAME, False),
            ("command-body", "request", COMMAND_BODY, False),
        ],
    }.items()
    for label, channel, value, present in checks
]


@pytest.mark.capability_case("codex.loading")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "channel", "sentinel", "present"),
    EXPECTATIONS,
    indirect=("observation",),
    scope="module",
)
def test_codex_loading(
    observation: ResponsesRequest, channel: str, sentinel: str, present: bool
) -> None:
    assert (sentinel in observation.text(channel)) is present
