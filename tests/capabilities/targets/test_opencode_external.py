from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from capabilities.harness import Paths, require_command, run_probe, sanitized_env
from capabilities.runtime import run


@dataclass(frozen=True, slots=True)
class Runtime:
    opencode: str
    strings: str
    paths: Paths
    env: dict[str, str]


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Runtime:
    paths = Paths.create(tmp_path_factory.mktemp("opencode-external").resolve())
    return Runtime(
        require_command("opencode"),
        require_command("strings"),
        paths,
        sanitized_env(
            {
                "HOME": str(paths.home),
                "TMPDIR": str(paths.tmp),
                "XDG_CACHE_HOME": str(paths.cache),
                "XDG_CONFIG_HOME": str(paths.config),
                "XDG_DATA_HOME": str(paths.data),
                "XDG_STATE_HOME": str(paths.state),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
        ),
    )


def command_output(runtime: Runtime, command: str, *arguments: str) -> str:
    result = run_probe(
        run,
        command,
        *arguments,
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout + result.stderr


@pytest.mark.capability_case("opencode.tui-config")
@pytest.mark.capability_live
def test_opencode_installed_binary_exposes_tui_config(runtime: Runtime) -> None:
    output = command_output(runtime, runtime.strings, runtime.opencode)

    assert all(
        token in output for token in ("OPENCODE_TUI_CONFIG", "tui.json", "diff_style")
    )


@pytest.mark.capability_case("opencode.tui-runtime")
@pytest.mark.capability_live
def test_opencode_native_help_exposes_tui_runtime(runtime: Runtime) -> None:
    output = command_output(runtime, runtime.opencode, "--help")

    assert all(
        token in output for token in ("start opencode tui", "--mini", "--no-replay")
    )


@pytest.mark.parametrize(
    ("case_id", "reason"),
    [
        pytest.param(
            "opencode.sharing-runtime",
            "requires the external sharing service",
            marks=pytest.mark.capability_case("opencode.sharing-runtime"),
            id="sharing-runtime",
        ),
        pytest.param(
            "opencode.websearch-provider-env",
            "requires provider credentials and an external search service",
            marks=pytest.mark.capability_case("opencode.websearch-provider-env"),
            id="websearch-provider-env",
        ),
        pytest.param(
            "opencode.remote-config",
            "requires an externally hosted well-known configuration",
            marks=pytest.mark.capability_case("opencode.remote-config"),
            id="remote-config",
        ),
        pytest.param(
            "opencode.managed-config",
            "requires administrator-managed host policy",
            marks=pytest.mark.capability_case("opencode.managed-config"),
            id="managed-config",
        ),
        pytest.param(
            "opencode.enterprise-runtime",
            "requires an authenticated enterprise authority",
            marks=pytest.mark.capability_case("opencode.enterprise-runtime"),
            id="enterprise-runtime",
        ),
        pytest.param(
            "opencode.updater-runtime",
            "would mutate the installed OpenCode executable",
            marks=pytest.mark.capability_case("opencode.updater-runtime"),
            id="updater-runtime",
        ),
        pytest.param(
            "opencode.skill-urls-external",
            "requires fetching a skill from an external URL",
            marks=pytest.mark.capability_case("opencode.skill-urls-external"),
            id="skill-urls-external",
        ),
        pytest.param(
            "opencode.watcher-runtime",
            "requires a stable persistent event subscription probe",
            marks=pytest.mark.capability_case("opencode.watcher-runtime"),
            id="watcher-runtime",
        ),
    ],
)
@pytest.mark.capability_live
def test_opencode_external_authority_or_persistent_runtime_is_unavailable(
    case_id: str, reason: str
) -> None:
    pytest.skip(f"unavailable: {case_id} {reason}")
