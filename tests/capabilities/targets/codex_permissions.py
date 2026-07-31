from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from capabilities.harness import (
    Paths,
    cached_scenario_fixture,
    require_command,
    require_containment,
    run_probe,
    sanitized_env,
)
from capabilities.model import CheckResult, NativeObservation
from capabilities.protocols.common import ProtocolShapeError, structural_summary
from capabilities.protocols.responses import (
    ResponsesRequest,
    responses_done,
    responses_tool,
)
from capabilities.runtime import (
    CommandResult,
    Seatbelt,
    loopback_seatbelt,
    run,
    run_pty,
)
from capabilities.server import RecordedServer
from capabilities.targets.codex import write_config

TOOL_ROUND_TRIPS = 2
type Decision = Literal["allow", "prompt", "forbidden"] | None


@dataclass(frozen=True, slots=True)
class Runtime:
    codex: str
    seatbelt: Seatbelt
    paths: Paths
    recorder: Path
    record: Path


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    escalated: bool
    rules: tuple[tuple[str, str], ...]
    command: str | None = None
    executed: tuple[str, ...] = ()
    rejected: bool = False


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    process: CommandResult
    requests: tuple[ResponsesRequest, ...]
    executed: tuple[str, ...]

    @property
    def feedback(self) -> str:
        return "\n".join(
            observation.text
            for request in self.requests[1:]
            for observation in request.function_outputs()
        )

    @property
    def rejected(self) -> bool:
        return "rejected: policy forbids" in self.feedback


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Runtime:
    codex, sandbox = require_command("codex"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("codex-permissions").resolve())
    with RecordedServer.start(
        0,
        lambda _request, _count: (responses_done("containment"), "text/event-stream"),
    ) as stub:
        require_containment(
            loopback_seatbelt(sandbox),
            stub,
            paths,
            sanitized_env({"HOME": str(paths.home), "PATH": "/usr/bin:/bin"}),
        )
    recorder, record = (
        paths.work / "permission-blackbox-recorder",
        paths.work / "executions.log",
    )
    recorder.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$1\" >> {shlex.quote(str(record))}\n"
        '[ "$2" = fail ] && exit 1\nexit 0\n'
    )
    recorder.chmod(recorder.stat().st_mode | stat.S_IXUSR)
    record.touch()
    return Runtime(codex, loopback_seatbelt(sandbox), paths, recorder, record)


def decode_execpolicy(output: str) -> NativeObservation[Decision]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise ProtocolShapeError("invalid Codex execpolicy inspector JSON") from error
    if not isinstance(value, dict):
        raise ProtocolShapeError(
            f"unknown Codex execpolicy envelope: {structural_summary(value)}"
        )
    match value.get("decision"):
        case None | "allow" | "prompt" | "forbidden" as decision:
            return NativeObservation("execpolicy", "$.decision", decision)
        case decision:
            raise ProtocolShapeError(
                f"unknown Codex execpolicy decision: {structural_summary(decision)}"
            )


CONTRACTS = {
    "exact": (("permission-contract",), "allow"),
    "trailing": (("permission-contract", "status"), "allow"),
    "strictest": (("permission-contract", "danger"), "forbidden"),
    "near-prefix": (("permission-contractx",), None),
    "star-is-literal": (("anything",), None),
    "literal-star": (("*",), "prompt"),
    # The compiler emits a narrower prompt beside a broader allow and relies
    # on strictest-wins to resolve it, so pin that directly.
    "prompt-beats-allow": (("permission-contract", "guarded"), "prompt"),
    "allow-survives-sibling-prompt": (("permission-contract", "safe"), "allow"),
    # The inspector matches one flat argv: a shell wrapper's payload is never
    # decomposed in either direction, and an operator is an ordinary token.
    # Segmentation lives above this surface and is only visible in a live run.
    "wrapper-hides-payload": (
        ("/bin/zsh", "-lc", "permission-contract danger"),
        "allow",
    ),
    "wrapper-payload-unmatched": (("/bin/bash", "-lc", "permission-contract"), None),
    "operator-is-argument": (
        ("permission-contract", "a", "&&", "permission-contract", "danger"),
        "allow",
    ),
}


