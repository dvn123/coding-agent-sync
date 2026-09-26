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
from capabilities.protocols.opencode import decode_config_entries
from capabilities.runtime import Seatbelt, loopback_seatbelt, run
from capabilities.targets.opencode import environment, isolate_service, supported

INLINE_USERNAME = "inline-precedence"
LOCAL_REFERENCE = "config-local-reference"
LAYERS = ("config-dir", "explicit", "project", "dot-opencode", "inline")


@dataclass(frozen=True, slots=True)
class Runtime:
    executable: str
    seatbelt: Seatbelt
    paths: Paths
    env: dict[str, str]


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def rule(action: str, effect: str, resource: str = "*") -> dict[str, str]:
    return {"action": action, "resource": resource, "effect": effect}


def provider() -> dict[str, Any]:
    model = {
        "name": "Config Main Probe",
        "capabilities": {"tools": True, "input": ["text"], "output": ["text"]},
        "limit": {"context": 100000, "output": 1000},
        "variants": [{"id": "precise", "body": {"temperature": 0.25}}],
    }
    return {
        "name": "Config Probe Provider",
        "package": "aisdk:@ai-sdk/openai-compatible",
        "env": [],
        "settings": {
            "apiKey": "not-a-credential",
            "baseURL": "http://127.0.0.1:9/v1",
        },
        "models": {"main": model, "small": model | {"name": "Config Small Probe"}},
    }


def layer_config(name: str) -> dict[str, Any]:
    return {
        "username": f"{name}-precedence",
        "commands": {f"{name}-layer": {"template": f"{name} layer"}},
    }


def global_config(paths: Paths) -> dict[str, Any]:
    """Every probed setting in its native 2.x shape, plus two 1.x fields 2.x drops."""
    return layer_config("config-dir") | {
        "$schema": "https://opencode.ai/config.json",
        "model": "test/main",
        "providers": {"test": provider()},
        "shell": "/bin/sh",
        "logLevel": "ERROR",
        "server": {"port": 43121, "hostname": "127.0.0.1"},
        "skills": [str(paths.root / "extra-skills")],
        "references": {
            "docs": {
                "path": str(paths.root / LOCAL_REFERENCE),
                "description": "Local config reference",
                "hidden": True,
            }
        },
        "watcher": {"ignore": ["**/.probe/**"]},
        "snapshots": False,
        "share": "manual",
        "update": "disable",
        "default_agent": "probe-agent",
        "agents": {
            "title": {"model": "test/small"},
            "probe-agent": {
                "mode": "primary",
                "description": "Config agent",
                "system": "CONFIG_AGENT_PROMPT",
                "model": "test/main#precise",
                "steps": 7,
                "permissions": [rule("shell", "deny")],
            },
        },
        "commands": {
            "config-dir-layer": {"template": "config-dir layer"},
            "config-command": {
                "template": "CONFIG_COMMAND $ARGUMENTS",
                "description": "Config command",
                "agent": "probe-agent",
                "model": "test/main#precise",
                "subagent": False,
            },
        },
        "mcp": {
            "servers": {
                "local-disabled": {
                    "type": "local",
                    "command": ["false"],
                    "disabled": True,
                    "timeout": {"catalog": 1000, "execution": 1000},
                },
                "remote-disabled": {
                    "type": "remote",
                    "url": "http://127.0.0.1:9/mcp",
                    "disabled": True,
                    "oauth": False,
                },
            }
        },
        "plugins": ["file:///nonexistent/config-probe"],
        "formatter": {
            "probe": {"disabled": True, "command": ["false"], "extensions": [".probe"]}
        },
        "lsp": {
            "probe": {"disabled": True, "command": ["false"], "extensions": [".probe"]}
        },
        # A wholly denied action is 2.x tool enablement.
        "permissions": [rule("shell", "allow"), rule("webfetch", "deny")],
        "media": {
            "image": {
                "auto_resize": False,
                "max_width": 901,
                "max_height": 902,
                "max_base64_bytes": 903,
            }
        },
        "enterprise": {"url": "https://enterprise.invalid"},
        "tool_output": {"max_lines": 7, "max_bytes": 701},
        "compaction": {"auto": False, "keep": {"tokens": 404}, "buffer": 505},
        "experimental": {
            "subagent_depth": 2,
            "policies": [
                # 1.x `enabled_providers` and `disabled_providers`.
                {"action": "provider.use", "resource": "*", "effect": "deny"},
                {"action": "provider.use", "resource": "test", "effect": "allow"},
                {
                    "action": "provider.use",
                    "resource": "disabled-probe",
                    "effect": "deny",
                },
                {
                    "action": "provider.use",
                    "resource": "blocked-probe",
                    "effect": "deny",
                },
            ],
        },
    }


def write_fixtures(paths: Paths) -> tuple[Path, Path]:
    (paths.work / ".git").mkdir()
    (paths.root / LOCAL_REFERENCE).mkdir()
    (paths.root / LOCAL_REFERENCE / "README.md").write_text("local reference\n")
    (paths.root / "extra-skills").mkdir()
    config_dir = paths.root / "config-dir"
    write_json(config_dir / "opencode.json", global_config(paths))
    # OPENCODE_CONFIG_DIR replaces the global directory in 2.x rather than
    # adding a layer, so the XDG global file must not load.
    write_json(paths.config / "opencode" / "opencode.json", layer_config("xdg-global"))
    explicit = paths.root / "explicit.json"
    write_json(explicit, layer_config("explicit"))
    write_json(paths.work / "opencode.json", layer_config("project"))
    write_json(paths.work / ".opencode" / "opencode.json", layer_config("dot-opencode"))
    return explicit, config_dir


