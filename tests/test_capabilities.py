from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import capabilities.conftest as capability_plugin
import capabilities.harness as capability_harness
import capabilities.registry as capability_registry
import capabilities.report as capability_report_module
import pytest
from capabilities.conftest import (
    State,
    prepare_targets,
    pytest_configure,
    requested_case_ids,
    selected_case_ids,
    validate_collected_cases,
)
from capabilities.harness import Paths, sanitized_env
from capabilities.model import (
    Case,
    Comparison,
    EvidenceKind,
    Observation,
    Support,
    Surface,
    SurfaceManagement,
)
from capabilities.protocols.common import ProtocolShapeError
from capabilities.registry import (
    CASES,
    CASES_BY_ID,
    CASES_BY_SURFACE,
    TARGETS,
    validate_registry,
)
from capabilities.report import SCHEMA, aggregate, capability_report, write_report
from capabilities.runtime import CommandResult, CommandTimeout, run_pty
from capabilities.server import RecordedServer
from capabilities.targets.opencode import environment as opencode_environment
from capabilities.targets.opencode import supported


def sample_case() -> Case:
    return Case("sample.case", "claude", "targets/test_claude_loading.py")


def test_registry_is_valid() -> None:
    validate_registry()
    assert len({case.id for case in CASES}) == len(CASES)
    assert tuple(CASES_BY_ID.values()) == CASES
    assert all(case.target in TARGETS for case in CASES)
    assert all(
        (Path(__file__).parent / "capabilities" / case.evidence).is_file()
        for case in CASES
    )


def test_surface_evidence_index_is_complete_and_immutable() -> None:
    expected = {
        surface: tuple(case for case in CASES if surface in case.surfaces)
        for surface in {surface for case in CASES for surface in case.surfaces}
    }
    assert dict(CASES_BY_SURFACE) == expected
    with pytest.raises(TypeError):
        cast(dict[str, tuple[Case, ...]], CASES_BY_SURFACE)["sample.surface"] = ()
    assert {case.id for case in CASES_BY_SURFACE["opencode.model-selection"]} == {
        "opencode.model-selection-config",
        "opencode.model-selection-runtime",
    }


def test_registry_rejects_unknown_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        capability_registry,
        "CASES",
        (*CASES, Case("sample.unknown", "opencode", "registry.py", surfaces=("x",))),
    )
    with pytest.raises(ValueError, match=r"unknown declared target surfaces: \['x'\]"):
        capability_registry.validate_registry()


def test_opencode_compiler_evidence_uses_generated_artifact_surfaces() -> None:
    case = CASES_BY_ID["opencode.compiler"]
    assert case.evidence_kind is EvidenceKind.COMPILER_E2E
    assert case.surfaces == (
        "opencode.global-instructions",
        "opencode.instructions-config",
        "opencode.agents",
    )


def test_opencode_environment_disables_default_plugins(tmp_path: Path) -> None:
    paths = Paths.create(tmp_path)
    config = tmp_path / "opencode.json"

    assert (
        opencode_environment(paths, config, "/usr/bin")[
            "OPENCODE_DISABLE_DEFAULT_PLUGINS"
        ]
        == "true"
    )
    assert (
        opencode_environment(
            paths,
            config,
            "/usr/bin",
            OPENCODE_DISABLE_DEFAULT_PLUGINS="false",
        )["OPENCODE_DISABLE_DEFAULT_PLUGINS"]
        == "false"
    )


def test_claude_agent_teams_evidence_is_explicitly_experimental() -> None:
    case = CASES_BY_ID["claude.agent-teams-experimental"]
    assert case.surfaces == ("claude.agent-teams-experimental",)
    assert case.evidence_kind is EvidenceKind.INSTALLED_STATIC
    assert "claude.agent-teams" not in CASES_BY_ID


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
        ["tests/capabilities/targets/test_opencode_loading.py"], root
    )
    assert expected == {
        "opencode.loading",
        "opencode.command-config",
        "opencode.skill-paths",
        "opencode.skill-urls",
        "opencode.skills-catalog",
    }
    with pytest.raises(pytest.UsageError, match="opencode.command-config"):
        validate_collected_cases(expected, set())
    validate_collected_cases(expected, expected)


