from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

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

MODEL = "runtime-main"
SMALL_MODEL = "runtime-small"
AGENT = "runtime-agent"
AGENT_PROMPT = "OPENCODE_RUNTIME_AGENT_PROMPT_0e2c7f"
REFERENCE_DESCRIPTION = "OPENCODE_LOCAL_REFERENCE_DESCRIPTION_93b2ad"
ATTACHMENT_BODY = "OPENCODE_ATTACHMENT_BODY_529ac3"


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    state: ToolResponder
    env: dict[str, str]
    attachment: Path


def write_config(paths: Paths, base_url: str) -> Path:
    config = opencode_config(base_url, MODEL, "runtime", {"*": "allow"})
    provider = cast(dict[str, Any], config["provider"]["test"])
    main: dict[str, Any] = provider["models"][MODEL]
    main.update(
        {
            "attachment": True,
            "temperature": True,
            "modalities": {"input": ["text", "image"], "output": ["text"]},
            "variants": {"precise": {"temperature": 0.25}},
        }
    )
    provider["models"][SMALL_MODEL] = main | {"name": "Runtime Small Probe"}
    config.update(
        {
            "model": f"test/{MODEL}",
            "small_model": f"test/{SMALL_MODEL}",
            "enabled_providers": ["test"],
            "disabled_providers": ["disabled-probe"],
            "default_agent": AGENT,
            "subagent_depth": 2,
            "agent": {
                AGENT: {
                    "description": "Runtime agent fixture",
                    "mode": "primary",
                    "prompt": AGENT_PROMPT,
                    "model": f"test/{MODEL}",
                    "variant": "precise",
                    "steps": 5,
                    "permission": {"bash": "deny", "read": "allow"},
                }
            },
            "references": {
                "runtime-docs": {
                    "path": str(paths.root / "reference"),
                    "description": REFERENCE_DESCRIPTION,
                }
            },
        }
    )
    path = paths.root / "opencode.json"
    path.write_text(json.dumps(config))
    return path


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-runtime").resolve())
    (paths.work / ".git").mkdir()
    reference = paths.root / "reference"
    reference.mkdir()
    (reference / "README.md").write_text("reference fixture\n")
    attachment = paths.work / "attachment.txt"
    attachment.write_text(f"{ATTACHMENT_BODY}\n")
    state = ToolResponder(
        "skill",
        {},
        MODEL,
        "runtime_probe",
        "runtime probe complete",
        result_role="assistant",
        split_role=True,
        enabled=False,
    )
    stub = recorded_server(request, state.respond)
    config_path = write_config(paths, stub.base_url)
    env = environment(paths, config_path, "/usr/bin:/bin:/usr/sbin:/sbin")
    value = Runtime(
        executable,
        loopback_seatbelt(sandbox),
        paths,
        stub,
        state,
        env,
        attachment,
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
        "OpenCode runtime probe failed\n"
        f"stdout={process.stdout}\nstderr={process.stderr}"
    )
    return process


def model_requests(
    runtime: Runtime,
    *,
    title: bool = True,
    attachment: bool = False,
    agent: str | None = None,
) -> tuple[OpenCodeRequest, ...]:
    runtime.state.enabled = False
    runtime.stub.requests.clear()
    args = [
        "run",
        "Reply with the runtime probe complete.",
        "--pure",
        "--format",
        "json",
        "--model",
        f"test/{MODEL}",
        "--variant",
        "precise",
    ]
    if title:
        args += ["--title", "capability runtime probe"]
    if attachment:
        args += ["--file", str(runtime.attachment)]
    if agent:
        args += ["--agent", agent]
    execute(runtime, *args)
    return tuple(OpenCodeRequest.decode(request) for request in runtime.stub.requests)


