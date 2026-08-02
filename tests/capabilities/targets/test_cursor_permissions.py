from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    cached_scenario_fixture,
    recorded_server,
    require_containment,
    run_probe,
)
from capabilities.model import TextObservation
from capabilities.protocols.cursor import CursorEvent
from capabilities.protocols.openai import ToolResponder
from capabilities.runtime import Seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.cursor import (
    command as cursor_command,
)
from capabilities.targets.cursor import (
    environment,
)
from capabilities.targets.cursor import (
    runtime as cursor_runtime,
)

DENIED = "blackbox-denied"
ALLOWED = "blackbox-allowed"
NEAR_PREFIX = "blackbox-allowedx"
EXACT = "blackbox-exact"
TRAILING = "blackbox-trailing"
UNMATCHED = "blackbox-unmatched"
OPT = "blackbox-opt"
WRAP_OK = "twrap"
WRAP2 = "twrap2"
API = "blackbox-api"
OPT2 = "blackbox-opt2"
SMART = "blackbox-smart"
DESKTOP = "blackbox-desktop"
HOOKED = "blackbox-hooked"
BARE_EXACT = "blackbox-texact"
MODEL = "permission-probe"


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    state: ToolResponder
    env: dict[str, str]
    markers: dict[str, Path]


@dataclass(frozen=True, slots=True)
class PermissionResult:
    returncode: int
    executed: frozenset[str]
    outcome: TextObservation | None


