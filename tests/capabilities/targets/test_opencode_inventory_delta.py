from __future__ import annotations

import json
from pathlib import Path

import pytest

from capabilities.harness import (
    Paths,
    recorded_server,
    require_command,
    require_containment,
    run_probe,
)
from capabilities.runtime import loopback_seatbelt, run
from capabilities.targets.opencode import config as opencode_config
from capabilities.targets.opencode import environment

CUSTOM_TOOL_OUTPUT = "OPENCODE_CUSTOM_TOOL_OUTPUT_e3a71c"


def offline_plugin_dependencies(config: Path) -> None:
    (config / "node_modules").mkdir(parents=True)
    dependency = {"@opencode-ai/plugin": "*"}
    (config / "package.json").write_text(json.dumps({"dependencies": dependency}))
    (config / "package-lock.json").write_text(
        json.dumps({"packages": {"": {"dependencies": dependency}}})
    )


@pytest.mark.capability_case("opencode.custom-tools-runtime")
@pytest.mark.capability_live
def test_opencode_executes_directory_custom_tool(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> None:
    opencode, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-custom-tool").resolve())
    (paths.work / ".git").mkdir()
    config_dir = paths.config / "opencode"
    tools = config_dir / "tools"
    tools.mkdir(parents=True)
    offline_plugin_dependencies(config_dir)
    (tools / "inventory_probe.js").write_text(
        "export default {\n"
        "  args: {},\n"
        "  description: 'Capability inventory custom tool',\n"
        "  async execute(_args, context) {\n"
        f"    return {json.dumps(CUSTOM_TOOL_OUTPUT)} + ':' + context.directory\n"
        "  },\n"
        "}\n"
    )
    stub = recorded_server(
        request, lambda _payload, _count: (b"{}", "application/json")
    )
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
            "debug",
            "agent",
            "build",
            "--tool",
            "inventory_probe",
            "--params",
            "{}",
            "--pure",
        ),
        cwd=paths.work,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    payload = json.loads(result.stdout)
    assert payload["tool"] == "inventory_probe"
    assert payload["result"]["output"] == f"{CUSTOM_TOOL_OUTPUT}:{paths.work}"


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
    assert all(
        token in result.stdout
        for token in (
            "https://opencode.ai/theme.json",
            ".opencode/themes",
            "themes/*.json",
        )
    )
