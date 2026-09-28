"""Compiled OpenCode shell denies hold through wrappers.

The compiler emits each deny bare and installs the bundled opencode-unwrap
plugin, which denies a command when the rules deny it with its assignments,
program path, or wrappers peeled. This runs OpenCode on exactly what
`run_sync` writes; the same home without the plugin is the control.
"""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
)
from capabilities.protocols.openai import ToolResponder
from capabilities.protocols.opencode import OpenCodeEvent, ToolUseObservation
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.opencode import config as opencode_config
from capabilities.targets.opencode import environment
from coding_agents_sync import run_sync

RECORDER = "unwrap-recorder"
TOOL = "unwrap-tool"
REASON = "run unwrap-tool instead"
POLICY = """\
schema: coding-agents/v4
kind: permission-policy
id: user
name: user
description: permissions
unmatched: allow
wrappers: [timeout, env, nohup, sudo]
targets:
  codex: {omit: {unmatched: Codex gates unmatched commands by sandbox.}}
  cursor: {omit: {unmatched: Cursor Desktop has no deny channel.}}
"""
RULES = f"""\
schema: coding-agents/v4
kind: permission-rules
id: test
name: test
description: permission rules
deny:
  - {{command: {RECORDER}, reason: {REASON}}}
  - [{TOOL}, push]
targets:
  cursor: {{omit: {{commands.deny: Cursor Desktop has no deny channel.}}}}
"""
ASK_POLICY = """\
schema: coding-agents/v4
kind: permission-policy
id: user
name: user
description: permissions
wrappers: [timeout]
"""
ASK_RULES = f"""\
schema: coding-agents/v4
kind: permission-rules
id: test
name: test
description: permission rules
allow:
  - [{TOOL}, status]
targets:
  codex: {{omit: {{commands.allow: Codex allow rules skip its sandbox approval.}}}}
"""
# One per integration path: a wrapper, an assignment, and a program path in
# front of a single-token deny, and a wrapper in front of a multi-token one.
# The plugin's own golden table covers the peeling itself.
DENIED = {
    "timeout": f"timeout 30 {RECORDER} timeout",
    "assign": f"FOO=1 {RECORDER} assign",
    "absolute": f"{{bin}}/{RECORDER} absolute",
    "subcommand": f"timeout 30 {TOOL} push subcommand",
}


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    state: ToolResponder
    env: dict[str, str]
    recorder_dir: Path
    config: Path


def executable(path: Path, script: str) -> None:
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    return build(request, tmp_path_factory, POLICY, RULES)


@pytest.fixture(scope="module")
def asking(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    """Unmatched commands ask, and one subcommand is allowed."""
    return build(request, tmp_path_factory, ASK_POLICY, ASK_RULES)


def build(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    policy: str,
    rules: str,
) -> Runtime:
    opencode, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-unwrap").resolve())
    recorder_dir = paths.root / "recorder"
    recorder_dir.mkdir()
    for name in (RECORDER, TOOL):
        executable(
            paths.bin / name,
            '#!/bin/sh\nfor a in "$@"; do last="$a"; done\n'
            '/usr/bin/touch "$RECORDER_DIR/$last"\n',
        )
    executable(paths.bin / "timeout", '#!/bin/sh\nshift\nexec "$@"\n')
    state = ToolResponder(
        "shell",
        {},
        "unwrap-probe",
        "call_unwrap_probe",
        "unwrap probe complete",
        result_role="assistant",
        split_role=True,
    )
    stub = recorded_server(request, state.respond)
    sources = paths.root / "sources" / "permissions"
    (sources / "commands").mkdir(parents=True)
    (sources / "policy.yaml").write_text(policy)
    (sources / "commands" / "test.yaml").write_text(rules)
    config = paths.home / ".config" / "opencode" / "opencode.json"
    config.parent.mkdir(parents=True)
    base = opencode_config(stub.base_url, "unwrap-probe", "unwrap", {})
    del base["permission"]
    config.write_text(json.dumps(base))
    run_sync(config_root=sources.parent, home=paths.home)
    env = environment(
        paths,
        config,
        f"{paths.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        RECORDER_DIR=str(recorder_dir),
        XDG_CONFIG_HOME=str(paths.home / ".config"),
    )
    value = Runtime(
        opencode,
        loopback_seatbelt(sandbox),
        paths,
        stub,
        state,
        env,
        recorder_dir,
        config,
    )
    require_containment(value.seatbelt, stub, paths, env)
    return value


def run_shell(
    runtime: Runtime, command: str, *, asked: bool = False
) -> ToolUseObservation | None:
    runtime.state.tool = "shell"
    runtime.state.arguments = {"command": command.format(bin=runtime.paths.bin)}
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.executable,
            "run",
            "Call the shell tool exactly as instructed by the model.",
            "--standalone",
            "--format",
            "json",
            "--model",
            "test/unwrap-probe",
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
    )
    if asked:
        # `opencode run` rejects a permission request and exits nonzero.
        assert "permission requested: shell" in process.stderr, process.stderr
        return None
    assert process.returncode == 0, process.stderr
    return next(
        (
            value
            for item in OpenCodeEvent.decode_lines(process.stdout)
            if (value := item.tool_use()) is not None and value.tool == "shell"
        ),
        None,
    )


