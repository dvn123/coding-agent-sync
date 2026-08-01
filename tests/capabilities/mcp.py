from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from pytest_httpserver import HTTPServer
from werkzeug import Response
from werkzeug.exceptions import BadRequest

TOOL_NAME = "echo"
TOOL_DESCRIPTION = "MCP_PROBE_TOOL_DESCRIPTION_62D0"
TOOL_ARGUMENT = "MCP_PROBE_ARGUMENT_38B4"
TOOL_RESULT = "MCP_PROBE_RESULT_937A"


@dataclass(slots=True)
class MCPProtocol:
    requests: list[dict[str, Any]] = field(default_factory=list)

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any] | None:
        self.requests.append(request)
        identifier = request.get("id")
        if identifier is None:
            return None
        method = request.get("method")
        if method == "initialize":
            params = request.get("params")
            protocol_version = (
                params.get("protocolVersion")
                if isinstance(params, dict)
                else "2025-03-26"
            )
            return self._result(
                identifier,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "capability-probe", "version": "1.0"},
                },
            )
        if method == "tools/list":
            return self._result(
                identifier,
                {
                    "tools": [
                        {
                            "name": TOOL_NAME,
                            "description": TOOL_DESCRIPTION,
                            "inputSchema": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                                "additionalProperties": False,
                            },
                        }
                    ]
                },
            )
        if method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
                return self._error(identifier, -32602, "unknown probe tool")
            arguments = params.get("arguments")
            value = arguments.get("value") if isinstance(arguments, dict) else None
            if value != TOOL_ARGUMENT:
                return self._error(identifier, -32602, "invalid probe argument")
            return self._result(
                identifier,
                {
                    "content": [{"type": "text", "text": TOOL_RESULT}],
                    "isError": False,
                },
            )
        if method == "ping":
            return self._result(identifier, {})
        return self._error(identifier, -32601, f"unsupported method: {method}")

    @staticmethod
    def _result(identifier: object, result: object) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    @staticmethod
    def _error(identifier: object, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": identifier,
            "error": {"code": code, "message": message},
        }

    def called(self, method: str) -> bool:
        return any(request.get("method") == method for request in self.requests)


@dataclass(slots=True)
class RemoteMCP:
    protocol: MCPProtocol
    _server: HTTPServer

    @classmethod
    def start(cls) -> Self:
        protocol = MCPProtocol()
        server = HTTPServer("127.0.0.1", 0, threaded=True)

        def handle(request) -> Response:
            try:
                payload = request.get_json()
            except BadRequest:
                return Response(status=400)
            if not isinstance(payload, dict):
                return Response(status=400)
            response = protocol.dispatch(payload)
            if response is None:
                return Response(status=202)
            return Response(
                json.dumps(response, separators=(",", ":")),
                content_type="application/json",
            )

        server.expect_request("/mcp", method="POST").respond_with_handler(handle)
        server.start()
        return cls(protocol, server)

    @property
    def url(self) -> str:
        return self._server.url_for("/mcp")

    def close(self) -> None:
        self._server.stop()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def stdio_command(log: Path) -> tuple[str, ...]:
    return sys.executable, str(Path(__file__).resolve()), "--stdio", str(log)


def read_stdio_requests(log: Path) -> tuple[dict[str, Any], ...]:
    if not log.exists():
        return ()
    return tuple(json.loads(line) for line in log.read_text().splitlines() if line)


def mcp_checks(requests: tuple[dict[str, Any], ...] | list[dict[str, Any]]):
    methods = {request.get("method") for request in requests}
    return {
        "mcp-initialized": "initialize" in methods,
        "mcp-tools-listed": "tools/list" in methods,
        "mcp-tool-called": "tools/call" in methods,
    }


def find_tool_name(request: dict[str, Any]) -> str | None:
    tool_groups = [request.get("tools")]
    inputs = request.get("input")
    if isinstance(inputs, list):
        tool_groups.extend(
            item.get("tools")
            for item in inputs
            if isinstance(item, dict) and item.get("type") == "tool_search_output"
        )
    for tools in tool_groups:
        if not isinstance(tools, list):
            continue
        if name := _find_tool_name(tools):
            return name
    return None


def _find_tool_name(tools: list[Any]) -> str | None:
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        nested = tool.get("tools")
        namespace = tool.get("name")
        if (
            isinstance(nested, list)
            and isinstance(namespace, str)
            and (name := _find_tool_name(nested))
        ):
            return f"{namespace}__{name}"
        function = tool.get("function")
        candidate = function if isinstance(function, dict) else tool
        description = candidate.get("description")
        name = candidate.get("name")
        if (
            isinstance(description, str)
            and TOOL_DESCRIPTION in description
            and isinstance(name, str)
        ):
            return name
    return None


@dataclass(slots=True)
class ChatMCPResponder:
    model: str
    tool_name: str | None = None

    def respond(self, request: dict[str, Any], _count: int) -> tuple[bytes, str]:
        from capabilities.protocols.openai import tool_call_sse

        self.tool_name = self.tool_name or find_tool_name(request)
        called = TOOL_RESULT in json.dumps(request)
        body = (
            tool_call_sse(
                self.tool_name,
                {"value": TOOL_ARGUMENT},
                called=False,
                model=self.model,
                call_id="mcp_probe_call",
                completion="mcp probe complete",
            )
            if self.tool_name and not called
            else tool_call_sse(
                self.tool_name or "unused",
                {},
                called=True,
                model=self.model,
                call_id="mcp_probe_call",
                completion="mcp probe complete",
            )
        )
        return body, "text/event-stream"


