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
from capabilities.protocols.opencode import OpenCodeRequest
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.opencode import (
    api,
    environment,
    isolate_service,
    settled,
)

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
    # Native 2.x shapes throughout: a provider or model entry cannot mix 1.x
    # and 2.x fields, and a 1.x variant's options become provider settings
    # rather than request body fields.
    model = {
        "name": "Runtime Probe",
        "capabilities": {"tools": True, "input": ["text", "image"], "output": ["text"]},
        "limit": {"context": 100000, "output": 1000},
        "variants": [{"id": "precise", "body": {"temperature": 0.25}}],
    }
    config = {
        "$schema": "https://opencode.ai/config.json",
        "update": "disable",
        "model": f"test/{MODEL}",
        "default_agent": AGENT,
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}],
        "providers": {
            "test": {
                "name": "Local runtime probe",
                "package": "aisdk:@ai-sdk/openai-compatible",
                "env": [],
                "settings": {
                    "apiKey": "not-a-credential",
                    "baseURL": f"{base_url}/v1",
                },
                "models": {
                    MODEL: model,
                    SMALL_MODEL: model | {"name": "Runtime Small Probe"},
                },
            }
        },
        # 1.x `enabled_providers` and `disabled_providers` are these policies.
        "experimental": {
            "policies": [
                {"action": "provider.use", "resource": "*", "effect": "deny"},
                {"action": "provider.use", "resource": "test", "effect": "allow"},
                {
                    "action": "provider.use",
                    "resource": "disabled-probe",
                    "effect": "deny",
                },
            ]
        },
        "agents": {
            # 1.x `small_model` is the built-in title agent's model.
            "title": {"model": f"test/{SMALL_MODEL}"},
            AGENT: {
                "description": "Runtime agent fixture",
                "mode": "primary",
                "system": AGENT_PROMPT,
                "model": f"test/{MODEL}#precise",
                "steps": 5,
                "permissions": [
                    {"action": "shell", "resource": "*", "effect": "deny"},
                    {"action": "read", "resource": "*", "effect": "allow"},
                ],
            },
        },
        "references": {
            "runtime-docs": {
                "path": str(paths.root / "reference"),
                "description": REFERENCE_DESCRIPTION,
            }
        },
    }
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
    isolate_service(request, value)
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
        "--standalone",
        "--format",
        "json",
        # 2.x folds the variant into the model reference.
        "--model",
        f"test/{MODEL}#precise",
    ]
    if title:
        args += ["--title", "capability runtime probe"]
    if attachment:
        args += ["--file", str(runtime.attachment)]
    if agent:
        args += ["--agent", agent]
    execute(runtime, *args)
    return tuple(OpenCodeRequest.decode(request) for request in runtime.stub.requests)


def tool_names(request: OpenCodeRequest) -> set[str]:
    return {tool["function"]["name"] for tool in request.raw.get("tools", [])}


def observe(runtime: Runtime, name: str) -> CheckResult:
    if name == "provider-model":
        expected = {MODEL, SMALL_MODEL}
        catalog = settled(
            runtime,
            api("get", "/api/model", runtime.paths.work),
            lambda value: (
                expected
                <= {
                    item["id"] for item in value["data"] if item["providerID"] == "test"
                }
            ),
        )["data"]
        requests = model_requests(runtime)
        request = requests[0]
        return CheckResult(
            {
                "provider-listed": expected
                <= {item["id"] for item in catalog if item["providerID"] == "test"},
                "selected-model": request.raw["model"] == MODEL,
                "selected-variant": request.raw.get("temperature") == 0.25,
                "provider-reached": len(requests) == 1,
            },
            json.dumps(catalog) + "\n" + json.dumps(request.raw, sort_keys=True),
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
        # 2.x has no `agent list` or `debug agent <name>`; `debug agents`
        # reports each agent with its resolved model and ordered permissions.
        agents = settled(
            runtime,
            ("debug", "agents"),
            lambda value: any(item["id"] == AGENT for item in value),
        )
        agent = next((item for item in agents if item["id"] == AGENT), {})
        model = agent.get("model") or {}
        request = model_requests(runtime)[0]
        # 2.x has no per-agent tool map: a wholly denied action removes its
        # tool from the model request instead.
        tools = tool_names(request)
        return CheckResult(
            {
                "agent-listed": bool(agent),
                "default-agent-selected": AGENT_PROMPT in request.text("system"),
                "agent-model": (model.get("providerID"), model.get("id"))
                == ("test", MODEL),
                "agent-variant": model.get("variant") == "precise",
                "agent-steps": agent.get("steps") == 5,
                "tool-disabled": "shell" not in tools,
                "tool-enabled": "read" in tools,
            },
            json.dumps(agent) + "\n" + json.dumps(sorted(tools)),
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
