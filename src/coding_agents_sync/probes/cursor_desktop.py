from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Never

import typer

from ..sync import run_sync

PROBE_SCHEMA = "coding-agents/cursor-desktop-probe/v2"
PROBE_ROOT = Path("/tmp/coding-agents-cursor")
CURSOR_APP = Path("/Applications/Cursor.app")
CURSOR_BUNDLE_ID = "com.todesktop.230313mzl4w4u92"
RETIRED_RULES_PLUGIN = Path(".cursor/plugins/local/coding-agents-rules")
RUN_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
RULE_SENTINEL_PATTERN = re.compile(r"CURSOR_DESKTOP_RULE_[0-9A-F]{32}")
COMPILED_RULE_NAME = "cursor-desktop-capability-probe.mdc"
LOCAL_RULE_EVENT = re.compile(
    r'LocalCursorRulesService load completed \{[^\n]*"ruleCount":(\d+)(?:,|\})'
)
PLUGIN_RULE_EVENT = re.compile(
    r"CursorPluginsAgentSkillsService load completed "
    r'\{[^\n]*"ruleCount":(\d+)(?:,|\})'
)
# Cursor has no scripting definition, so it exposes no stable window document path
# to bind to the workspace. Pair the owned CLI process group with the narrowest
# reliable app-level guard: refuse to quit unless the probe still owns the only
# Cursor window.
OWNED_CURSOR_QUIT_SCRIPT = f'''tell application id "{CURSOR_BUNDLE_ID}"
if (count of windows) is not 1 then
error "Cursor ownership changed; refusing app-wide quit"
end if
quit
end tell'''


class ProbeStatus(StrEnum):
    PREPARED = "prepared"
    LOADED = "loaded"
    UNAVAILABLE = "unavailable"
    HARNESS_ERROR = "harness_error"
    CLEANED = "cleaned"


class ProbeError(RuntimeError):
    status = ProbeStatus.HARNESS_ERROR


class ProbeUnavailable(ProbeError):
    status = ProbeStatus.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class ProbeState:
    schema: str
    run_id: str
    run_dir: str
    rule_sentinel: str
    cursor_version: str
    launch_started_ns: int | None = None
    launcher_pid: int | None = None

    @property
    def path(self) -> Path:
        return Path(self.run_dir)

    @property
    def home(self) -> Path:
        return self.path / "home"

    @property
    def workspace(self) -> Path:
        return self.home / "workspace"

    @property
    def compiled_rule(self) -> Path:
        return self.home / ".cursor" / "rules" / COMPILED_RULE_NAME


@dataclass(frozen=True, slots=True)
class ProbeResult:
    status: ProbeStatus
    detail: str
    cursor_version: str
    matching_log_count: int = 0
    local_rule_count: int | None = None
    plugin_rule_count: int | None = None
    generated_plugin_present: bool | None = None


def _state_file(run_dir: Path) -> Path:
    return run_dir / "probe.json"


def _write_probe_source(config_root: Path, sentinel: str) -> None:
    rules = config_root / "rules"
    rules.mkdir(parents=True)
    (rules / "cursor-desktop-capability-probe.md").write_text(
        "---\n"
        "schema: coding-agents/v4\n"
        "kind: rule\n"
        "id: cursor-desktop-capability-probe\n"
        "name: Cursor Desktop capability probe\n"
        "description: Inert ancestor-rule capability sentinel.\n"
        "targets:\n"
        "  cursor:\n"
        "    native:\n"
        "      always_apply: true\n"
        "---\n"
        f"{sentinel}\n",
        encoding="utf-8",
    )


def _cursor_cli(application: Path) -> Path:
    return application / "Contents/Resources/app/bin/cursor"


def _cursor_version(application: Path) -> str:
    try:
        with (application / "Contents/Info.plist").open("rb") as plist:
            version = plistlib.load(plist)["CFBundleShortVersionString"]
    except (KeyError, OSError, TypeError, plistlib.InvalidFileException) as error:
        raise ProbeUnavailable(
            f"cannot read Cursor Desktop version from {application}: {error}"
        ) from error
    if not isinstance(version, str) or not version:
        raise ProbeUnavailable(
            f"Cursor Desktop has an invalid bundle version at {application}"
        )
    return version


