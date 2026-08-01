from __future__ import annotations

import json
import re

import pytest

from capabilities.targets.cursor_desktop_static import (
    AGENT_EXEC_BUNDLE,
    ENVIRONMENT_SCHEMA,
    EXTENSIONS,
    MCP_BUNDLE,
    read,
    require_installed,
)


@pytest.mark.capability_case("cursor-desktop.agents")
@pytest.mark.capability_live
def test_desktop_registers_a_custom_subagent_provider() -> None:
    require_installed(AGENT_EXEC_BUNDLE)
    bundle = read(AGENT_EXEC_BUNDLE)

    assert re.search(
        r"\.map\([\w$]+=>new [\w$]+\.Vz\([\w$]+,[\w$]+,\"workspace\","
        r"[\w$]+\)\),[\w$]+=new [\w$]+\.Vz\([\w$]+\.homedir\(\),[\w$]+,"
        r"\"user\",[\w$]+\)",
        bundle,
    )
    assert "registerSubagentsProvider" in bundle


@pytest.mark.capability_case("cursor-desktop.mcp")
@pytest.mark.capability_live
def test_desktop_bundles_the_mcp_extension() -> None:
    package = EXTENSIONS / "cursor-mcp/package.json"
    require_installed(package, MCP_BUNDLE, AGENT_EXEC_BUNDLE)

    assert json.loads(package.read_text())["name"] == "cursor-mcp"
    assert "DefinitionMcpLoader" in read(MCP_BUNDLE)
    assert "pushMcpWorkspaceProjectDir" in read(AGENT_EXEC_BUNDLE)


@pytest.mark.capability_case("cursor-desktop.hooks")
@pytest.mark.capability_live
def test_desktop_bundles_hook_lifecycle_identifiers() -> None:
    require_installed(AGENT_EXEC_BUNDLE)
    bundle = read(AGENT_EXEC_BUNDLE)

    assert all(
        token in bundle
        for token in ("beforeMCPExecution", "afterMCPExecution", "beforeShellExecution")
    )
    assert "hooksConfigTracker:" in bundle


@pytest.mark.capability_case("cursor-desktop.plugins")
@pytest.mark.capability_live
def test_desktop_wires_plugin_services_to_workspace_and_user_home() -> None:
    require_installed(AGENT_EXEC_BUNDLE)
    bundle = read(AGENT_EXEC_BUNDLE)

    assert re.search(
        r"er\(\{workspacePaths:[\w$]+,getThirdPartyExtensibilityEnabled:[\w$]+,"
        r"getAllowUserLocalPluginImports:[\w$]+",
        bundle,
    )
    assert "userHomeDirectory:we.homedir()" in bundle
    assert "pluginsService:" in bundle
    assert "refreshPluginHooks:" in bundle


@pytest.mark.capability_case("cursor-cloud.environment")
@pytest.mark.capability_live
def test_desktop_registers_the_cloud_environment_schema() -> None:
    package = EXTENSIONS / "cursor-always-local/package.json"
    require_installed(package, ENVIRONMENT_SCHEMA)
    validations = json.loads(package.read_text())["contributes"]["jsonValidation"]
    schema = json.loads(ENVIRONMENT_SCHEMA.read_text())

    assert {entry["fileMatch"] for entry in validations} >= {
        ".cursor/environment.json",
        ".cursor/permissions.json",
    }
    assert set(schema["definitions"]["common"]["properties"]) >= {
        "name",
        "user",
        "install",
        "start",
        "mcpServerAllowlist",
    }
