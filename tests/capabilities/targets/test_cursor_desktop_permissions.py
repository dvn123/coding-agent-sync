from __future__ import annotations

import json
import re

import pytest

from capabilities.targets.cursor_desktop_static import (
    AGENT_EXEC_BUNDLE,
    DESKTOP_BUNDLE,
    PERMISSIONS_SCHEMA,
    read,
    require_installed,
)


@pytest.mark.capability_case("cursor-desktop.mcp-permissions")
@pytest.mark.capability_live
def test_desktop_schema_lists_the_mcp_allowlist_key() -> None:
    require_installed(PERMISSIONS_SCHEMA, DESKTOP_BUNDLE, AGENT_EXEC_BUNDLE)
    properties = json.loads(PERMISSIONS_SCHEMA.read_text())["properties"]

    allowlist = properties["mcpAllowlist"]

    assert allowlist["type"] == "array"
    assert allowlist["items"] == {
        "type": "string",
        "minLength": 1,
        "pattern": r"^[^:*]+:[^:*]+$|^[^:*]+:\*$|^\*:[^:*]+$|^\*:\*$",
    }


@pytest.mark.capability_case("cursor-desktop.mcp-permissions")
@pytest.mark.capability_live
def test_desktop_parses_the_mcp_allowlist_separately_from_terminal_permissions() -> (
    None
):
    require_installed(PERMISSIONS_SCHEMA, DESKTOP_BUNDLE, AGENT_EXEC_BUNDLE)
    bundle = read(DESKTOP_BUNDLE)

    for pattern in (
        r"mcpAllowlist:Array\.isArray\([\w$]+\.mcpAllowlist\)",
        r"terminalAllowlist:Array\.isArray\([\w$]+\.terminalAllowlist\)",
    ):
        assert re.search(pattern, bundle), pattern


@pytest.mark.capability_case("cursor-desktop.auto-review")
@pytest.mark.capability_live
def test_desktop_schema_and_bundle_expose_auto_review_instruction_channels() -> None:
    require_installed(PERMISSIONS_SCHEMA, DESKTOP_BUNDLE, AGENT_EXEC_BUNDLE)
    properties = json.loads(PERMISSIONS_SCHEMA.read_text())["properties"]
    bundle = read(DESKTOP_BUNDLE)

    assert properties["autoRun"] == properties["autoReview"]
    assert properties["autoRun"]["type"] == "object"
    assert set(properties["autoRun"]["properties"]) == {
        "allow_instructions",
        "block_instructions",
    }
    assert set(properties["approvalMode"]["enum"]) == {
        "allowlist",
        "unrestricted",
        "manual",
    }
    for pattern in (
        r"approvalMode:[\w$]+\([\w$]+\.approvalMode\)",
        r"([\w$]+)\.autoReview\?\?\1\.autoRun",
        r'([\w$]+)==="allowlist"\|\|\1==="unrestricted"\|\|\1==="manual"',
        r"allowInstructions:[\w$]+\([\w$]+\.allow_instructions\),"
        r"blockInstructions:[\w$]+\([\w$]+\.block_instructions\)",
    ):
        assert re.search(pattern, bundle), pattern


@pytest.mark.capability_case("cursor-desktop.auto-review")
@pytest.mark.capability_live
def test_desktop_lets_the_permission_file_approval_mode_decide_auto_run() -> None:
    require_installed(PERMISSIONS_SCHEMA, DESKTOP_BUNDLE, AGENT_EXEC_BUNDLE)
    bundle = read(DESKTOP_BUNDLE)

    assert re.search(
        r'case"unrestricted":case"allowlist":return!0;case"manual":return!1', bundle
    )
    assert re.search(
        r'case"unrestricted":return!0;case"allowlist":case"manual":return!1', bundle
    )
    assert re.search(
        r"canAddToAllowlistFromIde\([\w$]+\)\{return [\w$]+\(\)\.isAdminControlled\|\|"
        r"this\._hasAdminConfiguredPermissionsFilePaths\|\|"
        r"this\._permissionsFileApprovalMode!==void 0\?!1:",
        bundle,
    )


@pytest.mark.capability_case("cursor-desktop.permissions")
@pytest.mark.capability_live
def test_desktop_lowers_the_terminal_allowlist_to_allows_with_no_deny_list() -> None:
    require_installed(PERMISSIONS_SCHEMA, DESKTOP_BUNDLE, AGENT_EXEC_BUNDLE)
    bundle = read(AGENT_EXEC_BUNDLE)

    assert re.search(r"\.map\([\w$]+=>`Shell\(\$\{[\w$]+\}\)`\)", bundle)
    assert re.search(r"\{allow:[\w$]+,deny:\[\]\}", bundle)
    assert re.search(
        r"getEffectiveTerminalAllowlist\(\)\.map\([\w$]+=>`Shell\(\$\{[\w$]+\}\)`\)",
        read(DESKTOP_BUNDLE),
    )


@pytest.mark.capability_case("cursor-desktop.permissions")
@pytest.mark.capability_live
def test_desktop_evaluates_every_parsed_command_in_a_chain() -> None:
    require_installed(PERMISSIONS_SCHEMA, DESKTOP_BUNDLE, AGENT_EXEC_BUNDLE)
    bundle = read(AGENT_EXEC_BUNDLE)

    for token in (
        "Parser failed to parse command (possible bypass)",
        "Parser found no commands (possible bypass)",
        "allCommandsRunnable",
        "allCommandsAllowlisted",
        "unapprovedCommands",
        "notAllowedCommands",
    ):
        assert token in bundle, token
    assert "dashboardTerminalAllowlistOverriddenByPermissionsFile" in bundle
    assert "dashboardTerminalAllowlistOverriddenByPermissionsFile" in read(
        DESKTOP_BUNDLE
    )
