from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest
import yaml

from coding_agents_sync import run_sync


def source(root: Path, kind: str, native: dict, **extra) -> Path:
    path = root / ("skills/probe/SKILL.md" if kind == "skill" else "agents/probe.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema": "coding-agents/v1",
        "kind": kind,
        "id": "probe",
        "name": "probe",
        "description": "Probe current capabilities",
        "only": ["codex"],
        "targets": {"codex": {"native": native}},
        **extra,
    }
    path.write_text("---\n" + yaml.safe_dump(meta) + "---\nProbe body.\n")
    return path


def test_skill_metadata_merges_and_retires_without_touching_source(
    tmp_path: Path,
) -> None:
    root, home = tmp_path / "config", tmp_path / "home"
    native = {
        "openai": {
            "interface": {
                "display_name": "Probe",
                "short_description": "Inspect a project",
            },
            "policy": {"allow_implicit_invocation": True},
            "dependencies": {
                "tools": [
                    {
                        "type": "mcp",
                        "value": "docs",
                        "transport": "streamable_http",
                        "url": "https://example.com/mcp",
                    }
                ]
            },
        }
    }
    path = source(root, "skill", native, disable_model_invocation=True)
    bundled = path.parent / "agents/openai.yaml"
    bundled.parent.mkdir()
    original = "interface:\n  brand_color: '#112233'\npolicy:\n  future_policy: keep\n"
    bundled.write_text(original)

    run_sync(config_root=root, home=home)

    deployed = home / ".codex/skills/probe"
    expected = native["openai"] | {
        "policy": {
            "allow_implicit_invocation": False,
            "future_policy": "keep",
        }
    }
    expected["interface"] = expected["interface"] | {"brand_color": "#112233"}
    assert yaml.safe_load((deployed / "agents/openai.yaml").read_text()) == expected
    assert "openai:" not in (deployed / "SKILL.md").read_text()
    assert bundled.read_text() == original
    assert run_sync(config_root=root, home=home, check=True) == ()

    source(root, "skill", {})
    run_sync(config_root=root, home=home)
    assert (deployed / "agents/openai.yaml").read_text() == original


def test_agent_controls_and_model_policy_compile_to_toml(tmp_path: Path) -> None:
    root, home = tmp_path / "config", tmp_path / "home"
    native = {
        "features": {
            "shell_tool": False,
            "apps": False,
            "request_permissions_tool": False,
        },
        "skills": {
            "config": [{"path": "/example/skill/SKILL.md", "enabled": False}],
            "bundled": {"enabled": False},
            "include_instructions": False,
        },
        "model_verbosity": "low",
        "model_reasoning_summary": "none",
    }
    source(root, "agent", native)
    (root / "model-policy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": "coding-agents/model-policy/v1",
                "default": "sweet-spot",
                "profiles": {
                    "sweet-spot": {
                        "codex": {"model": "example-model", "effort": "ultra"}
                    },
                    "inherit": {"codex": {}},
                },
            }
        )
    )

    run_sync(config_root=root, home=home)

    agent = home / ".codex/agents/probe.toml"
    assert tomllib.loads(agent.read_text()) == native | {
        "name": "probe",
        "description": "Probe current capabilities",
        "developer_instructions": "Probe body.",
        "model": "example-model",
        "model_reasoning_effort": "ultra",
    }
    source(root, "agent", native, model_policy="inherit")
    run_sync(config_root=root, home=home)
    assert "model" not in tomllib.loads(agent.read_text())
    assert "model_reasoning_effort" not in tomllib.loads(agent.read_text())


@pytest.mark.parametrize(
    "kind,native,match",
    [
        (
            "skill",
            {"openai": {"interface": {"unknown": "typo"}}},
            "invalid codex native",
        ),
        (
            "skill",
            {
                "openai": {
                    "dependencies": {"tools": [{"value": "docs", "type": "shell"}]}
                }
            },
            "invalid codex native",
        ),
        ("agent", {"features": {"shell_tool": True}}, "invalid codex native"),
        (
            "agent",
            {"skills": {"config": [{"path": "x", "enabled": True}]}},
            "invalid codex native",
        ),
    ],
)
def test_invalid_current_fields_fail_before_writes(
    tmp_path: Path,
    kind: str,
    native: dict,
    match: str,
) -> None:
    root, home = tmp_path / "config", tmp_path / "home"
    source(root, kind, native)
    with pytest.raises(ValueError, match=match):
        run_sync(config_root=root, home=home)
    assert not home.exists()


def test_model_policy_conflict_fails_before_writes(tmp_path: Path) -> None:
    root, home = tmp_path / "config", tmp_path / "home"
    source(root, "agent", {"model": "native-model"})
    (root / "model-policy.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": "coding-agents/model-policy/v1",
                "default": "sweet-spot",
                "profiles": {
                    "sweet-spot": {"codex": {"model": "policy-model"}},
                    "inherit": {},
                },
            }
        )
    )
    with pytest.raises(ValueError, match="model policy owns Codex fields: model"):
        run_sync(config_root=root, home=home)
    assert not home.exists()


def test_agent_max_effort_reaches_codex(tmp_path: Path) -> None:
    root, home = tmp_path / "config", tmp_path / "home"
    source(root, "agent", {}, effort="max")
    run_sync(config_root=root, home=home)
    agent = tomllib.loads((home / ".codex/agents/probe.toml").read_text())
    assert agent["model_reasoning_effort"] == "max"


def test_portable_starter_compiles_on_a_fresh_home(tmp_path: Path) -> None:
    root, home = tmp_path / "config", tmp_path / "home"
    shutil.copytree(Path(__file__).parents[1] / "examples/portable", root)
    run_sync(config_root=root, home=home)
    assert (home / ".codex/AGENTS.md").is_file()
    assert (home / ".claude/skills/project-review/SKILL.md").is_file()
    assert (home / ".config/opencode/agents/reviewer.md").is_file()
    metadata = home / ".codex/skills/project-review/agents/openai.yaml"
    assert (
        yaml.safe_load(metadata.read_text())["interface"]["display_name"]
        == "Project review"
    )
    assert run_sync(config_root=root, home=home, check=True) == ()
