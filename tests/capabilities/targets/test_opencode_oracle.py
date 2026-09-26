"""The audit's OpenCode model agrees with OpenCode on a real policy.

`coding-agents-audit decisions` predicts every probe command from a model of
OpenCode's matcher and the opencode-unwrap plugin. This compiles the policy
under `CODING_AGENTS_AUDIT_ROOT` with `unmatched: allow`, runs a seeded
sample of probes through OpenCode with that ruleset and the plugin, and
requires every observed decision to equal the prediction. The sample favours
probes whose prediction the plugin decides.
"""

from __future__ import annotations

import json
import os
import random
import shutil
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
from capabilities.protocols.opencode import OpenCodeEvent
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.opencode import config as opencode_config
from capabilities.targets.opencode import environment
from coding_agents_sync import audit
from coding_agents_sync.targets import opencode
from coding_agents_sync.targets.permissions import fold_edit_write

SAMPLE = 60
DENIALS = ("Permission denied", "is denied by the shell rules")


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    state: ToolResponder
    env: dict[str, str]
    predicted: dict[str, str]


def sample(rules: audit.OpenCodeRules, probes: list[str]) -> dict[str, str]:
    """Up to SAMPLE probes each where the plugin denies, where it peels but
    allows, and where there is nothing to peel."""
    strata: dict[str, list[str]] = {"plugin": [], "peeled": [], "plain": []}
    for probe in probes:
        spelled = audit.unwrap_spellings(probe)
        if not spelled:
            strata["plain"].append(probe)
        elif rules.native(probe) != "deny" and rules.decision(probe) == "deny":
            strata["plugin"].append(probe)
        else:
            strata["peeled"].append(probe)
    chosen = random.Random(0)
    return {
        probe: rules.decision(probe)
        for group in strata.values()
        for probe in chosen.sample(group, min(SAMPLE, len(group)))
    }


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    root = os.environ.get("CODING_AGENTS_AUDIT_ROOT")
    if not root:
        pytest.skip("set CODING_AGENTS_AUDIT_ROOT to a coding-agents config root")
    executable, sandbox = require_command("opencode"), require_command("sandbox-exec")
    sources = audit._with_unmatched(audit.load_sources(Path(root)), "allow")
    assert sources.permissions is not None
    tools = fold_edit_write(sources.permissions.tools, "OpenCode")[0]
    (value,) = opencode._permissions(sources, Path("opencode.json"), tools)
    shell = [
        (rule["resource"], rule["effect"])
        for rule in value.value
        if rule["action"] == "shell"
    ]
    predicted = sample(
        audit.OpenCodeRules(shell), audit.probe_commands(sources.permissions)
    )
    paths = Paths.create(tmp_path_factory.mktemp("opencode-oracle").resolve())
    state = ToolResponder(
        "shell",
        {},
        "oracle-probe",
        "call_oracle_probe",
        "oracle probe complete",
        result_role="assistant",
        split_role=True,
    )
    stub = recorded_server(request, state.respond)
    config = paths.root / "opencode.json"
    base = opencode_config(stub.base_url, "oracle-probe", "oracle", {})
    del base["permission"]
    config.write_text(json.dumps(base | {"permissions": value.value}))
    plugins = paths.config / "opencode" / "plugins"
    plugins.mkdir(parents=True)
    shutil.copy(opencode.UNWRAP_PLUGIN, plugins / opencode.UNWRAP_PLUGIN.name)
    # An empty PATH keeps every allowed probe from finding its program.
    env = environment(paths, config, str(paths.bin))
    value_runtime = Runtime(
        executable, loopback_seatbelt(sandbox), paths, stub, state, env, predicted
    )
    require_containment(value_runtime.seatbelt, stub, paths, env)
    return value_runtime


def observed(runtime: Runtime, command: str) -> str:
    runtime.state.tool = "shell"
    runtime.state.arguments = {"command": command}
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
            "test/oracle-probe",
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    observation = next(
        value
        for item in OpenCodeEvent.decode_lines(process.stdout)
        if (value := item.tool_use()) is not None and value.tool == "shell"
    )
    denied = observation.status == "error" and any(
        text in (observation.error or "") for text in DENIALS
    )
    return "deny" if denied else "allow"


@pytest.mark.capability_case("opencode.unwrap")
@pytest.mark.capability_live
def test_opencode_decides_every_sampled_probe_as_predicted(runtime: Runtime) -> None:
    mismatches = {
        probe: (predicted, actual)
        for probe, predicted in runtime.predicted.items()
        if (actual := observed(runtime, probe)) != predicted
    }
    assert runtime.predicted and not mismatches, mismatches
