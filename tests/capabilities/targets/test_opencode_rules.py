from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from capabilities.protocols.opencode import OpenCodeRequest
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.opencode import config as opencode_config
from capabilities.targets.opencode import environment, supported
from coding_agents_sync import run_sync

GLOBAL_INSTRUCTIONS = "OPENCODE_GLOBAL_AGENTS_SENTINEL"
PROJECT_INSTRUCTIONS = "OPENCODE_PROJECT_AGENTS_SENTINEL"
CONFIG_INSTRUCTIONS = "OPENCODE_CONFIG_INSTRUCTIONS_SENTINEL"
FILE_AGENT = "file-agent-probe"
FILE_AGENT_DESCRIPTION = "OPENCODE_FILE_AGENT_DESCRIPTION"
FILE_AGENT_BODY = "OPENCODE_FILE_AGENT_BODY"
COMPILER_GLOBAL = "OPENCODE_COMPILER_GLOBAL"
COMPILER_RULE = "OPENCODE_COMPILER_RULE"
COMPILER_AGENT = "opencode-compiler-agent"


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    state: ToolResponder
    env: dict[str, str]
    config_path: Path


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def source_document(
    kind: str, identifier: str, body: str, description: str = ""
) -> str:
    return (
        "---\n"
        "schema: coding-agents/v3\n"
        f"kind: {kind}\n"
        f"id: {identifier}\n"
        f"name: {identifier}\n"
        f"description: {description}\n"
        "---\n"
        f"{body}\n"
    )


def compiler_sources(root: Path) -> None:
    (root / "global").mkdir(parents=True)
    (root / "rules").mkdir()
    (root / "agents").mkdir()
    (root / "global" / "AGENTS.md").write_text(
        source_document("global", "global", COMPILER_GLOBAL)
    )
    (root / "rules" / "rule.md").write_text(
        source_document("rule", "rule", COMPILER_RULE)
    )
    (root / "agents" / f"{COMPILER_AGENT}.md").write_text(
        source_document("agent", COMPILER_AGENT, "compiler agent", "compiler agent")
    )


def base_config(base_url: str, model: str) -> dict[str, Any]:
    return opencode_config(base_url, model, "rules", {"*": "allow"})


def write_fixtures(paths: Paths, base_url: str) -> Path:
    (paths.work / ".git").mkdir()
    config_path = paths.root / "opencode.json"
    config = base_config(base_url, "rules-probe") | {"instructions": ["IGNORED.md"]}
    write_json(config_path, config)
    global_agents = paths.config / "opencode" / "AGENTS.md"
    global_agents.parent.mkdir(parents=True)
    global_agents.write_text(f"{GLOBAL_INSTRUCTIONS}\n")
    (paths.work / "AGENTS.md").write_text(f"{PROJECT_INSTRUCTIONS}\n")
    (paths.work / "IGNORED.md").write_text(f"{CONFIG_INSTRUCTIONS}\n")
    agent = paths.config / "opencode" / "agents" / f"{FILE_AGENT}.md"
    agent.parent.mkdir(parents=True)
    agent.write_text(
        "---\n"
        f"description: {FILE_AGENT_DESCRIPTION}\n"
        "mode: subagent\n"
        "---\n"
        f"{FILE_AGENT_BODY}\n"
    )
    return config_path


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-rules").resolve())
    state = ToolResponder(
        "skill",
        {},
        "rules-probe",
        "call_rules_probe",
        "rules probe complete",
        result_role="assistant",
        split_role=True,
        enabled=False,
    )
    stub = recorded_server(request, state.respond)
    config_path = write_fixtures(paths, stub.base_url)
    env = environment(paths, config_path, "/usr/bin:/bin:/usr/sbin:/sbin")
    value = Runtime(
        executable,
        loopback_seatbelt(sandbox),
        paths,
        stub,
        state,
        env,
        config_path,
    )
    require_containment(value.seatbelt, stub, paths, env)
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
        f"OpenCode rules probe failed\nstdout={process.stdout}\nstderr={process.stderr}"
    )
    return process


def root_request(runtime: Runtime) -> OpenCodeRequest:
    runtime.state.enabled = False
    runtime.stub.requests.clear()
    execute(
        runtime,
        "run",
        "Reply with the rules probe complete.",
        "--title",
        "capability rules probe",
        "--pure",
        "--format",
        "json",
        "--model",
        "test/rules-probe",
    )
    assert len(runtime.stub.requests) == 1
    return OpenCodeRequest.decode(runtime.stub.requests[0])


