from __future__ import annotations

from typing import Any

from capabilities.model import TextObservation

from .common import ProtocolShapeError
from .openai import ChatRequest, JsonEvent


class CursorRequest(ChatRequest):
    __slots__ = ()

    @classmethod
    def decode(cls, request: dict[str, Any]) -> CursorRequest:
        decoded = super().decode(request)
        if request.get("stream") is not True:
            raise ProtocolShapeError("Cursor chat request is not streaming")
        return decoded


class CursorEvent(JsonEvent):
    __slots__ = ()

    def shell_outcome(self) -> TextObservation | None:
        if self.raw["type"] != "tool_call" or self.raw.get("subtype") == "started":
            return None
        tool_call = self.raw.get("tool_call")
        shell = tool_call.get("shellToolCall") if isinstance(tool_call, dict) else None
        result = shell.get("result") if isinstance(shell, dict) else None
        outcomes = (
            {"success", "permissionDenied", "rejected"} & result.keys()
            if isinstance(result, dict)
            else set()
        )
        if len(outcomes) != 1:
            raise ProtocolShapeError(f"unknown Cursor shell result: {result!r}")
        return TextObservation(
            "tool_call",
            f"{self.observations[0].source_path}.tool_call.shellToolCall.result",
            outcomes.pop(),
        )

    @classmethod
    def decode_lines(cls, output: str) -> tuple[CursorEvent, ...]:
        events = super().decode_lines(output)
        for event in events:
            if event.raw["type"] == "tool_call":
                subtype = event.raw.get("subtype")
                if subtype not in {"started", "completed"}:
                    raise ProtocolShapeError(
                        f"unknown Cursor tool-call subtype: {subtype!r}"
                    )
        return events