def test_node_selection_does_not_require_the_whole_case() -> None:
    root = Path(__file__).parents[1]
    assert not requested_case_ids(
        [
            "tests/capabilities/targets/test_opencode_loading.py"
            "::test_opencode_loading[catalog-name]"
        ],
        root,
    )


def test_comparison_distinguishes_regression_and_unavailable() -> None:
    assert (
        Observation(sample_case(), Support.UNVERIFIED, "").comparison
        is Comparison.NOT_PROBED
    )
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
        is Comparison.NOT_OBSERVED
    )
    assert (
        Observation(sample_case(), Support.HARNESS_ERROR, "").comparison
        is Comparison.REGRESSION
    )
    assert (
        Observation(sample_case(), Support.CONTRADICTED, "").comparison
        is Comparison.REGRESSION
    )
    expected_unsupported = Case(
        "sample.gain",
        "claude",
        "targets/test_claude_loading.py",
        Support.UNSUPPORTED,
    )
    assert (
        Observation(expected_unsupported, Support.SUPPORTED, "").comparison
        is Comparison.CAPABILITY_GAIN
    )


def test_report_keeps_unobserved_declared_surfaces_explicit() -> None:
    case = CASES_BY_ID["claude.loading"]
    observed = Observation(case, Support.SUPPORTED, "five checks", "1.2.3", 5)
    report = capability_report({"claude.loading": observed}, {"claude": "1.2.3"})

    assert SCHEMA == "coding-agents/capability-report/v2"
    assert report["schema"] == SCHEMA
    reported_case = next(item for item in report["cases"] if item["id"] == case.id)
    assert reported_case["actual"] is Support.SUPPORTED
    assert reported_case["comparison"] is Comparison.CONFIRMED
    assert reported_case["check_count"] == 5
    surface = next(item for item in report["surfaces"] if item["id"] == "claude.memory")
    assert surface["actual"] is Support.UNVERIFIED
    assert surface["comparison"] is Comparison.NOT_PROBED
    assert surface["case"] == surface["primary_case"] == "claude.memory"
    assert surface["cases"] == ["claude.memory"]
    assert surface["observations"][0]["id"] == "claude.memory"


def test_surface_report_aggregates_all_evidence_without_hiding_primary_case() -> None:
    config_case = CASES_BY_ID["opencode.model-selection-config"]
    runtime_case = CASES_BY_ID["opencode.model-selection-runtime"]
    report = capability_report(
        {
            config_case.id: Observation(
                config_case, Support.SUPPORTED, "config passed", "1.2.3", 2
            ),
            runtime_case.id: Observation(
                runtime_case, Support.CONTRADICTED, "runtime failed", "1.2.3", 3
            ),
        },
        {"opencode": "1.2.3"},
    )

    surface = next(
        item for item in report["surfaces"] if item["id"] == "opencode.model-selection"
    )
    assert surface["case"] == surface["primary_case"] == config_case.id
    assert surface["evidence"] == config_case.evidence
    assert surface["expected"] is Support.SUPPORTED
    assert surface["actual"] is Support.CONTRADICTED
    assert surface["comparison"] is Comparison.REGRESSION
    assert surface["check_count"] == 5
    assert surface["cases"] == [config_case.id, runtime_case.id]
    assert [item["id"] for item in surface["observations"]] == surface["cases"]


def test_surface_report_marks_conflicting_passing_evidence_contradicted() -> None:
    supported_case = CASES_BY_ID["opencode.models-config"]
    unsupported_case = Case(
        "sample.models-negative",
        "opencode",
        "targets/test_opencode_config_resolution.py",
        expected=Support.UNSUPPORTED,
        surfaces=("opencode.models",),
    )
    observations = (
        Observation(supported_case, Support.SUPPORTED, "config passed", check_count=1),
        Observation(
            unsupported_case, Support.UNSUPPORTED, "negative passed", check_count=1
        ),
    )

    actual, comparison, _ = aggregate(observations)

    assert actual is Support.CONTRADICTED
    assert comparison is Comparison.REGRESSION


