from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Self

from capabilities.model import TextObservation

from .common import DecodedRequest, ProtocolShapeError, strings, structural_summary


def openai_sse(deltas: list[tuple[dict[str, Any], str | None]], model: str) -> bytes:
    chunks = (
        {
            "id": "chatcmpl-capability",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        for delta, finish_reason in deltas
    )
    return (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    ).encode()


def tool_call_sse(
    tool: str,
    arguments: dict[str, Any],
    *,
    called: bool,
    model: str,
    call_id: str,
    completion: str,
    split_role: bool = False,
) -> bytes:
    finish: str = "stop" if called else "tool_calls"
    if called:
        delta: dict[str, Any] = {"content": completion}
    else:
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(arguments)},
                }
            ]
        }
    deltas: list[tuple[dict[str, Any], str | None]] = (
        [({"role": "assistant"}, None)] if split_role else []
    )
    deltas += [(delta if split_role else {"role": "assistant", **delta}, None)]
    deltas.append(({}, finish))
    return openai_sse(deltas, model)


@dataclass(slots=True)
class ToolResponder:
    tool: str
    arguments: dict[str, Any]
    model: str
    call_id: str
    completion: str
    result_role: str = "tool"
    split_role: bool = False
    enabled: bool = True

    def respond(self, request: dict[str, Any], _count: int) -> tuple[bytes, str]:
        called = not self.enabled or any(
            isinstance(message, dict)
            and message.get("role") == self.result_role
            and (self.result_role != "assistant" or message.get("tool_calls"))
            for message in request.get("messages", [])
        )
        return (
            tool_call_sse(
                self.tool,
                self.arguments,
                called=called,
                model=self.model,
                call_id=self.call_id,
                completion=self.completion,
                split_role=self.split_role,
            ),
            "text/event-stream",
        )


class ChatRequest(DecodedRequest):
    __slots__ = ()

    @classmethod
    def decode(cls, request: dict[str, Any]) -> Self:
        if not isinstance(request.get("model"), str) or not isinstance(
            request.get("messages"), list
        ):
            raise ProtocolShapeError(
                f"invalid chat request: {structural_summary(request)}"
            )
        observations = list(strings(request, channel="request", source_path="$"))
        for index, message in enumerate(request["messages"]):
            path = f"$.messages[{index}]"
            if not isinstance(message, dict) or not isinstance(
                message.get("role"), str
            ):
                raise ProtocolShapeError(f"invalid chat message at {path}")
            observations.extend(
                strings(
                    message.get("content"),
                    channel=message["role"],
                    source_path=f"{path}.content",
                )
            )
        tools = request.get("tools", [])
        if not isinstance(tools, list):
            raise ProtocolShapeError("invalid chat tools field")
        for index, tool in enumerate(tools):
            if not isinstance(tool, dict):
                raise ProtocolShapeError(f"invalid chat tool at $.tools[{index}]")
            observations.extend(
                strings(
                    tool,
                    channel="tools",
                    source_path=f"$.tools[{index}]",
                )
            )
        return cls(request, tuple(observations))


@dataclass(frozen=True, slots=True)
class JsonEvent:
    raw: dict[str, Any]
    observations: tuple[TextObservation, ...]

    @classmethod
    def decode_lines(cls, output: str) -> tuple[Self, ...]:
        events = []
        for index, line in enumerate(filter(None, output.splitlines())):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ProtocolShapeError(
                    f"invalid JSON event at stdout[{index}]"
                ) from error
            if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                raise ProtocolShapeError(f"invalid event at stdout[{index}]")
            events.append(
                cls(
                    value,
                    (
                        TextObservation(
                            value["type"],
                            f"stdout[{index}]",
                            json.dumps(value, separators=(",", ":"), sort_keys=True),
                        ),
                        *strings(
                            value,
                            channel=value["type"],
                            source_path=f"stdout[{index}]",
                        ),
                    ),
                )
            )
        return tuple(events)
