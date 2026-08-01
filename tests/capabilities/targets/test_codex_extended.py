from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from capabilities.harness import Paths, require_command, run_probe, sanitized_env
from capabilities.runtime import run


@dataclass(frozen=True, slots=True)
class Runtime:
    codex: str
    paths: Paths
    env: dict[str, str]


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Runtime:
    paths = Paths.create(tmp_path_factory.mktemp("codex-extended").resolve())
    return Runtime(
        require_command("codex"),
        paths,
        sanitized_env(
            {
                "CODEX_HOME": str(paths.home / ".codex"),
                "HOME": str(paths.home),
                "TMPDIR": str(paths.tmp),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
        ),
    )


def help_output(runtime: Runtime, *arguments: str) -> str:
    result = run_probe(
        run,
        runtime.codex,
        *arguments,
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout + result.stderr


@pytest.mark.parametrize(
    ("arguments", "tokens"),
    [
        pytest.param(
            ("mcp", "--help"),
            ("Manage external MCP servers for Codex", "add", "login"),
            marks=pytest.mark.capability_case("codex.mcp"),
            id="mcp",
        ),
        pytest.param(
            ("--help",),
            ("--dangerously-bypass-hook-trust", "enabled hooks"),
            marks=pytest.mark.capability_case("codex.hooks"),
            id="hooks",
        ),
        pytest.param(
            ("plugin", "--help"),
            ("Manage Codex plugins", "marketplace", "remove"),
            marks=pytest.mark.capability_case("codex.plugins"),
            id="plugins",
        ),
        pytest.param(
            ("resume", "--help"),
            ("Resume a previous interactive session", "--last", "--all"),
            marks=pytest.mark.capability_case("codex.runtime-sessions"),
            id="runtime-sessions",
        ),
    ],
)
@pytest.mark.capability_live
def test_codex_native_help_exposes_extended_surface(
    runtime: Runtime, arguments: tuple[str, ...], tokens: tuple[str, ...]
) -> None:
    output = help_output(runtime, *arguments)

    assert all(token in output for token in tokens)


@pytest.mark.capability_case("codex.apps-connectors")
@pytest.mark.capability_live
def test_codex_apps_connectors_require_desktop_or_external_authority() -> None:
    pytest.skip(
        "unavailable: apps and connectors require a configured Desktop "
        "or external service"
    )


@pytest.mark.capability_case("codex.managed-policy")
@pytest.mark.capability_live
def test_codex_managed_policy_requires_administrator_configuration() -> None:
    pytest.skip("unavailable: managed policy requires administrator configuration")


@pytest.mark.capability_case("codex.gui-cloud")
@pytest.mark.capability_live
def test_codex_gui_cloud_requires_authenticated_interactive_session() -> None:
    pytest.skip("unavailable: GUI and cloud workflows require an authenticated session")
