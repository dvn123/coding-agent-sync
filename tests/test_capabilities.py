from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import capabilities.conftest as capability_plugin
import capabilities.harness as capability_harness
import pytest
from capabilities.conftest import (
    State,
    prepare_targets,
    requested_case_ids,
    selected_case_ids,
    validate_collected_cases,
)
from capabilities.harness import Paths, sanitized_env
from capabilities.model import Case, Comparison, Observation, Support
from capabilities.protocols.common import ProtocolShapeError
from capabilities.registry import CASES, validate_registry
from capabilities.runtime import CommandResult, CommandTimeout, run_pty
from capabilities.server import RecordedServer
from capabilities.targets.opencode import supported


def sample_case() -> Case:
    return Case("sample.case", "claude", "targets/claude_loading.py")


def test_registry_is_valid() -> None:
    validate_registry()
    assert len({case.id for case in CASES}) == len(CASES)


def test_cursor_local_cache_miss_does_not_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(capability_harness.shutil, "which", lambda _: "/cursor-agent")
    monkeypatch.setattr(
        capability_harness,
        "_command_output",
        lambda *args, **kwargs: "cursor-agent 1.2.3",
    )

    def fail_download(*args: object, **kwargs: object) -> None:
        raise AssertionError("download attempted")

    monkeypatch.setattr(capability_harness.urllib.request, "urlretrieve", fail_download)

    local, detail = capability_harness.resolve_cursor_local(allow_download=False)

    assert local is None
    assert detail == "cached Cursor local runtime is unavailable; download disabled"


def test_requested_evidence_must_collect_its_case() -> None:
    root = Path(__file__).parents[1]
    expected = requested_case_ids(
        ["tests/capabilities/targets/opencode_loading.py"], root
    )
    assert expected == {"opencode.loading"}
    with pytest.raises(pytest.UsageError, match="opencode.loading"):
        validate_collected_cases(expected, set())
    validate_collected_cases(expected, expected)


def test_node_selection_does_not_require_the_whole_case() -> None:
    root = Path(__file__).parents[1]
    assert not requested_case_ids(
        [
            "tests/capabilities/targets/opencode_loading.py"
            "::test_opencode_loading[catalog-name]"
        ],
        root,
    )


def test_comparison_distinguishes_regression_and_unavailable() -> None:
    assert (
        Observation(sample_case(), Support.SUPPORTED, "").comparison
        is Comparison.CONFIRMED
    )
    assert (
        Observation(sample_case(), Support.UNSUPPORTED, "").comparison
        is Comparison.REGRESSION
    )
    assert (
        Observation(sample_case(), Support.UNAVAILABLE, "").comparison
        is Comparison.CONFIRMED
    )
    assert (
        Observation(sample_case(), Support.HARNESS_ERROR, "").comparison
        is Comparison.REGRESSION
    )
    expected_unsupported = Case(
        "sample.gain",
        "claude",
        "targets/claude_loading.py",
        Support.UNSUPPORTED,
    )
    assert (
        Observation(expected_unsupported, Support.SUPPORTED, "").comparison
        is Comparison.CAPABILITY_GAIN
    )


def test_target_and_case_options_filter_cases() -> None:
    class Config:
        @staticmethod
        def getoption(name: str):
            return {
                "capability_cases": ["codex.loading", "claude.loading"],
                "target": ["codex"],
            }[name]

    assert selected_case_ids(cast(pytest.Config, Config())) == {"codex.loading"}


def test_failed_updater_warns_and_tests_installed_version(monkeypatch) -> None:
    class Config:
        @staticmethod
        def getoption(name: str):
            assert name == "skip_update"
            return False

    messages: list[str] = []
    monkeypatch.setattr(capability_plugin, "state", State())
    monkeypatch.setattr(capability_plugin, "version", lambda _target: "installed")
    monkeypatch.setattr(
        capability_plugin, "update", lambda _target: (False, "network unavailable")
    )
    monkeypatch.setattr(
        capability_plugin, "line", lambda _config, message: messages.append(message)
    )
    prepare_targets(cast(pytest.Config, Config()), {"claude.loading"})
    assert capability_plugin.state
    assert capability_plugin.state.versions == {"claude": "installed"}
    assert any(message.startswith("warn  claude update:") for message in messages)
    assert any("installed -> installed" in message for message in messages)


@pytest.mark.parametrize(
    ("reports", "expected"),
    [
        ([("call", False, False)], Support.SUPPORTED),
        ([("call", True, False)], Support.UNSUPPORTED),
        ([("setup", False, True)], Support.UNAVAILABLE),
        ([("setup", True, False)], Support.HARNESS_ERROR),
    ],
)
def test_report_classification(
    reports: list[tuple[str, bool, bool]], expected: Support
) -> None:
    state = State(
        reports={
            "sample.case": [
                cast(
                    pytest.TestReport,
                    SimpleNamespace(
                        when=when,
                        failed=failed,
                        skipped=skipped,
                        longrepr=("", "", "unavailable"),
                    ),
                )
                for when, failed, skipped in reports
            ]
        }
    )
    observation = state.observation(sample_case())
    assert observation and observation.actual is expected


def test_mixed_pass_and_skip_is_not_reported_supported() -> None:
    reports = [
        SimpleNamespace(
            when="call",
            failed=False,
            skipped=False,
            longrepr=None,
        ),
        SimpleNamespace(
            when="call",
            failed=False,
            skipped=True,
            longrepr=("", "", "unavailable"),
        ),
    ]
    state = State(
        reports={"sample.case": [cast(pytest.TestReport, item) for item in reports]}
    )
    observation = state.observation(sample_case())
    assert observation and observation.actual is Support.UNAVAILABLE


def test_opencode_interface_failure_is_unavailable() -> None:
    with pytest.raises(pytest.skip.Exception, match="unavailable"):
        supported("the test interface", lambda: CommandResult(1, "", "rejected"))


@pytest.mark.parametrize(
    "error",
    [CommandTimeout("timed out"), ProtocolShapeError("changed shape")],
)
def test_opencode_harness_failures_are_not_reported_unavailable(
    error: Exception,
) -> None:
    def fail() -> None:
        raise error

    with pytest.raises(type(error)):
        supported("the test interface", fail)


def test_environment_does_not_forward_credentials(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/bin")
    env = sanitized_env()
    assert env["PATH"] == "/bin"
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_pty_timeout_raises_and_terminates(tmp_path: Path) -> None:
    with pytest.raises(CommandTimeout, match="did not finish"):
        run_pty(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            cwd=tmp_path,
            env=sanitized_env(),
            complete=lambda: False,
            timeout=0.05,
        )


def test_paths_create_every_slotted_directory(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path / "probe")
    assert all(
        getattr(paths, field).is_dir()
        for field in Paths.__annotations__
        if field != "root"
    )


def test_recorded_server_validates_and_records_native_json() -> None:
    with RecordedServer.start(
        0,
        lambda payload, count: (
            json.dumps({"count": count, "payload": payload}).encode(),
            "application/json",
        ),
    ) as server:
        request = urllib.request.Request(
            f"{server.base_url}/v1/messages",
            b'{"native":"request"}',
            {"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            assert json.load(response) == {
                "count": 1,
                "payload": {"native": "request"},
            }
        assert server.requests == [{"native": "request"}]

        invalid = urllib.request.Request(
            f"{server.base_url}/v1/messages",
            b"text",
            {"Content-Type": "text/plain"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError, match="415"):
            urllib.request.urlopen(invalid)
