from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class Support(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONTRADICTED = "contradicted"
    UNVERIFIED = "unverified"
    UNAVAILABLE = "unavailable"
    HARNESS_ERROR = "harness_error"


class Comparison(StrEnum):
    CONFIRMED = "confirmed"
    CAPABILITY_GAIN = "capability_gain"
    REGRESSION = "regression"
    NOT_PROBED = "not_probed"
    NOT_OBSERVED = "not_observed"


class EvidenceKind(StrEnum):
    TARGET_NATIVE = "target_native"
    COMPILER_E2E = "compiler_e2e"
    INSTALLED_STATIC = "installed_static"
    CONFIG_RESOLUTION = "config_resolution"


class SurfaceManagement(StrEnum):
    GENERATED = "generated"
    PATCH_ONLY = "patch_only"
    UNMANAGED = "unmanaged"


@dataclass(frozen=True, slots=True)
class Surface:
    id: str
    management: SurfaceManagement


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    target: str
    evidence: str
    expected: Support = Support.SUPPORTED
    surfaces: tuple[str, ...] = ()
    evidence_kind: EvidenceKind = EvidenceKind.TARGET_NATIVE


@dataclass(frozen=True, slots=True)
class Observation:
    case: Case
    actual: Support
    detail: str
    version: str = ""
    check_count: int = 0

    @property
    def comparison(self) -> Comparison:
        return compare(self.case.expected, self.actual)


def compare(expected: Support, actual: Support) -> Comparison:
    if actual is Support.UNVERIFIED:
        return Comparison.NOT_PROBED
    if actual is Support.UNAVAILABLE:
        return Comparison.NOT_OBSERVED
    if actual is Support.HARNESS_ERROR:
        return Comparison.REGRESSION
    if actual is Support.CONTRADICTED:
        return Comparison.REGRESSION
    if actual is expected:
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
    updater: tuple[str, ...] | None
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
