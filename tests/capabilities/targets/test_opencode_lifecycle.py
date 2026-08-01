from __future__ import annotations

import base64
import contextlib
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
)
from capabilities.model import CheckResult
from capabilities.protocols.openai import ToolResponder, openai_sse
from capabilities.protocols.opencode import OpenCodeRequest
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.targets.opencode import config as opencode_config
from capabilities.targets.opencode import environment

PLUGIN_SYSTEM = "OPENCODE_PLUGIN_SYSTEM_HOOK_59f42e"
REFERENCE_DESCRIPTION = "OPENCODE_GIT_REFERENCE_DESCRIPTION_768cda"
REFERENCE_BODY = "OPENCODE_GIT_REFERENCE_BODY_ae92c1"
FORMATTER_BODY = "OPENCODE_FORMATTER_BODY_c491d7"
COMPACTION_SUMMARY = "OPENCODE_COMPACTION_SUMMARY_eb9d20"


def executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def checked(command: str, *arguments: str, cwd: Path, env: dict[str, str]) -> None:
    result = run_probe(run, command, *arguments, cwd=cwd, env=env, timeout=30)
    assert result.returncode == 0, result.stderr or result.stdout


def wait_for(path: Path, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return True
        time.sleep(0.05)
    return path.is_file()


@dataclass(frozen=True, slots=True)
class LocalRuntime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    env: dict[str, str]
    reference_checkout: Path


def offline_plugin_dependencies(paths: Paths) -> None:
    config = paths.config / "opencode"
    (config / "node_modules").mkdir(parents=True)
    dependency = {"@opencode-ai/plugin": "*"}
    (config / "package.json").write_text(json.dumps({"dependencies": dependency}))
    (config / "package-lock.json").write_text(
        json.dumps({"packages": {"": {"dependencies": dependency}}})
    )


def git_reference(paths: Paths, git: str, env: dict[str, str]) -> tuple[Path, Path]:
    source = paths.root / "reference-source"
    remote = paths.root / "remotes" / "probe" / "reference.git"
    source.mkdir()
    remote.parent.mkdir(parents=True)
    checked(git, "init", "--quiet", "--bare", str(remote), cwd=paths.root, env=env)
    checked(git, "init", "--quiet", cwd=source, env=env)
    checked(git, "config", "user.email", "probe@example.invalid", cwd=source, env=env)
    checked(git, "config", "user.name", "Capability Probe", cwd=source, env=env)
    (source / "REFERENCE.md").write_text(f"{REFERENCE_BODY}\n")
    checked(git, "add", "REFERENCE.md", cwd=source, env=env)
    checked(git, "commit", "--quiet", "-m", "reference fixture", cwd=source, env=env)
    checked(git, "branch", "-M", "main", cwd=source, env=env)
    checked(git, "remote", "add", "origin", remote.as_uri(), cwd=source, env=env)
    checked(git, "push", "--quiet", "-u", "origin", "main", cwd=source, env=env)
    checked(
        git,
        "--git-dir",
        str(remote),
        "symbolic-ref",
        "HEAD",
        "refs/heads/main",
        cwd=paths.root,
        env=env,
    )
    return (
        paths.root / "remotes",
        paths.data / "opencode/repos/github.com/probe/reference@main",
    )


@pytest.fixture(scope="module")
def local_runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> LocalRuntime:
    opencode = require_command("opencode")
    git = require_command("git")
    sandbox = require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-lifecycle").resolve())
    (paths.work / ".git").mkdir()
    offline_plugin_dependencies(paths)
    base_env = environment(
        paths,
        paths.root / "pending.json",
        f"{Path(git).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
    )
    remotes, checkout = git_reference(paths, git, base_env)
    state = ToolResponder(
        "skill",
        {},
        "lifecycle-probe",
        "lifecycle_probe",
        "lifecycle probe complete",
        result_role="assistant",
        split_role=True,
        enabled=False,
    )

    def respond(payload: dict[str, Any], count: int) -> tuple[bytes, str]:
        wait_for(checkout / "REFERENCE.md")
        return state.respond(payload, count)

    stub = recorded_server(request, respond)
    config = opencode_config(
        stub.base_url,
        "lifecycle-probe",
        "lifecycle",
        {"*": "allow"},
    )
    config.update(
        {
            "references": {
                "git-probe": {
                    "repository": "github:probe/reference",
                    "branch": "main",
                    "description": REFERENCE_DESCRIPTION,
                }
            },
        }
    )
    config_path = paths.root / "opencode.json"
    config_path.write_text(json.dumps(config))
    env = environment(
        paths,
        config_path,
        f"{Path(git).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        OPENCODE_DISABLE_DEFAULT_PLUGINS="true",
        OPENCODE_REPO_CLONE_GITHUB_BASE_URL=f"{remotes.as_uri()}/",
    )
    value = LocalRuntime(
        opencode,
        loopback_seatbelt(sandbox),
        paths,
        stub,
        env,
        checkout,
    )
    require_containment(value.seatbelt, stub, paths, env)
    return value


def observe_local(runtime: LocalRuntime, _name: str) -> CheckResult:
    runtime.stub.requests.clear()
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.executable,
            "run",
            "Reply with lifecycle probe complete.",
            "--title",
            "capability lifecycle probe",
            "--pure",
            "--format",
            "json",
            "--model",
            "test/lifecycle-probe",
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr or process.stdout
    model = OpenCodeRequest.decode(runtime.stub.requests[0])
    materialized = wait_for(runtime.reference_checkout / "REFERENCE.md")
    return CheckResult(
        {
            "git-reference-visible": REFERENCE_DESCRIPTION in model.text("system")
            and str(runtime.reference_checkout) in model.text("system"),
            "git-reference-materialized": materialized
            and REFERENCE_BODY
            in (runtime.reference_checkout / "REFERENCE.md").read_text(),
        },
        model.text("system") + "\n" + process.stderr,
    )


_local_observations: dict[str, CheckResult] = {}


@pytest.fixture(scope="module")
def local_observation(
    request: pytest.FixtureRequest, local_runtime: LocalRuntime
) -> CheckResult:
    name = str(request.param)
    if name not in _local_observations:
        _local_observations[name] = observe_local(local_runtime, name)
    return _local_observations[name]


@pytest.mark.capability_case("opencode.references-git")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("local_observation", "check"),
    tuple(
        pytest.param("local", check, id=check)
        for check in ("git-reference-visible", "git-reference-materialized")
    ),
    indirect=("local_observation",),
    scope="module",
)
def test_opencode_git_reference(local_observation: CheckResult, check: str) -> None:
    assert local_observation.checks[check], local_observation.detail


