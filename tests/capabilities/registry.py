from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from .model import Case, EvidenceKind, Support, Surface, SurfaceManagement, Target

TARGETS = {
    name: Target(name, command, (command, action))
    for name, command, action in (
        ("claude", "claude", "update"),
        ("codex", "codex", "update"),
        ("cursor-agent", "cursor-agent", "update"),
        ("opencode", "opencode", "upgrade"),
    )
}
TARGETS["cursor-desktop"] = Target("cursor-desktop", "", None)

CASES = (
    Case(
        "claude.loading",
        "claude",
        "targets/test_claude_loading.py",
        surfaces=("claude.skills", "claude.commands"),
    ),
    Case(
        "claude.permissions",
        "claude",
        "targets/test_claude_permissions.py",
        surfaces=("claude.permissions",),
    ),
    Case(
        "claude.settings",
        "claude",
        "targets/test_claude_config.py",
        surfaces=("claude.settings",),
        evidence_kind=EvidenceKind.CONFIG_RESOLUTION,
    ),
    Case(
        "claude.sandbox",
        "claude",
        "targets/test_claude_config.py",
        surfaces=("claude.sandbox",),
    ),
    Case(
        "claude.instructions-rules",
        "claude",
        "targets/test_claude_rules.py",
        surfaces=("claude.instructions", "claude.rules"),
        evidence_kind=EvidenceKind.COMPILER_E2E,
    ),
    Case(
        "claude.agents",
        "claude",
        "targets/test_claude_rules.py",
        surfaces=("claude.agents",),
        evidence_kind=EvidenceKind.COMPILER_E2E,
    ),
    Case(
        "claude.agent-tools",
        "claude",
        "targets/test_claude_agent_tools.py",
        surfaces=("claude.agents",),
    ),
    *(
        Case(
            f"claude.{name}",
            "claude",
            "targets/test_claude_mcp.py",
            surfaces=(f"claude.{name}",),
        )
        for name in ("mcp-runtime", "mcp-oauth")
    ),
    *(
        Case(
            f"claude.{name}",
            "claude",
            "targets/test_claude_extended.py",
            surfaces=(f"claude.{name}",),
            evidence_kind=EvidenceKind.INSTALLED_STATIC,
        )
        for name in (
            "mcp",
            "hooks",
            "agent-teams-experimental",
            "memory",
            "plugins",
            "output-styles",
            "keybindings-statusline",
            "managed-policy",
            "sessions-workflows",
        )
    ),
    *(
        Case(
            f"claude.{name}",
            "claude",
            "targets/test_claude_extended.py",
            surfaces=(f"claude.{name}",),
        )
        for name in ("providers-auth", "gui-cloud")
    ),
    Case(
        "codex.loading",
        "codex",
        "targets/test_codex_loading.py",
        surfaces=(
            "codex.skills",
            "codex.command-skills",
            "codex.skill-registrations",
        ),
    ),
    Case(
        "codex.permissions",
        "codex",
        "targets/test_codex_permissions.py",
        surfaces=("codex.exec-policy",),
    ),
    Case(
        "codex.config",
        "codex",
        "targets/test_codex_config.py",
        surfaces=("codex.config",),
        evidence_kind=EvidenceKind.CONFIG_RESOLUTION,
    ),
    Case(
        "codex.permission-profiles",
        "codex",
        "targets/test_codex_config.py",
        surfaces=("codex.permission-profiles",),
    ),
    Case(
        "codex.model-providers",
        "codex",
        "targets/test_codex_config.py",
        surfaces=("codex.model-providers",),
    ),
    Case(
        "codex.profiles",
        "codex",
        "targets/test_codex_config.py",
        surfaces=("codex.profiles",),
        evidence_kind=EvidenceKind.CONFIG_RESOLUTION,
    ),
    Case(
        "codex.instructions",
        "codex",
        "targets/test_codex_rules.py",
        surfaces=("codex.instructions",),
        evidence_kind=EvidenceKind.COMPILER_E2E,
    ),
    Case(
        "codex.agents",
        "codex",
        "targets/test_codex_rules.py",
        surfaces=("codex.agents", "codex.agent-registrations"),
        evidence_kind=EvidenceKind.COMPILER_E2E,
    ),
    Case(
        "codex.rule-fragments",
        "codex",
        "targets/test_codex_rules.py",
        surfaces=("codex.rule-fragments",),
        evidence_kind=EvidenceKind.COMPILER_E2E,
    ),
    *(
        Case(
            f"codex.{name}",
            "codex",
            "targets/test_codex_mcp.py",
            surfaces=(f"codex.{name}",),
        )
        for name in ("mcp-runtime", "mcp-oauth")
    ),
    *(
        Case(
            f"codex.{name}",
            "codex",
            "targets/test_codex_extended.py",
            surfaces=(f"codex.{name}",),
            evidence_kind=EvidenceKind.INSTALLED_STATIC,
        )
        for name in ("mcp", "hooks", "plugins", "runtime-sessions")
    ),
    *(
        Case(
            f"codex.{name}",
            "codex",
            "targets/test_codex_extended.py",
            surfaces=(f"codex.{name}",),
        )
        for name in ("apps-connectors", "managed-policy", "gui-cloud")
    ),
    Case(
        "cursor-agent.loading",
        "cursor-agent",
        "targets/test_cursor_loading.py",
        surfaces=(
            "cursor-agent.global-rule",
            "cursor-agent.rules",
            "cursor-agent.skills",
            "cursor-agent.commands",
        ),
    ),
    Case(
        "cursor-agent.permissions",
        "cursor-agent",
        "targets/test_cursor_permissions.py",
        surfaces=("cursor-agent.permissions",),
    ),
    Case(
        "cursor-agent.config",
        "cursor-agent",
        "targets/test_cursor_config.py",
        surfaces=("cursor-agent.config",),
    ),
    Case(
        "cursor-agent.agents-project",
        "cursor-agent",
        "targets/test_cursor_agents_loading.py",
        surfaces=("cursor-agent.agents-project",),
    ),
    Case(
        "cursor-agent.agents-user",
        "cursor-agent",
        "targets/test_cursor_agents_loading.py",
        expected=Support.UNSUPPORTED,
        surfaces=("cursor-agent.agents-user",),
    ),
    *(
        Case(
            f"cursor-agent.{name}",
            "cursor-agent",
            "targets/test_cursor_mcp.py",
            surfaces=(f"cursor-agent.{name}",),
        )
        for name in ("mcp-runtime", "mcp-oauth", "mcp-allowlist")
    ),
    *(
        Case(
            f"cursor-agent.{case_id}",
            "cursor-agent",
            "targets/test_cursor_contract_permissions.py",
            surfaces=(f"cursor-agent.{surface}",),
            evidence_kind=EvidenceKind.INSTALLED_STATIC,
        )
        for case_id, surface in (
            ("mcp", "mcp"),
            ("hooks", "hooks"),
            ("plugins", "plugins"),
            ("sandbox", "sandbox"),
            ("worktree", "worktrees"),
            ("private-worker", "private-worker"),
        )
    ),
    *(
        Case(
            f"cursor-agent.{name}",
            "cursor-agent",
            "targets/test_cursor_extended.py",
            surfaces=(f"cursor-agent.{name}",),
            evidence_kind=EvidenceKind.INSTALLED_STATIC,
        )
        for name in ("sessions", "modes")
    ),
    Case(
        "cursor-desktop.permissions",
        "cursor-desktop",
        "targets/test_cursor_desktop_permissions.py",
        surfaces=("cursor-desktop.terminal-permissions",),
        evidence_kind=EvidenceKind.INSTALLED_STATIC,
    ),
    Case(
        "cursor-desktop.mcp-permissions",
        "cursor-desktop",
        "targets/test_cursor_desktop_permissions.py",
        surfaces=("cursor-desktop.mcp-permissions",),
        evidence_kind=EvidenceKind.INSTALLED_STATIC,
    ),
    Case(
        "cursor-desktop.auto-review",
        "cursor-desktop",
        "targets/test_cursor_desktop_permissions.py",
        surfaces=("cursor-desktop.auto-review",),
        evidence_kind=EvidenceKind.INSTALLED_STATIC,
    ),
    Case(
        "cursor-desktop.rules",
        "cursor-desktop",
        "targets/test_cursor_desktop_rules.py",
        surfaces=("cursor-desktop.rules",),
        evidence_kind=EvidenceKind.COMPILER_E2E,
    ),
    *(
        Case(
            f"cursor-desktop.{name}",
            "cursor-desktop",
            "targets/test_cursor_desktop_config.py",
            surfaces=(f"cursor-desktop.{name}",),
            evidence_kind=EvidenceKind.INSTALLED_STATIC,
        )
        for name in ("global-rule", "skills", "settings", "custom-modes")
    ),
    *(
        Case(
            case_id,
            "cursor-desktop",
            "targets/test_cursor_desktop_loading.py",
            surfaces=(surface,),
            evidence_kind=EvidenceKind.INSTALLED_STATIC,
        )
        for case_id, surface in (
            ("cursor-desktop.agents", "cursor-desktop.agents"),
            ("cursor-desktop.mcp", "cursor-desktop.mcp"),
            ("cursor-desktop.hooks", "cursor-desktop.hooks"),
            ("cursor-desktop.plugins", "cursor-desktop.plugins"),
            ("cursor-cloud.environment", "cursor-cloud.environment"),
        )
    ),
    *(
        Case(
            f"cursor-desktop.{name}",
            "cursor-desktop",
            "targets/test_cursor_extended.py",
            surfaces=(f"cursor-desktop.{name}",),
            evidence_kind=EvidenceKind.INSTALLED_STATIC,
        )
        for name in ("commands", "sandbox", "extensions")
    ),
    *(
        Case(
            f"cursor-cloud.{name}",
            "cursor-desktop",
            "targets/test_cursor_extended.py",
            surfaces=(f"cursor-cloud.{name}",),
        )
        for name in (
            "instructions",
            "hooks",
            "automations",
            "admin",
            "handoff",
            "computer-use",
            "network-policy",
        )
    ),
    Case(
        "opencode.loading",
        "opencode",
        "targets/test_opencode_loading.py",
        surfaces=("opencode.skills", "opencode.commands"),
    ),
    *(
        Case(
            f"opencode.{case_id}",
            "opencode",
            "targets/test_opencode_loading.py",
            surfaces=(f"opencode.{surface}",),
        )
        for case_id, surface in (
            ("command-config", "command-config"),
            ("skill-paths", "skill-paths"),
            ("skill-urls", "skill-urls"),
            ("skills-catalog", "skills-catalog"),
        )
    ),
    *(
        Case(
            f"opencode.{case_id}",
            "opencode",
            "targets/test_opencode_config_resolution.py",
            surfaces=((f"opencode.{surface}",) if surface else ()),
            evidence_kind=EvidenceKind.CONFIG_RESOLUTION,
        )
        for case_id, surface in (
            ("config-discovery", "config-discovery"),
            ("model-selection-config", "model-selection"),
            ("provider-filtering-config", "provider-filtering"),
            ("providers-config", "providers"),
            ("models-config", "models"),
            ("model-variants-config", "model-variants"),
            ("shell-config", "shell"),
            ("logging-config", "logging"),
            ("server-config", "server"),
            ("command-config-resolution", "command-config"),
            ("skill-paths-config", "skill-paths"),
            ("references-local-config", "references-local"),
            ("watcher-config", "watcher"),
            ("snapshots-config", "snapshots"),
            ("sharing-config", "sharing-config"),
            ("updates-config", "updates"),
            ("identity-config", "identity"),
            ("agent-selection-config", "agent-selection"),
            ("subagent-depth-config", "subagent-depth"),
            ("agent-config-resolution", "agent-config"),
            ("mcp-local-config", "mcp-local"),
            ("mcp-remote-config", "mcp-remote"),
            ("plugins-config", "plugins"),
            ("formatter-config", "formatter-config"),
            ("lsp-config", "lsp-config"),
            ("permissions-config", "permissions"),
            ("tool-enablement-config", "tool-enablement"),
            ("attachments-config", "attachments"),
            ("enterprise-config", "enterprise"),
            ("tool-output-config", "tool-output"),
            ("compaction-config", "compaction"),
            ("experimental-config", "experimental"),
        )
    ),
    *(
        Case(
            f"opencode.{case_id}",
            "opencode",
            "targets/test_opencode_runtime.py",
            surfaces=(f"opencode.{surface}",),
        )
        for case_id, surface in (
            ("models-runtime", "models"),
            ("model-selection-runtime", "model-selection"),
            ("model-variants-runtime", "model-variants"),
            ("providers-runtime", "providers"),
            ("small-model-runtime", "small-model-selection"),
            ("agent-selection-runtime", "agent-selection"),
            ("agent-config-runtime", "agent-config"),
            ("tool-enablement-runtime", "tool-enablement"),
            ("references-local", "references-local"),
            ("attachments", "attachments"),
        )
    ),
    *(
        Case(
            f"opencode.{case_id}",
            "opencode",
            "targets/test_opencode_tools.py",
            surfaces=(f"opencode.{surface}",),
        )
        for case_id, surface in (
            ("shell-runtime", "shell"),
            ("tool-output-runtime", "tool-output"),
        )
    ),
    Case(
        "opencode.snapshots-runtime",
        "opencode",
        "targets/test_opencode_snapshots.py",
        surfaces=("opencode.snapshots",),
    ),
    Case(
        "opencode.permissions",
        "opencode",
        "targets/test_opencode_permissions.py",
        surfaces=("opencode.permissions",),
    ),
    Case(
        "opencode.instructions",
        "opencode",
        "targets/test_opencode_rules.py",
        surfaces=("opencode.global-instructions", "opencode.instructions-config"),
    ),
    Case(
        "opencode.agents",
        "opencode",
        "targets/test_opencode_rules.py",
        surfaces=("opencode.agents",),
    ),
    *(
        Case(
            f"opencode.{name}",
            "opencode",
            "targets/test_opencode_mcp.py",
            surfaces=(f"opencode.{name}",),
        )
        for name in ("mcp-local-runtime", "mcp-remote-runtime", "mcp-oauth")
    ),
    *(
        Case(
            f"opencode.{name}",
            "opencode",
            "targets/test_opencode_lifecycle.py",
            surfaces=(f"opencode.{name}",),
        )
        for name in (
            "plugins-runtime",
            "references-git",
            "formatter-runtime",
            "lsp-runtime",
            "compaction-runtime",
            "server-auth",
            "api-catalog",
            "api-sessions",
        )
    ),
    *(
        Case(
            f"opencode.{name}",
            "opencode",
            "targets/test_opencode_external.py",
            surfaces=(f"opencode.{name}",),
            evidence_kind=EvidenceKind.INSTALLED_STATIC,
        )
        for name in ("tui-config", "tui-runtime")
    ),
    *(
        Case(
            f"opencode.{name}",
            "opencode",
            "targets/test_opencode_external.py",
            surfaces=(f"opencode.{name}",),
        )
        for name in (
            "sharing-runtime",
            "websearch-provider-env",
            "remote-config",
            "managed-config",
            "enterprise-runtime",
            "updater-runtime",
            "skill-urls-external",
            "watcher-runtime",
        )
    ),
    Case(
        "opencode.custom-tools-runtime",
        "opencode",
        "targets/test_opencode_inventory_delta.py",
        surfaces=("opencode.custom-tools",),
    ),
    Case(
        "opencode.custom-themes-static",
        "opencode",
        "targets/test_opencode_inventory_delta.py",
        surfaces=("opencode.custom-themes",),
        evidence_kind=EvidenceKind.INSTALLED_STATIC,
    ),
    Case(
        "opencode.compiler",
        "opencode",
        "targets/test_opencode_rules.py",
        surfaces=(
            "opencode.global-instructions",
            "opencode.instructions-config",
            "opencode.agents",
        ),
        evidence_kind=EvidenceKind.COMPILER_E2E,
    ),
)


