from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from capabilities.harness import resolve_cursor_local, update, version
from capabilities.model import Case, Comparison, Observation, Support
from capabilities.registry import CASES, CASES_BY_ID, TARGETS, validate_registry
from capabilities.report import capability_report, write_report


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
                Support.CONTRADICTED,
                f"{len(failures)} of {len(calls)} checks failed",
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
                case.expected,
                f"{len(calls)} checks passed",
            )
        return Observation(
            case, actual, detail, self.versions.get(case.target, ""), len(calls)
        )


state: State | None = None


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool:
    targets = Path(__file__).parent / "targets"
    return not config.getoption("--capabilities-live") and (
        collection_path == targets or collection_path.parent == targets
    )


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
        "--capabilities-desktop",
        action="store_true",
        help="allow the Cursor Desktop behavioral capability probe to launch the GUI",
    )
    group.addoption(
        "--capability-report",
        metavar="PATH",
        type=Path,
        help="write deterministic JSON capability evidence to PATH",
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
        "--update-targets",
        action="store_true",
        help="update selected targets before probing them",
    )
    group.addoption(
        "--bootstrap-cursor-runtime",
        action="store_true",
        help="download a matching Cursor local runtime into the managed cache",
    )
    group.addoption(
        "--skip-update",
        action="store_true",
        help="deprecated no-op; target updates require --update-targets",
    )


def pytest_configure(config: pytest.Config) -> None:
    global state
    if config.getoption("update_targets") and config.getoption("skip_update"):
        raise pytest.UsageError("--update-targets and --skip-update cannot be combined")
    if config.getoption("--capabilities-live"):
        validate_registry()
        state = State()


def line(config: pytest.Config, message: str) -> None:
    if reporter := config.pluginmanager.get_plugin("terminalreporter"):
        reporter.write_line(message)


def selected_case_ids(config: pytest.Config) -> set[str]:
    requested = set(config.getoption("capability_cases") or ())
    if unknown := requested - CASES_BY_ID.keys():
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
        if config.getoption("update_targets"):
            ok, detail = update(target)
            if not ok:
                raise pytest.UsageError(f"{name} update failed: {detail}")
            line(config, f"ok  {name} update: {detail}")
        observed = version(target)
        if config.getoption("update_targets") and not observed:
            raise pytest.UsageError(
                f"{name} update completed but no installed version was observed"
            )
        state.versions[name] = observed
        line(
            config,
            f"info  {name} version: {before or 'missing'} -> {observed or 'missing'}",
        )
    if "cursor-agent" in selected_targets and state.versions.get("cursor-agent"):
        local, detail = resolve_cursor_local(
            allow_download=bool(config.getoption("bootstrap_cursor_runtime"))
        )
        line(config, f"{'ok' if local else 'warn'}  cursor local runtime: {detail}")


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if state is None:
        skip = pytest.mark.skip(reason="requires --capabilities-live")
        for item in items:
            if item.get_closest_marker("capability_live"):
                item.add_marker(skip)
        return
    selected = selected_case_ids(config)
    desktop_rules = "cursor-desktop.rules"
    if desktop_rules in selected and not config.getoption("--capabilities-desktop"):
        if desktop_rules in set(config.getoption("capability_cases") or ()):
            raise pytest.UsageError(
                "--case cursor-desktop.rules requires --capabilities-desktop"
            )
        selected.remove(desktop_rules)
    collected = set()
    deselected = []
    retained = []
    for item in items:
        if not item.get_closest_marker("capability_live"):
            retained.append(item)
            continue
        marker = item.get_closest_marker("capability_case")
        case_id = str(marker.args[0]) if marker and marker.args else ""
        if case_id not in CASES_BY_ID:
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
    if config.option.collectonly:
        return
    prepare_targets(config, collected)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if state is not None and (case_id := state.node_cases.get(report.nodeid)):
        state.reports.setdefault(case_id, []).append(report)


def pytest_sessionfinish(session: pytest.Session) -> None:
    if state is None:
        return
    observations = {
        case.id: observation
        for case in CASES
        if (observation := state.observation(case)) is not None
    }
    report = capability_report(observations, state.versions)
    line(
        session.config,
        f"{'target':15} {'surface':38} {'actual':13} {'comparison':15} "
        f"{'checks':6} {'management':11} {'evidence-kind':16} detail",
    )
    for surface in report["surfaces"]:
        line(
            session.config,
            f"{surface['target']:15} {surface['id']:38} "
            f"{surface['actual']:13} {surface['comparison']:15} "
            f"{surface['check_count']:6} "
            f"{surface['management']:11} "
            f"{surface['evidence_kind'] or '-':16} {surface['detail']}",
        )
    if path := session.config.getoption("capability_report"):
        write_report(path, report)
        line(session.config, f"report   {path}")
    observed = list(observations.values())
    failures = [item for item in observed if item.comparison is Comparison.REGRESSION]
    gains = [item for item in observed if item.comparison is Comparison.CAPABILITY_GAIN]
    unavailable = [item for item in observed if item.actual is Support.UNAVAILABLE]
    unprobed = sum(
        surface["comparison"] is Comparison.NOT_PROBED for surface in report["surfaces"]
    )
    line(
        session.config,
        f"summary  {len(observed)} observed of {len(CASES)} cases, "
        f"{len(failures)} failures, "
        f"{len(gains)} gains, {len(unavailable)} unavailable, "
        f"{unprobed} surfaces not probed",
    )
    if failures:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
