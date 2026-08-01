from __future__ import annotations

from pathlib import Path

import pytest

from capabilities.targets.cursor_desktop_static import (
    AGENT_EXEC_BUNDLE,
    DESKTOP_BUNDLE,
    read,
    require_installed,
)


@pytest.mark.parametrize(
    ("bundle_path", "tokens"),
    [
        pytest.param(
            AGENT_EXEC_BUNDLE,
            ("registerCursorRulesProvider", "getAllCursorRules"),
            marks=pytest.mark.capability_case("cursor-desktop.global-rule"),
            id="global-rule",
        ),
        pytest.param(
            AGENT_EXEC_BUNDLE,
            (
                "MergedAgentSkillsService.getAllAgentSkills",
                "getAllAgentSkills",
                "updateAgentSkills",
            ),
            marks=pytest.mark.capability_case("cursor-desktop.skills"),
            id="skills",
        ),
        pytest.param(
            AGENT_EXEC_BUNDLE,
            (
                ".cursor/settings.json contains syntax errors",
                'createFileSystemWatcher("**/.cursor/settings.json")',
            ),
            marks=pytest.mark.capability_case("cursor-desktop.settings"),
            id="settings",
        ),
        pytest.param(
            DESKTOP_BUNDLE,
            ("SubmittedCustomMode", 'x!=="customModes"', "Export Custom Modes"),
            marks=pytest.mark.capability_case("cursor-desktop.custom-modes"),
            id="custom-modes",
        ),
    ],
)
@pytest.mark.capability_live
def test_desktop_installed_bundle_exposes_config_surface(
    bundle_path: Path, tokens: tuple[str, ...]
) -> None:
    require_installed(bundle_path)
    bundle = read(bundle_path)

    for token in tokens:
        assert token in bundle, token
