import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
    sanitized_env,
)
from capabilities.protocols.responses import (
    ResponsesRequest,
    responses_done,
    responses_function_call,
    responses_tool_search,
)
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.server import RecordedServer
from capabilities.sources import source_document
from capabilities.targets.codex import write_config
from capabilities.targets.test_codex_permissions import decode_execpolicy
from coding_agents_sync import run_sync

GLOBAL_SENTINEL = "CODEX_COMPILED_GLOBAL_SENTINEL"
RULE_SENTINEL = "CODEX_COMPILED_RULE_SENTINEL"
AGENT_NAME = "compiled-codex-reviewer"
AGENT_DESCRIPTION = "CODEX_COMPILED_AGENT_DESCRIPTION_SENTINEL"
AGENT_BODY = "CODEX_COMPILED_AGENT_BODY_SENTINEL"
GENERATED_RULE = "generated-rule-probe"
FRAGMENT_RULE = "fragment-rule-probe"


@dataclass(slots=True)
class Responder:
    agent_type: str | None = None
    wait_for_agent: bool = False

    def respond(self, request: dict[str, object], _count: int) -> tuple[bytes, str]:
        inputs = request.get("input")
        if self.wait_for_agent and isinstance(inputs, list):
            output = next(
                (
                    item.get("output")
                    for item in inputs
                    if isinstance(item, dict)
                    and item.get("type") == "function_call_output"
                    and item.get("call_id") == "spawn-compiled-agent"
                ),
                None,
            )
            if isinstance(output, str):
                self.wait_for_agent = False
                return responses_function_call(
                    "wait-compiled-agent",
                    "wait_agent",
                    {"targets": [json.loads(output)["agent_id"]]},
                    namespace="multi_agent_v1",
                ), "text/event-stream"
        tools = request.get("tools")
        # Codex lists the multi-agent tools directly, or behind a tool search.
        listed = isinstance(tools, list) and any(
            isinstance(tool, dict) and tool.get("name") == "multi_agent_v1"
            for tool in tools
        )
        searched = isinstance(inputs, list) and any(
            isinstance(item, dict) and item.get("type") == "tool_search_output"
            for item in inputs
        )
        if self.agent_type and (listed or searched):
            agent_type, self.agent_type = self.agent_type, None
            self.wait_for_agent = True
            return responses_function_call(
                "spawn-compiled-agent",
                "spawn_agent",
                {
                    "agent_type": agent_type,
                    "message": "Reply with compiled agent probe complete.",
                    "task_name": "compiled_agent_probe",
                },
                namespace="multi_agent_v1",
            ), "text/event-stream"
        if (
            self.agent_type
            and isinstance(tools, list)
            and any(
                isinstance(tool, dict) and tool.get("type") == "tool_search"
                for tool in tools
            )
        ):
            return responses_tool_search(
                "agent-registration", "spawn agent"
            ), "text/event-stream"
        return responses_done("compiler-e2e"), "text/event-stream"


@dataclass(frozen=True, slots=True)
class Runtime:
    codex: str
    seatbelt: Seatbelt
    stub: RecordedServer
    responder: Responder
    root: Path


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    codex, sandbox = require_command("codex"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("codex-compiler-e2e").resolve())
    responder = Responder()
    stub = recorded_server(request, responder.respond)
    value = Runtime(codex, loopback_seatbelt(sandbox), stub, responder, paths.root)
    require_containment(
        value.seatbelt,
        stub,
        paths,
        sanitized_env({"HOME": str(paths.home), "PATH": "/usr/bin:/bin"}),
    )
    return value


def paths_for(runtime: Runtime, name: str) -> Paths:
    return Paths.create(runtime.root / name)


