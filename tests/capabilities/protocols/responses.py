from __future__ import annotations

import json
from typing import Any, Self

from capabilities.model import TextObservation

from .common import DecodedRequest, ProtocolShapeError, strings, structural_summary


def responses_sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
        for event in events
    ).encode()


def responses_done(scenario: str) -> bytes:
    return responses_sse(
        [
            {
                "type": "response.created",
                "response": {"id": f"resp-{scenario}"},
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": f"msg-{scenario}",
                    "content": [{"type": "output_text", "text": "probe complete"}],
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": f"resp-{scenario}",
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    },
                },
            },
        ]
    )


def responses_tool(call_id: str, command: str, *, escalated: bool = False) -> bytes:
    arguments: dict[str, Any] = {"cmd": command, "yield_time_ms": 1000}
    if escalated:
        arguments |= {
            "sandbox_permissions": "require_escalated",
            "justification": "Exercise the isolated capability probe.",
        }
    return responses_sse(
        [
            {"type": "response.created", "response": {"id": f"resp-{call_id}"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": "exec_command",
                    "arguments": json.dumps(arguments, separators=(",", ":")),
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": f"resp-{call_id}",
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    },
                },
            },
        ]
    )


class ResponsesRequest(DecodedRequest):
    __slots__ = ()

    @classmethod
    def decode(cls, request: dict[str, Any]) -> Self:
        if not isinstance(request.get("model"), str) or not isinstance(
            request.get("input"), list
        ):
            raise ProtocolShapeError(
                f"invalid Responses request: {structural_summary(request)}"
            )
        observations = list(strings(request, channel="request", source_path="$"))
        instructions = request.get("instructions")
        if instructions is not None:
            if not isinstance(instructions, str):
                raise ProtocolShapeError("invalid Responses instructions field")
            observations.append(
                TextObservation("instructions", "$.instructions", instructions)
            )
        for index, item in enumerate(request["input"]):
            path = f"$.input[{index}]"
            match item:
                case {"type": "message", "role": str(role), "content": content}:
                    observations.extend(
                        strings(
                            content,
                            channel=role,
                            source_path=f"{path}.content",
                        )
                    )
                case {"type": "function_call_output", "output": str(output)}:
                    observations.append(
                        TextObservation(
                            "function_call_output",
                            f"{path}.output",
                            output,
                        )
                    )
                case {
                    "type": "function_call",
                    "name": str(),
                    "arguments": str(),
                }:
                    pass
                case {"type": str(kind)}:
                    raise ProtocolShapeError(
                        f"unknown Responses item {kind!r} at {path}"
                    )
                case _:
                    raise ProtocolShapeError(f"invalid Responses item at {path}")
        tools = request.get("tools", [])
        if not isinstance(tools, list) or not all(
            isinstance(tool, dict) and isinstance(tool.get("type"), str)
            for tool in tools
        ):
            raise ProtocolShapeError("invalid Responses tools field")
        observations.extend(
            strings(
                tools,
                channel="tools",
                source_path="$.tools",
            )
        )
        return cls(request, tuple(observations))

    def function_outputs(self) -> tuple[TextObservation, ...]:
        return tuple(
            observation
            for observation in self.observations
            if observation.channel == "function_call_output"
        )