def _surfaces(
    target: str,
    management: SurfaceManagement,
    names: tuple[str, ...],
) -> tuple[Surface, ...]:
    return tuple(Surface(f"{target}.{name}", management) for name in names)


# This declared consumer-surface inventory distinguishes what the current
# compiler generates, can reconcile only through native patches, or does not manage.
SURFACES = (
    *_surfaces(
        "claude",
        SurfaceManagement.GENERATED,
        ("instructions", "rules", "skills", "commands", "agents", "permissions"),
    ),
    *_surfaces(
        "claude",
        SurfaceManagement.PATCH_ONLY,
        ("settings", "mcp", "sandbox", "hooks", "agent-teams-experimental"),
    ),
    *_surfaces(
        "claude",
        SurfaceManagement.UNMANAGED,
        (
            "memory",
            "plugins",
            "output-styles",
            "keybindings-statusline",
            "providers-auth",
            "managed-policy",
            "sessions-workflows",
            "gui-cloud",
            "mcp-runtime",
            "mcp-oauth",
        ),
    ),
    *_surfaces(
        "codex",
        SurfaceManagement.GENERATED,
        (
            "instructions",
            "exec-policy",
            "rule-fragments",
            "skills",
            "command-skills",
            "agents",
            "skill-registrations",
            "agent-registrations",
        ),
    ),
    *_surfaces(
        "codex",
        SurfaceManagement.PATCH_ONLY,
        (
            "config",
            "mcp",
            "permission-profiles",
            "model-providers",
            "hooks",
            "plugins",
        ),
    ),
    *_surfaces(
        "codex",
        SurfaceManagement.UNMANAGED,
        (
            "profiles",
            "apps-connectors",
            "managed-policy",
            "runtime-sessions",
            "gui-cloud",
            "mcp-runtime",
            "mcp-oauth",
        ),
    ),
    *_surfaces(
        "cursor-agent",
        SurfaceManagement.GENERATED,
        ("global-rule", "rules", "skills", "agents-user", "permissions"),
    ),
    *_surfaces(
        "cursor-agent",
        SurfaceManagement.PATCH_ONLY,
        ("config", "mcp", "sandbox"),
    ),
    *_surfaces(
        "cursor-agent",
        SurfaceManagement.UNMANAGED,
        (
            "agents-project",
            "commands",
            "hooks",
            "plugins",
            "worktrees",
            "private-worker",
            "sessions",
            "modes",
            "mcp-runtime",
            "mcp-oauth",
            "mcp-allowlist",
        ),
    ),
    *_surfaces(
        "cursor-desktop",
        SurfaceManagement.GENERATED,
        ("global-rule", "rules", "skills", "agents", "terminal-permissions"),
    ),
    *_surfaces(
        "cursor-desktop",
        SurfaceManagement.PATCH_ONLY,
        (
            "settings",
            "mcp",
            "mcp-permissions",
            "auto-review",
            "plugins",
        ),
    ),
    *_surfaces(
        "cursor-desktop",
        SurfaceManagement.UNMANAGED,
        ("commands", "custom-modes", "sandbox", "hooks", "extensions"),
    ),
    *_surfaces(
        "cursor-cloud",
        SurfaceManagement.UNMANAGED,
        (
            "environment",
            "instructions",
            "hooks",
            "automations",
            "admin",
            "handoff",
            "computer-use",
            "network-policy",
        ),
    ),
    *_surfaces(
        "opencode",
        SurfaceManagement.GENERATED,
        (
            "global-instructions",
            "instructions-config",
            "skills",
            "commands",
            "agents",
            "permissions",
        ),
    ),
    *_surfaces(
        "opencode",
        SurfaceManagement.PATCH_ONLY,
        (
            "model-selection",
            "small-model-selection",
            "providers",
            "provider-filtering",
            "models",
            "model-variants",
            "agent-config",
            "subagent-depth",
            "command-config",
            "skill-paths",
            "skill-urls",
            "mcp-local",
            "mcp-remote",
            "plugins",
            "references-local",
            "references-git",
            "compaction",
            "attachments",
            "snapshots",
            "watcher",
            "tool-output",
            "formatter-config",
            "lsp-config",
            "experimental",
            "identity",
            "updates",
            "sharing-config",
            "shell",
            "server",
            "agent-selection",
            "tool-enablement",
            "logging",
            "enterprise",
        ),
    ),
    *_surfaces(
        "opencode",
        SurfaceManagement.UNMANAGED,
        (
            "api-catalog",
            "api-sessions",
            "formatter-runtime",
            "lsp-runtime",
            "sharing-runtime",
            "tui-config",
            "tui-runtime",
            "websearch-provider-env",
            "config-discovery",
            "skills-catalog",
            "server-auth",
            "remote-config",
            "managed-config",
            "plugins-runtime",
            "compaction-runtime",
            "enterprise-runtime",
            "updater-runtime",
            "skill-urls-external",
            "watcher-runtime",
            "mcp-local-runtime",
            "mcp-remote-runtime",
            "mcp-oauth",
            "custom-tools",
            "custom-themes",
        ),
    ),
)


