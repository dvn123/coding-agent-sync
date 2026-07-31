from __future__ import annotations

import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from capabilities.harness import (
    Paths,
    cached_scenario_fixture,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
)
from capabilities.model import CheckResult
from capabilities.protocols.openai import ToolResponder
from capabilities.protocols.opencode import (
    OpenCodeEvent,
    ToolUseObservation,
    decode_config,
)
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.opencode import config as opencode_config
from capabilities.targets.opencode import environment, supported

RECORDER = "blackbox-recorder"
ALLOWED_COMMAND = f"{RECORDER} allowed"
DENIED_COMMAND = f"{RECORDER} denied"
WRAPPED_COMMAND = f"timeout 30 {RECORDER} wrapped"
NEAR_PREFIX_COMMAND = f"{RECORDER}x near-prefix"
OPTC = "blackbox-opt"
CHAIN_ALLOW_PATTERN = f"{RECORDER} ok*"
DENIAL_TEXT = "prevents you from using this specific tool call"
RECORDER_ARGUMENT = re.compile(rf"{RECORDER} ([\w-]+)")

# Every chain pairs an allowed segment with a denied one and repeats the
# operator with both segments allowed, so a refusal is attributable to
# segmentation rather than to the operator itself. `||` short-circuits, so its
# control only reaches the left segment.
CHAINS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "chain-and": (f"{RECORDER} ok-and && {RECORDER} no-and", "denied", ()),
    "chain-and-allowed": (
        f"{RECORDER} ok-and-a && {RECORDER} ok-and-b",
        "completed",
        ("ok-and-a", "ok-and-b"),
    ),
    "chain-or": (f"{RECORDER} ok-or || {RECORDER} no-or", "denied", ()),
    "chain-or-allowed": (
        f"{RECORDER} ok-or-a || {RECORDER} ok-or-b",
        "completed",
        ("ok-or-a",),
    ),
    "chain-pipe": (f"{RECORDER} ok-pipe | {RECORDER} no-pipe", "denied", ()),
    "chain-pipe-allowed": (
        f"{RECORDER} ok-pipe-a | {RECORDER} ok-pipe-b",
        "completed",
        ("ok-pipe-a", "ok-pipe-b"),
    ),
    "chain-semicolon": (f"{RECORDER} ok-semi ; {RECORDER} no-semi", "denied", ()),
    "chain-semicolon-allowed": (
        f"{RECORDER} ok-semi-a ; {RECORDER} ok-semi-b",
        "completed",
        ("ok-semi-a", "ok-semi-b"),
    ),
    "chain-newline": (f"{RECORDER} ok-line\n{RECORDER} no-line", "denied", ()),
    "chain-newline-allowed": (
        f"{RECORDER} ok-line-a\n{RECORDER} ok-line-b",
        "completed",
        ("ok-line-a", "ok-line-b"),
    ),
    "chain-substitution": (
        f"{RECORDER} ok-subst $({RECORDER} no-subst)",
        "denied",
        (),
    ),
    "chain-substitution-allowed": (
        f"{RECORDER} ok-subst-a $({RECORDER} ok-subst-b)",
        "completed",
        ("ok-subst-a", "ok-subst-b"),
    ),
    "chain-backtick": (f"{RECORDER} ok-tick `{RECORDER} no-tick`", "denied", ()),
    "chain-backtick-allowed": (
        f"{RECORDER} ok-tick-a `{RECORDER} ok-tick-b`",
        "completed",
        ("ok-tick-a", "ok-tick-b"),
    ),
    # A quoted operator is an argument of one allowed command, not a segment.
    "quoted-operator": (
        f'{RECORDER} ok-quoted "x && {RECORDER} no-quoted"',
        "completed",
        ("ok-quoted",),
    ),
    # Codex's word-only parser bails on `$VAR` and stops applying its rules;
    # check whether this matcher keeps enforcing through an expansion.
    "denied-with-variable": (f"{RECORDER} no-var $HOME", "denied", ()),
    # Each interpreter is allowed outright and carries a payload that would be
    # denied on its own, so execution shows the payload is never inspected.
    "interpreter-sh": (f"sh -c '{RECORDER} no-sh'", "completed", ("no-sh",)),
    "interpreter-bash": (f"bash -c '{RECORDER} no-bash'", "completed", ("no-bash",)),
    "interpreter-eval": (f"eval '{RECORDER} no-eval'", "completed", ("no-eval",)),
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
    secret: Path
    example: Path
    external_denied: Path
    external_allowed: Path


