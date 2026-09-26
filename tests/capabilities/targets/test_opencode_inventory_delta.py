from __future__ import annotations

import json
import re

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
)
from capabilities.protocols.openai import ToolResponder
from capabilities.protocols.opencode import OpenCodeEvent
from capabilities.runtime import loopback_seatbelt, run
from capabilities.targets.opencode import config as opencode_config
from capabilities.targets.opencode import environment

CUSTOM_TOOL = "inventory_probe"
CUSTOM_TOOL_OUTPUT = "OPENCODE_CUSTOM_TOOL_OUTPUT_e3a71c"
# 2.x discovers themes by joining `themes` onto each config directory, so the
# minified join is the only stable trace of discovery in the binary.
THEME_DISCOVERY = re.compile(r'\.join\(\w+,"themes"\)')


@pytest.mark.capability_case("opencode.custom-tools-runtime")
@pytest.mark.capability_live
def test_opencode_executes_plugin_custom_tool(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> None:
    opencode, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-custom-tool").resolve())
    (paths.work / ".git").mkdir()
    # 2.x dropped the `tools/` directory and 1.x plugin functions; a custom
    # tool is a tool registered by a `plugins/` definition.
    plugins = paths.config / "opencode" / "plugins"
    plugins.mkdir(parents=True)
    (plugins / f"{CUSTOM_TOOL}.js").write_text(
        "export default {\n"
        f"  id: {json.dumps(CUSTOM_TOOL)},\n"
        "  async setup(ctx) {\n"
        "    const directory = ctx.location.directory\n"
        "    await ctx.tool.transform((editor) => {\n"
        "      editor.add({\n"
        f"        name: {json.dumps(CUSTOM_TOOL)},\n"
        "        description: 'Capability inventory custom tool',\n"
        "        input: { type: 'object', properties: {} },\n"
        # Plugin tools default to CodeMode, reachable only through `execute`.
        "        options: { codemode: false },\n"
        "        async execute() {\n"
        f"          const output = {json.dumps(CUSTOM_TOOL_OUTPUT)}\n"
        "          return { content: output + ':' + directory }\n"
        "        },\n"
        "      })\n"
        "    })\n"
        "  },\n"
        "}\n"
    )
    state = ToolResponder(
        CUSTOM_TOOL,
        {},
        "custom-tool-probe",
        "call_custom_tool_probe",
        "custom tool probe complete",
    )
    stub = recorded_server(request, state.respond)
    config = opencode_config(
        stub.base_url,
        "custom-tool-probe",
        "custom tool",
        {"*": "allow"},
    )
    config_path = paths.root / "opencode.json"
    config_path.write_text(json.dumps(config))
    env = environment(paths, config_path, "/usr/bin:/bin:/usr/sbin:/sbin")
    seatbelt = loopback_seatbelt(sandbox)
    require_containment(seatbelt, stub, paths, env)

    result = run_probe(
        run,
        *seatbelt.command(
            opencode,
            "run",
            f"Call the {CUSTOM_TOOL} tool exactly as instructed by the model.",
            "--standalone",
            "--format",
            "json",
            "--model",
            "test/custom-tool-probe",
        ),
        cwd=paths.work,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    observation = next(
        (
            value
            for event in OpenCodeEvent.decode_lines(result.stdout)
            if (value := event.tool_use()) is not None and value.tool == CUSTOM_TOOL
        ),
        None,
    )
    assert observation is not None, result.stdout
    assert observation.status == "completed", observation
    assert observation.output == f"{CUSTOM_TOOL_OUTPUT}:{paths.work}"


@pytest.mark.capability_case("opencode.custom-themes-static")
@pytest.mark.capability_live
def test_opencode_installed_binary_exposes_custom_theme_discovery(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    opencode, strings = require_command("opencode"), require_command("strings")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-custom-theme").resolve())
    env = environment(
        paths,
        paths.root / "missing-opencode.json",
        "/usr/bin:/bin:/usr/sbin:/sbin",
    )

    result = run_probe(
        run,
        strings,
        opencode,
        cwd=paths.work,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    assert "https://opencode.ai/theme.json" in result.stdout
    assert THEME_DISCOVERY.search(result.stdout)
