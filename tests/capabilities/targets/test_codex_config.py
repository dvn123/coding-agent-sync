from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
    sanitized_env,
)
from capabilities.protocols.responses import ResponsesRequest, responses_done
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer

BASE_MODEL = "codex-config-resolution-probe"
PROFILE_MODEL = "codex-profile-resolution-probe"


@dataclass(frozen=True, slots=True)
class Runtime:
    codex: str
    seatbelt: Seatbelt
    stub: RecordedServer
    root: Path


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    paths = Paths.create(tmp_path_factory.mktemp("codex-config").resolve())
    stub = recorded_server(
        request,
        lambda _request, _count: (
            responses_done("codex-config"),
            "text/event-stream",
        ),
    )
    value = Runtime(
        require_command("codex"),
        loopback_seatbelt(require_command("sandbox-exec")),
        stub,
        paths.root,
    )
    require_containment(
        value.seatbelt,
        stub,
        paths,
        sanitized_env({"HOME": str(paths.home), "PATH": "/usr/bin:/bin"}),
    )
    return value


def paths_for(runtime: Runtime, name: str) -> Paths:
    return Paths.create(runtime.root / name)


def environment(paths: Paths) -> dict[str, str]:
    return sanitized_env(
        {
            "CODEX_HOME": str(paths.home / ".codex"),
            "CODEX_CAPABILITY_KEY": "local-placeholder",
            "HOME": str(paths.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": str(paths.tmp),
        }
    )


def write_api_config(
    paths: Paths, runtime: Runtime, *, model: str = BASE_MODEL
) -> Path:
    codex_home = paths.home / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        "\n".join(
            (
                f"model = {json.dumps(model)}",
                'model_provider = "capability-probe"',
                'model_reasoning_effort = "high"',
                'approval_policy = "never"',
                'sandbox_mode = "danger-full-access"',
                'web_search = "disabled"',
                'shell_environment_policy.inherit = "none"',
                "",
                "[model_providers.capability-probe]",
                'name = "Local capability probe"',
                f"base_url = {json.dumps(f'{runtime.stub.base_url}/v1')}",
                'env_key = "CODEX_CAPABILITY_KEY"',
                'wire_api = "responses"',
                "requires_openai_auth = false",
                "",
                f"[projects.{json.dumps(str(paths.work))}]",
                'trust_level = "trusted"',
            )
        )
    )
    return codex_home


def invoke(
    runtime: Runtime, paths: Paths, *, profile: str | None = None
) -> ResponsesRequest:
    start = len(runtime.stub.requests)
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.codex,
            *(("--profile", profile) if profile else ()),
            "exec",
            "--skip-git-repo-check",
            "--strict-config",
            "--ephemeral",
            "--json",
            "-C",
            str(paths.work),
            "Reply with configuration probe complete.",
        ),
        cwd=paths.work,
        env=environment(paths),
        timeout=20,
        stdin=-3,
    )
    requests = runtime.stub.requests[start:]
    assert process.returncode == 0, process.stderr
    assert len(requests) == 1
    return ResponsesRequest.decode(requests[0])


@pytest.mark.capability_case("codex.config")
@pytest.mark.capability_live
def test_codex_resolves_strict_user_config_into_request(runtime: Runtime) -> None:
    paths = paths_for(runtime, "config")
    write_api_config(paths, runtime)

    request = invoke(runtime, paths)

    assert request.raw["model"] == BASE_MODEL
    assert request.raw["reasoning"]["effort"] == "high"


@pytest.mark.capability_case("codex.model-providers")
@pytest.mark.capability_live
def test_codex_routes_custom_model_provider_to_isolated_responses_stub(
    runtime: Runtime,
) -> None:
    paths = paths_for(runtime, "model-provider")
    write_api_config(paths, runtime)

    request = invoke(runtime, paths)

    assert request.raw["model"] == BASE_MODEL


@pytest.mark.capability_case("codex.profiles")
@pytest.mark.capability_live
def test_codex_overlays_named_profile_file(runtime: Runtime) -> None:
    paths = paths_for(runtime, "profile")
    codex_home = write_api_config(paths, runtime)
    (codex_home / "deep.config.toml").write_text(
        f'model = {json.dumps(PROFILE_MODEL)}\nmodel_reasoning_effort = "low"\n'
    )

    request = invoke(runtime, paths, profile="deep")

    assert request.raw["model"] == PROFILE_MODEL
    assert request.raw["reasoning"]["effort"] == "low"


@pytest.mark.capability_case("codex.permission-profiles")
@pytest.mark.capability_live
def test_codex_custom_permission_profile_denies_workspace_subpath(
    runtime: Runtime,
) -> None:
    paths = paths_for(runtime, "permission-profile")
    codex_home = paths.home / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        "\n".join(
            (
                'default_permissions = "project-edit"',
                "",
                "[permissions.project-edit]",
                'extends = ":workspace"',
                "",
                '[permissions.project-edit.filesystem.":workspace_roots"]',
                '"blocked" = "deny"',
            )
        )
    )
    allowed = paths.work / "allowed"
    blocked_dir = paths.work / "blocked"
    blocked_dir.mkdir()
    denied = blocked_dir / "denied"

    # The target command is already enclosed by Codex's selected Seatbelt
    # profile; nesting it under the harness Seatbelt masks that behavior.
    process = run_probe(
        run,
        runtime.codex,
        "sandbox",
        "-P",
        "project-edit",
        "-C",
        str(paths.work),
        "/bin/sh",
        "-c",
        f'/usr/bin/touch "{allowed}"; /usr/bin/touch "{denied}"',
        cwd=paths.work,
        env=environment(paths),
        timeout=10,
    )

    assert process.returncode != 0
    assert allowed.exists(), process.stderr
    assert not denied.exists()
