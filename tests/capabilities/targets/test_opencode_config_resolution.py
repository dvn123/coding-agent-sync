from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from capabilities.harness import (
    Paths,
    cached_scenario_fixture,
    require_command,
    run_probe,
)
from capabilities.model import CheckResult
from capabilities.protocols.opencode import decode_config
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.targets.opencode import environment, supported

INLINE_USERNAME = "inline-precedence"
LOCAL_REFERENCE = "config-local-reference"


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    env: dict[str, str]


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def provider() -> dict[str, Any]:
    model = {
        "name": "Config Main Probe",
        "temperature": True,
        "tool_call": True,
        "limit": {"context": 100000, "output": 1000},
        "variants": {"precise": {"temperature": 0.25}},
    }
    return {
        "name": "Config Probe Provider",
        "npm": "@ai-sdk/openai-compatible",
        "env": [],
        "models": {"main": model, "small": model | {"name": "Config Small Probe"}},
        "options": {
            "apiKey": "not-a-credential",
            "baseURL": "http://127.0.0.1:9/v1",
        },
    }


def layer_config(name: str) -> dict[str, Any]:
    return {"command": {f"{name}-layer": {"template": f"{name} layer"}}}


def write_fixtures(paths: Paths) -> tuple[Path, Path]:
    (paths.work / ".git").mkdir()
    reference = paths.root / LOCAL_REFERENCE
    reference.mkdir()
    (reference / "README.md").write_text("local reference\n")
    skill_path = paths.root / "extra-skills"
    skill_path.mkdir()

    write_json(
        paths.config / "opencode" / "opencode.json",
        {
            "$schema": "https://opencode.ai/config.json",
            "username": "global-precedence",
            "model": "test/main",
            "small_model": "test/small",
            "enabled_providers": ["test"],
            "disabled_providers": ["disabled-probe"],
            "provider": {"test": provider()},
            "shell": "/bin/sh",
            "logLevel": "ERROR",
            "server": {
                "port": 43121,
                "hostname": "127.0.0.1",
                "mdns": False,
                "mdnsDomain": "probe.local",
                "cors": ["https://probe.invalid"],
            },
            "skills": {"paths": [str(skill_path)]},
            "references": {
                "docs": {
                    "path": str(reference),
                    "description": "Local config reference",
                    "hidden": True,
                }
            },
            "watcher": {"ignore": ["**/.probe/**"]},
            "snapshot": False,
            "share": "manual",
            "autoupdate": False,
            "default_agent": "probe-agent",
            "subagent_depth": 2,
            "agent": {
                "probe-agent": {
                    "mode": "primary",
                    "description": "Config agent",
                    "prompt": "CONFIG_AGENT_PROMPT",
                    "model": "test/main",
                    "variant": "precise",
                    "steps": 7,
                    "permission": {"bash": "deny"},
                }
            },
            "command": {
                **layer_config("global")["command"],
                "config-command": {
                    "template": "CONFIG_COMMAND $ARGUMENTS",
                    "description": "Config command",
                    "agent": "probe-agent",
                    "model": "test/main",
                    "variant": "precise",
                    "subtask": False,
                },
            },
            "mcp": {
                "local-disabled": {
                    "type": "local",
                    "command": ["false"],
                    "enabled": False,
                    "timeout": 1000,
                },
                "remote-disabled": {
                    "type": "remote",
                    "url": "http://127.0.0.1:9/mcp",
                    "enabled": False,
                    "oauth": False,
                    "timeout": 1000,
                },
            },
            "plugin": ["file:///nonexistent/config-probe.mjs"],
            "formatter": {
                "probe": {
                    "disabled": True,
                    "command": ["false"],
                    "extensions": [".probe"],
                }
            },
            "lsp": {
                "probe": {
                    "disabled": True,
                    "command": ["false"],
                    "extensions": [".probe"],
                }
            },
            "permission": {"bash": "allow"},
            "tools": {"webfetch": False},
            "attachment": {
                "image": {
                    "auto_resize": False,
                    "max_width": 901,
                    "max_height": 902,
                    "max_base64_bytes": 903,
                }
            },
            "enterprise": {"url": "https://enterprise.invalid"},
            "tool_output": {"max_lines": 7, "max_bytes": 701},
            "compaction": {
                "auto": False,
                "prune": True,
                "tail_turns": 3,
                "preserve_recent_tokens": 404,
                "reserved": 505,
            },
            "experimental": {
                "continue_loop_on_deny": True,
                "mcp_timeout": 1200,
                "policies": [
                    {
                        "effect": "deny",
                        "action": "provider.use",
                        "resource": "blocked-probe",
                    }
                ],
            },
        },
    )

    explicit = paths.root / "explicit.json"
    write_json(
        explicit,
        {"username": "explicit-precedence"} | layer_config("explicit"),
    )
    write_json(
        paths.work / "opencode.json",
        {"username": "project-precedence"} | layer_config("project"),
    )
    write_json(
        paths.work / ".opencode" / "opencode.json",
        {"username": "dot-opencode-precedence"} | layer_config("dot-opencode"),
    )
    config_dir = paths.root / "config-dir"
    write_json(
        config_dir / "opencode.json",
        {"username": "config-dir-precedence"} | layer_config("config-dir"),
    )
    return explicit, config_dir