# `rejected` is a declined approval prompt, `permissionDenied` a deny-list
# match, and `--force` approves prompts, so the forced scenarios separate the
# unmatched fallback from a denial. Each scenario names the recorders that must
# have run, so the `chain-*` scenarios show whether one allowed segment carries
# an unlisted one.
SCENARIOS = {
    "denied-exact": (DENIED, "permissionDenied", (), False),
    "denied-arguments": (f"{DENIED} --flag", "permissionDenied", (), False),
    "denied-forced": (DENIED, "permissionDenied", (), True),
    "allowed-exact": (ALLOWED, "success", (ALLOWED,), False),
    "allowed-arguments": (f"{ALLOWED} --flag", "success", (ALLOWED,), False),
    "allowed-near-prefix": (NEAR_PREFIX, "rejected", (), False),
    "exact": (f"{EXACT} status", "success", (EXACT,), False),
    "exact-extra-argument": (f"{EXACT} status --short", "rejected", (), False),
    "trailing-exact": (f"{TRAILING} status", "success", (TRAILING,), False),
    "trailing-arguments": (f"{TRAILING} status --short", "success", (TRAILING,), False),
    "trailing-near-prefix": (f"{TRAILING} statusx", "rejected", (), False),
    "unmatched": (UNMATCHED, "rejected", (), False),
    "unmatched-forced": (UNMATCHED, "success", (UNMATCHED,), True),
    "chain-and": (f"{ALLOWED} a && {UNMATCHED} b", "rejected", (), False),
    "chain-pipe": (f"{ALLOWED} a | {UNMATCHED} b", "rejected", (), False),
    "chain-semicolon": (f"{ALLOWED} a ; {UNMATCHED} b", "rejected", (), False),
    "chain-substitution": (f"{ALLOWED} $({UNMATCHED} b)", "rejected", (), False),
    # Codex's word-only parser bails on `$VAR` and stops applying its rules;
    # check whether this matcher keeps enforcing through an expansion.
    "denied-with-variable": (f"{DENIED} $HOME", "permissionDenied", (), False),
    "chain-or": (f"{ALLOWED} a || {UNMATCHED} b", "rejected", (), False),
    "chain-backtick": (f"{ALLOWED} `{UNMATCHED} b`", "rejected", (), False),
    "chain-newline": (f"{ALLOWED} a\n{UNMATCHED} b", "rejected", (), False),
    "quoted-operator": (
        f'{ALLOWED} "a && {UNMATCHED} b"',
        "success",
        (ALLOWED,),
        False,
    ),
    # An allowlisted interpreter carries its payload as an argument, so
    # segmentation never sees the inner command and it runs unapproved.
    "interpreter-sh-c": (f"sh -c '{UNMATCHED} b'", "success", (UNMATCHED,), False),
    "interpreter-eval": (f"eval '{UNMATCHED} b'", "success", (UNMATCHED,), False),
    "chain-denied-segment": (
        f"{ALLOWED} a && {DENIED} b",
        "permissionDenied",
        (),
        False,
    ),
    "chain-and-both-allowed": (
        f"{ALLOWED} a && {TRAILING} status",
        "success",
        (ALLOWED, TRAILING),
        False,
    ),
    "chain-pipe-both-allowed": (
        f"{ALLOWED} a | {TRAILING} status",
        "success",
        (ALLOWED, TRAILING),
        False,
    ),
    "chain-semicolon-both-allowed": (
        f"{ALLOWED} a ; {TRAILING} status",
        "success",
        (ALLOWED, TRAILING),
        False,
    ),
    # PROBE P1: does Cursor resolve an exec wrapper to the inner command?
    "probe-wrapper-inner-only": (f"timeout 30 {ALLOWED} a", "rejected", (), False),
    "probe-wrapper-head-allowed": (
        f"{WRAP_OK} 30 {ALLOWED} a",
        "success",
        (ALLOWED,),
        False,
    ),
    # PROBE P2: interior wildcard. one=token-bounded ok, two=greedy, zero=empty.
    "probe-interior-one": (f"{OPT} -C /tmp status", "success", (OPT,), False),
    "probe-interior-two": (
        f"{OPT} -C /tmp -c evil status",
        "permissionDenied",
        (),
        False,
    ),
    # The interior hole needs at least one token; it does not match empty.
    "probe-interior-zero": (f"{OPT} -C status", "rejected", (), False),
    "probe-interior-tail": (f"{OPT} -C /tmp status --short", "success", (OPT,), False),
    # PROBE P2b: can the greedy hole smuggle a whole subcommand past the guard?
    "probe-smuggle-subcommand": (
        f"{OPT} -C /tmp push origin status",
        "permissionDenied",
        (),
        False,
    ),
    "probe-guarded-forced": (
        f"{OPT} -C /tmp push origin status",
        "permissionDenied",
        (),
        True,
    ),
    "probe-guard-spares-plain": (f"{OPT} -C /tmp status", "success", (OPT,), False),
    # Negative control: the literal option name really is anchored.
    "probe-wrong-option": (f"{OPT} -X /tmp status", "rejected", (), False),
    # Does the hole span a quoted argument containing spaces only, or tokens?
    "probe-smuggle-flagvalue": (
        f"{OPT} -C /tmp -c core.pager=evil status",
        "permissionDenied",
        (),
        False,
    ),
    "probe-api-plain": (f"{API} api /repos/x", "success", (API,), False),
    "probe-api-guarded": (
        f"{API} api /repos/x -X DELETE",
        "permissionDenied",
        (),
        False,
    ),
    "probe-api-guarded-trailing": (
        f"{API} api /repos/x -X DELETE --silent",
        "permissionDenied",
        (),
        False,
    ),
    # If this succeeds the trailing star is optional after an interior hole.
    # The trailing `*` is not optional, so the bare form must also be emitted.
    "probe-trailing-optional": (f"{OPT2} -C /tmp status", "rejected", (), False),
    "chain-substitution-allowed": (
        f"{ALLOWED} $({TRAILING} status)",
        "success",
        (ALLOWED, TRAILING),
        False,
    ),
    # A wrapper allow attached to its rule covers only that payload; the
    # leading hole covers the wrapper's own arguments.
    "wrapper-attached-exact": (f"{WRAP2} {ALLOWED}", "success", (ALLOWED,), False),
    "wrapper-attached-args": (f"{WRAP2} {ALLOWED} a", "success", (ALLOWED,), False),
    "wrapper-attached-wrapper-args": (
        f"{WRAP2} 30 {ALLOWED} a",
        "success",
        (ALLOWED,),
        False,
    ),
    "wrapper-attached-other-payload": (
        f"{WRAP2} {UNMATCHED} b",
        "rejected",
        (),
        False,
    ),
    # The hole the compiler must never emit: a bare wrapper allow matches any
    # payload the wrapper carries.
    "wrapper-blanket-hole": (
        f"{WRAP_OK} 30 {UNMATCHED} b",
        "success",
        (UNMATCHED,),
        False,
    ),
    # An explicit allow survives any redirect: the bundle's redirect-safety
    # classification gates auto-allow decisions, not a user allowlist entry,
    # so even a target outside the workspace runs (containment owns the
    # write itself).
    "redirect-workspace": (f"{ALLOWED} a > out.txt", "success", (ALLOWED,), False),
    "redirect-dev-null": (f"{ALLOWED} a > /dev/null", "success", (ALLOWED,), False),
    "redirect-outside": (
        f"{ALLOWED} a > $HOME/probe-out.txt",
        "success",
        (ALLOWED,),
        False,
    ),
    # smartAllowlistDenylist is a soft deny: it forces a prompt rather than
    # blocking, so --force still runs the command.
    "smart-prompt": (f"{SMART} a", "rejected", (), False),
    "smart-forced": (f"{SMART} a", "success", (SMART,), True),
    # The Desktop permissions.json beside cli-config.json is honored by the
    # same runtime: its terminalAllowlist allows this command alone.
    "desktop-allowlist": (f"{DESKTOP} status", "success", (DESKTOP,), False),
    # A beforeShellExecution hook denies an otherwise allowlisted command,
    # and --force does not override the hook verdict.
    "hook-denied": (f"{HOOKED} a", "rejected", (), False),
    "hook-denied-forced": (f"{HOOKED} a", "rejected", (), True),
    # PROBE: is `Shell(prog:)` (colon, empty argstring) the exact-bare form?
    # Valid means the bare command runs and any argument revokes the match.
    "bare-exact-exact": (BARE_EXACT, "success", (BARE_EXACT,), False),
    "bare-exact-args": (f"{BARE_EXACT} a", "rejected", (), False),
}


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, seatbelt = cursor_runtime()
    paths = Paths.create(tmp_path_factory.mktemp("cursor-permissions").resolve())
    cursor = paths.root / "cursor"
    cursor.mkdir()
    (cursor / "cli-config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "approvalMode": "allowlist",
                "permissions": {
                    "allow": [
                        f"Shell({ALLOWED})",
                        f"Shell({EXACT}:status)",
                        f"Shell({TRAILING}:status)",
                        f"Shell({TRAILING}:status *)",
                        # Allowlisting an interpreter asks whether the engine
                        # inspects the payload it carries.
                        "Shell(sh)",
                        "Shell(eval)",
                        # PROBE: interior wildcard between two literal tokens.
                        f"Shell({OPT}:-C * status)",
                        f"Shell({OPT}:-C * status *)",
                        # PROBE: allowlisted wrapper head, control for the shim.
                        f"Shell({WRAP_OK})",
                        # PROBE B: multi-token literal sequence after a hole.
                        f"Shell({API}:api *)",
                        # PROBE G: only the trailing-star form, no bare form.
                        f"Shell({OPT2}:-C * status *)",
                        # A wrapper allow attached to its rule, with and
                        # without the wrapper's own arguments. Never a bare
                        # `Shell(twrap2)`, which would allow any payload.
                        f"Shell({WRAP2}:{ALLOWED})",
                        f"Shell({WRAP2}:{ALLOWED} *)",
                        f"Shell({WRAP2}:* {ALLOWED})",
                        f"Shell({WRAP2}:* {ALLOWED} *)",
                        # Allowlisted, then clawed back by the hook below.
                        f"Shell({HOOKED})",
                        # PROBE: exact-bare form, colon with an empty
                        # argstring.
                        f"Shell({BARE_EXACT}:)",
                    ],
                    "deny": [
                        f"Shell({DENIED})",
                        # PROBE P2c: interior wildcard in the DENY channel,
                        # i.e. can a guard claw back the greedy hole?
                        f"Shell({OPT}:* push *)",
                        f"Shell({OPT}:* -c *)",
                        f"Shell({API}:api * -X DELETE *)",
                        f"Shell({API}:api * -X DELETE)",
                    ],
                    # Undocumented soft-deny channel: forces a prompt for
                    # the matched command instead of blocking it outright.
                    "smartAllowlistDenylist": [f"Shell({SMART})"],
                },
            }
        )
    )
    # The Desktop permissions file shares the CLI's permission provider, so
    # its terminalAllowlist is enforced by this runtime too.
    (cursor / "permissions.json").write_text(
        json.dumps({"approvalMode": "allowlist", "terminalAllowlist": [DESKTOP]})
    )
    hook = paths.bin / "shell-hook.sh"
    hook.write_text(
        "#!/bin/sh\n"
        "payload=$(cat)\n"
        'case "$payload" in\n'
        f"*{HOOKED}*) "
        'printf \'{"permission":"deny","user_message":"probe hook"}\' ;;\n'
        "esac\n"
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
    hooks_dir = paths.home / ".cursor"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {"beforeShellExecution": [{"command": str(hook)}]},
            }
        )
    )
    markers = {
        command: paths.root / f"{command}.executed"
        for command in (
            DENIED,
            ALLOWED,
            NEAR_PREFIX,
            EXACT,
            TRAILING,
            UNMATCHED,
            OPT,
            API,
            OPT2,
            SMART,
            DESKTOP,
            HOOKED,
            BARE_EXACT,
        )
    }
    for command, marker in markers.items():
        recorder = paths.bin / command
        recorder.write_text(f"#!/bin/sh\n/usr/bin/touch {marker}\n")
        recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR)
    for wrapper_name in ("timeout", WRAP_OK, WRAP2):
        wrapper = paths.bin / wrapper_name
        wrapper.write_text(
            "#!/bin/sh\n"
            # A numeric first argument is the wrapper's own (like timeout's
            # duration); anything else is already the payload.
            'case "$1" in [0-9]*) shift ;; esac\n'
            'exec "$@"\n'
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    state = ToolResponder(
        "Shell",
        {"command": DENIED, "description": "Run permission probe"},
        MODEL,
        "call_permission_probe",
        "done",
    )
    stub = recorded_server(request, state.respond)
    env = environment(
        paths,
        cursor,
        PATH=f"{paths.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
    )
    value = Runtime(executable, seatbelt, paths, stub, state, env, markers)
    require_containment(
        value.seatbelt,
        stub,
        paths,
        env,
    )
    return value


def invoke(runtime: Runtime, command: str, *, force: bool) -> PermissionResult:
    runtime.state.arguments["command"] = command
    for marker in runtime.markers.values():
        marker.unlink(missing_ok=True)
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            *cursor_command(
                runtime.executable,
                runtime.stub.base_url,
                MODEL,
                f"Run {command}.",
                force=force,
            )
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=20,
    )
    events = CursorEvent.decode_lines(process.stdout)
    outcome = next(
        (
            observation
            for event in events
            if (observation := event.shell_outcome()) is not None
        ),
        None,
    )
    return PermissionResult(
        process.returncode,
        frozenset(name for name, marker in runtime.markers.items() if marker.exists()),
        outcome,
    )


def observe(runtime: Runtime, name: str) -> PermissionResult:
    command, _, _, force = SCENARIOS[name]
    return invoke(runtime, command, force=force)


observation = cached_scenario_fixture(observe)


@pytest.mark.capability_case("cursor-agent.permissions")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "expected_outcome", "expected_execution"),
    tuple(
        pytest.param(name, outcome, executed, id=name)
        for name, (_, outcome, executed, _force) in SCENARIOS.items()
    ),
    indirect=("observation",),
    scope="module",
)
def test_cursor_permissions(
    observation: PermissionResult,
    expected_outcome: str,
    expected_execution: tuple[str, ...],
) -> None:
    assert observation.returncode == 0
    assert observation.outcome is not None
    assert observation.outcome.text == expected_outcome
    assert observation.executed == frozenset(expected_execution)
