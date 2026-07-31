from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class Support(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    UNVERIFIED = "unverified"
    UNAVAILABLE = "unavailable"
    HARNESS_ERROR = "harness_error"


class Comparison(StrEnum):
    CONFIRMED = "confirmed"
    CAPABILITY_GAIN = "capability_gain"
    REGRESSION = "regression"


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    target: str
    evidence: str
    expected: Support = Support.SUPPORTED


@dataclass(frozen=True, slots=True)
class Observation:
    case: Case
    actual: Support
    detail: str
    version: str = ""

    @property
    def comparison(self) -> Comparison:
        return compare(self.case.expected, self.actual)


def compare(expected: Support, actual: Support) -> Comparison:
    if actual is Support.HARNESS_ERROR:
        return Comparison.REGRESSION
    if actual in {expected, Support.UNAVAILABLE}:
        return Comparison.CONFIRMED
    if actual is Support.SUPPORTED and expected in {
        Support.UNSUPPORTED,
        Support.UNVERIFIED,
    }:
        return Comparison.CAPABILITY_GAIN
    return Comparison.REGRESSION


@dataclass(frozen=True, slots=True)
class Target:
    name: str
    command: str
    updater: tuple[str, ...]
    version_args: tuple[str, ...] = ("--version",)


@dataclass(frozen=True, slots=True)
class TextObservation:
    """One native protocol string with its semantic channel and source path."""

    channel: str
    source_path: str
    text: str


@dataclass(frozen=True, slots=True)
class NativeObservation[T]:
    """One typed native value with its semantic channel and source path."""

    channel: str
    source_path: str
    value: T


@dataclass(frozen=True, slots=True)
class CheckResult:
    checks: Mapping[str, object]
    detail: str