def execpolicy_contract(runtime: Runtime, name: str) -> NativeObservation[Decision]:
    rules = runtime.paths.root / "contract.rules"
    rules.write_text(
        "\n".join(
            (
                'prefix_rule(pattern=["permission-contract"], decision="allow")',
                'prefix_rule(pattern=["permission-contract", "danger"], '
                'decision="forbidden")',
                'prefix_rule(pattern=["permission-contract", "guarded"], '
                'decision="prompt")',
                'prefix_rule(pattern=["*"], decision="prompt")',
                'prefix_rule(pattern=["/bin/zsh"], decision="allow")',
            )
        )
    )
    command, _ = CONTRACTS[name]
    result = run(
        *runtime.seatbelt.command(
            runtime.codex,
            "execpolicy",
            "check",
            "--rules",
            str(rules),
            *command,
        ),
        cwd=runtime.paths.work,
        env=sanitized_env({"HOME": str(runtime.paths.home), "PATH": "/usr/bin:/bin"}),
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    return decode_execpolicy(result.stdout)


def run_scenario(
    runtime: Runtime,
    recorder: Path,
    record: Path,
    scenario: Scenario,
) -> ScenarioResult:
    name = scenario.name
    scenario_root = runtime.paths.root / name
    scenario_root.mkdir()
    codex_home, home = scenario_root / ".codex", scenario_root / "home"
    rules = codex_home / "rules"
    rules.mkdir(parents=True)
    home.mkdir()
    quoted = shlex.quote(str(recorder))
    command = (scenario.command or f"{{rec}} {name}").format(rec=quoted)
    patterns = [
        shlex.split(pattern.format(rec=quoted)) for pattern, _ in scenario.rules
    ]
    (rules / "probe.rules").write_text(
        "".join(
            f"prefix_rule(pattern={json.dumps(pattern)}, "
            f"decision={json.dumps(decision)})\n"
            for pattern, (_, decision) in zip(patterns, scenario.rules, strict=True)
        )
    )

    def responder(_request: dict, count: int) -> tuple[bytes, str]:
        body = (
            responses_tool(f"call-{name}", command, escalated=scenario.escalated)
            if count == 1
            else responses_done(name)
        )
        return body, "text/event-stream"

    stub = RecordedServer.start(0, responder)
    write_config(codex_home, runtime.paths.work, stub.base_url)
    env = sanitized_env(
        {
            "CODEX_HOME": str(codex_home),
            "CODEX_CAPABILITY_KEY": "local-placeholder",
            "HOME": str(home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": str(runtime.paths.tmp),
            "TERM": "xterm-256color",
        }
    )
    inspector = run(
        *runtime.seatbelt.command(
            runtime.codex,
            "execpolicy",
            "check",
            "--rules",
            str(rules / "probe.rules"),
            *patterns[0],
        ),
        cwd=runtime.paths.work,
        env=env,
        timeout=5,
    )
    assert inspector.returncode == 0
    assert decode_execpolicy(inspector.stdout).value == scenario.rules[0][1]
    logged = len(record.read_text().splitlines())
    try:
        if scenario.escalated:
            process = run_probe(
                run_pty,
                runtime.seatbelt.command(
                    runtime.codex,
                    "--no-alt-screen",
                    "--strict-config",
                    "-a",
                    "on-request",
                    "-C",
                    str(runtime.paths.work),
                    f"Run the deterministic {name} probe tool call.",
                ),
                cwd=runtime.paths.work,
                env=env,
                complete=lambda: len(stub.requests) >= TOOL_ROUND_TRIPS,
                timeout=20,
            )
        else:
            process = run_probe(
                run,
                *runtime.seatbelt.command(
                    runtime.codex,
                    "exec",
                    "--skip-git-repo-check",
                    "--strict-config",
                    "--ephemeral",
                    "--json",
                    "-C",
                    str(runtime.paths.work),
                    f"Run the deterministic {name} probe tool call.",
                ),
                cwd=runtime.paths.work,
                env=env,
                timeout=20,
                stdin=subprocess.DEVNULL,
            )
    finally:
        stub.close()
    return ScenarioResult(
        process,
        tuple(ResponsesRequest.decode(request) for request in stub.requests),
        tuple(sorted(record.read_text().splitlines()[logged:])),
    )


# Each operator pairs an allowed segment with a forbidden one, plus an
# all-allowed control. `forbidden` is the only decision that blocks under
# `danger-full-access`, so it marks which segments the policy evaluated. The
# third column is which segments still ran in the forbidden variant; `fail`
# makes the recorder exit nonzero so `||` reaches its right operand.
CHAINS = (
    ("and", "{rec} {a} && {rec} {b}", ()),
    ("or", "{rec} {a} fail || {rec} {b}", ()),
    ("pipe", "{rec} {a} | {rec} {b}", ()),
    ("semi", "{rec} {a} ; {rec} {b}", ()),
    ("newline", "{rec} {a}\n{rec} {b}", ()),
    ("subst", "{rec} {a} $({rec} {b})", ("a", "b")),
    ("tick", "{rec} {a} `{rec} {b}`", ("a", "b")),
)


def chain(
    operator: str, template: str, decision: str, ran: tuple[str, ...]
) -> tuple[str, Scenario]:
    key = f"chain-{operator}" + ("" if decision == "forbidden" else "-ok")
    segments = {"a": f"{key}-a", "b": f"{key}-b"}
    return key, Scenario(
        key,
        True,
        (
            ("{rec} " + segments["a"], "allow"),
            ("{rec} " + segments["b"], decision),
        ),
        template.format(rec="{rec}", **segments),
        tuple(sorted(segments[slot] for slot in ran)),
        not ran,
    )


SHELL_SCENARIOS = {
    **dict(
        chain(operator, template, "forbidden", ran)
        for operator, template, ran in CHAINS
    ),
    **dict(
        chain(operator, template, "allow", ("a", "b"))
        for operator, template, _ in CHAINS
    ),
    # An unmatched segment is not blocked at all, so only `forbidden` can show
    # that the right-hand segment was evaluated.
    "chain-unlisted": Scenario(
        "chain-unlisted",
        True,
        (("{rec} chain-unlisted-a", "allow"),),
        "{rec} chain-unlisted-a && {rec} chain-unlisted-b",
        ("chain-unlisted-a", "chain-unlisted-b"),
    ),
    # Differential for the two scenarios below: same rule shape, same head, an
    # ordinary argument instead of a substitution.
    "subst-baseline": Scenario(
        "subst-baseline",
        True,
        (("{rec} subst-baseline-a", "forbidden"),),
        "{rec} subst-baseline-a extra",
        (),
        True,
    ),
    # The word-only parser also rejects plain variable expansion, which is far
    # more common in real commands than substitution.
    "var-unchecked": Scenario(
        "var-unchecked",
        True,
        (("{rec} var-unchecked-a", "forbidden"),),
        "{rec} var-unchecked-a $HOME",
        ("var-unchecked-a",),
    ),
    "quoted-var-unchecked": Scenario(
        "quoted-var-unchecked",
        True,
        (("{rec} quoted-var-unchecked-a", "forbidden"),),
        '{rec} quoted-var-unchecked-a "home $HOME"',
        ("quoted-var-unchecked-a",),
    ),
    # Forbidding the outer command separates "does not descend into a
    # substitution" from "skips the command that contains one".
    "subst-unchecked": Scenario(
        "subst-unchecked",
        True,
        (("{rec} subst-unchecked-a", "forbidden"),),
        "{rec} subst-unchecked-a $({rec} subst-unchecked-b)",
        ("subst-unchecked-a", "subst-unchecked-b"),
    ),
    "tick-unchecked": Scenario(
        "tick-unchecked",
        True,
        (("{rec} tick-unchecked-a", "forbidden"),),
        "{rec} tick-unchecked-a `{rec} tick-unchecked-b`",
        ("tick-unchecked-a", "tick-unchecked-b"),
    ),
    "quoted": Scenario(
        "quoted",
        True,
        (("{rec} quoted-a", "allow"), ("{rec} quoted-b", "forbidden")),
        '{rec} quoted-a "&& {rec} quoted-b"',
        ("quoted-a",),
    ),
    "quoted-head": Scenario(
        "quoted-head",
        True,
        (("{rec} quoted-head-a", "forbidden"),),
        '{rec} quoted-head-a "&& {rec} quoted-head-b"',
        (),
        True,
    ),
    # The interpreter head is allowed and the payload it carries is forbidden;
    # the `-head` controls prove the head itself is still evaluated.
    "interp-sh": Scenario(
        "interp-sh",
        True,
        (("/bin/sh", "allow"), ("{rec} interp-sh-a", "forbidden")),
        "/bin/sh -c '{rec} interp-sh-a'",
        ("interp-sh-a",),
    ),
    "interp-sh-head": Scenario(
        "interp-sh-head",
        True,
        (("/bin/sh", "forbidden"),),
        "/bin/sh -c '{rec} interp-sh-head-a'",
        (),
        True,
    ),
    "interp-bash": Scenario(
        "interp-bash",
        True,
        (("/bin/bash", "allow"), ("{rec} interp-bash-a", "forbidden")),
        "/bin/bash -c '{rec} interp-bash-a'",
        ("interp-bash-a",),
    ),
    "interp-eval": Scenario(
        "interp-eval",
        True,
        (("eval", "allow"), ("{rec} interp-eval-a", "forbidden")),
        "eval '{rec} interp-eval-a'",
        ("interp-eval-a",),
    ),
    "interp-eval-head": Scenario(
        "interp-eval-head",
        True,
        (("eval", "forbidden"),),
        "eval '{rec} interp-eval-head-a'",
        (),
        True,
    ),
}

AGENT_SCENARIOS = {
    "forbidden": Scenario("host", True, (("{rec} host", "forbidden"),)),
    "allowed": Scenario("allowed-host", True, (("{rec} allowed-host", "allow"),)),
    "sandbox": Scenario("sandbox", False, (("{rec} host", "forbidden"),)),
    **SHELL_SCENARIOS,
}


def observe(runtime: Runtime, name: str) -> CheckResult:
    if name.startswith("contract-"):
        contract = name.removeprefix("contract-")
        observed = execpolicy_contract(runtime, contract)
        return CheckResult({name: observed.value}, repr(observed))

    result = run_scenario(
        runtime,
        runtime.recorder,
        runtime.record,
        AGENT_SCENARIOS[name],
    )
    legacy = {
        "forbidden": {
            "forbidden-host-not-executed": not result.executed,
            "forbidden-feedback": "policy forbids commands starting with"
            in result.feedback,
            "forbidden-two-requests": len(result.requests) >= TOOL_ROUND_TRIPS,
        },
        "allowed": {
            "allowed-host-executed": bool(result.executed),
            "allowed-two-requests": len(result.requests) >= TOOL_ROUND_TRIPS,
        },
        "sandbox": {
            "sandbox-control-executed": bool(result.executed),
            "sandbox-exit-zero": result.process.returncode == 0,
        },
    }
    checks = legacy.get(name) or {
        f"{name}-executed": result.executed,
        f"{name}-rejected": result.rejected,
    }
    return CheckResult(checks, repr(result))


observation = cached_scenario_fixture(observe)


EXPECTED = {
    **{f"contract-{name}": decision for name, (_, decision) in CONTRACTS.items()},
    "forbidden-host-not-executed": True,
    "forbidden-feedback": True,
    "allowed-host-executed": True,
    "sandbox-control-executed": True,
    "forbidden-two-requests": True,
    "allowed-two-requests": True,
    "sandbox-exit-zero": True,
    **{
        f"{name}-{check}": getattr(scenario, check)
        for name, scenario in SHELL_SCENARIOS.items()
        for check in ("executed", "rejected")
    },
}

CHECK_SCENARIOS = {
    **{f"contract-{name}": f"contract-{name}" for name in CONTRACTS},
    "forbidden-host-not-executed": "forbidden",
    "forbidden-feedback": "forbidden",
    "forbidden-two-requests": "forbidden",
    "allowed-host-executed": "allowed",
    "allowed-two-requests": "allowed",
    "sandbox-control-executed": "sandbox",
    "sandbox-exit-zero": "sandbox",
    **{
        f"{name}-{check}": name
        for name in SHELL_SCENARIOS
        for check in ("executed", "rejected")
    },
}


@pytest.mark.capability_case("codex.permissions")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check", "expected"),
    tuple(
        pytest.param(CHECK_SCENARIOS[check], check, expected, id=check)
        for check, expected in EXPECTED.items()
    ),
    indirect=("observation",),
    scope="module",
)
def test_codex_permissions(
    observation: CheckResult, check: str, expected: object
) -> None:
    assert observation.checks[check] == expected, observation.detail