@dataclass(frozen=True, slots=True)
class PluginRuntime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    env: dict[str, str]
    marker: Path


@pytest.fixture(scope="module")
def plugin_runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> PluginRuntime:
    opencode, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-plugin-lifecycle").resolve())
    (paths.work / ".git").mkdir()
    offline_plugin_dependencies(paths)
    marker = paths.root / "plugin-loaded"
    plugin = paths.root / "probe-plugin.mjs"
    plugin.write_text(
        "export default async () => {\n"
        f"  await Bun.write({json.dumps(str(marker))}, 'loaded')\n"
        "  return {\n"
        '    "experimental.chat.system.transform": async (_input, output) => {\n'
        f"      output.system.unshift({json.dumps(PLUGIN_SYSTEM)})\n"
        "    },\n"
        "  }\n"
        "}\n"
    )
    state = ToolResponder(
        "skill",
        {},
        "plugin-lifecycle-probe",
        "plugin_lifecycle_probe",
        "plugin lifecycle complete",
        result_role="assistant",
        split_role=True,
        enabled=False,
    )
    stub = recorded_server(request, state.respond)
    config = opencode_config(
        stub.base_url,
        "plugin-lifecycle-probe",
        "plugin lifecycle",
        {"*": "allow"},
    )
    config["plugin"] = [plugin.as_uri()]
    config_path = paths.root / "opencode.json"
    config_path.write_text(json.dumps(config))
    env = environment(
        paths,
        config_path,
        "/usr/bin:/bin:/usr/sbin:/sbin",
        OPENCODE_DISABLE_DEFAULT_PLUGINS="true",
    )
    value = PluginRuntime(
        opencode,
        loopback_seatbelt(sandbox),
        paths,
        stub,
        env,
        marker,
    )
    require_containment(value.seatbelt, stub, paths, env)
    return value


