from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

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
from capabilities.protocols.anthropic import AnthropicRequest, AnthropicResponder
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.claude import environment

COMMAND = "permission-blackbox-probe"
UNLISTED_MARKER = "unlisted"
UNLISTED = f"{COMMAND}x {UNLISTED_MARKER}"
TOOL_ID = "toolu_permission_blackbox"
OPT = "blackbox-opt"
OPT_PERMS = {
    "allow": (f"Bash({OPT} -C * status *)", f"Bash({OPT} -C * status)"),
    "ask": (
        f"Bash({OPT} * -c *)",
        f"Bash({OPT} * -c)",
        f"Bash({OPT} * push *)",
        f"Bash({OPT} * push)",
    ),
}


@dataclass(frozen=True, slots=True)
class Scenario:
    tool: str
    arguments: dict[str, str]
    permissions: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class Runtime:
    claude: str
    seatbelt: Seatbelt
    stub: RecordedServer
    model: AnthropicResponder
    paths: Paths
    settings: Path
    recorder_dir: Path
    env: dict[str, str]


SCENARIOS = {
    "allow-exact": Scenario(
        "Bash", {"command": COMMAND}, {"allow": (f"Bash({COMMAND})",)}
    ),
    "deny-exact": Scenario(
        "Bash",
        {"command": COMMAND},
        {"allow": (f"Bash({COMMAND})",), "deny": (f"Bash({COMMAND})",)},
    ),
    "deny-trailing": Scenario(
        "Bash",
        {"command": f"{COMMAND} trailing"},
        {
            "allow": (f"Bash({COMMAND} *)", f"Bash({COMMAND}x *)"),
            "deny": (f"Bash({COMMAND} *)",),
        },
    ),
    "allow-near-prefix": Scenario(
        "Bash",
        {"command": f"{COMMAND}x near-prefix"},
        {
            "allow": (f"Bash({COMMAND} *)", f"Bash({COMMAND}x *)"),
            "deny": (f"Bash({COMMAND} *)",),
        },
    ),
    "ask-over-allow": Scenario(
        "Bash",
        {"command": COMMAND},
        {"allow": (f"Bash({COMMAND})",), "ask": (f"Bash({COMMAND})",)},
    ),
    "wrapper-head-allowed": Scenario(
        "Bash",
        {"command": f"timeout 30 {COMMAND}"},
        {"allow": ("Bash(timeout *)",)},
    ),
    **{
        f"wrapper-{name}": Scenario(
            "Bash",
            {"command": f"{prefix} {COMMAND}"},
            {"allow": (f"Bash({COMMAND})", f"Bash({COMMAND} *)")},
        )
        for name, prefix in (
            ("timeout", "timeout 30"),
            ("timeout-k", "timeout -k 5 30"),
            ("time", "time"),
            ("nice", "nice -n 5"),
            ("nohup", "nohup"),
            ("stdbuf", "stdbuf -oL"),
            ("command", "command"),
            ("builtin", "builtin"),
            ("noglob", "noglob"),
            ("xargs", "xargs"),
            ("xargs-args", "xargs -n 1"),
            ("env", "env"),
            ("env-assign", "env FOO=bar"),
            ("sudo", "sudo"),
            ("sh-c", "sh -c"),
            ("unrelated", f"{COMMAND}x"),
        )
    },
    # Isolates whether the optional-trailing rewrite survives a second `*`.
    # The guard sequence is trailing in one case and followed by a token in
    # the other; only the pattern's star count differs between them.
    # PROBE P3: wrapper-head allow versus an ask/deny on the resolved inner.
    "probe-wrapper-ask-inner": Scenario(
        "Bash",
        {"command": f"timeout 30 {COMMAND}"},
        {
            "allow": ("Bash(timeout *)",),
            "ask": (f"Bash({COMMAND})", f"Bash({COMMAND} *)"),
        },
    ),
    "probe-wrapper-deny-inner": Scenario(
        "Bash",
        {"command": f"timeout 30 {COMMAND}"},
        {
            "allow": ("Bash(timeout *)",),
            "deny": (f"Bash({COMMAND})", f"Bash({COMMAND} *)"),
        },
    ),
    # PROBE A1: option-tolerant allow with a body guard over its greedy hole.
    "probe-opt-plain": Scenario(
        "Bash", {"command": f"{OPT} -C /tmp status --short"}, OPT_PERMS
    ),
    "probe-opt-smuggle-flag": Scenario(
        "Bash", {"command": f"{OPT} -C /tmp -c core.pager=evil status"}, OPT_PERMS
    ),
    "probe-opt-smuggle-sub": Scenario(
        "Bash", {"command": f"{OPT} -C /tmp push origin status"}, OPT_PERMS
    ),
    # PROBE C: is a leading VAR= assignment peeled, alone and before a wrapper?
    "probe-var-plain": Scenario(
        "Bash",
        {"command": f"FOO=bar {COMMAND}"},
        {"allow": (f"Bash({COMMAND})", f"Bash({COMMAND} *)")},
    ),
    "probe-var-wrapper": Scenario(
        "Bash",
        {"command": f"FOO=bar timeout 30 {COMMAND}"},
        {"allow": (f"Bash({COMMAND})", f"Bash({COMMAND} *)")},
    ),
    "two-star-trailing": Scenario(
        "Bash",
        {"command": f"{COMMAND} two-star-trailing -X DELETE"},
        {
            "allow": (f"Bash({COMMAND} *)",),
            "ask": (
                f"Bash({COMMAND} * -X DELETE *)",
                f"Bash({COMMAND} * -X DELETE)",
            ),
        },
    ),
    "two-star-followed": Scenario(
        "Bash",
        {"command": f"{COMMAND} two-star-followed -X DELETE more"},
        {
            "allow": (f"Bash({COMMAND} *)",),
            "ask": (f"Bash({COMMAND} * -X DELETE *)",),
        },
    ),
    "one-star-trailing": Scenario(
        "Bash",
        {"command": f"{COMMAND} one-star-trailing"},
        {
            "allow": (f"Bash({COMMAND} *)",),
            "ask": (f"Bash({COMMAND} one-star-trailing *)",),
        },
    ),
    # Chained commands: agents emit these constantly, so pin whether an allow
    # on one segment covers an unlisted segment.
    "chain-and": Scenario(
        "Bash",
        {"command": f"{COMMAND} chain-and && {COMMAND}x chain-and-second"},
        {"allow": (f"Bash({COMMAND} *)",)},
    ),
    "chain-pipe": Scenario(
        "Bash",
        {"command": f"{COMMAND} chain-pipe | {COMMAND}x chain-pipe-second"},
        {"allow": (f"Bash({COMMAND} *)",)},
    ),
    "chain-semicolon": Scenario(
        "Bash",
        {"command": f"{COMMAND} chain-semi; {COMMAND}x chain-semi-second"},
        {"allow": (f"Bash({COMMAND} *)",)},
    ),
    "chain-both-allowed": Scenario(
        "Bash",
        {"command": f"{COMMAND} chain-ok && {COMMAND} chain-ok-second"},
        {"allow": (f"Bash({COMMAND} *)",)},
    ),
    # Codex's word-only parser bails on `$VAR` and stops applying its rules;
    # check whether a string matcher keeps enforcing through an expansion.
    "variable-denied": Scenario(
        "Bash",
        {"command": f"{COMMAND} variable-denied $HOME"},
        {"allow": ("Bash(*)",), "deny": (f"Bash({COMMAND} *)",)},
    ),
    # `fail` makes the recorder exit nonzero so the right operand is reached.
    "chain-or": Scenario(
        "Bash",
        {"command": f"{COMMAND} chain-or fail || {UNLISTED}"},
        {"allow": (f"Bash({COMMAND} *)",)},
    ),
    "chain-backtick": Scenario(
        "Bash",
        {"command": f"{COMMAND} chain-backtick `{UNLISTED}`"},
        {"allow": (f"Bash({COMMAND} *)",)},
    ),
    "chain-substitution": Scenario(
        "Bash",
        {"command": f"{COMMAND} chain-substitution $({UNLISTED})"},
        {"allow": (f"Bash({COMMAND} *)",)},
    ),
    "chain-newline": Scenario(
        "Bash",
        {"command": f"{COMMAND} chain-newline\n{UNLISTED}"},
        {"allow": (f"Bash({COMMAND} *)",)},
    ),
    "chain-pipe-both-allowed": Scenario(
        "Bash",
        {"command": f"{COMMAND} chain-pipe-ok | {COMMAND} chain-pipe-ok-second"},
        {"allow": (f"Bash({COMMAND} *)",)},
    ),
    "chain-semicolon-both-allowed": Scenario(
        "Bash",
        {"command": f"{COMMAND} chain-semi-ok; {COMMAND} chain-semi-ok-second"},
        {"allow": (f"Bash({COMMAND} *)",)},
    ),
    # One command whose argument only looks like a chain.
    "quoted-operator": Scenario(
        "Bash",
        {"command": f'{COMMAND} "quoted-operator && {UNLISTED}"'},
        {"allow": (f"Bash({COMMAND} *)",)},
    ),
    # The interpreter is allowlisted, the payload it carries is not.
    **{
        f"interpreter-{name}": Scenario(
            "Bash", {"command": command}, {"allow": (f"Bash({head}:*)",)}
        )
        for name, head, command in (
            ("sh-c", "sh", f"sh -c '{UNLISTED}'"),
            ("bash-c", "bash", f"bash -c '{UNLISTED}'"),
            ("eval", "eval", f"eval '{UNLISTED}'"),
        )
    },
    # Controls: whether the payload matters at all to either decision.
    "interpreter-sh-c-denied-payload": Scenario(
        "Bash",
        {"command": f"sh -c '{UNLISTED}'"},
        {"allow": ("Bash(sh:*)",), "deny": (f"Bash({COMMAND}x *)",)},
    ),
    "interpreter-eval-allowed-payload": Scenario(
        "Bash",
        {"command": f"eval '{COMMAND} eval-allowed'"},
        {"allow": ("Bash(eval:*)", f"Bash({COMMAND} *)")},
    ),
    "deny-env": Scenario(
        "Read",
        {"file_path": "{work}/.env"},
        {"allow": ("Read(**/.env.example)",), "deny": ("Read(**/.env*)",)},
    ),
    "deny-env-example": Scenario(
        "Read",
        {"file_path": "{work}/.env.example"},
        {"allow": ("Read(**/.env.example)",), "deny": ("Read(**/.env*)",)},
    ),
}
EXPECTED = {
    "allow-exact": {"exit-zero": True, "executed": True, "result-error": False},
    "deny-exact": {
        "exit-zero": True,
        "executed": False,
        "result-error": True,
        "explicit-denial": True,
    },
    "deny-trailing": {"exit-zero": True, "executed": False, "result-error": True},
    "allow-near-prefix": {
        "exit-zero": True,
        "executed": True,
        "result-error": False,
    },
    "ask-over-allow": {
        "exit-zero": True,
        "executed": False,
        "result-error": True,
    },
    # Control for execution failure versus permission mismatch.
    "wrapper-head-allowed": {"exit-zero": True, "executed": True},
    **{
        f"wrapper-{name}": {"exit-zero": True, "executed": executed, "denied": False}
        for name, executed in (
            ("timeout", True),
            ("timeout-k", True),
            ("time", True),
            ("nice", True),
            ("nohup", True),
            ("stdbuf", True),
            ("command", True),
            ("noglob", True),
            # These are permitted but do not reach the recorder.
            ("builtin", False),
            ("xargs", False),
        )
    },
    **{
        f"wrapper-{name}": {"exit-zero": True, "executed": False, "denied": True}
        for name in ("xargs-args", "env", "env-assign", "sudo", "sh-c", "unrelated")
    },
    # A second `*` suppresses the optional-trailing rewrite, so the bare form
    # is generated alongside it; `two-star-trailing` only asks because of it.
    "two-star-trailing": {"exit-zero": True, "executed": False},
    "two-star-followed": {"exit-zero": True, "executed": False},
    "one-star-trailing": {"exit-zero": True, "executed": False},
    # Expectations encode "an allow on one segment does not cover the other".
    "chain-and": {"exit-zero": True, "executed": False, "denied": True},
    "chain-pipe": {"exit-zero": True, "executed": False, "denied": True},
    "chain-semicolon": {"exit-zero": True, "executed": False, "denied": True},
    "chain-both-allowed": {"exit-zero": True, "executed": True, "denied": False},
    "variable-denied": {"exit-zero": True, "executed": False, "denied": True},
    # Substitutions and a bare newline segment exactly like the operators: the
    # whole call is refused before any segment runs.
    **{
        name: {
            "exit-zero": True,
            "executed": False,
            "unlisted-executed": False,
            "denied": True,
        }
        for name in (
            "chain-or",
            "chain-backtick",
            "chain-substitution",
            "chain-newline",
        )
    },
    # Controls: neither operator is refused on sight.
    **{
        name: {"exit-zero": True, "executed": True, "denied": False}
        for name in ("chain-pipe-both-allowed", "chain-semicolon-both-allowed")
    },
    # A quoted operator stays one argument, so no second command is parsed.
    "quoted-operator": {
        "exit-zero": True,
        "executed": True,
        "unlisted-executed": False,
        "denied": False,
    },
    # An allowlisted interpreter head hides its payload: matching stops at the
    # head, so the unlisted command inside runs, and a deny on it never applies.
    **{
        name: {"exit-zero": True, "unlisted-executed": True, "denied": False}
        for name in (
            "interpreter-sh-c",
            "interpreter-bash-c",
            "interpreter-sh-c-denied-payload",
        )
    },
    # `eval` is refused whatever it carries, so no payload question arises.
    **{
        name: {"exit-zero": True, "executed": False, "denied": True}
        for name in ("interpreter-eval", "interpreter-eval-allowed-payload")
    },
    # Hypothesis: strictest across peeled and unpeeled matches, so the inner
    # ask/deny beats the wrapper-head allow. A failure means a bypass.
    "probe-wrapper-ask-inner": {
        "exit-zero": True,
        "executed": False,
        "result-error": True,
    },
    "probe-wrapper-deny-inner": {
        "exit-zero": True,
        "executed": False,
        "denied": True,
    },
    "probe-opt-plain": {"exit-zero": True, "executed": True, "result-error": False},
    "probe-opt-smuggle-flag": {
        "exit-zero": True,
        "executed": False,
        "result-error": True,
    },
    "probe-opt-smuggle-sub": {
        "exit-zero": True,
        "executed": False,
        "result-error": True,
    },
    # Hypothesis: an assignment prefix is not peeled, so both are refused.
    "probe-var-plain": {"exit-zero": True, "executed": False},
    "probe-var-wrapper": {"exit-zero": True, "executed": False},
    "deny-env": {"exit-zero": True, "result-error": True},
    "deny-env-example": {"exit-zero": True, "result-error": True},
}


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    claude, sandbox = require_command("claude"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("claude-permissions").resolve())
    settings = paths.root / "settings.json"
    recorder_dir = paths.root / "recorder"
    recorder_dir.mkdir()
    recorder = paths.bin / COMMAND
    # One marker per invocation, named after the first argument, so a chain can
    # attribute execution to a segment. `fail` forces a nonzero exit.
    recorder.write_text(
        '#!/bin/sh\n/usr/bin/touch "$RECORDER_DIR/${1:-executed}"\n'
        '[ "$2" = fail ] && exit 1\nexit 0\n'
    )
    recorder.chmod(0o755)
    (paths.bin / f"{COMMAND}x").symlink_to(recorder)
    # The sanitized probe PATH needs a timeout shim.
    for name, script in (
        (
            "timeout",
            '#!/bin/sh\nwhile [ "${1#-}" != "$1" ]; do shift 2; done\n'
            'shift\nexec "$@"\n',
        ),
        ("stdbuf", '#!/bin/sh\nshift\nexec "$@"\n'),
        ("noglob", '#!/bin/sh\nexec "$@"\n'),
        ("sudo", '#!/bin/sh\nexec "$@"\n'),
        (OPT, '#!/bin/sh\n/usr/bin/touch "$RECORDER_DIR/optran"\nexit 0\n'),
    ):
        shim = paths.bin / name
        shim.write_text(script)
        shim.chmod(0o755)
    (paths.work / ".env").write_text("SECRET=blocked\n")
    (paths.work / ".env.example").write_text("EXAMPLE=also-blocked\n")
    model = AnthropicResponder()
    stub = recorded_server(request, model.respond)
    value = Runtime(
        claude,
        loopback_seatbelt(sandbox),
        stub,
        model,
        paths,
        settings,
        recorder_dir,
        environment(
            paths,
            stub.base_url,
            PATH=f"{paths.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            RECORDER_DIR=str(recorder_dir),
        ),
    )
    require_containment(
        value.seatbelt,
        stub,
        paths,
        value.env,
    )
    return value


