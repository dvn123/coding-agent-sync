from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from capabilities.harness import resolve_cursor_local, update, version
from capabilities.model import Case, Comparison, Observation, Support
from capabilities.registry import CASES, TARGETS, validate_registry


@dataclass(slots=True)
class State:
    node_cases: dict[str, str] = field(default_factory=dict)
    reports: dict[str, list[pytest.TestReport]] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)

    def observation(self, case: Case) -> Observation | None:
        reports = self.reports.get(case.id)
        if not reports:
            return None
        errors = [
            report
            for report in reports
            if report.failed and report.when in {"setup", "teardown"}
        ]
        failures = [
            report for report in reports if report.failed and report.when == "call"
        ]
        calls = [report for report in reports if report.when == "call"]
        skipped = [report for report in reports if report.skipped]
        if errors:
            actual, detail = (
                Support.HARNESS_ERROR,
                f"{len(errors)} pytest setup/teardown errors",
            )
        elif failures:
            actual, detail = (
                Support.UNSUPPORTED,
                f"{len(failures)} of {len(calls)} assertions failed",
            )
        elif skipped:
            actual = Support.UNAVAILABLE
            detail = "; ".join(
                sorted(
                    {
                        str(report.longrepr[2])
                        if isinstance(report.longrepr, tuple)
                        else str(report.longrepr)
                        for report in skipped
                    }
                )
            )
        else:
            actual, detail = (
                Support.SUPPORTED,
                f"{len(calls)} native assertions passed",
            )
        return Observation(case, actual, detail, self.versions.get(case.target, ""))


state: State | None = None


def requested_case_ids(arguments: Iterable[str], cwd: Path) -> set[str]:
    paths = []
    for argument in arguments:
        value = str(argument)
        if "::" in value:
            continue
        path = Path(value)
        paths.append((path if path.is_absolute() else cwd / path).resolve())
    return {
        case.id
        for case in CASES
        if any(
            (evidence := (Path(__file__).parent / case.evidence).resolve()) == path
            or path in evidence.parents
            for path in paths
        )
    }


def validate_collected_cases(expected: set[str], collected: set[str]) -> None:
    if missing := expected - collected:
        raise pytest.UsageError(
            f"capability cases collected no marked tests: {sorted(missing)}"
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("coding-agent capabilities")
    group.addoption(
        "--capabilities-live",
        action="store_true",
        help="run installed coding-agent capability probes",
    )
    group.addoption(
        "--target",
        action="append",
        choices=sorted(TARGETS),
        help="run one target; repeat to select multiple",
    )
    group.addoption(
        "--case",
        action="append",
        dest="capability_cases",
        help="run one capability case; repeat to select multiple",
    )
    group.addoption(
        "--skip-update",
        action="store_true",
        help="test installed versions without running target updaters",
    )


def pytest_configure(config: pytest.Config) -> None:
    global state
    if config.getoption("--capabilities-live"):
        validate_registry()
        state = State()


def line(config: pytest.Config, message: str) -> None:
    if reporter := config.pluginmanager.get_plugin("terminalreporter"):
        reporter.write_line(message)


def selected_case_ids(config: pytest.Config) -> set[str]:
    known = {case.id for case in CASES}
    requested = set(config.getoption("capability_cases") or ())
    if unknown := requested - known:
        raise pytest.UsageError(f"unknown capability cases: {sorted(unknown)}")
    targets = set(config.getoption("target") or ())
    return {
        case.id
        for case in CASES
        if (not requested or case.id in requested)
        and (not targets or case.target in targets)
    }


def prepare_targets(config: pytest.Config, case_ids: set[str]) -> None:
    assert state is not None
    selected_targets = {case.target for case in CASES if case.id in case_ids}
    for name, target in TARGETS.items():
        if name not in selected_targets:
            continue
        before = version(target)
        if not config.getoption("skip_update"):
            ok, detail = update(target)
            line(config, f"{'ok' if ok else 'warn'}  {name} update: {detail}")
        observed = version(target)
        state.versions[name] = observed
        line(
            config,
            f"info  {name} version: {before or 'missing'} -> {observed or 'missing'}",
        )
    if "cursor" in selected_targets and state.versions.get("cursor"):
        local, detail = resolve_cursor_local(
            allow_download=not config.getoption("skip_update")
        )
        line(config, f"{'ok' if local else 'warn'}  cursor local runtime: {detail}")
        if local:
            os.environ["CURSOR_AGENT_LOCAL"] = str(local)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if state is None:
        skip = pytest.mark.skip(reason="requires --capabilities-live")
        for item in items:
            if item.get_closest_marker("capability_live"):
                item.add_marker(skip)
        return
    known = {case.id for case in CASES}
    selected = selected_case_ids(config)
    collected = set()
    deselected = []
    retained = []
    for item in items:
        if not item.get_closest_marker("capability_live"):
            retained.append(item)
            continue
        marker = item.get_closest_marker("capability_case")
        case_id = str(marker.args[0]) if marker and marker.args else ""
        if case_id not in known:
            raise pytest.UsageError(
                f"unknown capability case: {case_id or item.nodeid}"
            )
        if case_id not in selected:
            deselected.append(item)
            continue
        state.node_cases[item.nodeid] = case_id
        collected.add(case_id)
        retained.append(item)
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = retained
    validate_collected_cases(
        (requested_case_ids(config.args, config.invocation_params.dir) & selected)
        | set(config.getoption("capability_cases") or ()),
        collected,
    )
    if not collected:
        raise pytest.UsageError("no live capability cases selected")
    prepare_targets(config, collected)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if state is not None and (case_id := state.node_cases.get(report.nodeid)):
        state.reports.setdefault(case_id, []).append(report)


def pytest_sessionfinish(session: pytest.Session) -> None:
    if state is None:
        return
    observations = [
        observation
        for case in CASES
        if (observation := state.observation(case)) is not None
    ]
    for observation in observations:
        line(
            session.config,
            f"{observation.comparison.value:10} {observation.case.id}: "
            f"{observation.actual.value} ({observation.detail})",
        )
    failures = [
        item for item in observations if item.comparison is Comparison.REGRESSION
    ]
    gains = [
        item for item in observations if item.comparison is Comparison.CAPABILITY_GAIN
    ]
    unavailable = [item for item in observations if item.actual is Support.UNAVAILABLE]
    line(
        session.config,
        f"summary  {len(observations)} cases, {len(failures)} failures, "
        f"{len(gains)} gains, {len(unavailable)} unavailable",
    )
    if failures:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