@pytest.mark.capability_case("opencode.plugins-runtime")
@pytest.mark.capability_live
def test_opencode_plugin_runtime(plugin_runtime: PluginRuntime) -> None:
    process = run_probe(
        run,
        *plugin_runtime.seatbelt.command(
            plugin_runtime.executable,
            "run",
            "Reply with plugin lifecycle complete.",
            "--title",
            "capability plugin lifecycle probe",
            "--format",
            "json",
            "--model",
            "test/plugin-lifecycle-probe",
        ),
        cwd=plugin_runtime.paths.work,
        env=plugin_runtime.env,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr or process.stdout
    model = OpenCodeRequest.decode(plugin_runtime.stub.requests[0])
    assert plugin_runtime.marker.read_text() == "loaded"
    assert PLUGIN_SYSTEM in model.text("system")


def lsp_server(path: Path, marker: Path) -> None:
    path.write_text(
        "import json, sys\n"
        f"marker = {str(marker)!r}\n"
        "def send(value):\n"
        "    body = json.dumps(value).encode()\n"
        "    sys.stdout.buffer.write("
        "f'Content-Length: {len(body)}\\r\\n\\r\\n'.encode() + body)\n"
        "    sys.stdout.buffer.flush()\n"
        "while True:\n"
        "    headers = {}\n"
        "    while True:\n"
        "        line = sys.stdin.buffer.readline()\n"
        "        if not line: raise SystemExit\n"
        "        if line in (b'\\r\\n', b'\\n'): break\n"
        "        key, value = line.decode().split(':', 1)\n"
        "        headers[key.lower()] = value.strip()\n"
        "    size = int(headers['content-length'])\n"
        "    message = json.loads(sys.stdin.buffer.read(size))\n"
        "    method = message.get('method')\n"
        "    if method == 'initialize':\n"
        "        result = {'capabilities':{'textDocumentSync':1}}\n"
        "        send({'jsonrpc':'2.0','id':message['id'],'result':result})\n"
        "    elif method == 'textDocument/didOpen':\n"
        "        open(marker, 'w').write('opened')\n"
        "    elif method == 'shutdown':\n"
        "        send({'jsonrpc':'2.0','id':message['id'],'result':None})\n"
        "    elif 'id' in message:\n"
        "        send({'jsonrpc':'2.0','id':message['id'],'result':None})\n"
    )


@dataclass(frozen=True, slots=True)
class ToolRuntime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    stub: RecordedServer
    env: dict[str, str]
    target: Path
    formatter_marker: Path
    lsp_marker: Path


@pytest.fixture(scope="module")
def tool_runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> ToolRuntime:
    opencode, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-tool-lifecycle").resolve())
    (paths.work / ".git").mkdir()
    target = paths.work / "probe.probe"
    formatter_marker = paths.root / "formatter-ran"
    formatter = paths.bin / "probe-formatter"
    executable(
        formatter,
        f'/usr/bin/touch "{formatter_marker}"\n'
        f'printf "\\n{FORMATTER_BODY}\\n" >> "$1"\n',
    )
    lsp_marker = paths.root / "lsp-opened"
    lsp = paths.root / "probe_lsp.py"
    lsp_server(lsp, lsp_marker)
    state = ToolResponder(
        "write",
        {"filePath": str(target), "content": "before lifecycle\n"},
        "tool-lifecycle-probe",
        "tool_lifecycle_probe",
        "tool lifecycle complete",
    )
    stub = recorded_server(request, state.respond)
    config = opencode_config(
        stub.base_url,
        "tool-lifecycle-probe",
        "tool lifecycle",
        {"edit": "allow"},
    )
    config.update(
        {
            "formatter": {
                "probe": {
                    "command": [str(formatter), "$FILE"],
                    "extensions": [".probe"],
                }
            },
            "lsp": {
                "probe": {
                    "command": [sys.executable, str(lsp)],
                    "extensions": [".probe"],
                }
            },
        }
    )
    config_path = paths.root / "opencode.json"
    config_path.write_text(json.dumps(config))
    env = environment(
        paths,
        config_path,
        "/usr/bin:/bin:/usr/sbin:/sbin",
        OPENCODE_DISABLE_LSP_DOWNLOAD="true",
    )
    value = ToolRuntime(
        opencode,
        loopback_seatbelt(sandbox),
        paths,
        stub,
        env,
        target,
        formatter_marker,
        lsp_marker,
    )
    require_containment(value.seatbelt, stub, paths, env)
    return value


def observe_tools(runtime: ToolRuntime, _name: str) -> CheckResult:
    process = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.executable,
            "run",
            "Call the write tool exactly as instructed by the model.",
            "--title",
            "capability tool lifecycle probe",
            "--pure",
            "--format",
            "json",
            "--model",
            "test/tool-lifecycle-probe",
        ),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr or process.stdout
    return CheckResult(
        {
            "formatter-executed": runtime.formatter_marker.is_file()
            and FORMATTER_BODY in runtime.target.read_text(),
            "lsp-initialized": runtime.lsp_marker.is_file(),
        },
        process.stdout,
    )


