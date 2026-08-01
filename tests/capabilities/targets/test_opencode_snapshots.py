from __future__ import annotations

import json
import re
from dataclasses import dataclass

import pytest

from capabilities.harness import (
    Paths,
    cached_scenario_fixture,
    require_command,
    run_probe,
)
from capabilities.model import CheckResult
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.targets.opencode import environment, supported

HASH = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    env: dict[str, str]


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Runtime:
    executable = require_command("opencode")
    git = require_command("git")
    sandbox = require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-snapshots").resolve())
    config_path = paths.root / "opencode.json"
    config_path.write_text(json.dumps({"snapshot": True, "autoupdate": False}))
    env = environment(
        paths,
        config_path,
        f"{git.rsplit('/', 1)[0]}:/usr/bin:/bin:/usr/sbin:/sbin",
    )
    initialized = run_probe(run, git, "init", "--quiet", cwd=paths.work, env=env)
    assert initialized.returncode == 0, initialized.stderr
    return Runtime(executable, loopback_seatbelt(sandbox), paths, env)


def debug(runtime: Runtime, *arguments: str):
    return supported(
        "the snapshot inspector",
        run_probe,
        run,
        *runtime.seatbelt.command(
            runtime.executable, "debug", "snapshot", *arguments, "--pure"
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
    )


def observe(runtime: Runtime, _name: str) -> CheckResult:
    fixture = runtime.paths.work / "snapshot.txt"
    fixture.write_text("before snapshot\n")
    tracked = debug(runtime, "track")
    snapshot = tracked.stdout.strip()
    fixture.write_text("after snapshot\n")
    diff = debug(runtime, "diff", snapshot)
    patch = debug(runtime, "patch", snapshot)
    detail = "\n".join((tracked.stdout, diff.stdout, patch.stdout))
    return CheckResult(
        {
            "snapshot-created": HASH.fullmatch(snapshot) is not None,
            "snapshot-diff": all(
                value in diff.stdout
                for value in (
                    "snapshot.txt",
                    "-before snapshot",
                    "+after snapshot",
                )
            ),
            "snapshot-patch": snapshot in patch.stdout and str(fixture) in patch.stdout,
        },
        detail,
    )


observation = cached_scenario_fixture(observe)


@pytest.mark.capability_case("opencode.snapshots-runtime")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check"),
    tuple(
        pytest.param("snapshots", check, id=check)
        for check in ("snapshot-created", "snapshot-diff", "snapshot-patch")
    ),
    indirect=("observation",),
    scope="module",
)
def test_opencode_snapshots(observation: CheckResult, check: str) -> None:
    assert observation.checks[check], observation.detail
