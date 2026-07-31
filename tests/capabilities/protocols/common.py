from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from capabilities.model import TextObservation


class ProtocolShapeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DecodedRequest:
    raw: dict[str, Any]
    observations: tuple[TextObservation, ...]

    def text(self, channel: str | None = None) -> str:
        return "\n".join(
            item.text
            for item in self.observations
            if channel is None or item.channel == channel
        )


@dataclass(frozen=True, slots=True)
class DecodedInspector[T]:
    raw: T
    observations: tuple[TextObservation, ...]

    def text(self, channel: str | None = None) -> str:
        return "\n".join(
            item.text
            for item in self.observations
            if channel is None or item.channel == channel
        )


def structural_summary(value: Any, depth: int = 0) -> str:
    if depth >= 3:
        return type(value).__name__
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                f"{key}: {structural_summary(nested, depth + 1)}"
                for key, nested in sorted(value.items())
            )
            + "}"
        )
    if isinstance(value, list):
        values = ", ".join(structural_summary(item, depth + 1) for item in value[:3])
        return f"[{values}{', ...' if len(value) > 3 else ''}]"
    return type(value).__name__


def strings(value: Any, *, channel: str, source_path: str) -> Iterator[TextObservation]:
    if isinstance(value, str):
        yield TextObservation(channel, source_path, value)
    elif isinstance(value, dict):
        for key, nested in value.items():
            yield from strings(
                nested,
                channel=channel,
                source_path=f"{source_path}.{key}",
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from strings(
                nested,
                channel=channel,
                source_path=f"{source_path}[{index}]",
            )