def _case_indexes() -> tuple[Mapping[str, Case], Mapping[str, tuple[Case, ...]]]:
    cases: dict[str, Case] = {}
    surfaces: dict[str, list[Case]] = {}
    for case in CASES:
        if case.id in cases:
            raise ValueError(f"duplicate capability ID: {case.id}")
        cases[case.id] = case
        for surface in case.surfaces:
            surfaces.setdefault(surface, []).append(case)
    return MappingProxyType(cases), MappingProxyType(
        {surface: tuple(cases) for surface, cases in surfaces.items()}
    )


CASES_BY_ID, CASES_BY_SURFACE = _case_indexes()


def validate_registry() -> None:
    unknown = {case.target for case in CASES} - TARGETS.keys()
    if unknown:
        raise ValueError(f"unknown targets: {sorted(unknown)}")
    for case in CASES:
        if not (Path(__file__).parent / case.evidence).is_file():
            raise ValueError(f"{case.id}: missing evidence {case.evidence}")
    ids = [surface.id for surface in SURFACES]
    if duplicates := {surface_id for surface_id in ids if ids.count(surface_id) > 1}:
        raise ValueError(f"duplicate capability surfaces: {sorted(duplicates)}")
    unknown_surfaces = {surface for case in CASES for surface in case.surfaces} - set(
        ids
    )
    if unknown_surfaces:
        raise ValueError(
            f"unknown declared target surfaces: {sorted(unknown_surfaces)}"
        )
    if invalid := [
        case.id
        for case in CASES
        if case.expected not in {Support.SUPPORTED, Support.UNSUPPORTED}
    ]:
        raise ValueError(f"non-binary expected support: {sorted(invalid)}")