@dataclass(frozen=True, slots=True)
class ToolResult:
    process_returncode: int
    observation: ToolUseObservation | None


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-permissions").resolve())
    recorder_dir = paths.root / "recorder"
    recorder_dir.mkdir()
    recorder = paths.bin / RECORDER
    recorder.write_text('#!/bin/sh\n/usr/bin/touch "$RECORDER_DIR/$1"\n')
    recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR)
    recorder.with_name(f"{RECORDER}x").symlink_to(recorder)
    optrec = paths.bin / OPTC
    optrec.write_text(
        '#!/bin/sh\nfor a in "$@"; do last="$a"; done\n'
        '/usr/bin/touch "$RECORDER_DIR/$last"\n'
    )
    optrec.chmod(optrec.stat().st_mode | stat.S_IXUSR)
    wrapper = paths.bin / "timeout"
    wrapper.write_text('#!/bin/sh\nshift\nexec "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    secret, example = paths.work / ".env", paths.work / ".env.example"
    secret.write_text("SECRET=blocked\n")
    example.write_text("EXAMPLE=allowed\n")
    external_denied_dir = paths.root / "external-denied"
    external_allowed_dir = paths.root / "external-allowed"
    external_denied_dir.mkdir()
    external_allowed_dir.mkdir()
    external_denied = external_denied_dir / "value.txt"
    external_allowed = external_allowed_dir / "value.txt"
    external_denied.write_text("blocked\n")
    external_allowed.write_text("allowed\n")
    state = ToolResponder(
        "bash",
        {},
        "permission-probe",
        "call_permission_probe",
        "permission probe complete",
        result_role="assistant",
        split_role=True,
    )
    stub = recorded_server(request, state.respond)
    config = paths.root / "opencode.json"
    permission = {
        "bash": {
            "*": "allow",
            f"{RECORDER} *": "deny",
            ALLOWED_COMMAND: "allow",
            CHAIN_ALLOW_PATTERN: "allow",
            **dict.fromkeys(("sh -c *", "bash -c *", "eval *"), "allow"),
            # PROBE A2: broad deny, then a narrower option-tolerant allow,
            # then guards. Order alone must decide all three.
            f"{OPTC} *": "deny",
            f"{OPTC} -C * status *": "allow",
            f"{OPTC} * -c *": "deny",
            f"{OPTC} * push *": "deny",
        },
        "read": {
            "*": "allow",
            "**/.env*": "deny",
            "**/.env.example": "allow",
        },
        "edit": {
            "*": "allow",
            "**/.env*": "deny",
            "**/.env.example": "allow",
        },
        "external_directory": {
            "*": "allow",
            f"{external_denied_dir}/*": "deny",
            f"{external_allowed_dir}/*": "allow",
        },
    }
    config.write_text(
        json.dumps(
            opencode_config(stub.base_url, "permission-probe", "permission", permission)
        )
    )
    env = environment(
        paths,
        config,
        f"{paths.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        RECORDER_DIR=str(recorder_dir),
    )
    value = Runtime(
        executable,
        loopback_seatbelt(sandbox),
        paths,
        stub,
        state,
        env,
        recorder_dir,
        secret,
        example,
        external_denied,
        external_allowed,
    )
    require_containment(
        value.seatbelt,
        stub,
        paths,
        env,
    )
    return value


def run_tool(runtime: Runtime, tool: str, arguments: dict[str, Any]) -> ToolResult:
    runtime.state.tool, runtime.state.arguments = tool, arguments
    prompt = f"Call the {tool} tool exactly as instructed by the model."
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.executable,
            "run",
            prompt,
            "--pure",
            "--format",
            "json",
            "--model",
            "test/permission-probe",
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
    )
    events = OpenCodeEvent.decode_lines(process.stdout)
    observation = next(
        (
            value
            for item in events
            if (value := item.tool_use()) is not None and value.tool == tool
        ),
        None,
    )
    return ToolResult(process.returncode, observation)


