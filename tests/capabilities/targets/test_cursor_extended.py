from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import Paths, require_command, run_probe, sanitized_env
from capabilities.runtime import run
from capabilities.targets.cursor_desktop_static import (
    AGENT_EXEC_BUNDLE,
    read,
    require_installed,
)


@dataclass(frozen=True, slots=True)
class Runtime:
    cursor: str
    paths: Paths
    env: dict[str, str]


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Runtime:
    paths = Paths.create(tmp_path_factory.mktemp("cursor-extended").resolve())
    return Runtime(
        require_command("cursor-agent"),
        paths,
        sanitized_env(
            {
                "HOME": str(paths.home),
                "CURSOR_CONFIG_DIR": str(paths.config),
                "TMPDIR": str(paths.tmp),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
        ),
    )


def help_output(runtime: Runtime, *arguments: str) -> str:
    result = run_probe(
        run,
        runtime.cursor,
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
            ("--help",),
            ("--resume [chatId]", "--continue", "create-chat", "Resume a chat session"),
            marks=pytest.mark.capability_case("cursor-agent.sessions"),
            id="sessions",
        ),
        pytest.param(
            ("--help",),
            ("--mode <mode>", 'choices: "plan", "ask"', "--plan"),
            marks=pytest.mark.capability_case("cursor-agent.modes"),
            id="modes",
        ),
    ],
)
@pytest.mark.capability_live
def test_cursor_agent_native_help_exposes_extended_surface(
    runtime: Runtime, arguments: tuple[str, ...], tokens: tuple[str, ...]
) -> None:
    output = help_output(runtime, *arguments)

    assert all(token in output for token in tokens)


@pytest.mark.parametrize(
    ("bundle_path", "tokens"),
    [
        pytest.param(
            AGENT_EXEC_BUNDLE,
            ("commandHandlers", "commandHistory", "commandNames"),
            marks=pytest.mark.capability_case("cursor-desktop.commands"),
            id="commands",
        ),
        pytest.param(
            AGENT_EXEC_BUNDLE,
            (
                "sandboxingControls",
                "sandboxPolicyResolver",
                "sandboxNetworkExplicitAllowlist",
            ),
            marks=pytest.mark.capability_case("cursor-desktop.sandbox"),
            id="sandbox",
        ),
        pytest.param(
            AGENT_EXEC_BUNDLE,
            ("extensionConflicts", "extensionPluginOriginalPaths", "extensions"),
            marks=pytest.mark.capability_case("cursor-desktop.extensions"),
            id="extensions",
        ),
    ],
)
@pytest.mark.capability_live
def test_cursor_desktop_bundle_exposes_extended_surface(
    bundle_path: Path, tokens: tuple[str, ...]
) -> None:
    require_installed(bundle_path)
    bundle = read(bundle_path)

    assert all(token in bundle for token in tokens)


@pytest.mark.parametrize(
    "case_id",
    [
        pytest.param(
            "cursor-cloud.instructions",
            marks=pytest.mark.capability_case("cursor-cloud.instructions"),
            id="instructions",
        ),
        pytest.param(
            "cursor-cloud.hooks",
            marks=pytest.mark.capability_case("cursor-cloud.hooks"),
            id="hooks",
        ),
        pytest.param(
            "cursor-cloud.automations",
            marks=pytest.mark.capability_case("cursor-cloud.automations"),
            id="automations",
        ),
        pytest.param(
            "cursor-cloud.admin",
            marks=pytest.mark.capability_case("cursor-cloud.admin"),
            id="admin",
        ),
        pytest.param(
            "cursor-cloud.handoff",
            marks=pytest.mark.capability_case("cursor-cloud.handoff"),
            id="handoff",
        ),
        pytest.param(
            "cursor-cloud.computer-use",
            marks=pytest.mark.capability_case("cursor-cloud.computer-use"),
            id="computer-use",
        ),
        pytest.param(
            "cursor-cloud.network-policy",
            marks=pytest.mark.capability_case("cursor-cloud.network-policy"),
            id="network-policy",
        ),
    ],
)
@pytest.mark.capability_live
def test_cursor_cloud_requires_authenticated_tenant(case_id: str) -> None:
    pytest.skip(f"unavailable: {case_id} requires an authenticated Cursor Cloud tenant")
