from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import coding_agents_sync.cursor_desktop_probe as probe
from coding_agents_sync.cursor_desktop_probe import (
    PROBE_SCHEMA,
    ProbeError,
    ProbeState,
    ProbeStatus,
    _cleanup_state,
    _cursor_version,
    _launch_cursor,
    _quit_owned_cursor,
    inspect_rule_loading,
    load_probe,
    prepare_probe,
    verify_probe,
)

DESKTOP_RESOURCES = probe.CURSOR_APP / "Contents/Resources/app"
DESKTOP_SCHEMA = (
    DESKTOP_RESOURCES / "extensions/cursor-always-local/schemas/permissions.schema.json"
)
DESKTOP_BUNDLE = DESKTOP_RESOURCES / "out/vs/workbench/workbench.desktop.main.js"
AGENT_EXEC_BUNDLE = DESKTOP_RESOURCES / "extensions/cursor-agent-exec/dist/main.js"

installed = pytest.mark.skipif(
    not DESKTOP_BUNDLE.is_file(), reason="Cursor Desktop is not installed"
)


@cache
def desktop_bundle() -> str:
    return DESKTOP_BUNDLE.read_text(errors="replace")


def state_at(path: Path) -> ProbeState:
    return ProbeState(
        PROBE_SCHEMA,
        path.name,
        str(path),
        "CURSOR_DESKTOP_RULE_A",
        "3.13.10",
    )


def write_agent_log(
    state: ProbeState,
    local_rule_count: int,
    plugin_rule_count: int,
    *,
    filename: str = "Cursor Agent Exec.log",
    root: Path | None = None,
) -> Path:
    root = root or state.path / "logs"
    log = root / "run/window/exthost" / filename
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "cursor_agent_exec.startup.workspace_paths "
        f'{{"workspacePathCount":1,"workspacePaths":["{state.workspace}"]}}\n'
        "LocalCursorRulesService load completed "
        f'{{"durationMs":12,"ruleCount":{local_rule_count}}}\n'
        "CursorPluginsAgentSkillsService load completed "
        f'{{"durationMs":7,"ruleCount":{plugin_rule_count},"skillCount":0}}\n'
    )
    return root


def test_prepare_compiles_isolated_ancestor_rule_without_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(probe, "_cursor_version", lambda _application: "3.13.10")

    state = prepare_probe(
        root=tmp_path,
        application=tmp_path / "Cursor.app",
        launch=False,
    )

    assert state.workspace.parent == state.home
    assert state.compiled_rule.is_file()
    assert "alwaysApply: true" in state.compiled_rule.read_text()
    assert state.rule_sentinel in state.compiled_rule.read_text()
    assert not (state.home / ".cursor/plugins").exists()
    assert load_probe(state.run_id, root=tmp_path) == state


def test_prepare_rejects_retired_overlapping_plugin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(probe, "_cursor_version", lambda _application: "3.13.10")
    user_home = tmp_path / "user"
    overlapping_plugin = user_home / probe.RETIRED_RULES_PLUGIN
    overlapping_plugin.mkdir(parents=True)

    with pytest.raises(ProbeError, match="retired overlapping Cursor plugin"):
        prepare_probe(
            root=tmp_path / "runs",
            application=tmp_path / "Cursor.app",
            launch=False,
            user_home=user_home,
        )


def test_cursor_version_comes_from_launched_application_bundle(tmp_path: Path) -> None:
    application = tmp_path / "Cursor.app"
    info = application / "Contents/Info.plist"
    info.parent.mkdir(parents=True)
    with info.open("wb") as plist:
        probe.plistlib.dump({"CFBundleShortVersionString": "3.13.10"}, plist)

    assert _cursor_version(application) == "3.13.10"


def test_launch_uses_normal_profile_with_isolated_workspace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = state_at(tmp_path / ("a" * 32))
    state.path.mkdir()
    state.workspace.mkdir(parents=True)
    cli = tmp_path / "Cursor.app/Contents/Resources/app/bin/cursor"
    cli.parent.mkdir(parents=True)
    cli.touch()
    observed: dict[str, Any] = {}

    class Process:
        pid = 123

        @staticmethod
        def poll() -> None:
            return None

    def popen(command: tuple[str, ...], **kwargs: Any) -> Process:
        observed.update(command=command, **kwargs)
        return Process()

    monkeypatch.setattr(probe, "_cursor_is_running", lambda: False)
    monkeypatch.setattr(probe.subprocess, "Popen", popen)
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)

    assert _launch_cursor(state, tmp_path / "Cursor.app") == 123
    assert observed["command"] == (
        str(cli),
        "--new-window",
        "--wait",
        "--suppress-popups-on-startup",
        "--classic",
        str(state.workspace),
    )
    assert "env" not in observed
    assert observed["start_new_session"] is True


