"""Pi loads the compiled tree: observed offline through RPC mode.

RPC mode answers `get_commands` without a model, and a probe extension in the
isolated agent directory records the registered tools and the assembled
system prompt at session start. Pi runs with no network activity.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from capabilities.harness import require_command
from capabilities.runtime import run
from coding_agents_sync import run_sync

GLOBAL_MARKER = "PI_GLOBAL_MARKER_5c21e0"
RULE_MARKER = "PI_RULE_MARKER_8a0f3d"
SHARED_SKILL = "pi-shared-probe"
OWN_SKILL = "pi-own-probe"
PROMPT = "pi-prompt-probe"
# Pi's built-in tools plus the two its bundled extensions register inactive.
BUILT_IN_TOOLS = {
    "read",
    "bash",
    "powershell",
    "edit",
    "write",
    "grep",
    "find",
    "ls",
    "codemode",
    "tool_search",
}
PROBE_EXTENSION = """\
import { writeFileSync } from "node:fs";
export default function (pi) {
  pi.on("session_start", (_event, ctx) => {
    writeFileSync(process.env.PI_PROBE_OUT, JSON.stringify({
      tools: pi.getAllTools().map((tool) => tool.name),
      systemPrompt: ctx.getSystemPrompt(),
    }));
  });
}
"""


@dataclass(frozen=True, slots=True)
class Observed:
    commands: dict[str, dict[str, Any]]
    tools: list[str]
    system_prompt: str


def write_sources(config: Path) -> None:
    files = {
        "global/AGENTS.md": f"{GLOBAL_MARKER}\n",
        "rules/probe.md": f"{RULE_MARKER}\n",
        f"skills/{SHARED_SKILL}/SKILL.md": (
            "---\ndescription: Shared probe skill.\npaths: ['**/*.py']\n---\n"
            "shared body\n"
        ),
        f"skills/{OWN_SKILL}/SKILL.md": (
            "---\ndescription: Pi-only probe skill.\nonly: [pi]\n---\nown body\n"
        ),
        f"commands/{PROMPT}.md": (
            "---\ndescription: Probe prompt.\nonly: [pi]\nexecution:\n"
            "  subtask: true\ntargets:\n  pi:\n    omit: [execution]\n---\n"
            "Run $ARGUMENTS\n"
        ),
    }
    for relative, content in files.items():
        path = config / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


@pytest.fixture(scope="module")
def observed() -> Any:
    executable = require_command("pi")
    with TemporaryDirectory(prefix="pi-loading-") as directory:
        root = Path(directory).resolve()
        config, home, work = root / "config", root / "home", root / "work"
        write_sources(config)
        run_sync(config_root=config, home=home)
        extensions = home / ".pi" / "agent" / "extensions"
        extensions.mkdir(parents=True)
        (extensions / "probe.js").write_text(PROBE_EXTENSION)
        work.mkdir()
        probe = root / "probe.json"
        requests = root / "requests.jsonl"
        requests.write_text(json.dumps({"type": "get_commands"}) + "\n")
        env = {
            "HOME": str(home),
            "PATH": os.environ["PATH"],
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
            "PI_PROBE_OUT": str(probe),
        }
        with requests.open() as stdin:
            result = run(
                executable,
                "--mode",
                "rpc",
                "--no-session",
                cwd=work,
                env=env,
                stdin=stdin.fileno(),
            )
        responses = [json.loads(line) for line in result.stdout.splitlines() if line]
        commands = next(
            item["data"]["commands"]
            for item in responses
            if item.get("command") == "get_commands"
        )
        recorded = json.loads(probe.read_text())
        yield Observed(
            {command["name"]: command for command in commands},
            recorded["tools"],
            recorded["systemPrompt"],
        )


@pytest.mark.capability_case("pi.loading")
@pytest.mark.capability_live
def test_pi_loads_compiled_instructions_skills_and_prompts(observed: Observed) -> None:
    assert GLOBAL_MARKER in observed.system_prompt
    assert RULE_MARKER in observed.system_prompt
    assert {f"skill:{SHARED_SKILL}", f"skill:{OWN_SKILL}", PROMPT} <= set(
        observed.commands
    )
    shared = observed.commands[f"skill:{SHARED_SKILL}"]["sourceInfo"]["path"]
    assert "/.agents/skills/" in shared


@pytest.mark.capability_case("pi.prompt-execution")
@pytest.mark.capability_live
def test_pi_prompt_templates_carry_no_delegated_execution(observed: Observed) -> None:
    """A prompt template is a name, a description, and its source; a new field
    may be the delegated execution a command needs."""
    assert set(observed.commands[PROMPT]) == {
        "name",
        "description",
        "source",
        "sourceInfo",
    }


@pytest.mark.capability_case("pi.web-tools")
@pytest.mark.capability_live
def test_pi_registers_no_web_tool(observed: Observed) -> None:
    """Any new built-in tool fails this, so a web tool cannot arrive unseen."""
    assert set(observed.tools) == BUILT_IN_TOOLS
