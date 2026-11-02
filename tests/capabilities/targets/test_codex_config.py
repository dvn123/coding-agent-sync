import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
from coding_agents_sync.sync import run_sync

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


@pytest.mark.capability_case("codex.config")
@pytest.mark.capability_live
@pytest.mark.parametrize(("mode", "offered"), (("disabled", False), ("cached", True)))
def test_codex_web_search_mode_decides_whether_the_tool_is_offered(
    runtime: Runtime, mode: str, offered: bool
) -> None:
    """`web_search = "disabled"` is how a policy's websearch deny reaches Codex."""
    paths = paths_for(runtime, f"web-search-{mode}")
    config = write_api_config(paths, runtime) / "config.toml"
    config.write_text(
        config.read_text().replace('web_search = "disabled"', f'web_search = "{mode}"')
    )

    request = invoke(runtime, paths)

    tools = request.raw.get("tools", [])
    assert any("web_search" in tool.get("type", "") for tool in tools) is offered


def app_server_schema(runtime: Runtime, name: str) -> dict[str, Any]:
    """One response schema of the app-server protocol the installed Codex speaks."""
    paths = paths_for(runtime, f"schema-{name}")
    out = paths.tmp / "schema"
    process = run_probe(
        run,
        runtime.codex,
        "app-server",
        "generate-json-schema",
        "--out",
        str(out),
        cwd=paths.work,
        env=environment(paths),
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    return json.loads((out / "v2" / f"{name}.json").read_text())


@pytest.mark.capability_case("codex.tool-gates")
@pytest.mark.capability_live
def test_codex_config_has_no_per_tool_gate(runtime: Runtime) -> None:
    """The config schema holds one tool setting, `web_search`; a new one may
    be the per-tool ask or deny Codex lacks."""
    schema = app_server_schema(runtime, "ConfigReadResponse")

    assert set(schema["definitions"]["ToolsV2"]["properties"]) == {"web_search"}


@pytest.mark.capability_case("codex.skill-execution")
@pytest.mark.capability_live
def test_codex_skills_carry_no_delegated_execution(runtime: Runtime) -> None:
    """What Codex keeps of a skill has no agent or subtask; a new field may be
    the delegated execution a command needs."""
    schema = app_server_schema(runtime, "SkillsListResponse")

    assert set(schema["definitions"]["SkillMetadata"]["properties"]) == {
        "dependencies",
        "description",
        "enabled",
        "interface",
        "name",
        "path",
        "pluginId",
        "scope",
        "shortDescription",
    }


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


@pytest.mark.capability_case("codex.agent-background")
@pytest.mark.capability_live
def test_codex_refuses_an_agent_role_with_background(runtime: Runtime) -> None:
    """Codex drops a role file with a field it does not know, with a warning."""
    paths = paths_for(runtime, "agent-background")
    agents = write_api_config(paths, runtime) / "agents"
    agents.mkdir()
    for name, extra in (("background-probe", "background = true\n"), ("control", "")):
        (agents / f"{name}.toml").write_text(
            f'name = "{name}"\ndescription = "Role probe."\n'
            f'developer_instructions = "Role probe."\n{extra}'
        )

    process = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.codex,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "-C",
            str(paths.work),
            "Reply with configuration probe complete.",
        ),
        cwd=paths.work,
        env=environment(paths),
        timeout=20,
        stdin=-3,
    )
    output = process.stdout + process.stderr

    assert process.returncode == 0, output
    assert "background-probe.toml: unknown field `background`" in output
    assert "control.toml" not in output


@pytest.mark.capability_case("codex.unmatched-ask")
@pytest.mark.capability_live
def test_codex_has_no_policy_that_asks_for_every_unlisted_command(
    runtime: Runtime,
) -> None:
    """`untrusted`, the policy that asked unless a rule allowed, is refused."""
    paths = paths_for(runtime, "unmatched-ask")
    config = write_api_config(paths, runtime) / "config.toml"
    config.write_text(
        config.read_text().replace(
            'approval_policy = "never"', 'approval_policy = "untrusted"'
        )
    )

    process = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.codex,
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "-C",
            str(paths.work),
            "Reply with configuration probe complete.",
        ),
        cwd=paths.work,
        env=environment(paths),
        timeout=20,
        stdin=-3,
    )

    assert process.returncode != 0
    assert 'approval_policy = "untrusted" is no longer supported' in process.stderr


@pytest.mark.capability_case("codex.workspace-ask")
@pytest.mark.capability_live
def test_codex_permission_profile_has_no_ask_access_level(runtime: Runtime) -> None:
    """Codex refuses a directory whose access level is `ask` at config load."""
    paths = paths_for(runtime, "workspace-ask")
    codex_home = paths.home / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        "\n".join(
            (
                'default_permissions = "review"',
                "",
                "[permissions.review]",
                'extends = ":workspace"',
                "",
                '[permissions.review.filesystem.":workspace_roots"]',
                '"review" = "ask"',
            )
        )
    )

    process = run_probe(
        run,
        runtime.codex,
        "sandbox",
        "-P",
        "review",
        "-C",
        str(paths.work),
        "/usr/bin/true",
        cwd=paths.work,
        env=environment(paths),
        timeout=10,
    )

    assert process.returncode != 0
    assert "permissions" in process.stderr