def ran(runtime: Runtime, marker: str) -> bool:
    return (runtime.recorder_dir / marker).exists()


@pytest.mark.capability_case("opencode.unwrap")
@pytest.mark.capability_live
def test_compiled_rules_carry_no_wrapper_copies(runtime: Runtime) -> None:
    shell = [
        rule["resource"]
        for rule in json.loads(runtime.config.read_text())["permissions"]
        if rule["action"] == "shell"
    ]
    assert shell == ["*", f"{RECORDER} *", f"{TOOL} push *"]


@pytest.mark.capability_case("opencode.unwrap")
@pytest.mark.capability_live
@pytest.mark.parametrize("marker", DENIED)
def test_wrapped_denied_command_is_denied(runtime: Runtime, marker: str) -> None:
    observation = run_shell(runtime, DENIED[marker])
    assert observation is not None and observation.status == "error", observation
    assert "is denied by the shell rules" in (observation.error or ""), observation
    assert not ran(runtime, marker)


@pytest.mark.capability_case("opencode.unwrap")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    "command", [f"{RECORDER} direct", f"timeout 30 {RECORDER} wrapped"]
)
def test_a_denied_command_names_its_reason(runtime: Runtime, command: str) -> None:
    """OpenCode's own deny and a peeled one both tell the model what to run."""
    observation = run_shell(runtime, command)
    assert observation is not None and observation.status == "error", observation
    assert REASON in (observation.error or ""), observation
    assert not ran(runtime, command.rsplit(" ", 1)[1])


@pytest.mark.capability_case("opencode.unwrap")
@pytest.mark.capability_live
def test_peeled_allowed_command_runs(runtime: Runtime) -> None:
    observation = run_shell(runtime, f"nohup {TOOL} status allowed")
    assert observation is not None and observation.status == "completed", observation
    assert ran(runtime, "allowed")


@pytest.mark.capability_case("opencode.unwrap")
@pytest.mark.capability_live
def test_without_the_plugin_the_wrapped_command_runs(runtime: Runtime) -> None:
    plugins = runtime.paths.home / ".config" / "opencode" / "plugins"
    moved = plugins.with_name("plugins.off")
    plugins.rename(moved)
    try:
        observation = run_shell(runtime, f"timeout 30 {RECORDER} control")
    finally:
        moved.rename(plugins)
    assert observation is not None and observation.status == "completed", observation
    assert ran(runtime, "control")


@pytest.mark.capability_case("opencode.unwrap")
@pytest.mark.capability_live
def test_an_allow_carries_through_a_transparent_wrapper(asking: Runtime) -> None:
    """`timeout 30 tool status` falls to the catch-all ask; the plugin allows it."""
    observation = run_shell(asking, f"timeout 30 {TOOL} status through")
    assert observation is not None and observation.status == "completed", observation
    assert ran(asking, "through")


@pytest.mark.capability_case("opencode.unwrap")
@pytest.mark.capability_live
def test_a_wrapped_unallowed_command_still_asks(asking: Runtime) -> None:
    assert run_shell(asking, f"timeout 30 {TOOL} log asked", asked=True) is None
    assert not ran(asking, "asked")


@pytest.mark.capability_case("opencode.unwrap")
@pytest.mark.capability_live
def test_without_the_plugin_a_wrapped_allowed_command_asks(asking: Runtime) -> None:
    plugins = asking.paths.home / ".config" / "opencode" / "plugins"
    moved = plugins.with_name("plugins.off")
    plugins.rename(moved)
    try:
        command = f"timeout 30 {TOOL} status unwrapped"
        assert run_shell(asking, command, asked=True) is None
    finally:
        moved.rename(plugins)
    assert not ran(asking, "unwrapped")