@pytest.mark.parametrize(
    ("observations", "expected", "comparison"),
    [
        (
            (
                Observation(
                    CASES_BY_ID["opencode.models-config"],
                    Support.SUPPORTED,
                    "config passed",
                ),
                Observation(
                    Case("sample.unavailable", "opencode", "registry.py"),
                    Support.UNAVAILABLE,
                    "runtime unavailable",
                ),
            ),
            Support.SUPPORTED,
            Comparison.CONFIRMED,
        ),
        (
            (
                Observation(
                    Case("sample.unavailable", "opencode", "registry.py"),
                    Support.UNAVAILABLE,
                    "runtime unavailable",
                ),
            ),
            Support.UNAVAILABLE,
            Comparison.NOT_OBSERVED,
        ),
    ],
)
def test_surface_aggregate_keeps_binary_conclusions_when_evidence_is_partial(
    observations: tuple[Observation, ...], expected: Support, comparison: Comparison
) -> None:
    actual, observed_comparison, detail = aggregate(observations)

    assert actual is expected
    assert observed_comparison is comparison
    assert "runtime unavailable" in detail


def test_surface_report_unverifies_mixed_expectations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supported_case = Case(
        "sample.supported",
        "opencode",
        "targets/test_opencode_config_resolution.py",
        surfaces=("opencode.sample",),
    )
    unsupported_case = Case(
        "sample.unsupported",
        "opencode",
        "targets/test_opencode_config_resolution.py",
        expected=Support.UNSUPPORTED,
        surfaces=("opencode.sample",),
    )
    monkeypatch.setattr(
        capability_report_module, "CASES", (supported_case, unsupported_case)
    )
    monkeypatch.setattr(
        capability_report_module,
        "CASES_BY_SURFACE",
        {"opencode.sample": (supported_case, unsupported_case)},
    )
    monkeypatch.setattr(
        capability_report_module,
        "SURFACES",
        (Surface("opencode.sample", SurfaceManagement.UNMANAGED),),
    )

    surface = capability_report(
        {
            supported_case.id: Observation(supported_case, Support.SUPPORTED, "passed"),
            unsupported_case.id: Observation(
                unsupported_case, Support.UNSUPPORTED, "passed"
            ),
        },
        {},
    )["surfaces"][0]

    assert surface["case"] == surface["primary_case"] == supported_case.id
    assert surface["expected"] is Support.UNVERIFIED
    assert surface["actual"] is Support.CONTRADICTED


def test_desktop_version_comes_from_the_desktop_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import coding_agents_sync.probes.cursor_desktop as desktop_probe

    monkeypatch.setattr(desktop_probe, "_cursor_version", lambda _app: "3.14.15")
    assert capability_harness.version(TARGETS["cursor-desktop"]) == "3.14.15"


def test_report_json_is_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    report = capability_report({}, {})

    write_report(path, report)
    first = path.read_text()
    write_report(path, report)

    assert path.read_text() == first
    assert json.loads(first)["schema"] == SCHEMA


def test_target_and_case_options_filter_cases() -> None:
    class Config:
        @staticmethod
        def getoption(name: str):
            return {
                "capability_cases": ["codex.loading", "claude.loading"],
                "target": ["codex"],
            }[name]

    assert selected_case_ids(cast(pytest.Config, Config())) == {"codex.loading"}


def test_targets_are_not_updated_without_explicit_opt_in(monkeypatch) -> None:
    class Config:
        @staticmethod
        def getoption(name: str):
            return {
                "update_targets": False,
                "bootstrap_cursor_runtime": False,
            }[name]

    messages: list[str] = []
    monkeypatch.setattr(capability_plugin, "state", State())
    monkeypatch.setattr(capability_plugin, "version", lambda _target: "installed")
    monkeypatch.setattr(capability_plugin, "update", lambda _target: pytest.fail())
    monkeypatch.setattr(
        capability_plugin, "line", lambda _config, message: messages.append(message)
    )

    prepare_targets(cast(pytest.Config, Config()), {"claude.loading"})

    assert capability_plugin.state
    assert capability_plugin.state.versions == {"claude": "installed"}
    assert any("installed -> installed" in message for message in messages)