@pytest.mark.capability_case("codex.loading")
@pytest.mark.capability_live
def test_codex_lists_a_skill_description_with_angle_brackets(runtime: Runtime) -> None:
    """A description keeps its `<` and `>`; nothing needs stripping them."""
    paths = paths_for(runtime, "skill-description")
    skills = write_api_config(paths, runtime) / "skills"
    for name, description in (
        ("angle-probe", "Uses <angle> brackets ANGLE_PROBE_SENTINEL"),
        ("plain-probe", "Plain PLAIN_PROBE_SENTINEL"),
    ):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\nProbe.\n"
        )

    process = run_probe(
        run,
        runtime.codex,
        "debug",
        "prompt-input",
        "Probe.",
        cwd=paths.work,
        env=environment(paths),
        timeout=30,
        stdin=-3,
    )

    assert process.returncode == 0, process.stderr
    assert "- plain-probe: Plain PLAIN_PROBE_SENTINEL" in process.stdout
    assert "- angle-probe: Uses <angle> brackets ANGLE_PROBE_SENTINEL" in process.stdout


@pytest.mark.capability_case("codex.permission-profiles")
@pytest.mark.capability_live
def test_codex_root_write_profile_keeps_its_deny_entries(runtime: Runtime) -> None:
    """`":root" = "write"` opens every directory, and a deny entry still binds."""
    paths = paths_for(runtime, "root-write")
    codex_home = paths.home / ".codex"
    codex_home.mkdir()
    outside = paths.root / "outside"
    private = paths.root / "private"
    outside.mkdir()
    private.mkdir()
    (codex_home / "config.toml").write_text(
        "\n".join(
            (
                'default_permissions = "open"',
                "",
                "[permissions.open]",
                'extends = ":workspace"',
                "",
                "[permissions.open.filesystem]",
                '":root" = "write"',
                f'{json.dumps(str(private))} = "deny"',
            )
        )
    )

    process = run_probe(
        run,
        runtime.codex,
        "sandbox",
        "-P",
        "open",
        "-C",
        str(paths.work),
        "/bin/sh",
        "-c",
        f'/usr/bin/touch "{outside}/written"; /usr/bin/touch "{private}/written"',
        cwd=paths.work,
        env=environment(paths),
        timeout=10,
    )

    assert (outside / "written").exists(), process.stderr
    assert not (private / "written").exists()


COMPILED_POLICY = """\
description: Compiled permission profile probe.
secret_paths: ['**/.probe-secret', '~/vault/key']
workspace:
  allow: [~/shared]
  deny: [~/private]
targets:
  cursor:
    omit:
      - workspace
"""


@pytest.mark.capability_case("codex.permission-profiles")
@pytest.mark.capability_live
def test_codex_compiled_profile_denies_secret_reads_and_writes_allowed_roots(
    runtime: Runtime,
) -> None:
    paths = paths_for(runtime, "compiled-profile")
    config_root = paths.root / "sources"
    (config_root / "permissions").mkdir(parents=True)
    (config_root / "permissions" / "policy.yaml").write_text(COMPILED_POLICY)
    run_sync(config_root=config_root, home=paths.home)
    for path, text in {
        paths.work / "readme": "ok",
        paths.work / "app" / ".probe-secret": "secret",
        paths.home / "vault" / "key": "secret",
        paths.home / "private" / "note": "secret",
    }.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (paths.home / "shared").mkdir()

    def sandboxed(script: str) -> int:
        return run_probe(
            run,
            runtime.codex,
            "sandbox",
            "-P",
            "coding-agents",
            "-C",
            str(paths.work),
            "/bin/sh",
            "-c",
            script,
            cwd=paths.work,
            env=environment(paths),
            timeout=10,
        ).returncode

    assert sandboxed(f'/bin/cat "{paths.work / "readme"}"') == 0
    for denied in (
        paths.work / "app" / ".probe-secret",
        paths.home / "vault" / "key",
        paths.home / "private" / "note",
    ):
        assert sandboxed(f'/bin/cat "{denied}"') != 0, denied
    assert sandboxed(f'/usr/bin/touch "{paths.home / "shared" / "new"}"') == 0
    assert sandboxed(f'/usr/bin/touch "{paths.home / "outside"}"') != 0

    # With unmatched directories allowed the whole disk is writable, and the
    # deny entries still hold.
    (config_root / "permissions" / "policy.yaml").write_text(
        COMPILED_POLICY.replace("workspace:\n", "workspace:\n  unmatched: allow\n")
    )
    run_sync(config_root=config_root, home=paths.home)
    assert sandboxed(f'/usr/bin/touch "{paths.home / "outside"}"') == 0
    for denied in (paths.home / "vault" / "key", paths.home / "private" / "note"):
        assert sandboxed(f'/bin/cat "{denied}"') != 0, denied
        assert sandboxed(f'/usr/bin/touch "{denied}"') != 0, denied
