from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Self

from pytest_httpserver import HTTPServer
from werkzeug import Response
from werkzeug.exceptions import BadRequest

Responder = Callable[[dict[str, Any], int], tuple[bytes, str]]


@dataclass(slots=True)
class RecordedServer:
    requests: list[dict[str, Any]]
    responder: Responder
    _server: HTTPServer

    @classmethod
    def start(cls, port: int, responder: Responder) -> Self:
        requests: list[dict[str, Any]] = []
        server = HTTPServer("127.0.0.1", port, threaded=True)

        def handle(request) -> Response:
            if request.content_type != "application/json":
                return Response(status=415)
            try:
                payload = request.get_json()
            except BadRequest:
                return Response(status=400)
            if not isinstance(payload, dict):
                return Response(status=400)
            requests.append(payload)
            body, content_type = responder(payload, len(requests))
            return Response(body, content_type=content_type)

        server.expect_request("/v1/messages", method="POST").respond_with_handler(
            handle
        )
        server.expect_request("/v1/responses", method="POST").respond_with_handler(
            handle
        )
        server.expect_request(
            "/v1/chat/completions", method="POST"
        ).respond_with_handler(handle)
        server.start()
        return cls(requests, responder, server)

    @property
    def base_url(self) -> str:
        return self._server.url_for("").rstrip("/")

    def close(self) -> None:
        self._server.stop()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
