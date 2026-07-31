from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Self

from capabilities.model import TextObservation

from .common import DecodedRequest, ProtocolShapeError, strings, structural_summary


def _event(kind: str, **data: Any) -> tuple[str, dict[str, Any]]:
    return kind, {"type": kind} | data


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(
        f"event: {kind}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"
        for kind, data in events
    ).encode()


def _message_start(identifier: str) -> tuple[str, dict[str, Any]]:
    return _event(
        "message_start",
        message={
            "id": identifier,
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


FINAL_RESPONSE = _sse(
    [
        _message_start("msg_capability_final"),
        _event(
            "content_block_start",
            index=0,
            content_block={"type": "text", "text": ""},
        ),
        _event(
            "content_block_delta",
            index=0,
            delta={"type": "text_delta", "text": "done"},
        ),
        _event("content_block_stop", index=0),
        _event(
            "message_delta",
            delta={"stop_reason": "end_turn", "stop_sequence": None},
            usage={"output_tokens": 1},
        ),
        _event("message_stop"),
    ]
)


def tool_call_response(name: str, arguments: dict[str, Any], tool_id: str) -> bytes:
    return _sse(
        [
            _message_start("msg_capability_tool"),
            _event(
                "content_block_start",
                index=0,
                content_block={
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": {},
                },
            ),
            _event(
                "content_block_delta",
                index=0,
                delta={
                    "type": "input_json_delta",
                    "partial_json": json.dumps(arguments),
                },
            ),
            _event("content_block_stop", index=0),
            _event(
                "message_delta",
                delta={"stop_reason": "tool_use", "stop_sequence": None},
                usage={"output_tokens": 1},
            ),
            _event("message_stop"),
        ]
    )


class AnthropicRequest(DecodedRequest):
    __slots__ = ()

    @classmethod
    def decode(cls, request: dict[str, Any]) -> Self:
        required = {"model": str, "max_tokens": int, "stream": bool, "messages": list}
        for field, kind in required.items():
            if not isinstance(request.get(field), kind):
                raise ProtocolShapeError(
                    f"invalid Anthropic field {field}: {structural_summary(request)}"
                )
        if request["stream"] is not True:
            raise ProtocolShapeError("Anthropic request is not streaming")
        observations = list(strings(request, channel="request", source_path="$"))
        for index, message in enumerate(request["messages"]):
            path = f"$.messages[{index}]"
            if not isinstance(message, dict) or not isinstance(
                message.get("role"), str
            ):
                raise ProtocolShapeError(f"invalid Anthropic message at {path}")
            content = message.get("content")
            if isinstance(content, str):
                observations.append(
                    TextObservation(message["role"], f"{path}.content", content)
                )
                continue
            if not isinstance(content, list):
                raise ProtocolShapeError(f"invalid Anthropic content at {path}")
            for block_index, block in enumerate(content):
                block_path = f"{path}.content[{block_index}]"
                match block:
                    case {"type": "text", "text": str(text)}:
                        observations.append(
                            TextObservation(message["role"], f"{block_path}.text", text)
                        )
                    case {
                        "type": "tool_use",
                        "id": str(),
                        "name": str(),
                        "input": dict(),
                    } | {"type": "image", "source": dict()}:
                        pass
                    case {
                        "type": "tool_result",
                        "tool_use_id": str(),
                        "content": result,
                    }:
                        observations.extend(
                            strings(
                                result,
                                channel=message["role"],
                                source_path=f"{block_path}.content",
                            )
                        )
                    case {"type": str(kind)}:
                        raise ProtocolShapeError(
                            f"unknown Anthropic block {kind!r} at {block_path}"
                        )
                    case _:
                        raise ProtocolShapeError(
                            f"invalid Anthropic block at {block_path}"
                        )
        return cls(request, tuple(observations))

    def tool_result(self, tool_id: str) -> dict[str, Any] | None:
        for message in self.raw["messages"]:
            content = message.get("content", [])
            if not isinstance(content, list):
                continue
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("tool_use_id") == tool_id
                ):
                    return block
        return None


@dataclass(slots=True)
class AnthropicResponder:
    tool_call: tuple[str, dict[str, Any], str] | None = None

    def respond(self, request: dict[str, Any], _count: int) -> tuple[bytes, str]:
        tool_result = (
            AnthropicRequest.decode(request).tool_result(self.tool_call[2])
            if self.tool_call
            else None
        )
        body = (
            tool_call_response(*self.tool_call)
            if self.tool_call and tool_result is None
            else FINAL_RESPONSE
        )
        return body, "text/event-stream"