@pytest.fixture(scope="module")
def runtime(tmp_path_factory: pytest.TempPathFactory) -> Runtime:
    executable, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-config").resolve())
    explicit, config_dir = write_fixtures(paths)
    return Runtime(
        executable,
        loopback_seatbelt(sandbox),
        paths,
        environment(
            paths,
            explicit,
            "/usr/bin:/bin:/usr/sbin:/sbin",
            OPENCODE_CONFIG_DIR=str(config_dir),
            OPENCODE_CONFIG_CONTENT=json.dumps(
                {"username": INLINE_USERNAME} | layer_config("inline")
            ),
        ),
    )


def resolve(runtime: Runtime) -> CheckResult:
    process = supported(
        "the `debug config` inspector",
        run_probe,
        run,
        *runtime.seatbelt.command(runtime.executable, "debug", "config", "--pure"),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=30,
    )
    config = decode_config(process.stdout).raw
    test = config["provider"]["test"]
    return CheckResult(
        {
            "config-precedence": config["username"] == INLINE_USERNAME
            and {
                "global-layer",
                "explicit-layer",
                "project-layer",
                "dot-opencode-layer",
                "config-dir-layer",
                "inline-layer",
            }
            <= config["command"].keys(),
            "identity-config": config["username"] == INLINE_USERNAME,
            "model-selection": config["model"] == "test/main"
            and config["small_model"] == "test/small",
            "provider-filtering": config["enabled_providers"] == ["test"]
            and config["disabled_providers"] == ["disabled-probe"],
            "provider-config": test["npm"] == "@ai-sdk/openai-compatible"
            and test["options"]["baseURL"] == "http://127.0.0.1:9/v1",
            "model-config": set(test["models"]) == {"main", "small"},
            "model-variants": test["models"]["main"]["variants"]["precise"]
            == {"temperature": 0.25},
            "shell-config": config["shell"] == "/bin/sh",
            "logging-config": config["logLevel"] == "ERROR",
            "server-config": config["server"]
            == {
                "port": 43121,
                "hostname": "127.0.0.1",
                "mdns": False,
                "mdnsDomain": "probe.local",
                "cors": ["https://probe.invalid"],
            },
            "command-config": config["command"]["config-command"]["variant"]
            == "precise",
            "skill-paths-config": config["skills"]["paths"]
            == [str(runtime.paths.root / "extra-skills")],
            "local-reference-config": config["references"]["docs"]["path"]
            == str(runtime.paths.root / LOCAL_REFERENCE),
            "watcher-config": config["watcher"] == {"ignore": ["**/.probe/**"]},
            "snapshot-config": config["snapshot"] is False,
            "sharing-config": config["share"] == "manual",
            "updates-config": config["autoupdate"] is False,
            "default-agent-config": config["default_agent"] == "probe-agent",
            "subagent-depth-config": config["subagent_depth"] == 2,
            "agent-config": config["agent"]["probe-agent"]["steps"] == 7,
            "mcp-local-config": config["mcp"]["local-disabled"]["type"] == "local",
            "mcp-remote-config": config["mcp"]["remote-disabled"]["type"] == "remote",
            "plugin-config": config["plugin"]
            == ["file:///nonexistent/config-probe.mjs"],
            "formatter-config": config["formatter"]["probe"]["disabled"] is True,
            "lsp-config": config["lsp"]["probe"]["disabled"] is True,
            "permission-config": config["permission"]["bash"] == "allow",
            "tool-enablement-config": config["tools"]["webfetch"] is False,
            "attachment-config": config["attachment"]["image"]["max_width"] == 901,
            "enterprise-config": config["enterprise"]["url"]
            == "https://enterprise.invalid",
            "tool-output-config": config["tool_output"]
            == {"max_lines": 7, "max_bytes": 701},
            "compaction-config": config["compaction"]
            == {
                "auto": False,
                "prune": True,
                "tail_turns": 3,
                "preserve_recent_tokens": 404,
                "reserved": 505,
            },
            "experimental-policy-config": config["experimental"]["policies"]
            == [
                {
                    "effect": "deny",
                    "action": "provider.use",
                    "resource": "blocked-probe",
                }
            ],
        },
        json.dumps(config, sort_keys=True),
    )