def test_prepare_cleans_partial_state_and_normalizes_operational_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cleaned: list[ProbeState] = []
    monkeypatch.setattr(probe, "_cursor_version", lambda _application: "3.13.10")
    monkeypatch.setattr(
        probe,
        "_state_file",
        lambda _run_dir: (_ for _ in ()).throw(OSError("state write failed")),
    )
    monkeypatch.setattr(probe, "_cleanup_state", cleaned.append)

    with pytest.raises(ProbeError, match="state write failed"):
        prepare_probe(
            root=tmp_path,
            application=tmp_path / "Cursor.app",
        )

    assert len(cleaned) == 1
    assert cleaned[0].launcher_pid is None


def test_run_reports_unexpected_failure_as_structured_harness_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_prepare() -> ProbeState:
        raise OSError("unexpected setup failure")

    monkeypatch.setattr(probe, "prepare_probe", fail_prepare)

    result = CliRunner().invoke(probe.main, ["run"])

    assert result.exit_code == 3
    assert json.loads(result.output) == {
        "detail": "Cursor Desktop probe failed: unexpected setup failure",
        "status": "harness_error",
    }


@pytest.mark.parametrize("plugin_rule_count", [0, 1, 7])
def test_inspector_requires_one_local_rule_and_ignores_unrelated_plugin_count(
    plugin_rule_count: int,
    tmp_path: Path,
) -> None:
    state = state_at(tmp_path / ("a" * 32))
    logs = write_agent_log(state, 1, plugin_rule_count)

    result = inspect_rule_loading(state, logs_root=logs)

    assert result
    assert result.status is ProbeStatus.LOADED
    assert result.matching_log_count == 3
    assert result.local_rule_count == 1
    assert result.plugin_rule_count == plugin_rule_count
    assert result.generated_plugin_present is False


def test_inspector_reads_timestamped_cursor_agent_log(tmp_path: Path) -> None:
    state = state_at(tmp_path / ("a" * 32))
    logs = write_agent_log(
        state,
        1,
        0,
        filename="Cursor Agent Exec.20260727T000856.log",
    )

    assert inspect_rule_loading(state, logs_root=logs)


def test_inspector_rejects_loader_evidence_from_another_workspace(
    tmp_path: Path,
) -> None:
    state = state_at(tmp_path / ("a" * 32))
    logs = write_agent_log(state_at(tmp_path / ("b" * 32)), 1, 0)

    assert inspect_rule_loading(state, logs_root=logs) is None


def test_inspector_rejects_split_workspace_and_rule_evidence(tmp_path: Path) -> None:
    state = state_at(tmp_path / ("a" * 32))
    logs = write_agent_log(
        state,
        2,
        0,
        filename="Cursor Agent Exec.owned.log",
    )
    write_agent_log(
        state_at(tmp_path / ("b" * 32)),
        1,
        0,
        filename="Cursor Agent Exec.other.log",
        root=logs,
    )

    assert inspect_rule_loading(state, logs_root=logs) is None


@pytest.mark.parametrize(
    ("local_rule_count", "plugin_rule_count"),
    [(0, 0), (2, 0)],
)
def test_inspector_rejects_wrong_rule_counts(
    tmp_path: Path, local_rule_count: int, plugin_rule_count: int
) -> None:
    state = state_at(tmp_path / ("a" * 32))
    logs = write_agent_log(state, local_rule_count, plugin_rule_count)

    assert inspect_rule_loading(state, logs_root=logs) is None


def test_inspector_ignores_logs_older_than_owned_launch(tmp_path: Path) -> None:
    state = state_at(tmp_path / ("a" * 32))
    logs = write_agent_log(state, 1, 0)
    log_mtime = next(logs.rglob("Cursor Agent Exec*.log")).stat().st_mtime_ns
    state = ProbeState(**{**probe.asdict(state), "launch_started_ns": log_mtime + 1})

    assert inspect_rule_loading(state, logs_root=logs) is None


def test_verify_reports_missing_loader_evidence_as_harness_error(
    tmp_path: Path,
) -> None:
    result = verify_probe(state_at(tmp_path / ("a" * 32)), timeout=0)

    assert result.status is ProbeStatus.HARNESS_ERROR


def test_cleanup_refuses_symlinked_probe_state(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    path = tmp_path / ("a" * 32)
    path.symlink_to(target, target_is_directory=True)

    with pytest.raises(ProbeError, match="invalid ownership"):
        _cleanup_state(state_at(path))
    assert target.exists()


def test_quit_refuses_process_without_owned_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = ProbeState(
        **{**probe.asdict(state_at(tmp_path / ("a" * 32))), "launcher_pid": 123}
    )
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *_args, **_kwargs: probe.subprocess.CompletedProcess(
            (), 0, stdout="/Applications/Cursor.app/Contents/MacOS/Cursor"
        ),
    )

    with pytest.raises(ProbeError, match="not owned"):
        _quit_owned_cursor(state)