def compiler(runtime: Runtime) -> CheckResult:
    paths = Paths.create(runtime.paths.root / "compiler")
    sources = paths.root / "sources"
    compiler_sources(sources)
    config_path = paths.home / ".config" / "opencode" / "opencode.json"
    write_json(config_path, base_config(runtime.stub.base_url, "rules-probe"))
    run_sync(config_root=sources, home=paths.home)
    instructions_path = paths.home / ".config" / "opencode" / "AGENTS.md"
    agent_path = paths.home / ".config" / "opencode" / "agents" / f"{COMPILER_AGENT}.md"
    config = json.loads(config_path.read_text())
    compiled_runtime = Runtime(
        runtime.executable,
        runtime.seatbelt,
        paths,
        runtime.stub,
        runtime.state,
        environment(paths, config_path, "/usr/bin:/bin:/usr/sbin:/sbin"),
        config_path,
    )
    compiled_runtime.state.enabled = False
    compiled_runtime.stub.requests.clear()
    process = run_probe(
        run,
        *compiled_runtime.seatbelt.command(
            compiled_runtime.executable,
            "run",
            "Reply with the rules probe complete.",
            "--title",
            "capability compiler probe",
            "--pure",
            "--format",
            "json",
            "--model",
            "test/rules-probe",
        ),
        cwd=compiled_runtime.paths.work,
        env=compiled_runtime.env,
        timeout=30,
    )
    request = (
        OpenCodeRequest.decode(compiled_runtime.stub.requests[-1])
        if compiled_runtime.stub.requests
        else None
    )
    return CheckResult(
        {
            "compiler-files": all(
                path.is_file() for path in (instructions_path, agent_path, config_path)
            ),
            "compiler-content": COMPILER_GLOBAL in instructions_path.read_text()
            and str(sources / "rules" / "rule.md") in config["instructions"],
            "compiler-runtime": process.returncode == 0
            and request is not None
            and all(
                value in request.text() for value in (COMPILER_GLOBAL, COMPILER_RULE)
            ),
        },
        process.stderr or process.stdout,
    )


def observe(runtime: Runtime, name: str) -> CheckResult:
    if name == "compiler":
        return compiler(runtime)
    if name == "agents":
        process = supported(
            "the `debug agent` inspector",
            run,
            *runtime.seatbelt.command(
                runtime.executable, "debug", "agent", FILE_AGENT, "--pure"
            ),
            cwd=runtime.paths.work,
            env=runtime.env,
            timeout=30,
        )
        return CheckResult(
            {
                "agent-file-discovered": all(
                    value in process.stdout
                    for value in (FILE_AGENT, FILE_AGENT_DESCRIPTION, FILE_AGENT_BODY)
                )
            },
            process.stdout,
        )
    payload = root_request(runtime).text()
    return CheckResult(
        {
            "global-agents-visible": GLOBAL_INSTRUCTIONS in payload,
            "project-agents-visible": PROJECT_INSTRUCTIONS in payload,
            "config-instructions-visible": CONFIG_INSTRUCTIONS in payload,
            "agents-order": payload.find(GLOBAL_INSTRUCTIONS)
            < payload.find(PROJECT_INSTRUCTIONS),
        },
        payload,
    )


observation = cached_scenario_fixture(observe)


@pytest.mark.capability_case("opencode.instructions")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check"),
    tuple(
        pytest.param("instructions", check)
        for check in (
            "global-agents-visible",
            "project-agents-visible",
            "config-instructions-visible",
            "agents-order",
        )
    ),
    indirect=("observation",),
    scope="module",
)
def test_opencode_instructions(observation: CheckResult, check: str) -> None:
    assert observation.checks[check], observation.detail


@pytest.mark.capability_case("opencode.agents")
@pytest.mark.capability_live
def test_opencode_agents(runtime: Runtime) -> None:
    result = observe(runtime, "agents")
    assert result.checks["agent-file-discovered"], result.detail


@pytest.mark.capability_case("opencode.compiler")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check"),
    tuple(
        pytest.param("compiler", check)
        for check in ("compiler-files", "compiler-content", "compiler-runtime")
    ),
    indirect=("observation",),
    scope="module",
)
def test_opencode_compiler(observation: CheckResult, check: str) -> None:
    assert observation.checks[check], observation.detail