def observe(runtime: Runtime, _name: str) -> CheckResult:
    return resolve(runtime)


observation = cached_scenario_fixture(observe)

CASE_CHECKS = (
    ("opencode.config-discovery", "config-precedence"),
    ("opencode.identity-config", "identity-config"),
    ("opencode.model-selection-config", "model-selection"),
    ("opencode.provider-filtering-config", "provider-filtering"),
    ("opencode.providers-config", "provider-config"),
    ("opencode.models-config", "model-config"),
    ("opencode.model-variants-config", "model-variants"),
    ("opencode.shell-config", "shell-config"),
    ("opencode.logging-config", "logging-config"),
    ("opencode.server-config", "server-config"),
    ("opencode.command-config-resolution", "command-config"),
    ("opencode.skill-paths-config", "skill-paths-config"),
    ("opencode.references-local-config", "local-reference-config"),
    ("opencode.watcher-config", "watcher-config"),
    ("opencode.snapshots-config", "snapshot-config"),
    ("opencode.sharing-config", "sharing-config"),
    ("opencode.updates-config", "updates-config"),
    ("opencode.agent-selection-config", "default-agent-config"),
    ("opencode.subagent-depth-config", "subagent-depth-config"),
    ("opencode.agent-config-resolution", "agent-config"),
    ("opencode.mcp-local-config", "mcp-local-config"),
    ("opencode.mcp-remote-config", "mcp-remote-config"),
    ("opencode.plugins-config", "plugin-config"),
    ("opencode.formatter-config", "formatter-config"),
    ("opencode.lsp-config", "lsp-config"),
    ("opencode.permissions-config", "permission-config"),
    ("opencode.tool-enablement-config", "tool-enablement-config"),
    ("opencode.attachments-config", "attachment-config"),
    ("opencode.enterprise-config", "enterprise-config"),
    ("opencode.tool-output-config", "tool-output-config"),
    ("opencode.compaction-config", "compaction-config"),
    ("opencode.experimental-config", "experimental-policy-config"),
)


@pytest.mark.capability_live
@pytest.mark.parametrize(
    ("observation", "check"),
    tuple(
        pytest.param(
            "config",
            check,
            id=check,
            marks=pytest.mark.capability_case(case_id),
        )
        for case_id, check in CASE_CHECKS
    ),
    indirect=("observation",),
    scope="module",
)
def test_opencode_config_resolution(observation: CheckResult, check: str) -> None:
    assert observation.checks[check], observation.detail