_tool_observations: dict[str, CheckResult] = {}


@pytest.fixture(scope="module")
def tool_observation(
    request: pytest.FixtureRequest, tool_runtime: ToolRuntime
) -> CheckResult:
    name = str(request.param)
    if name not in _tool_observations:
        _tool_observations[name] = observe_tools(tool_runtime, name)
    return _tool_observations[name]


@pytest.mark.capability_case("opencode.formatter-runtime")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("tool_observation", "check"),
    (pytest.param("tools", "formatter-executed"),),
    indirect=("tool_observation",),
    scope="module",
)
def test_opencode_formatter_runtime(tool_observation: CheckResult, check: str) -> None:
    assert tool_observation.checks[check], tool_observation.detail


@pytest.mark.capability_case("opencode.lsp-runtime")
@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("tool_observation", "check"),
    (pytest.param("tools", "lsp-initialized", id="lsp-initialized"),),
    indirect=("tool_observation",),
    scope="module",
)
def test_opencode_lsp_runtime(tool_observation: CheckResult, check: str) -> None:
    assert tool_observation.checks[check], tool_observation.detail


def usage_sse(model: str, text: str, *, input_tokens: int, output_tokens: int) -> bytes:
    prefix = openai_sse(
        [
            ({"role": "assistant", "content": text}, None),
            ({}, "stop"),
        ],
        model,
    ).decode()
    usage = {
        "id": "chatcmpl-capability",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    }
    return prefix.replace(
        "data: [DONE]", f"data: {json.dumps(usage)}\n\ndata: [DONE]"
    ).encode()


@dataclass(slots=True)
class CompactionResponder:
    requests: list[dict[str, Any]]

    def respond(self, request: dict[str, Any], _count: int) -> tuple[bytes, str]:
        self.requests.append(request)
        text = json.dumps(request.get("messages", []))
        if "Create a new anchored summary" in text:
            return usage_sse(
                "compaction-probe",
                COMPACTION_SUMMARY,
                input_tokens=100,
                output_tokens=20,
            ), "text/event-stream"
        if len(self.requests) == 1:
            return usage_sse(
                "compaction-probe",
                "initial response",
                input_tokens=950,
                output_tokens=100,
            ), "text/event-stream"
        return usage_sse(
            "compaction-probe",
            "continued after compaction",
            input_tokens=100,
            output_tokens=20,
        ), "text/event-stream"