def denied(result: ToolResult, permission: str) -> bool:
    if result.observation is None:
        return False
    error = result.observation.error
    return (
        result.observation.status == "error"
        and DENIAL_TEXT in error
        and f'"permission":"{permission}"' in error
        and '"action":"deny"' in error
    )


def observe(runtime: Runtime, name: str) -> CheckResult:
    if name == "config":
        process = supported(
            "the `debug config` inspector",
            run,
            *runtime.seatbelt.command(runtime.executable, "debug", "config", "--pure"),
            cwd=runtime.paths.work,
            env=runtime.env,
            timeout=30,
        )
        resolved = decode_config(process.stdout)
        return CheckResult(
            {
                "resolved-deny": resolved.raw["permission"]["bash"][f"{RECORDER} *"]
                == "deny"
            },
            resolved.text("permission"),
        )

    scenarios = {
        "allowed": (
            "bash",
            {"command": ALLOWED_COMMAND, "description": "permission probe"},
        ),
        "denied-bash": (
            "bash",
            {"command": DENIED_COMMAND, "description": "permission probe"},
        ),
        "near-prefix": (
            "bash",
            {"command": NEAR_PREFIX_COMMAND, "description": "permission probe"},
        ),
        "wrapped": (
            "bash",
            {"command": WRAPPED_COMMAND, "description": "permission probe"},
        ),
        "probe-opt-plain": (
            "bash",
            {"command": f"{OPTC} -C /tmp status optplain", "description": "probe"},
        ),
        "probe-opt-broad": (
            "bash",
            {"command": f"{OPTC} other optbroad", "description": "probe"},
        ),
        "probe-opt-smuggle-flag": (
            "bash",
            {
                "command": f"{OPTC} -C /tmp -c core.pager=evil status optflag",
                "description": "probe",
            },
        ),
        "probe-opt-smuggle-sub": (
            "bash",
            {
                "command": f"{OPTC} -C /tmp push origin status optsub",
                "description": "probe",
            },
        ),
        "denied-read": ("read", {"filePath": str(runtime.secret)}),
        "allowed-read": ("read", {"filePath": str(runtime.example)}),
        "denied-edit": (
            "edit",
            {
                "filePath": str(runtime.secret),
                "oldString": "SECRET=blocked",
                "newString": "SECRET=edited",
            },
        ),
        "allowed-edit": (
            "edit",
            {
                "filePath": str(runtime.example),
                "oldString": "EXAMPLE=allowed",
                "newString": "EXAMPLE=edited",
            },
        ),
        "denied-external": (
            "read",
            {"filePath": str(runtime.external_denied)},
        ),
        "allowed-external": (
            "read",
            {"filePath": str(runtime.external_allowed)},
        ),
    } | {
        chain: ("bash", {"command": command, "description": "permission probe"})
        for chain, (command, _, _) in CHAINS.items()
    }
    if name == "allowed-edit":
        runtime.example.write_text("EXAMPLE=allowed\n")
    result = run_tool(runtime, *scenarios[name])
    assert result.process_returncode == 0, f"{name}: OpenCode exited nonzero"
    observation = result.observation
    completed = observation is not None and observation.status == "completed"
    if (chain := CHAINS.get(name)) is not None:
        command, outcome, executed = chain
        ran = frozenset(
            argument
            for argument in RECORDER_ARGUMENT.findall(command)
            if (runtime.recorder_dir / argument).exists()
        )
        return CheckResult(
            {
                f"{name}-outcome": completed
                if outcome == "completed"
                else denied(result, "bash"),
                f"{name}-markers": ran == frozenset(executed),
            },
            f"{observation} ran={sorted(ran)}",
        )
    checks = {
        "allowed": {
            "allowed-completed": completed,
            "allowed-executed": (runtime.recorder_dir / "allowed").exists(),
        },
        "denied-bash": {
            "denied-bash": denied(result, "bash"),
            "denied-not-executed": not (runtime.recorder_dir / "denied").exists(),
        },
        "near-prefix": {
            "near-prefix-completed": completed,
            "near-prefix-executed": (runtime.recorder_dir / "near-prefix").exists(),
        },
        # A wrapped command is one node headed by the wrapper, so the inner
        # `blackbox-recorder *` deny never applies and it runs.
        "wrapped": {
            "wrapped-completed": completed,
            "wrapped-executed": (runtime.recorder_dir / "wrapped").exists(),
        },
        "probe-opt-plain": {
            "probe-opt-plain-completed": completed,
            "probe-opt-plain-executed": (runtime.recorder_dir / "optplain").exists(),
        },
        "probe-opt-broad": {
            "probe-opt-broad-denied": denied(result, "bash"),
            "probe-opt-broad-not-executed": not (
                runtime.recorder_dir / "optbroad"
            ).exists(),
        },
        "probe-opt-smuggle-flag": {
            "probe-opt-smuggle-flag-denied": denied(result, "bash"),
            "probe-opt-smuggle-flag-not-executed": not (
                runtime.recorder_dir / "optflag"
            ).exists(),
        },
        "probe-opt-smuggle-sub": {
            "probe-opt-smuggle-sub-denied": denied(result, "bash"),
            "probe-opt-smuggle-sub-not-executed": not (
                runtime.recorder_dir / "optsub"
            ).exists(),
        },
        "denied-read": {"denied-read": denied(result, "read")},
        "allowed-read": {"allowed-read": completed},
        "denied-edit": {
            "denied-edit": observation is not None
            and observation.status == "error"
            and DENIAL_TEXT in observation.error,
            "denied-edit-unchanged": runtime.secret.read_text() == "SECRET=blocked\n",
        },
        "allowed-edit": {
            "allowed-edit": completed,
            "allowed-edit-changed": runtime.example.read_text() == "EXAMPLE=edited\n",
        },
        "denied-external": {"denied-external": denied(result, "external_directory")},
        "allowed-external": {"allowed-external": completed},
    }[name]
    return CheckResult(checks, str(observation))