@pytest.fixture(scope="module")
def runtime(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Runtime:
    executable, sandbox = require_command("opencode"), require_command("sandbox-exec")
    paths = Paths.create(tmp_path_factory.mktemp("opencode-config").resolve())
    explicit, config_dir = write_fixtures(paths)
    value = Runtime(
        executable,
        loopback_seatbelt(sandbox),
        paths,
        environment(
            paths,
            explicit,
            "/usr/bin:/bin:/usr/sbin:/sbin",
            OPENCODE_CONFIG_DIR=str(config_dir),
            OPENCODE_CONFIG_CONTENT=json.dumps(
                layer_config("inline") | {"username": INLINE_USERNAME}
            ),
        ),
    )
    isolate_service(request, value)
    return value


def resolve(runtime: Runtime) -> CheckResult:
    # 2.x has no merged view: `debug config` lists each normalized document,
    # lowest priority first, and a setting resolves to its last definition.
    process = supported(
        "the `debug config` inspector",
        run_probe,
        run,
        *runtime.seatbelt.command(runtime.executable, "debug", "config"),
        cwd=runtime.paths.work,
        env=runtime.env,
        timeout=60,
    )
    documents = [
        entry
        for entry in decode_config_entries(process.stdout)
        if entry["type"] == "document"
    ]

    def latest(key: str, name: str | None = None) -> Any:
        """The last definition of a setting, or of one named entry in a map."""
        return next(
            (
                info[key] if name is None else info[key][name]
                for info in (item["info"] for item in reversed(documents))
                if key in info and (name is None or name in info[key])
            ),
            {},
        )

    commands = {name for item in documents for name in item["info"].get("commands", {})}
    root = runtime.paths.root
    test = latest("providers", "test")
    policies = latest("experimental").get("policies", [])
    return CheckResult(
        {
            "config-precedence": [item.get("path") for item in documents]
            == [
                str(root / "config-dir" / "opencode.json"),
                str(root / "explicit.json"),
                str(runtime.paths.work / "opencode.json"),
                str(runtime.paths.work / ".opencode" / "opencode.json"),
                None,
            ]
            and latest("username") == INLINE_USERNAME
            and {f"{layer}-layer" for layer in LAYERS} <= commands
            and "xdg-global-layer" not in commands,
            "identity-config": latest("username") == INLINE_USERNAME,
            "model-selection": latest("model")
            == {"providerID": "test", "model": "main"}
            and latest("agents", "title").get("model")
            == {"providerID": "test", "model": "small"},
            "provider-filtering": policies[:3]
            == [
                {"action": "provider.use", "resource": "*", "effect": "deny"},
                {"action": "provider.use", "resource": "test", "effect": "allow"},
                {
                    "action": "provider.use",
                    "resource": "disabled-probe",
                    "effect": "deny",
                },
            ],
            "provider-config": test["package"] == "aisdk:@ai-sdk/openai-compatible"
            and test["settings"]["baseURL"] == "http://127.0.0.1:9/v1",
            "model-config": set(test["models"]) == {"main", "small"},
            "model-variants": test["models"]["main"]["variants"]
            == [{"id": "precise", "body": {"temperature": 0.25}}],
            "shell-config": latest("shell") == "/bin/sh",
            # 2.x ignores `logLevel` (OPENCODE_LOG_LEVEL replaces it) and
            # `server` (the service settings replace it).
            "logging-config-ignored": all(
                "logLevel" not in item["info"] for item in documents
            ),
            "server-config-ignored": all(
                "server" not in item["info"] for item in documents
            ),
            "command-config": latest("commands", "config-command").get("model")
            == {"providerID": "test", "model": "main", "variant": "precise"},
            "skill-paths-config": latest("skills") == [str(root / "extra-skills")],
            "local-reference-config": latest("references", "docs").get("path")
            == str(root / LOCAL_REFERENCE),
            "watcher-config": latest("watcher") == {"ignore": ["**/.probe/**"]},
            "snapshot-config": latest("snapshots") is False,
            "sharing-config": latest("share") == "manual",
            "updates-config": latest("update") == "disable",
            "default-agent-config": latest("default_agent") == "probe-agent",
            "subagent-depth-config": latest("experimental")["subagent_depth"] == 2,
            "agent-config": latest("agents", "probe-agent").get("steps") == 7,
            "mcp-local-config": latest("mcp")["servers"]["local-disabled"]["type"]
            == "local",
            "mcp-remote-config": latest("mcp")["servers"]["remote-disabled"]["type"]
            == "remote",
            "plugin-config": latest("plugins") == ["file:///nonexistent/config-probe"],
            "formatter-config": latest("formatter")["probe"]["disabled"] is True,
            "lsp-config": latest("lsp")["probe"]["disabled"] is True,
            "permission-config": rule("shell", "allow") in latest("permissions"),
            "tool-enablement-config": rule("webfetch", "deny") in latest("permissions"),
            "attachment-config": latest("media")["image"]["max_width"] == 901,
            "enterprise-config": latest("enterprise")["url"]
            == "https://enterprise.invalid",
            "tool-output-config": latest("tool_output")
            == {"max_lines": 7, "max_bytes": 701},
            "compaction-config": latest("compaction")
            == {"auto": False, "keep": {"tokens": 404}, "buffer": 505},
            "experimental-policy-config": {
                "action": "provider.use",
                "resource": "blocked-probe",
                "effect": "deny",
            }
            in policies,
        },
        json.dumps(documents, sort_keys=True),
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
    ("opencode.logging-config", "logging-config-ignored"),
    ("opencode.server-config", "server-config-ignored"),
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