@pytest.mark.capability_case("opencode.compaction-runtime")
@pytest.mark.capability_live
def test_opencode_compaction_runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> None:
    opencode, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-compaction").resolve())
    (paths.work / ".git").mkdir()
    state = CompactionResponder([])
    stub = recorded_server(request, state.respond)
    config = opencode_config(
        stub.base_url,
        "compaction-probe",
        "compaction",
        {"*": "allow"},
    )
    config["provider"]["test"]["models"]["compaction-probe"]["limit"] = {
        "context": 1000,
        "output": 100,
    }
    config["compaction"] = {
        "auto": True,
        "reserved": 100,
        "tail_turns": 0,
    }
    config_path = paths.root / "opencode.json"
    config_path.write_text(json.dumps(config))
    env = environment(paths, config_path, "/usr/bin:/bin:/usr/sbin:/sbin")
    seatbelt = loopback_seatbelt(sandbox)
    require_containment(seatbelt, stub, paths, env)
    process = run_probe(
        run,
        *seatbelt.command(
            opencode,
            "run",
            "Trigger the compaction lifecycle.",
            "--title",
            "capability compaction probe",
            "--pure",
            "--format",
            "json",
            "--model",
            "test/compaction-probe",
        ),
        cwd=paths.work,
        env=env,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr or process.stdout
    payloads = [json.dumps(item) for item in state.requests]
    assert len(payloads) >= 3 and any(
        "Create a new anchored summary" in item for item in payloads
    ), payloads
    assert any(COMPACTION_SUMMARY in item for item in payloads[2:]), payloads


def free_port() -> int:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        return int(server.getsockname()[1])


@dataclass(frozen=True, slots=True)
class ServerRuntime:
    process: subprocess.Popen[bytes]
    base_url: str
    directory_query: str
    authorization: str


def stop_server(process: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(5)
    return process.communicate()


def http_json(
    runtime: ServerRuntime,
    path: str,
    *,
    authorized: bool = True,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    headers = {"Accept": "application/json"}
    if authorized:
        headers["Authorization"] = runtime.authorization
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    with urlopen(
        Request(runtime.base_url + path, data=data, headers=headers, method=method),
        timeout=5,
    ) as response:
        return json.loads(response.read())


def start_server(
    opencode: str,
    seatbelt: Seatbelt,
    paths: Paths,
    env: dict[str, str],
    authorization: str,
) -> ServerRuntime:
    port = free_port()
    process = subprocess.Popen(
        seatbelt.command(
            opencode,
            "serve",
            "--pure",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "ERROR",
        ),
        cwd=paths.work,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    return ServerRuntime(
        process,
        f"http://127.0.0.1:{port}",
        f"directory={quote(str(paths.work), safe='')}",
        authorization,
    )


def wait_for_server(runtime: ServerRuntime, timeout: float = 10) -> str | None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        if runtime.process.poll() is not None:
            return f"exited with status {runtime.process.returncode}"
        try:
            http_json(runtime, "/global/health")
            return None
        except (URLError, OSError) as error:
            last_error = str(error)
            time.sleep(0.05)
    return f"did not become ready within {timeout:g}s ({last_error})"


@pytest.fixture(scope="module")
def server_runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> ServerRuntime:
    opencode, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-server").resolve())
    (paths.work / ".git").mkdir()
    config_path = paths.root / "opencode.json"
    config_path.write_text(json.dumps({"autoupdate": False}))
    username, password = "probe-user", "probe-password"
    env = environment(
        paths,
        config_path,
        "/usr/bin:/bin:/usr/sbin:/sbin",
        OPENCODE_SERVER_USERNAME=username,
        OPENCODE_SERVER_PASSWORD=password,
        OPENCODE_DISABLE_DEFAULT_PLUGINS="true",
    )
    seatbelt = loopback_seatbelt(sandbox)
    authorization = (
        "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    )
    failures = []
    for attempt in range(1, 3):
        runtime = start_server(opencode, seatbelt, paths, env, authorization)
        failure = wait_for_server(runtime)
        if failure is None:
            request.addfinalizer(lambda process=runtime.process: stop_server(process))
            return runtime
        stdout, stderr = stop_server(runtime.process)
        failures.append(
            f"attempt {attempt} ({runtime.base_url}): {failure}\n"
            f"stdout={stdout.decode()}\nstderr={stderr.decode()}"
        )
    pytest.fail(
        "OpenCode authenticated server did not become ready\n" + "\n".join(failures)
    )


@pytest.mark.capability_case("opencode.server-auth")
@pytest.mark.capability_live
def test_opencode_server_auth(server_runtime: ServerRuntime) -> None:
    with pytest.raises(HTTPError) as error:
        http_json(server_runtime, "/global/health", authorized=False)
    assert error.value.code == 401
    assert http_json(server_runtime, "/global/health")["healthy"] is True


@pytest.mark.capability_case("opencode.api-catalog")
@pytest.mark.capability_live
def test_opencode_api_catalog(server_runtime: ServerRuntime) -> None:
    agents = http_json(server_runtime, f"/agent?{server_runtime.directory_query}")
    assert {"build", "plan"} <= {item["name"] for item in agents}


@pytest.mark.capability_case("opencode.api-sessions")
@pytest.mark.capability_live
def test_opencode_api_sessions(server_runtime: ServerRuntime) -> None:
    created = http_json(
        server_runtime,
        f"/session?{server_runtime.directory_query}",
        method="POST",
        payload={},
    )
    sessions = http_json(server_runtime, f"/session?{server_runtime.directory_query}")
    assert created["id"] in {item["id"] for item in sessions}