observation = cached_scenario_fixture(observe)


EXPECTATIONS = (
    ("config", "resolved-deny"),
    ("allowed", "allowed-completed"),
    ("allowed", "allowed-executed"),
    ("denied-bash", "denied-bash"),
    ("denied-bash", "denied-not-executed"),
    ("near-prefix", "near-prefix-completed"),
    ("near-prefix", "near-prefix-executed"),
    ("wrapped", "wrapped-completed"),
    ("wrapped", "wrapped-executed"),
    ("probe-opt-plain", "probe-opt-plain-completed"),
    ("probe-opt-plain", "probe-opt-plain-executed"),
    ("probe-opt-broad", "probe-opt-broad-denied"),
    ("probe-opt-broad", "probe-opt-broad-not-executed"),
    ("probe-opt-smuggle-flag", "probe-opt-smuggle-flag-denied"),
    ("probe-opt-smuggle-flag", "probe-opt-smuggle-flag-not-executed"),
    ("probe-opt-smuggle-sub", "probe-opt-smuggle-sub-denied"),
    ("probe-opt-smuggle-sub", "probe-opt-smuggle-sub-not-executed"),
    ("denied-read", "denied-read"),
    ("allowed-read", "allowed-read"),
    ("denied-edit", "denied-edit"),
    ("denied-edit", "denied-edit-unchanged"),
    ("allowed-edit", "allowed-edit"),
    ("allowed-edit", "allowed-edit-changed"),
    ("denied-external", "denied-external"),
    ("allowed-external", "allowed-external"),
    *(
        (chain, f"{chain}-{check}")
        for chain in CHAINS
        for check in ("outcome", "markers")
    ),
)


@pytest.mark.capability_case("opencode.permissions")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check"),
    tuple(pytest.param(scenario, check, id=check) for scenario, check in EXPECTATIONS),
    indirect=("observation",),
    scope="module",
)
def test_opencode_permissions(observation: CheckResult, check: str) -> None:
    assert observation.checks[check], observation.detail