def test_quit_uses_normal_application_shutdown_and_waits_for_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = ProbeState(
        **{**probe.asdict(state_at(tmp_path / ("a" * 32))), "launcher_pid": 123}
    )
    commands: list[tuple[str, ...]] = []

    def run(
        command: tuple[str, ...], **_kwargs: Any
    ) -> probe.subprocess.CompletedProcess[str]:
        commands.append(command)
        stdout = f"Cursor {state.workspace}" if command[0] == "ps" else ""
        return probe.subprocess.CompletedProcess(command, 0, stdout=stdout)

    monkeypatch.setattr(probe.subprocess, "run", run)
    attempts = iter((None, None, ProcessLookupError()))

    def inspect_process(_pid: int, _signal: int) -> None:
        if isinstance(result := next(attempts), Exception):
            raise result

    monkeypatch.setattr(probe.os, "kill", inspect_process)
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)

    _quit_owned_cursor(state)

    assert commands == [
        ("ps", "-p", "123", "-o", "command="),
        (
            "/usr/bin/osascript",
            "-e",
            'tell application id "com.todesktop.230313mzl4w4u92" to quit',
        ),
    ]


def test_load_rejects_probe_state_ownership_mismatch(tmp_path: Path) -> None:
    run_id = "a" * 32
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    state = state_at(tmp_path / ("b" * 32))
    (run_dir / "probe.json").write_text(json.dumps(probe.asdict(state)))

    with pytest.raises(ProbeError, match="ownership mismatch"):
        load_probe(run_id, root=tmp_path)


# Desktop is a GUI, so the assertions below read the installed application
# bundle. They prove which `permissions.json` keys Desktop parses and the
# values it accepts, not how it executes a command.


@installed
def test_desktop_schema_lists_the_permission_keys_and_approval_modes() -> None:
    properties = json.loads(DESKTOP_SCHEMA.read_text())["properties"]

    assert properties.keys() == {
        "mcpAllowlist",
        "terminalAllowlist",
        "approvalMode",
        "autoRun",
        "autoReview",
    }
    assert set(properties["approvalMode"]["enum"]) == {
        "allowlist",
        "unrestricted",
        "manual",
    }


@installed
def test_desktop_parses_the_schema_keys_as_four_channels() -> None:
    bundle = desktop_bundle()

    for pattern in (
        r"mcpAllowlist:Array\.isArray\([\w$]+\.mcpAllowlist\)",
        r"terminalAllowlist:Array\.isArray\([\w$]+\.terminalAllowlist\)",
        r"approvalMode:[\w$]+\([\w$]+\.approvalMode\)",
        r"([\w$]+)\.autoReview\?\?\1\.autoRun",
        r'([\w$]+)==="allowlist"\|\|\1==="unrestricted"\|\|\1==="manual"',
        r"allowInstructions:[\w$]+\([\w$]+\.allow_instructions\),"
        r"blockInstructions:[\w$]+\([\w$]+\.block_instructions\)",
    ):
        assert re.search(pattern, bundle), pattern


@installed
def test_desktop_lets_the_permission_file_approval_mode_decide_auto_run() -> None:
    bundle = desktop_bundle()

    assert re.search(
        r'case"unrestricted":case"allowlist":return!0;case"manual":return!1', bundle
    )
    assert re.search(
        r'case"unrestricted":return!0;case"allowlist":case"manual":return!1', bundle
    )
    assert re.search(
        r"canAddToAllowlistFromIde\([\w$]+\)\{return [\w$]+\(\)\.isAdminControlled\|\|"
        r"this\._hasAdminConfiguredPermissionsFilePaths\|\|"
        r"this\._permissionsFileApprovalMode!==void 0\?!1:",
        bundle,
    )


@installed
def test_desktop_lowers_the_terminal_allowlist_to_allows_with_no_deny_list() -> None:
    bundle = AGENT_EXEC_BUNDLE.read_text(errors="replace")

    assert re.search(r"\.map\([\w$]+=>`Shell\(\$\{[\w$]+\}\)`\)", bundle)
    assert re.search(r"\{allow:[\w$]+,deny:\[\]\}", bundle)
    assert re.search(
        r"getEffectiveTerminalAllowlist\(\)\.map\([\w$]+=>`Shell\(\$\{[\w$]+\}\)`\)",
        desktop_bundle(),
    )


@installed
def test_desktop_evaluates_every_parsed_command_in_a_chain() -> None:
    bundle = AGENT_EXEC_BUNDLE.read_text(errors="replace")

    for token in (
        "Parser failed to parse command (possible bypass)",
        "Parser found no commands (possible bypass)",
        "allCommandsRunnable",
        "allCommandsAllowlisted",
        "unapprovedCommands",
        "notAllowedCommands",
    ):
        assert token in bundle, token
    # The workbench hands its composed allow list to this engine, so the
    # per-command evaluation above is the one Desktop shell calls go through.
    assert "dashboardTerminalAllowlistOverriddenByPermissionsFile" in bundle
    assert "dashboardTerminalAllowlistOverriddenByPermissionsFile" in desktop_bundle()
