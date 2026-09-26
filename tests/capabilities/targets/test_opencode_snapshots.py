from __future__ import annotations

import json
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
from capabilities.protocols.opencode import OpenCodeEvent
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.opencode import api, environment, inspect, isolate_service
from capabilities.targets.opencode import config as opencode_config

BEFORE, AFTER = "before snapshot", "after snapshot"


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    env: dict[str, str]
    fixture: Path


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable = require_command("opencode")
    git = require_command("git")
    sandbox = require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-snapshots").resolve())
    fixture = paths.work / "snapshot.txt"
    # 2.x has no snapshot inspector; snapshots are captured around each model
    # step, so a tool edit is what produces one.
    state = ToolResponder(
        "shell",
        {"command": f"printf '{AFTER}\\n' > {fixture.name}"},
        "snapshot-probe",
        "call_snapshot_probe",
        "snapshot probe complete",
    )
    stub = recorded_server(request, state.respond)
    config = opencode_config(
        stub.base_url, "snapshot-probe", "snapshot", {"*": "allow"}
    )
    config["snapshots"] = True
    config_path = paths.root / "opencode.json"
    config_path.write_text(json.dumps(config))
    env = environment(
        paths,
        config_path,
        f"{git.rsplit('/', 1)[0]}:/usr/bin:/bin:/usr/sbin:/sbin",
    )
    initialized = run_probe(run, git, "init", "--quiet", cwd=paths.work, env=env)
    assert initialized.returncode == 0, initialized.stderr
    value = Runtime(executable, loopback_seatbelt(sandbox), paths, stub, env, fixture)
    require_containment(value.seatbelt, stub, paths, env)
    isolate_service(request, value)
    return value


def observe(runtime: Runtime, _name: str) -> CheckResult:
    runtime.fixture.write_text(f"{BEFORE}\n")
    # The session must outlive the run so its snapshots can be inspected, so
    # this runs on the managed service rather than a private one.
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.executable,
            "run",
            "Call the shell tool exactly as instructed by the model.",
            "--format",
            "json",
            "--model",
            "test/snapshot-probe",
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=60,
    )
    assert process.returncode == 0, process.stderr or process.stdout
    changed = runtime.fixture.read_text()
    session = OpenCodeEvent.decode_lines(process.stdout)[0].raw["sessionID"]
    diffs: list[dict[str, Any]] = inspect(
        runtime, *api("get", f"/api/session/{session}/diff")
    )["data"]
    messages = inspect(runtime, *api("get", f"/api/session/{session}/message"))
    user = next(item["id"] for item in messages["data"] if item["type"] == "user")
    inspect(
        runtime,
        *api(
            "post",
            f"/api/session/{session}/revert/stage",
            data={"messageID": user, "files": True},
        ),
    )
    inspect(runtime, *api("post", f"/api/session/{session}/revert/commit"))
    diff = next((item for item in diffs if item["file"] == runtime.fixture.name), None)
    return CheckResult(
        {
            "snapshot-created": changed == f"{AFTER}\n" and diff is not None,
            "snapshot-diff": diff is not None
            and diff["status"] == "modified"
            and all(value in diff["patch"] for value in (f"-{BEFORE}", f"+{AFTER}")),
            "snapshot-restore": runtime.fixture.read_text() == f"{BEFORE}\n",
        },
        json.dumps({"diffs": diffs, "messages": messages["data"]}),
    )


observation = cached_scenario_fixture(observe)


@pytest.mark.capability_case("opencode.snapshots-runtime")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check"),
    tuple(
        pytest.param("snapshots", check, id=check)
        for check in ("snapshot-created", "snapshot-diff", "snapshot-restore")
    ),
    indirect=("observation",),
    scope="module",
)
def test_opencode_snapshots(observation: CheckResult, check: str) -> None:
    assert observation.checks[check], observation.detail
