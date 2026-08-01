from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
)
from capabilities.protocols.anthropic import AnthropicRequest, AnthropicResponder
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.sources import source_document
from capabilities.targets.claude import environment
from coding_agents_sync import run_sync

GLOBAL_SENTINEL = "CLAUDE_COMPILED_GLOBAL_SENTINEL"
RULE_SENTINEL = "CLAUDE_COMPILED_GLOB_RULE_SENTINEL"
AGENT_NAME = "compiled-claude-reviewer"
AGENT_DESCRIPTION = "CLAUDE_COMPILED_AGENT_DESCRIPTION_SENTINEL"
AGENT_BODY = "CLAUDE_COMPILED_AGENT_BODY_SENTINEL"
CLAUDE_FLAGS = (
    "--print",
    "--output-format",
    "stream-json",
    "--verbose",
    "--no-session-persistence",
    "--setting-sources",
    "user",
)


@dataclass(frozen=True, slots=True)
class Runtime:
    claude: str
    seatbelt: Seatbelt
    stub: RecordedServer
    root: Path


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    claude, sandbox = require_command("claude"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("claude-compiler-e2e").resolve())
    stub = recorded_server(request, AnthropicResponder().respond)
    value = Runtime(claude, loopback_seatbelt(sandbox), stub, paths.root)
    require_containment(
        value.seatbelt,
        stub,
        paths,
        environment(paths, stub.base_url),
    )
    return value


def paths_for(runtime: Runtime, name: str) -> Paths:
    return Paths.create(runtime.root / name)


def invoke(runtime: Runtime, paths: Paths, prompt: str) -> AnthropicRequest:
    start = len(runtime.stub.requests)
    result = run_probe(
        run,
        *runtime.seatbelt.command(runtime.claude, prompt, *CLAUDE_FLAGS),
        cwd=paths.work,
        env=environment(
            paths,
            runtime.stub.base_url,
            CLAUDE_CONFIG_DIR=str(paths.home / ".claude"),
        ),
        timeout=20,
    )
    requests = runtime.stub.requests[start:]
    assert result.returncode == 0, result.stderr
    assert len(requests) == 1
    return AnthropicRequest.decode(requests[0])


@pytest.mark.capability_case("claude.instructions-rules")
@pytest.mark.capability_live
def test_claude_compiled_global_and_glob_rule_reach_root_request(
    runtime: Runtime,
) -> None:
    paths = paths_for(runtime, "instructions-rules")
    config_root = paths.root / "coding-agents"
    (config_root / "global").mkdir(parents=True)
    (config_root / "rules").mkdir()
    (config_root / "global" / "AGENTS.md").write_text(
        source_document("global", "global", "global", "global", GLOBAL_SENTINEL)
    )
    (config_root / "rules" / "python.md").write_text(
        source_document(
            "rule",
            "python",
            "python",
            "glob rule",
            RULE_SENTINEL,
            "activation:\n  globs:\n    - '**/*.py'\n",
        )
    )

    run_sync(config_root=config_root, home=paths.home)

    compiled_global = (paths.home / ".claude" / "CLAUDE.md").read_text()
    compiled_rule = (paths.home / ".claude" / "rules" / "python.md").read_text()
    assert GLOBAL_SENTINEL in compiled_global
    assert RULE_SENTINEL in compiled_rule
    assert "activation:" not in compiled_rule

    request = invoke(runtime, paths, "Reply with compiler probe complete.")
    payload = request.text()
    assert GLOBAL_SENTINEL in payload
    # Current user-scoped Claude lowering intentionally ignores activation globs.
    assert RULE_SENTINEL in payload


@pytest.mark.capability_case("claude.agents")
@pytest.mark.capability_live
def test_claude_compiled_agent_is_discoverable_in_root_request(
    runtime: Runtime,
) -> None:
    paths = paths_for(runtime, "agents")
    config_root = paths.root / "coding-agents"
    agent_dir = config_root / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / f"{AGENT_NAME}.md").write_text(
        source_document(
            "agent",
            AGENT_NAME,
            AGENT_NAME,
            AGENT_DESCRIPTION,
            AGENT_BODY,
            "tools:\n  inherit: false\n  allow:\n    - Read\n",
        )
    )

    run_sync(config_root=config_root, home=paths.home)

    compiled = (paths.home / ".claude" / "agents" / f"{AGENT_NAME}.md").read_text()
    assert AGENT_DESCRIPTION in compiled
    assert AGENT_BODY in compiled
    assert "tools: Read" in compiled

    request = invoke(runtime, paths, "Reply with compiler probe complete.")
    payload = request.text()
    assert AGENT_NAME in payload
    assert AGENT_DESCRIPTION in payload
