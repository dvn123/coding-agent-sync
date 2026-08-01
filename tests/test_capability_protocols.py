from __future__ import annotations

import pytest
from capabilities.protocols.anthropic import AnthropicRequest
from capabilities.protocols.common import ProtocolShapeError
from capabilities.protocols.cursor import CursorEvent, CursorRequest
from capabilities.protocols.openai import ChatRequest, JsonEvent
from capabilities.protocols.opencode import (
    OpenCodeEvent,
    decode_config,
    decode_skill_catalog,
)
from capabilities.protocols.responses import ResponsesRequest
from capabilities.targets.opencode import config as opencode_config
from capabilities.targets.test_codex_permissions import decode_execpolicy


def test_anthropic_decoder_retains_channel_and_source_path() -> None:
    decoded = AnthropicRequest.decode(
        {
            "model": "claude-test",
            "max_tokens": 1,
            "stream": True,
            "messages": [
                {"role": "user", "content": "plain"},
                {
                    "role": "system",
                    "content": [{"type": "text", "text": "instructions"}],
                },
            ],
        }
    )
    assert ("user", "$.messages[0].content", "plain") in {
        (item.channel, item.source_path, item.text) for item in decoded.observations
    }
    assert "instructions" in decoded.text("system")


def test_responses_decoder_indexes_native_roles() -> None:
    decoded = ResponsesRequest.decode(
        {
            "model": "test",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "skill catalog"}],
                }
            ],
            "tools": [],
        }
    )
    match = next(
        item
        for item in decoded.observations
        if item.text == "skill catalog" and item.channel == "developer"
    )
    assert match.channel == "developer"
    assert match.source_path == "$.input[0].content[0].text"


def test_responses_decoder_accepts_native_tool_search_exchange() -> None:
    decoded = ResponsesRequest.decode(
        {
            "model": "test",
            "input": [
                {
                    "type": "tool_search_call",
                    "arguments": {"query": "probe"},
                    "execution": "client",
                },
                {
                    "type": "tool_search_output",
                    "execution": "client",
                    "tools": [
                        {
                            "type": "function",
                            "name": "mcp__probe__echo",
                            "description": "probe description",
                        }
                    ],
                },
            ],
            "tools": [],
        }
    )

    assert "mcp__probe__echo" in decoded.text("tool_search_output")
    assert "probe description" in decoded.text("tool_search_output")


def test_chat_decoder_separates_messages_and_tools() -> None:
    decoded = ChatRequest.decode(
        {
            "model": "test",
            "messages": [{"role": "user", "content": "prompt"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "skill", "description": "catalog"},
                }
            ],
        }
    )
    assert decoded.text("user") == "prompt"
    assert all(value in decoded.text("tools") for value in ("skill", "catalog"))


def test_json_event_decoder_retains_event_channel_and_stdout_path() -> None:
    decoded = JsonEvent.decode_lines('{"type":"tool_use","part":{"tool":"bash"}}\n')
    match = next(item for item in decoded[0].observations if item.text == "bash")
    assert match.channel == "tool_use"
    assert match.source_path == "stdout[0].part.tool"


def test_cursor_decoder_validates_stream_and_tool_call_subtype() -> None:
    request = CursorRequest.decode(
        {
            "model": "test",
            "stream": True,
            "messages": [{"role": "user", "content": "prompt"}],
        }
    )
    assert request.text("user") == "prompt"
    with pytest.raises(ProtocolShapeError, match="subtype"):
        CursorEvent.decode_lines('{"type":"tool_call","subtype":"new"}\n')


