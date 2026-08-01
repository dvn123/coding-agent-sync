from __future__ import annotations

import subprocess
from functools import cache
from pathlib import Path

import pytest

from capabilities.harness import require_command
from capabilities.targets.cursor import local_executable


@cache
def help_output(*arguments: str) -> str:
    result = subprocess.run(
        [require_command("cursor-agent"), *arguments],
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def local_bundle() -> str:
    executable = local_executable()
    if executable is None:
        pytest.skip("unavailable: matching Cursor local runtime is unavailable")
    root = Path(executable).parent
    for path in (root / "index.js", *root.glob("*.index.js")):
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        if "hooks.json" in text:
            return text
    pytest.skip("unavailable: Cursor local runtime has no inspectable hooks bundle")


@pytest.mark.capability_case("cursor-agent.mcp")
@pytest.mark.capability_live
def test_cursor_agent_declares_user_and_project_mcp_configuration_paths() -> None:
    help = help_output("mcp", "--help")

    assert ".cursor/mcp.json" in help
    assert "~/.cursor/mcp.json" in help


@pytest.mark.capability_case("cursor-agent.hooks")
@pytest.mark.capability_live
def test_cursor_agent_bundles_known_hook_events() -> None:
    bundle = local_bundle()

    assert "hooks.json" in bundle
    assert all(
        event in bundle
        for event in ("beforeShellExecution", "beforeMCPExecution", "afterMCPExecution")
    )


@pytest.mark.capability_case("cursor-agent.plugins")
@pytest.mark.capability_live
def test_cursor_agent_declares_plugin_directories_and_management() -> None:
    help = help_output("--help")
    plugin = help_output("plugin", "--help")

    assert "--plugin-dir <path>" in help
    assert "Load a local plugin directory" in help
    assert "marketplace" in plugin


@pytest.mark.capability_case("cursor-agent.sandbox")
@pytest.mark.capability_live
def test_cursor_agent_declares_the_sandbox_override() -> None:
    help = help_output("--help")

    assert "--sandbox <mode>" in help
    assert 'choices: "enabled",\n                               "disabled"' in help
    assert "overrides config" in help


@pytest.mark.capability_case("cursor-agent.worktree")
@pytest.mark.capability_live
def test_cursor_agent_declares_isolated_worktree_options() -> None:
    help = help_output("--help")

    assert "--worktree [name]" in help
    assert "--worktree-base <branch>" in help
    assert "--skip-worktree-setup" in help
    assert ".cursor/worktrees.json" in help


@pytest.mark.capability_case("cursor-agent.private-worker")
@pytest.mark.capability_live
def test_cursor_agent_declares_private_worker_contracts() -> None:
    help = help_output("worker", "--help")

    assert "private cloud worker" in help
    assert "--worker-dir <path>" in help
    assert "--pool" in help
    assert (
        "one cloud\n"
        "                                    agent claims this worker at a time" in help
    )
