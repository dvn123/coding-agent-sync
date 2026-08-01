from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import Case, Comparison, Observation, Support
from .registry import CASES, CASES_BY_SURFACE, SURFACES

SCHEMA = "coding-agents/capability-report/v2"


def unverified(case: Case, version: str = "") -> Observation:
    return Observation(
        case,
        Support.UNVERIFIED,
        "not selected or not collected",
        version,
    )


def observation_data(observation: Observation) -> dict[str, Any]:
    return {
        "id": observation.case.id,
        "target": observation.case.target,
        "evidence": observation.case.evidence,
        "evidence_kind": observation.case.evidence_kind,
        "surfaces": list(observation.case.surfaces),
        "expected": observation.case.expected,
        "support_when_checks_pass": observation.case.expected,
        "actual": observation.actual,
        "comparison": observation.comparison,
        "version": observation.version,
        "check_count": observation.check_count,
        "detail": observation.detail,
    }


def aggregate(observations: tuple[Observation, ...]) -> tuple[Support, Comparison, str]:
    selected = tuple(
        observation
        for observation in observations
        if observation.actual is not Support.UNVERIFIED
    )
    if not selected:
        return (
            Support.UNVERIFIED,
            Comparison.NOT_PROBED,
            "not selected or not collected",
        )
    actuals = {observation.actual for observation in selected}
    comparisons = {observation.comparison for observation in selected}
    binary = actuals & {Support.SUPPORTED, Support.UNSUPPORTED}
    if Comparison.REGRESSION in comparisons or len(binary) > 1:
        return (
            Support.CONTRADICTED,
            Comparison.REGRESSION,
            "evidence is contradicted: "
            + "; ".join(f"{item.case.id}: {item.detail}" for item in selected),
        )
    if Support.UNAVAILABLE in actuals and not binary:
        return (
            Support.UNAVAILABLE,
            Comparison.NOT_OBSERVED,
            "evidence is unavailable: "
            + "; ".join(f"{item.case.id}: {item.detail}" for item in selected),
        )
    actual = next(iter(binary or actuals))
    comparison = (
        Comparison.CAPABILITY_GAIN
        if Comparison.CAPABILITY_GAIN in comparisons
        else Comparison.CONFIRMED
    )
    return (
        actual,
        comparison,
        "; ".join(f"{item.case.id}: {item.detail}" for item in selected),
    )


def capability_report(
    observations: dict[str, Observation], versions: dict[str, str]
) -> dict[str, Any]:
    resolved = {
        case.id: observations.get(
            case.id, unverified(case, versions.get(case.target, ""))
        )
        for case in CASES
    }
    surfaces = []
    for surface in SURFACES:
        cases = CASES_BY_SURFACE.get(surface.id, ())
        surface_observations = tuple(resolved[case.id] for case in cases)
        actual, comparison, detail = aggregate(surface_observations)
        primary_case = cases[0] if cases else None
        expected = {case.expected for case in cases}
        expected_value = next(iter(expected), Support.UNVERIFIED)
        surfaces.append(
            {
                "id": surface.id,
                "target": surface.id.partition(".")[0],
                "management": surface.management,
                "case": primary_case.id if primary_case else None,
                "primary_case": primary_case.id if primary_case else None,
                "evidence": primary_case.evidence if primary_case else None,
                "evidence_kind": primary_case.evidence_kind if primary_case else None,
                "expected": expected_value
                if len(expected) == 1
                else Support.UNVERIFIED,
                "actual": actual,
                "comparison": comparison,
                "version": surface_observations[0].version
                if surface_observations
                else versions.get(surface.id.partition(".")[0], ""),
                "check_count": sum(
                    observation.check_count for observation in surface_observations
                ),
                "detail": detail,
                "cases": [case.id for case in cases],
                "observations": [
                    observation_data(observation)
                    for observation in surface_observations
                ],
            }
        )
    return {
        "schema": SCHEMA,
        "cases": [observation_data(resolved[case.id]) for case in CASES],
        "surfaces": surfaces,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