@dataclass(slots=True)
class CursorMCPResponder:
    model: str
    discovered: bool = False
    called_tool: bool = False

    def respond(self, request: dict[str, Any], _count: int) -> tuple[bytes, str]:
        from capabilities.protocols.openai import tool_call_sse

        discover = _find_named_tool(request, "GetMcpTools")
        invoke = _find_named_tool(request, "CallMcpTool")
        if not self.discovered and discover:
            self.discovered = True
            body = tool_call_sse(
                discover,
                {"server": "probe", "toolName": TOOL_NAME},
                called=False,
                model=self.model,
                call_id="mcp_probe_discover",
                completion="mcp probe complete",
            )
        elif (
            self.discovered
            and not self.called_tool
            and invoke
            and TOOL_DESCRIPTION in json.dumps(request)
        ):
            self.called_tool = True
            body = tool_call_sse(
                invoke,
                {
                    "server": "probe",
                    "toolName": TOOL_NAME,
                    "arguments": {"value": TOOL_ARGUMENT},
                    "description": "Call the MCP capability probe",
                },
                called=False,
                model=self.model,
                call_id="mcp_probe_call",
                completion="mcp probe complete",
            )
        else:
            body = tool_call_sse(
                "unused",
                {},
                called=True,
                model=self.model,
                call_id="mcp_probe_call",
                completion="mcp probe complete",
            )
        return body, "text/event-stream"


def _find_named_tool(request: dict[str, Any], name: str) -> str | None:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return None
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        candidate = function if isinstance(function, dict) else tool
        if candidate.get("name") == name:
            return name
    return None


@dataclass(slots=True)
class AnthropicMCPResponder:
    tool_name: str | None = None

    def respond(self, request: dict[str, Any], _count: int) -> tuple[bytes, str]:
        from capabilities.protocols.anthropic import FINAL_RESPONSE, tool_call_response

        self.tool_name = self.tool_name or find_tool_name(request)
        called = TOOL_RESULT in json.dumps(request)
        body = (
            tool_call_response(
                self.tool_name,
                {"value": TOOL_ARGUMENT},
                "mcp_probe_call",
            )
            if self.tool_name and not called
            else FINAL_RESPONSE
        )
        return body, "text/event-stream"


def _responses_function(
    name: str, arguments: dict[str, Any], *, namespace: str | None = None
) -> bytes:
    from capabilities.protocols.responses import responses_sse

    item = {
        "type": "function_call",
        "call_id": f"{name}_probe_call",
        "name": name,
        "arguments": json.dumps(arguments, separators=(",", ":")),
    }
    if namespace is not None:
        item["namespace"] = namespace
    return responses_sse(
        [
            {"type": "response.created", "response": {"id": "resp-mcp-probe"}},
            {
                "type": "response.output_item.done",
                "item": item,
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-mcp-probe",
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    },
                },
            },
        ]
    )


def _responses_tool_search(query: str) -> bytes:
    from capabilities.protocols.responses import responses_sse

    return responses_sse(
        [
            {"type": "response.created", "response": {"id": "resp-mcp-search"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "tool_search_call",
                    "id": "tsc-mcp-probe",
                    "call_id": "tool_search_probe_call",
                    "execution": "client",
                    "status": "completed",
                    "arguments": {"query": query},
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-mcp-search",
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                    },
                },
            },
        ]
    )


@dataclass(slots=True)
class ResponsesMCPResponder:
    search_deferred: bool = False
    tool_name: str | None = None
    searched: bool = False
    called_tool: bool = False

    def respond(self, request: dict[str, Any], _count: int) -> tuple[bytes, str]:
        from capabilities.protocols.responses import responses_done

        self.tool_name = self.tool_name or find_tool_name(request)
        if self.tool_name and not self.called_tool:
            self.called_tool = True
            namespace, separator, name = self.tool_name.rpartition("__")
            body = _responses_function(
                name if separator else self.tool_name,
                {"value": TOOL_ARGUMENT},
                namespace=namespace if separator and self.searched else None,
            )
        elif (
            self.search_deferred
            and not self.searched
            and _has_tool(request, "tool_search")
        ):
            self.searched = True
            body = _responses_tool_search(TOOL_DESCRIPTION)
        else:
            body = responses_done("mcp-probe")
        return body, "text/event-stream"


def _has_tool(request: dict[str, Any], name: str) -> bool:
    tools = request.get("tools")
    return isinstance(tools, list) and any(
        isinstance(tool, dict)
        and (tool.get("name") == name or tool.get("type") == name)
        for tool in tools
    )


def _serve_stdio(log: Path) -> None:
    protocol = MCPProtocol()
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        with log.open("a") as output:
            output.write(json.dumps(request, separators=(",", ":")) + "\n")
        response = protocol.dispatch(request)
        if response is not None:
            print(json.dumps(response, separators=(",", ":")), flush=True)


if __name__ == "__main__" and sys.argv[1:2] == ["--stdio"]:
    _serve_stdio(Path(sys.argv[2]))
