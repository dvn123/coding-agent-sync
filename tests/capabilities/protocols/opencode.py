from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .common import (
    DecodedInspector,
    ProtocolShapeError,
    strings,
    structural_summary,
)
from .openai import ChatRequest, JsonEvent


class OpenCodeRequest(ChatRequest):
    __slots__ = ()


@dataclass(frozen=True, slots=True)
class ToolUseObservation:
    channel: str
    source_path: str
    tool: str
    status: str
    input: dict[str, Any]
    output: Any
    error: str


class OpenCodeEvent(JsonEvent):
    __slots__ = ()

    def tool_use(self) -> ToolUseObservation | None:
        if self.raw["type"] != "tool_use":
            return None
        match self.raw:
            case {
                "part": {
                    "tool": str(tool),
                    "state": {"status": str(status), **state},
                }
            }:
                pass
            case _:
                raise ProtocolShapeError(
                    f"invalid OpenCode tool-use event: {structural_summary(self.raw)}"
                )
        input_ = state.get("input", {})
        error = state.get("error", "")
        if not isinstance(input_, dict) or not isinstance(error, str):
            raise ProtocolShapeError(
                f"invalid OpenCode tool-use state: {structural_summary(state)}"
            )
        return ToolUseObservation(
            "tool_use",
            f"{self.observations[0].source_path}.part.state",
            tool,
            status,
            input_,
            state.get("output"),
            error,
        )

    @classmethod
    def decode_lines(cls, output: str) -> tuple[OpenCodeEvent, ...]:
        events = super().decode_lines(output)
        for event in events:
            event.tool_use()
        return events


def decode_skill_catalog(output: str) -> DecodedInspector[list[dict[str, Any]]]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise ProtocolShapeError("invalid OpenCode skill inspector JSON") from error
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ProtocolShapeError(
            f"unknown OpenCode skill inspector envelope: {structural_summary(value)}"
        )
    return DecodedInspector(
        value,
        tuple(strings(value, channel="skill_catalog", source_path="$")),
    )


def decode_config(output: str) -> DecodedInspector[dict[str, Any]]:
    try:
        value = json.loads(output)
    except json.JSONDecodeError as error:
        raise ProtocolShapeError("invalid OpenCode config inspector JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("permission"), dict):
        raise ProtocolShapeError(
            f"unknown OpenCode config inspector envelope: {structural_summary(value)}"
        )
    return DecodedInspector(
        value,
        tuple(
            strings(
                value["permission"],
                channel="permission",
                source_path="$.permission",
            )
        ),
    )
