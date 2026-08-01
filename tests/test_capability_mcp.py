from __future__ import annotations

import json
import subprocess
import urllib.request

from capabilities.mcp import (
    TOOL_ARGUMENT,
    TOOL_DESCRIPTION,
    TOOL_NAME,
    TOOL_RESULT,
    CursorMCPResponder,
    MCPProtocol,
    RemoteMCP,
    ResponsesMCPResponder,
    find_tool_name,
    read_stdio_requests,
    stdio_command,
)
from capabilities.targets.cursor import command as cursor_command


def test_cursor_mcp_command_explicitly_approves_project_servers() -> None:
    command = cursor_command(
        "cursor-agent-local",
        "http://127.0.0.1:1234",
        "probe",
        "prompt",
        disable_project_configs=False,
        approve_mcps=True,
    )

    assert "--approve-mcps" in command
    assert "--disable-project-configs" not in command
    assert (
        cursor_command(
            "cursor-agent-local",
            "http://127.0.0.1:1234",
            "probe",
            "prompt",
        ).count("--approve-mcps")
        == 0
    )


def test_cursor_mcp_responder_discovers_before_calling() -> None:
    responder = CursorMCPResponder("probe")
    tools = [
        {"type": "function", "function": {"name": "GetMcpTools"}},
        {"type": "function", "function": {"name": "CallMcpTool"}},
    ]

    discovery, _ = responder.respond({"tools": tools}, 0)
    invocation, _ = responder.respond(
        {
            "tools": tools,
            "messages": [
                {"role": "tool", "content": f"{TOOL_NAME}: {TOOL_DESCRIPTION}"}
            ],
        },
        1,
    )

    assert '"name": "GetMcpTools"' in discovery.decode()
    assert '"name": "CallMcpTool"' in invocation.decode()
    assert TOOL_ARGUMENT in invocation.decode()


def test_mcp_protocol_initializes_lists_and_calls_tool() -> None:
    protocol = MCPProtocol()
    initialized = protocol.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        }
    )
    listed = protocol.dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    called = protocol.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": {"value": TOOL_ARGUMENT}},
        }
    )

    assert initialized is not None
    assert initialized["result"]["protocolVersion"] == "2025-03-26"
    assert listed is not None
    assert listed["result"]["tools"][0]["description"] == TOOL_DESCRIPTION
    assert called is not None
    assert called["result"]["content"][0]["text"] == TOOL_RESULT


def test_stdio_mcp_server_uses_newline_delimited_json_rpc(tmp_path) -> None:
    log = tmp_path / "mcp.log"
    process = subprocess.Popen(
        stdio_command(log),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    requests = (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": TOOL_NAME, "arguments": {"value": TOOL_ARGUMENT}},
        },
    )
    stdout, stderr = process.communicate(
        "".join(json.dumps(request) + "\n" for request in requests), timeout=5
    )

    assert process.returncode == 0, stderr
    responses = tuple(json.loads(line) for line in stdout.splitlines())
    assert responses[-1]["result"]["content"][0]["text"] == TOOL_RESULT
    assert read_stdio_requests(log) == requests


def test_find_tool_name_supports_chat_and_responses_shapes() -> None:
    assert (
        find_tool_name(
            {
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "mcp_probe_echo",
                            "description": TOOL_DESCRIPTION,
                        },
                    }
                ]
            }
        )
        == "mcp_probe_echo"
    )
    assert (
        find_tool_name(
            {
                "tools": [
                    {
                        "type": "function",
                        "name": "mcp__probe__echo",
                        "description": TOOL_DESCRIPTION,
                    }
                ]
            }
        )
        == "mcp__probe__echo"
    )
    assert (
        find_tool_name(
            {
                "input": [
                    {
                        "type": "tool_search_output",
                        "tools": [
                            {
                                "type": "namespace",
                                "name": "mcp__probe",
                                "tools": [
                                    {
                                        "type": "function",
                                        "name": "echo",
                                        "description": TOOL_DESCRIPTION,
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        )
        == "mcp__probe__echo"
    )


def test_responses_responder_discovers_deferred_mcp_tools() -> None:
    responder = ResponsesMCPResponder(search_deferred=True)

    body, content_type = responder.respond(
        {"tools": [{"type": "tool_search", "execution": "client"}]}, 0
    )

    text = body.decode()
    assert content_type == "text/event-stream"
    assert '"type":"tool_search_call"' in text
    assert '"execution":"client"' in text
    assert TOOL_DESCRIPTION in text
    assert responder.searched

    body, _ = responder.respond(
        {
            "input": [
                {
                    "type": "tool_search_output",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "mcp__probe",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "echo",
                                    "description": TOOL_DESCRIPTION,
                                }
                            ],
                        }
                    ],
                }
            ]
        },
        1,
    )

    text = body.decode()
    assert '"namespace":"mcp__probe"' in text
    assert '"name":"echo"' in text


def test_remote_mcp_accepts_streamable_http_json_rpc() -> None:
    with RemoteMCP.start() as remote:
        request = urllib.request.Request(
            remote.url,
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26"},
                }
            ).encode(),
            {"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.load(response)

    assert payload["result"]["serverInfo"]["name"] == "capability-probe"
    assert remote.protocol.called("initialize")
