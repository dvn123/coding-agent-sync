from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from capabilities.harness import Paths, require_command, run_probe, sanitized_env
from capabilities.runtime import run


@dataclass(frozen=True, slots=True)
class Runtime:
    claude: str
    paths: Paths
    env: dict[str, str]


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Runtime:
    paths = Paths.create(tmp_path_factory.mktemp("claude-extended").resolve())
    return Runtime(
        require_command("claude"),
        paths,
        sanitized_env(
            {
                "HOME": str(paths.home),
                "CLAUDE_CONFIG_DIR": str(paths.config),
                "TMPDIR": str(paths.tmp),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
        ),
    )


def command_output(runtime: Runtime, executable: str, *arguments: str) -> str:
    result = run_probe(
        run,
        executable,
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
            ("mcp", "add", "--help"),
            ("--scope <scope>", "local, user, or project"),
            marks=pytest.mark.capability_case("claude.mcp"),
            id="mcp",
        ),
        pytest.param(
            ("--help",),
            ("--include-hook-events", "skip hooks"),
            marks=pytest.mark.capability_case("claude.hooks"),
            id="hooks",
        ),
        pytest.param(
            ("--help",),
            ("auto-memory", "memory paths"),
            marks=pytest.mark.capability_case("claude.memory"),
            id="memory",
        ),
        pytest.param(
            ("plugin", "--help"),
            ("Manage Claude Code plugins", "marketplace", "validate"),
            marks=pytest.mark.capability_case("claude.plugins"),
            id="plugins",
        ),
        pytest.param(
            ("--help",),
            ("output styles", "custom themes"),
            marks=pytest.mark.capability_case("claude.output-styles"),
            id="output-styles",
        ),
        pytest.param(
            ("--help",),
            ("Admin-managed (policy)", "settings still apply", "safe-mode"),
            marks=pytest.mark.capability_case("claude.managed-policy"),
            id="managed-policy",
        ),
        pytest.param(
            ("--help",),
            ("--resume", "--no-session-persistence", "workflows"),
            marks=pytest.mark.capability_case("claude.sessions-workflows"),
            id="sessions-workflows",
        ),
    ],
)
@pytest.mark.capability_live
def test_claude_native_help_exposes_extended_surface(
    runtime: Runtime, arguments: tuple[str, ...], tokens: tuple[str, ...]
) -> None:
    output = command_output(runtime, runtime.claude, *arguments)

    assert all(token in output for token in tokens)


@pytest.mark.parametrize(
    "tokens",
    [
        pytest.param(
            ("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "--agent-teams"),
            marks=pytest.mark.capability_case("claude.agent-teams-experimental"),
            id="agent-teams-experimental",
        ),
        pytest.param(
            ("keybindings.json", "statusLine", "executeStatusLineCommand"),
            marks=pytest.mark.capability_case("claude.keybindings-statusline"),
            id="keybindings-statusline",
        ),
    ],
)
@pytest.mark.capability_live
def test_claude_installed_binary_exposes_extended_surface(
    runtime: Runtime, tokens: tuple[str, ...]
) -> None:
    output = command_output(runtime, require_command("strings"), runtime.claude)

    assert all(token in output for token in tokens)


@pytest.mark.capability_case("claude.providers-auth")
@pytest.mark.capability_live
def test_claude_provider_auth_requires_external_authority() -> None:
    pytest.skip("unavailable: provider authentication requires external credentials")


@pytest.mark.capability_case("claude.gui-cloud")
@pytest.mark.capability_live
def test_claude_gui_cloud_requires_authenticated_interactive_session() -> None:
    pytest.skip("unavailable: GUI and cloud workflows require an authenticated session")