def _cursor_is_running() -> bool:
    return (
        subprocess.run(
            ("pgrep", "-x", "Cursor"),
            check=False,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def _launch_cursor(state: ProbeState, application: Path) -> int:
    cli = _cursor_cli(application)
    if not cli.is_file():
        raise ProbeUnavailable(f"Cursor application is unavailable at {application}")
    if _cursor_is_running():
        raise ProbeUnavailable(
            "Cursor Desktop is already running; the probe will not attach to or "
            "restart an unowned session"
        )
    try:
        with (
            (state.path / "cursor.stdout.log").open("w") as stdout,
            (state.path / "cursor.stderr.log").open("w") as stderr,
        ):
            process = subprocess.Popen(
                (
                    str(cli),
                    "--new-window",
                    "--wait",
                    "--suppress-popups-on-startup",
                    "--classic",
                    str(state.workspace),
                ),
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
    except OSError as error:
        raise ProbeUnavailable(f"cannot launch Cursor Desktop: {error}") from error
    time.sleep(1)
    if process.poll() is not None:
        detail = (state.path / "cursor.stderr.log").read_text().strip()
        raise ProbeUnavailable(
            f"Cursor Desktop exited during startup: {detail or 'no diagnostic'}"
        )
    return process.pid


def prepare_probe(
    *,
    root: Path = PROBE_ROOT,
    application: Path = CURSOR_APP,
    launch: bool = True,
    user_home: Path | None = None,
) -> ProbeState:
    cursor_version = _cursor_version(application)
    overlapping_plugin = (user_home or Path.home()) / RETIRED_RULES_PLUGIN
    if overlapping_plugin.exists() or overlapping_plugin.is_symlink():
        raise ProbeError(
            f"retired overlapping Cursor plugin is still present at "
            f"{overlapping_plugin}"
        )
    run_id = uuid.uuid4().hex
    run_dir = root / run_id
    run_dir.mkdir(parents=True, mode=0o700)
    state = ProbeState(
        schema=PROBE_SCHEMA,
        run_id=run_id,
        run_dir=str(run_dir),
        rule_sentinel=f"CURSOR_DESKTOP_RULE_{run_id.upper()}",
        cursor_version=cursor_version,
    )
    try:
        _write_probe_source(run_dir / "source", state.rule_sentinel)
        state.workspace.mkdir(parents=True)
        run_sync(config_root=run_dir / "source", home=state.home)
        if (
            not state.compiled_rule.is_file()
            or state.rule_sentinel not in state.compiled_rule.read_text()
        ):
            raise ProbeError("synchronizer did not emit the expected Cursor user rule")
        if (state.home / ".cursor/plugins/local").exists():
            raise ProbeError("synchronizer emitted an overlapping Cursor local plugin")
        _state_file(run_dir).write_text(
            json.dumps(asdict(state), indent=2) + "\n",
            encoding="utf-8",
        )
        if launch:
            state = ProbeState(
                **{
                    **asdict(state),
                    "launch_started_ns": time.time_ns(),
                }
            )
            _state_file(run_dir).write_text(
                json.dumps(asdict(state), indent=2) + "\n",
                encoding="utf-8",
            )
            state = ProbeState(
                **{
                    **asdict(state),
                    "launcher_pid": _launch_cursor(state, application),
                }
            )
            _state_file(run_dir).write_text(
                json.dumps(asdict(state), indent=2) + "\n",
                encoding="utf-8",
            )
    except Exception as error:
        failure = (
            error
            if isinstance(error, ProbeError)
            else ProbeError(f"cannot prepare Cursor Desktop probe: {error}")
        )
        try:
            _cleanup_state(state)
        except ProbeError as cleanup_error:
            raise ProbeError(
                f"{failure}; cleanup also failed: {cleanup_error}"
            ) from error
        raise failure from error
    return state


def load_probe(run_id: str, *, root: Path = PROBE_ROOT) -> ProbeState:
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ProbeError("invalid probe run ID")
    run_dir = root / run_id
    try:
        raw: dict[str, Any] = json.loads(_state_file(run_dir).read_text())
        state = ProbeState(**raw)
    except (OSError, TypeError, ValueError) as error:
        raise ProbeError(f"invalid or missing probe state: {error}") from error
    if (
        state.schema != PROBE_SCHEMA
        or state.run_id != run_id
        or Path(state.run_dir) != run_dir
    ):
        raise ProbeError("probe state ownership mismatch")
    return state


def inspect_rule_loading(
    state: ProbeState, *, logs_root: Path | None = None
) -> ProbeResult | None:
    root = logs_root or Path.home() / "Library/Application Support/Cursor/logs"
    workspace_marker = (
        f'cursor_agent_exec.startup.workspace_paths {{"workspacePathCount":1,'
        f'"workspacePaths":[{json.dumps(str(state.workspace))}]}}'
    )
    try:
        for path in sorted(root.rglob("Cursor Agent Exec*.log")):
            if (
                state.launch_started_ns is not None
                and path.stat().st_mtime_ns < state.launch_started_ns
            ):
                continue
            log = path.read_text(encoding="utf-8", errors="replace")
            local_counts = [int(count) for count in LOCAL_RULE_EVENT.findall(log)]
            if workspace_marker not in log or 1 not in local_counts:
                continue
            sentinels = set(RULE_SENTINEL_PATTERN.findall(log))
            rule_path_logged = COMPILED_RULE_NAME in log
            identity_logged = bool(sentinels or rule_path_logged)
            if identity_logged and not (
                state.rule_sentinel in sentinels or str(state.compiled_rule) in log
            ):
                continue
            plugin_counts = [int(count) for count in PLUGIN_RULE_EVENT.findall(log)]
            plugin_count = plugin_counts[-1] if plugin_counts else None
            plugin_detail = (
                f"; unrelated plugin services reported {plugin_count} rule(s)"
                if plugin_count is not None
                else ""
            )
            loading_detail = (
                "Cursor Desktop loader diagnostics logged the owned rule identity "
                "and loaded"
                if identity_logged
                else "Cursor Desktop loader diagnostics do not expose rule path or "
                "content; "
                "the owned workspace loaded"
            )
            return ProbeResult(
                ProbeStatus.LOADED,
                f"{loading_detail} one ancestor rule and the retired "
                f"coding-agents-rules plugin is absent{plugin_detail}",
                state.cursor_version,
                1 + len(local_counts) + len(plugin_counts) + int(identity_logged),
                local_rule_count=1,
                plugin_rule_count=plugin_count,
                generated_plugin_present=False,
            )
    except OSError as error:
        raise ProbeError(f"Cursor Agent Exec log is unreadable: {error}") from error
    return None


def verify_probe(
    state: ProbeState, *, timeout: float = 30, interval: float = 0.2
) -> ProbeResult:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := inspect_rule_loading(state):
            return result
        time.sleep(interval)
    return ProbeResult(
        ProbeStatus.HARNESS_ERROR,
        "Cursor Desktop did not log the owned workspace with one ancestor rule",
        state.cursor_version,
    )


def _quit_owned_cursor(state: ProbeState) -> None:
    if state.launcher_pid:
        try:
            exited_pid, _ = os.waitpid(state.launcher_pid, os.WNOHANG)
        except ChildProcessError:
            exited_pid = 0
        if exited_pid:
            return
        try:
            inspection = subprocess.run(
                (
                    "ps",
                    "-ww",
                    "-p",
                    str(state.launcher_pid),
                    "-o",
                    "pgid=",
                    "-o",
                    "command=",
                ),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise ProbeError(f"cannot inspect owned Cursor process: {error}") from error
        if inspection.returncode != 0:
            return
        try:
            process_group, command = inspection.stdout.strip().split(maxsplit=1)
        except ValueError as error:
            raise ProbeError("cannot verify owned Cursor process group") from error
        if (
            process_group != str(state.launcher_pid)
            or str(state.workspace) not in command
        ):
            raise ProbeError("refusing to quit a Cursor process not owned by probe")
        try:
            subprocess.run(
                (
                    "/usr/bin/osascript",
                    "-e",
                    OWNED_CURSOR_QUIT_SCRIPT,
                ),
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            for _ in range(100):
                try:
                    exited_pid, _ = os.waitpid(state.launcher_pid, os.WNOHANG)
                except ChildProcessError:
                    exited_pid = 0
                if exited_pid:
                    return
                try:
                    os.killpg(state.launcher_pid, 0)
                except ProcessLookupError:
                    return
                time.sleep(0.1)
        except ProcessLookupError:
            return
        except OSError as error:
            raise ProbeError(f"cannot quit owned Cursor process: {error}") from error
        except subprocess.CalledProcessError as error:
            detail = error.stderr.strip() if error.stderr else str(error)
            raise ProbeError(f"cannot quit owned Cursor process: {detail}") from error
        except subprocess.SubprocessError as error:
            raise ProbeError(
                f"cannot quit owned Cursor process cleanly: {error}"
            ) from error
        raise ProbeError("owned Cursor process did not exit after a clean quit")


def _cleanup_state(state: ProbeState) -> None:
    if (
        state.schema != PROBE_SCHEMA
        or RUN_ID_PATTERN.fullmatch(state.run_id) is None
        or state.path.name != state.run_id
        or state.path.is_symlink()
    ):
        raise ProbeError("refusing to remove probe state with invalid ownership")
    if not state.path.exists():
        return
    _quit_owned_cursor(state)
    trash = Path("/usr/bin/trash")
    if not trash.is_file():
        raise ProbeError("macOS trash utility is unavailable")
    try:
        subprocess.run((str(trash), str(state.path)), check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as error:
        raise ProbeError(f"cannot trash owned probe state: {error}") from error


def cleanup_probe(run_id: str, *, root: Path = PROBE_ROOT) -> None:
    _cleanup_state(load_probe(run_id, root=root))


def _emit(value: ProbeState | ProbeResult | dict[str, object]) -> None:
    payload = asdict(value) if isinstance(value, (ProbeState, ProbeResult)) else value
    typer.echo(json.dumps(payload, sort_keys=True))


def _fail(error: ProbeError) -> Never:
    _emit({"status": error.status, "detail": str(error)})
    raise typer.Exit(code=2 if error.status is ProbeStatus.UNAVAILABLE else 3)


main = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    help="Probe Cursor Desktop loading of an ancestor user rule without overlap.",
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
)


@main.command(help="Compile a probe rule and launch an owned Desktop window.")
def prepare() -> None:
    try:
        state = prepare_probe()
    except ProbeError as error:
        _fail(error)
    _emit(
        {
            **asdict(state),
            "status": ProbeStatus.PREPARED,
            "workspace": str(state.workspace),
        }
    )


@main.command(help="Verify Cursor Desktop loaded the owned ancestor rule.")
def verify(
    run_id: Annotated[str, typer.Argument()],
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=0, show_default=True),
    ] = 30,
) -> None:
    try:
        result = verify_probe(load_probe(run_id), timeout=timeout)
    except ProbeError as error:
        _fail(error)
    _emit(result)
    if result.status is not ProbeStatus.LOADED:
        raise typer.Exit(code=3)


@main.command(help="Run the non-interactive Desktop rule-loading probe.")
def run(
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=0, show_default=True),
    ] = 30,
) -> None:
    state: ProbeState | None = None
    result: ProbeResult | None = None
    error: ProbeError | None = None
    try:
        state = prepare_probe()
        result = verify_probe(state, timeout=timeout)
    except Exception as caught:
        error = (
            caught
            if isinstance(caught, ProbeError)
            else ProbeError(f"Cursor Desktop probe failed: {caught}")
        )
    finally:
        if state is not None:
            try:
                cleanup_probe(state.run_id)
            except ProbeError as caught:
                error = (
                    ProbeError(f"{error}; cleanup also failed: {caught}")
                    if error is not None
                    else caught
                )
    if error is not None:
        _fail(error)
    assert result is not None
    _emit(result)
    if result.status is not ProbeStatus.LOADED:
        raise typer.Exit(code=3)


@main.command(help="Trash only the state owned by one probe run.")
def cleanup(run_id: Annotated[str, typer.Argument()]) -> None:
    try:
        cleanup_probe(run_id)
    except ProbeError as error:
        _fail(error)
    _emit({"status": ProbeStatus.CLEANED, "run_id": run_id})


if __name__ == "__main__":
    main()
