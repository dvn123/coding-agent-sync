from __future__ import annotations

from typing import Any

from coding_agents_sync.artifacts import (
    Artifact,
    ManagedRootArtifact,
    NativeConfigArtifact,
    RetiredRootArtifact,
    RetiredTextArtifact,
    TextArtifact,
    TreeArtifact,
)
from coding_agents_sync.models import SyncContext
from coding_agents_sync.sources import GlobalSource, RuleSource, SourceBundle

from .common import (
    cursor_agent_meta,
    render_markdown,
    skill_meta_for,
    skill_tree_artifact_override,
)
from .permissions import (
    CURSOR_TOOL_FLAGS,
    CURSOR_TOOL_PATTERNS,
    bucket_patterns,
    cursor_shell_variants,
    tool_patterns,
)

GLOBAL_RULE = "coding-agents-global.mdc"


def cursor_rule_document(rule: RuleSource) -> str:
    always = rule.activation.always is not False
    return render_markdown(
        {
            "description": rule.description.strip(),
            "globs": "" if always else ",".join(rule.activation.globs),
            "alwaysApply": always,
        },
        rule.body,
    )


def cursor_global_document(source: GlobalSource) -> str:
    return render_markdown(
        {
            "description": source.name,
            "globs": "",
            "alwaysApply": True,
        },
        source.body,
    )


class CursorTranslator:
    def __init__(self, ctx: SyncContext) -> None:
        self.ctx = ctx

    def translate(self, sources: SourceBundle) -> list[Artifact]:
        artifacts: list[Artifact] = [
            RetiredRootArtifact(self.ctx.cursor, "file"),
        ]
        if sources.globals:
            artifacts.append(
                RetiredTextArtifact(
                    self.ctx.cursor / "AGENTS.md",
                    sources.globals[0].body.rstrip() + "\n",
                )
            )
        artifacts.extend(self._rules(sources))
        artifacts.extend(self._skills(sources))
        artifacts.extend(self._agents(sources))
        artifacts.extend(self._permissions(sources))
        return artifacts

    def _rules(self, sources: SourceBundle) -> list[Artifact]:
        return [
            *self._cli_rules(sources),
            RetiredRootArtifact(self.ctx.cursor / "plugins" / "local", "dir"),
        ]

    def _cli_rules(self, sources: SourceBundle) -> list[Artifact]:
        root = self.ctx.cursor / "rules"
        global_artifacts = (
            [
                TextArtifact(
                    root / GLOBAL_RULE, cursor_global_document(sources.globals[0])
                )
            ]
            if sources.globals
            else []
        )
        managed = {f"{rule.stem}.mdc" for rule in sources.rules}
        if sources.globals:
            managed.add(GLOBAL_RULE)
        return [
            *global_artifacts,
            *(
                TextArtifact(root / f"{rule.stem}.mdc", cursor_rule_document(rule))
                for rule in sources.rules
            ),
            ManagedRootArtifact(root, managed, "file"),
        ]

    def _skills(self, sources: SourceBundle) -> list[Artifact]:
        root = self.ctx.cursor / "skills"
        artifacts: list[Artifact] = [
            TreeArtifact(
                skill.source_dir,
                root / skill.source_dir.name,
                skill_tree_artifact_override(
                    skill, meta=skill_meta_for(skill, "cursor")
                ),
            )
            for skill in sources.skills
        ]
        artifacts.append(
            ManagedRootArtifact(
                root, {skill.source_dir.name for skill in sources.skills}, "dir"
            )
        )
        return artifacts

    def _agents(self, sources: SourceBundle) -> list[Artifact]:
        root = self.ctx.cursor / "agents"
        artifacts: list[Artifact] = []
        for agent in sources.agents:
            meta = cursor_agent_meta(agent)
            artifacts.append(
                TextArtifact(root / agent.path.name, render_markdown(meta, agent.body))
            )
        artifacts.append(
            ManagedRootArtifact(
                root, {agent.path.name for agent in sources.agents}, "file"
            )
        )
        return artifacts

    def _permissions(self, sources: SourceBundle) -> list[Artifact]:
        if not (permissions := sources.permissions):
            return []
        # Cursor has no ask channel. A canonical ask lowers to absence from the
        # allowlist, which is the prompt both surfaces already fall back to; it
        # must never become a deny, which holds even under `--force` and would
        # make the command unrunnable rather than approvable.
        shell = bucket_patterns(permissions, cursor_shell_variants, lambda w: w)
        cli_deny = (
            [
                f"{operation}({path})"
                for path in permissions.secret_paths
                for operation in ("Read", "Write")
            ]
            + [f"Shell({pattern})" for pattern in shell["deny"]]
            + list(tool_patterns(permissions.tools, CURSOR_TOOL_PATTERNS, "deny"))
        )
        values: list[tuple[tuple[str, ...], Any]] = [
            (("approvalMode",), "allowlist"),
            (
                ("permissions", "allow"),
                [f"Shell({pattern})" for pattern in shell["allow"]]
                + list(tool_patterns(permissions.tools, CURSOR_TOOL_PATTERNS, "allow")),
            ),
        ]
        # A few Cursor Agent tool classes are booleans rather than allowlist
        # entries, so they are set rather than appended.
        values.extend(
            ((flag,), permissions.tools[tool] == "allow")
            for tool, flag in CURSOR_TOOL_FLAGS.items()
            if tool in permissions.tools
        )
        if cli_deny:
            values.append((("permissions", "deny"), list(dict.fromkeys(cli_deny))))
        return [
            NativeConfigArtifact(
                "cursor-cli",
                tuple(values),
            ),
            NativeConfigArtifact(
                "cursor-desktop",
                (
                    (("approvalMode",), "allowlist"),
                    # Desktop's shipped schema has no deny key, so a deny is
                    # conveyed only by absence, exactly as an ask is.
                    (("terminalAllowlist",), list(shell["allow"])),
                ),
            ),
        ]