def environment(paths: Paths) -> dict[str, str]:
    return sanitized_env(
        {
            "CODEX_HOME": str(paths.home / ".codex"),
            "CODEX_CAPABILITY_KEY": "local-placeholder",
            "HOME": str(paths.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "TMPDIR": str(paths.tmp),
        }
    )


def configure(paths: Paths, runtime: Runtime) -> Path:
    codex_home = paths.home / ".codex"
    codex_home.mkdir()
    write_config(codex_home, paths.work, runtime.stub.base_url, approval="never")
    return codex_home


def invoke(runtime: Runtime, paths: Paths) -> tuple[ResponsesRequest, ...]:
    start = len(runtime.stub.requests)
    result = run_probe(
        run,
        *runtime.seatbelt.command(
            runtime.codex,
            "exec",
            "--skip-git-repo-check",
            "--strict-config",
            "--ephemeral",
            "--json",
            "-C",
            str(paths.work),
            "Reply with compiler probe complete.",
        ),
        cwd=paths.work,
        env=environment(paths),
        timeout=20,
        stdin=-3,
    )
    requests = runtime.stub.requests[start:]
    assert result.returncode == 0, result.stderr
    return tuple(ResponsesRequest.decode(request) for request in requests)


@pytest.mark.capability_case("codex.instructions")
@pytest.mark.capability_live
def test_codex_compiled_global_and_rule_reach_ordered_instructions(
    runtime: Runtime,
) -> None:
    paths = paths_for(runtime, "instructions")
    config_root = paths.root / "coding-agents"
    (config_root / "global").mkdir(parents=True)
    (config_root / "rules").mkdir()
    (config_root / "global" / "AGENTS.md").write_text(
        source_document("global", GLOBAL_SENTINEL)
    )
    (config_root / "rules" / "rule.md").write_text(
        source_document("rule", RULE_SENTINEL)
    )
    configure(paths, runtime)
    run_sync(config_root=config_root, home=paths.home)

    compiled = (paths.home / ".codex" / "AGENTS.md").read_text()
    assert compiled.index(GLOBAL_SENTINEL) < compiled.index(RULE_SENTINEL)

    [request] = invoke(runtime, paths)
    payload = request.text()
    assert GLOBAL_SENTINEL in payload
    assert RULE_SENTINEL in payload
    assert payload.index(GLOBAL_SENTINEL) < payload.index(RULE_SENTINEL)


@pytest.mark.capability_case("codex.agents")
@pytest.mark.capability_live
def test_codex_compiled_agent_registration_is_used_by_spawned_agent(
    runtime: Runtime,
) -> None:
    paths = paths_for(runtime, "agents")
    config_root = paths.root / "coding-agents"
    agent_dir = config_root / "agents"
    agent_dir.mkdir(parents=True)
    (agent_dir / f"{AGENT_NAME}.md").write_text(
        source_document(
            AGENT_DESCRIPTION,
            AGENT_BODY,
            "only: [codex]\ntargets:\n  codex:\n    native:\n      model: gpt-5.2\n",
        )
    )
    configure(paths, runtime)
    run_sync(config_root=config_root, home=paths.home)

    # Codex discovers agents/*.toml itself, so config.toml registers nothing.
    config = (paths.home / ".codex" / "config.toml").read_text()
    agent = (paths.home / ".codex" / "agents" / f"{AGENT_NAME}.toml").read_text()
    assert AGENT_DESCRIPTION not in config
    assert "config_file" not in config
    assert AGENT_BODY in agent
    assert 'model = "gpt-5.2"' in agent

    runtime.responder.agent_type = AGENT_NAME
    requests = invoke(runtime, paths)
    catalog = "\n".join(request.text() for request in requests)
    assert AGENT_NAME in catalog
    assert AGENT_DESCRIPTION in catalog
    spawned = [request for request in requests if AGENT_BODY in request.text()]
    assert spawned, [
        output.text[:1000]
        for request in requests
        for output in request.function_outputs()
    ]
    assert len(spawned) == 1
    assert spawned[0].raw["model"] == "gpt-5.2"


SANDBOX_LINE = re.compile(r"`sandbox_mode` is `([^`]*)`")


def spawned_child(runtime: Runtime, role: str, extra: str) -> ResponsesRequest:
    """The request of a child spawned from a role with `extra` config, under a
    parent that has live web search and lets children spawn again."""
    paths = paths_for(runtime, f"tool-gates-{role}")
    codex_home = configure(paths, runtime)
    config = codex_home / "config.toml"
    config.write_text(
        config.read_text().replace('web_search = "disabled"', 'web_search = "live"')
        + "\n[agents]\nmax_depth = 3\n"
    )
    marker = f"{AGENT_BODY}_{role}"
    (codex_home / "agents").mkdir()
    (codex_home / "agents" / f"{role}.toml").write_text(
        f'name = "{role}"\ndescription = "Role probe."\n'
        f'developer_instructions = "{marker}"\n{extra}'
    )
    runtime.responder.agent_type = role
    [child] = [
        request for request in invoke(runtime, paths) if marker in request.text()
    ]
    return child


def gates(child: ResponsesRequest) -> tuple[set[str], list[str]]:
    tools = {tool.get("name") or tool.get("type") for tool in child.raw["tools"]}
    return tools, SANDBOX_LINE.findall(json.dumps(child.raw))


@pytest.mark.capability_case("codex.agent-tool-gates")
@pytest.mark.capability_live
def test_codex_role_turns_off_its_shell_but_not_its_sandbox_search_or_spawning(
    runtime: Runtime,
) -> None:
    """A role is a config layer for the agent it spawns, yet only its features
    reach the child: its sandbox, web search, and spawn switches do not."""
    control_tools, control_sandbox = gates(spawned_child(runtime, "control", ""))
    assert {"exec_command", "web_search", "multi_agent_v1"} <= control_tools
    assert control_sandbox == ["danger-full-access"]

    no_shell, _ = gates(
        spawned_child(runtime, "no-shell", "[features]\nshell_tool = false\n")
    )
    assert no_shell == control_tools - {"exec_command", "write_stdin"}

    for role, extra in (
        ("read-only", 'sandbox_mode = "read-only"\n'),
        ("no-search", 'web_search = "disabled"\n'),
        ("no-spawn", "[features]\nmulti_agent = false\n"),
    ):
        assert gates(spawned_child(runtime, role, extra)) == (
            control_tools,
            control_sandbox,
        ), role


@pytest.mark.capability_case("codex.rule-fragments")
@pytest.mark.capability_live
def test_codex_compiled_and_native_rule_fragments_have_effective_execpolicy(
    runtime: Runtime,
) -> None:
    paths = paths_for(runtime, "rule-fragments")
    config_root = paths.root / "coding-agents"
    rule_dir = config_root / "rules"
    rule_dir.mkdir(parents=True)
    (rule_dir / "generated.md").write_text(
        source_document(
            "generated rule",
            "Generated rule body",
            "only: [codex]\ntargets:\n  codex:\n    native:\n      rules:\n"
            f"        - pattern: [{GENERATED_RULE}]\n          decision: allow\n",
        )
    )
    fragments = config_root / "target-config" / "codex" / "raw" / "rules"
    fragments.mkdir(parents=True)
    (fragments / "native.rules").write_text(
        f'prefix_rule(pattern=["{FRAGMENT_RULE}"], decision="forbidden")\n'
    )
    codex_home = configure(paths, runtime)
    run_sync(config_root=config_root, home=paths.home)

    generated = codex_home / "rules" / "coding-agents.rules"
    fragment = codex_home / "rules" / "native.rules"
    assert f'pattern=["{GENERATED_RULE}"]' in generated.read_text()
    assert f'pattern=["{FRAGMENT_RULE}"]' in fragment.read_text()
    for rules, command, decision in (
        (generated, GENERATED_RULE, "allow"),
        (fragment, FRAGMENT_RULE, "forbidden"),
    ):
        result = run_probe(
            run,
            *runtime.seatbelt.command(
                runtime.codex,
                "execpolicy",
                "check",
                "--rules",
                str(rules),
                command,
            ),
            cwd=paths.work,
            env=environment(paths),
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert decode_execpolicy(result.stdout).value == decision