def test_cursor_decoder_returns_exact_shell_outcome_path() -> None:
    [event] = CursorEvent.decode_lines(
        '{"type":"tool_call","subtype":"completed","tool_call":'
        '{"shellToolCall":{"result":{"success":{"exitCode":0}}}}}\n'
    )
    outcome = event.shell_outcome()
    assert outcome is not None
    assert (
        outcome.channel,
        outcome.source_path,
        outcome.text,
    ) == (
        "tool_call",
        "stdout[0].tool_call.shellToolCall.result",
        "success",
    )
    with pytest.raises(ProtocolShapeError, match="shell result"):
        CursorEvent.decode_lines('{"type":"tool_call","subtype":"completed"}\n')[
            0
        ].shell_outcome()


def test_opencode_decoder_returns_typed_tool_use() -> None:
    [event] = OpenCodeEvent.decode_lines(
        '{"type":"tool_use","part":{"tool":"skill","state":'
        '{"status":"completed","input":{"name":"probe"},"output":"body"}}}\n'
    )
    observation = event.tool_use()
    assert observation is not None
    assert (
        observation.channel,
        observation.source_path,
        observation.tool,
        observation.status,
    ) == ("tool_use", "stdout[0].part.state", "skill", "completed")


def test_opencode_inspector_decoders_reject_unknown_envelopes() -> None:
    catalog = decode_skill_catalog('[{"name":"probe"}]')
    config = decode_config('{"permission":{"bash":{"*":"allow"}}}')
    assert catalog.raw[0]["name"] == "probe"
    assert catalog.observations[0].channel == "skill_catalog"
    assert catalog.observations[0].source_path == "$[0].name"
    assert config.raw["permission"] == {"bash": {"*": "allow"}}
    assert config.observations[0].source_path == "$.permission.bash.*"
    with pytest.raises(ProtocolShapeError, match="skill inspector"):
        decode_skill_catalog("{}")
    with pytest.raises(ProtocolShapeError, match="config inspector"):
        decode_config("[]")


def test_opencode_probe_config_is_wholly_native_v1() -> None:
    config = opencode_config(
        "http://127.0.0.1:1234",
        "probe",
        "protocol",
        {"bash": {"*": "allow"}},
    )

    assert set(config) == {
        "$schema",
        "model",
        "small_model",
        "autoupdate",
        "enabled_providers",
        "permission",
        "provider",
    }
    assert not {"permissions", "providers"} & config.keys()
    provider = config["provider"]["test"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == "http://127.0.0.1:1234/v1"
    assert provider["models"]["probe"] == {
        "name": "Protocol Probe",
        "tool_call": True,
        "limit": {"context": 100000, "output": 1000},
    }


def test_codex_execpolicy_decoder_retains_channel_and_source_path() -> None:
    observation = decode_execpolicy('{"decision":"allow"}')
    assert (
        observation.channel,
        observation.source_path,
        observation.value,
    ) == ("execpolicy", "$.decision", "allow")
    with pytest.raises(ProtocolShapeError, match="execpolicy envelope"):
        decode_execpolicy("[]")


@pytest.mark.parametrize(
    "decoder,payload",
    [
        (AnthropicRequest.decode, {}),
        (ResponsesRequest.decode, {"model": "test", "input": {}}),
        (ChatRequest.decode, {"model": "test", "messages": {}}),
    ],
)
def test_decoders_fail_closed_with_structural_diagnostic(decoder, payload) -> None:
    with pytest.raises(ProtocolShapeError, match="invalid"):
        decoder(payload)


@pytest.mark.parametrize(
    ("decoder", "payload", "diagnostic"),
    [
        (
            AnthropicRequest.decode,
            {
                "model": "test",
                "max_tokens": 1,
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "future", "text": "sentinel"}],
                    }
                ],
            },
            "unknown Anthropic block",
        ),
        (
            ResponsesRequest.decode,
            {
                "model": "test",
                "input": [{"type": "future", "content": "sentinel"}],
            },
            "unknown Responses item",
        ),
    ],
)
def test_decoders_reject_unknown_semantic_variants(
    decoder, payload, diagnostic: str
) -> None:
    with pytest.raises(ProtocolShapeError, match=diagnostic):
        decoder(payload)