def invoke(runtime: Runtime, scenario: Scenario) -> CheckResult:
    recorder_dir, settings = runtime.recorder_dir, runtime.settings
    for stale in recorder_dir.iterdir():
        stale.unlink()
    settings.write_text(
        json.dumps(
            {
                "permissions": {
                    key: list(patterns)
                    for key, patterns in scenario.permissions.items()
                }
            }
        )
    )
    runtime.model.tool_call = (
        scenario.tool,
        {
            key: value.format(work=runtime.paths.work)
            for key, value in scenario.arguments.items()
        },
        TOOL_ID,
    )
    start = len(runtime.stub.requests)
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.claude,
            f"Run the requested {scenario.tool} tool call exactly once.",
            "--bare",
            "--print",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--settings",
            str(settings),
            "--permission-mode",
            "dontAsk",
            "--tools",
            scenario.tool,
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
    )
    tool_result = next(
        (
            decoded
            for request in runtime.stub.requests[start:]
            if (decoded := AnthropicRequest.decode(request).tool_result(TOOL_ID))
        ),
        None,
    )
    result_text = json.dumps(tool_result, sort_keys=True).lower()
    compact = process.stdout.replace(" ", "")
    return CheckResult(
        {
            "exit-zero": process.returncode == 0,
            "executed": any(recorder_dir.iterdir()),
            "unlisted-executed": (recorder_dir / UNLISTED_MARKER).exists(),
            "result-error": bool(tool_result and tool_result.get("is_error")),
            "explicit-denial": any(
                word in result_text for word in ("denied", "permission", "not allowed")
            ),
            # Fails closed: absence of the field is not evidence of denial.
            "denied": '"permission_denials":[' in compact
            and '"permission_denials":[]' not in compact,
        },
        f"stdout={process.stdout}\nstderr={process.stderr}\nresult={tool_result}\n"
        f"markers={sorted(path.name for path in recorder_dir.iterdir())}",
    )


def observe(runtime: Runtime, name: str) -> CheckResult:
    return invoke(runtime, SCENARIOS[name])


observation = cached_scenario_fixture(observe)


EXPECTATIONS = [
    pytest.param(scenario, check, expected, id=f"{scenario}-{check}")
    for scenario, checks in EXPECTED.items()
    for check, expected in checks.items()
]


@pytest.mark.capability_case("claude.permissions")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check", "expected"),
    EXPECTATIONS,
    indirect=("observation",),
    scope="module",
)
def test_claude_permissions(
    observation: CheckResult, check: str, expected: bool
) -> None:
    assert observation.checks[check] is expected, observation.detail