@pytest.mark.parametrize(
    ("update_result", "versions", "message"),
    [
        ((False, "network unavailable"), ("installed",), "update failed"),
        (
            (True, "updated"),
            ("installed", ""),
            "no installed version was observed",
        ),
    ],
)
def test_requested_target_updates_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    update_result: tuple[bool, str],
    versions: tuple[str, ...],
    message: str,
) -> None:
    class Config:
        @staticmethod
        def getoption(name: str):
            return {
                "update_targets": True,
                "bootstrap_cursor_runtime": False,
            }[name]

    observed = iter(versions)
    monkeypatch.setattr(capability_plugin, "state", State())
    monkeypatch.setattr(capability_plugin, "version", lambda _target: next(observed))
    monkeypatch.setattr(capability_plugin, "update", lambda _target: update_result)
    monkeypatch.setattr(capability_plugin, "line", lambda *_: None)

    with pytest.raises(pytest.UsageError, match=message):
        prepare_targets(cast(pytest.Config, Config()), {"claude.loading"})


def test_update_and_skip_options_are_contradictory() -> None:
    class Config:
        @staticmethod
        def getoption(name: str):
            return {"update_targets": True, "skip_update": True}[name]

    with pytest.raises(pytest.UsageError, match="cannot be combined"):
        pytest_configure(cast(pytest.Config, Config()))


def test_cursor_cache_lookup_does_not_mutate_parent_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Config:
        @staticmethod
        def getoption(name: str):
            return {
                "update_targets": False,
                "bootstrap_cursor_runtime": False,
            }[name]

    monkeypatch.delenv("CURSOR_AGENT_LOCAL", raising=False)
    monkeypatch.setattr(capability_plugin, "state", State())
    monkeypatch.setattr(capability_plugin, "version", lambda _target: "installed")
    monkeypatch.setattr(
        capability_plugin,
        "resolve_cursor_local",
        lambda *, allow_download: (
            tmp_path / "cursor-agent-local",
            "cached" if not allow_download else pytest.fail(),
        ),
    )
    monkeypatch.setattr(capability_plugin, "line", lambda *_: None)

    prepare_targets(cast(pytest.Config, Config()), {"cursor-agent.loading"})

    assert "CURSOR_AGENT_LOCAL" not in os.environ


@pytest.mark.parametrize(
    ("reports", "expected"),
    [
        ([("call", False, False)], Support.SUPPORTED),
        ([("call", True, False)], Support.CONTRADICTED),
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


def test_passing_negative_evidence_reports_unsupported() -> None:
    case = Case(
        "sample.negative",
        "claude",
        "targets/test_claude_loading.py",
        expected=Support.UNSUPPORTED,
    )
    passed = State(
        reports={
            case.id: [
                cast(
                    pytest.TestReport,
                    SimpleNamespace(
                        when="call", failed=False, skipped=False, longrepr=None
                    ),
                )
            ]
        }
    )
    failed = State(
        reports={
            case.id: [
                cast(
                    pytest.TestReport,
                    SimpleNamespace(
                        when="call", failed=True, skipped=False, longrepr=None
                    ),
                )
            ]
        }
    )

    passed_observation = passed.observation(case)
    failed_observation = failed.observation(case)
    assert passed_observation and passed_observation.actual is Support.UNSUPPORTED
    assert failed_observation and failed_observation.actual is Support.CONTRADICTED


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


def test_opencode_interface_failure_is_a_call_failure() -> None:
    with pytest.raises(
        pytest.fail.Exception, match="failed the test interface"
    ) as error:
        supported("the test interface", lambda: CommandResult(1, "stdout", "stderr"))
    assert "stdout=stdout" in str(error.value)
    assert "stderr=stderr" in str(error.value)


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


def test_pty_completion_tolerates_a_closed_master(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import capabilities.runtime as capability_runtime

    def closed_master(*_: object) -> None:
        raise OSError

    monkeypatch.setattr(capability_runtime.os, "write", closed_master)
    result = run_pty(
        (sys.executable, "-c", "print('ready')"),
        cwd=tmp_path,
        env=sanitized_env(),
        complete=lambda: True,
    )
    assert isinstance(result, CommandResult)


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
