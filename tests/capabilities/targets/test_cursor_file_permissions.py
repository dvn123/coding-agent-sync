import json
from pathlib import Path

import pytest

from capabilities.harness import Paths, recorded_server, run_probe
from capabilities.model import CheckResult
from capabilities.protocols.cursor import CursorRequest
from capabilities.protocols.openai import ToolResponder
from capabilities.runtime import run
from capabilities.targets.cursor import command as cursor_command
from capabilities.targets.cursor import environment
from capabilities.targets.cursor import runtime as cursor_runtime

MODEL = "file-permission-probe"
MARKER = "CURSOR_FILE_PERMISSION_MARKER_4e1d"
# Where each probed file sits: inside the workspace or outside it.
PLACES = {"inside": "work", "outside": "outside"}

# The CLI's own Read(...) and Write(...) rules in cli-config.json, on a read
# or a write inside or outside the workspace, without --force.
SCENARIOS: dict[str, tuple[str, str, dict[str, list[str]], bool]] = {
    "read-inside": ("Read", "inside", {}, False),
    "read-outside": ("Read", "outside", {}, False),
    "read-inside-deny": ("Read", "inside", {"deny": ["Read(**/probe.txt)"]}, False),
    "read-outside-allow": ("Read", "outside", {"allow": ["Read({outside}/**)"]}, False),
    "read-outside-deny-force": (
        "Read",
        "outside",
        {"deny": ["Read(**/probe.txt)"]},
        True,
    ),
    "write-inside": ("Write", "inside", {}, False),
    "write-outside": ("Write", "outside", {}, False),
    "write-inside-deny": ("Write", "inside", {"deny": ["Write(**/probe.txt)"]}, False),
    "write-outside-allow": (
        "Write",
        "outside",
        {"allow": ["Write({outside}/**)"]},
        False,
    ),
    "write-inside-deny-force": (
        "Write",
        "inside",
        {"deny": ["Write(**/probe.txt)"]},
        True,
    ),
}


@pytest.fixture(scope="module")
def file_permissions(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, CheckResult]:
    executable, seatbelt = cursor_runtime()
    observed = {}
    for name, (tool, place, rules, force) in SCENARIOS.items():
        paths = Paths.create(tmp_path_factory.mktemp(f"cursor-{name}").resolve())
        outside = paths.root / "outside"
        outside.mkdir()
        target = paths.root / PLACES[place] / "probe.txt"
        if tool == "Read":
            target.write_text(f"{MARKER}\n")
        cursor = paths.home / ".cursor"
        cursor.mkdir()
        permissions = {
            key: [rule.replace("{outside}", str(outside)) for rule in values]
            for key, values in rules.items()
        }
        (cursor / "cli-config.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "approvalMode": "allowlist",
                    "permissions": {"allow": [], "deny": []} | permissions,
                }
            )
        )
        arguments = (
            {"path": str(target)}
            if tool == "Read"
            else {"path": str(target), "contents": f"{MARKER}\n"}
        )
        state = ToolResponder(tool, arguments, MODEL, "call_file_probe", "done")
        stub = recorded_server(request, state.respond)
        process = run_probe(
            run,
            *seatbelt.command(
                *cursor_command(
                    executable,
                    stub.base_url,
                    MODEL,
                    f"Use {tool} on {target}.",
                    force=force,
                )
            ),
            cwd=paths.work,
            env=environment(paths, cursor),
            timeout=30,
        )
        later = "\n".join(
            CursorRequest.decode(item).text() for item in stub.requests[1:]
        )
        observed[name] = CheckResult(
            {
                "exit-zero": process.returncode == 0,
                "tool-called": len(stub.requests) >= 2,
                "done": (
                    MARKER in later
                    if tool == "Read"
                    else target.is_file() and MARKER in target.read_text()
                ),
            },
            f"stdout={process.stdout[-1500:]}\nstderr={process.stderr[-800:]}\n"
            f"result={later[-800:]}",
        )
    return observed


@pytest.mark.capability_case("cursor-agent.file-permissions")
@pytest.mark.capability_live
def test_cursor_file_permissions_characterization(
    file_permissions: dict[str, CheckResult],
) -> None:
    out = Path(
        "/private/tmp/claude-501/-Users-diogonunes--local-share-src-coding-agent-sync"
        "/dd296b1c-3fa4-4f8c-86db-a40ccaa854c3/scratchpad"
    )
    (out / "cursorfile.json").write_text(
        json.dumps({k: v.checks for k, v in file_permissions.items()})
    )
    (out / "cursorfile-detail.txt").write_text(
        "\n\n".join(f"== {k}\n{v.detail}" for k, v in file_permissions.items())
    )
    raise AssertionError