def observe(runtime: Runtime, name: str) -> CheckResult:
    if name == "provider-model":
        process = supported(
            "the configured model catalog",
            run,
            *runtime.seatbelt.command(runtime.executable, "models", "test", "--pure"),
            cwd=runtime.paths.work,
            env=runtime.env,
            timeout=30,
        )
        requests = model_requests(runtime)
        request = requests[0]
        return CheckResult(
            {
                "provider-listed": all(
                    value in process.stdout
                    for value in (f"test/{MODEL}", f"test/{SMALL_MODEL}")
                ),
                "selected-model": request.raw["model"] == MODEL,
                "selected-variant": request.raw.get("temperature") == 0.25,
                "provider-reached": len(requests) == 1,
            },
            process.stdout + "\n" + json.dumps(request.raw, sort_keys=True),
        )
    if name == "small-model":
        requests = model_requests(runtime, title=False)
        return CheckResult(
            {
                "small-model-used": any(
                    request.raw["model"] == SMALL_MODEL for request in requests
                )
            },
            json.dumps([request.raw["model"] for request in requests]),
        )
    if name == "agent":
        listing = supported(
            "the agent catalog",
            run,
            *runtime.seatbelt.command(runtime.executable, "agent", "list", "--pure"),
            cwd=runtime.paths.work,
            env=runtime.env,
            timeout=30,
        )
        inspected = supported(
            "the `debug agent` inspector",
            run,
            *runtime.seatbelt.command(
                runtime.executable, "debug", "agent", AGENT, "--pure"
            ),
            cwd=runtime.paths.work,
            env=runtime.env,
            timeout=30,
        )
        agent = json.loads(inspected.stdout)
        request = model_requests(runtime)[0]
        return CheckResult(
            {
                "agent-listed": AGENT in listing.stdout,
                "default-agent-selected": AGENT_PROMPT in request.text("system"),
                "agent-model": agent["model"]
                == {"providerID": "test", "modelID": MODEL},
                "agent-variant": agent["variant"] == "precise",
                "agent-steps": agent["steps"] == 5,
                "tool-disabled": agent["tools"]["bash"] is False,
                "tool-enabled": agent["tools"]["read"] is True,
            },
            inspected.stdout,
        )
    if name == "reference":
        request = model_requests(runtime)[0]
        return CheckResult(
            {
                "local-reference-visible": all(
                    value in request.text("system")
                    for value in (
                        "runtime-docs",
                        str(runtime.paths.root / "reference"),
                        REFERENCE_DESCRIPTION,
                    )
                )
            },
            request.text("system"),
        )
    request = model_requests(runtime, attachment=True, agent=AGENT)[0]
    return CheckResult(
        {"attachment-visible": ATTACHMENT_BODY in request.text("user")},
        request.text("user"),
    )


observation = cached_scenario_fixture(observe)


@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check"),
    (
        pytest.param(
            "provider-model",
            "provider-listed",
            marks=pytest.mark.capability_case("opencode.models-runtime"),
        ),
        pytest.param(
            "provider-model",
            "selected-model",
            marks=pytest.mark.capability_case("opencode.model-selection-runtime"),
        ),
        pytest.param(
            "provider-model",
            "selected-variant",
            marks=pytest.mark.capability_case("opencode.model-variants-runtime"),
        ),
        pytest.param(
            "provider-model",
            "provider-reached",
            marks=pytest.mark.capability_case("opencode.providers-runtime"),
        ),
        pytest.param(
            "small-model",
            "small-model-used",
            marks=pytest.mark.capability_case("opencode.small-model-runtime"),
        ),
    ),
    indirect=("observation",),
    scope="module",
)
def test_opencode_provider_model_runtime(observation: CheckResult, check: str) -> None:
    assert observation.checks[check], observation.detail


@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check"),
    tuple(
        pytest.param(
            "agent",
            check,
            id=check,
            marks=pytest.mark.capability_case(case_id),
        )
        for case_id, check in (
            ("opencode.agent-selection-runtime", "agent-listed"),
            ("opencode.agent-selection-runtime", "default-agent-selected"),
            ("opencode.agent-config-runtime", "agent-model"),
            ("opencode.agent-config-runtime", "agent-variant"),
            ("opencode.agent-config-runtime", "agent-steps"),
            ("opencode.tool-enablement-runtime", "tool-disabled"),
            ("opencode.tool-enablement-runtime", "tool-enabled"),
        )
    ),
    indirect=("observation",),
    scope="module",
)
def test_opencode_agent_runtime(observation: CheckResult, check: str) -> None:
    assert observation.checks[check], observation.detail


@pytest.mark.capability_case("opencode.references-local")
@pytest.mark.capability_live
def test_opencode_local_reference(runtime: Runtime) -> None:
    result = observe(runtime, "reference")
    assert result.checks["local-reference-visible"], result.detail


@pytest.mark.capability_case("opencode.attachments")
@pytest.mark.capability_live
def test_opencode_attachment(runtime: Runtime) -> None:
    result = observe(runtime, "attachment")
    assert result.checks["attachment-visible"], result.detail
