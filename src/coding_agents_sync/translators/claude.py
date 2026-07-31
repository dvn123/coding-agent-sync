from __future__ import annotations

from coding_agents_sync.artifacts import (
    Artifact,
    ManagedRootArtifact,
    NativeConfigArtifact,
    TextArtifact,
    TreeArtifact,
)
from coding_agents_sync.models import SyncContext
from coding_agents_sync.sources import SourceBundle

from .common import (
    claude_agent_meta,
    command_meta_for,
    render_markdown,
    skill_meta_for,
    skill_tree_artifact_override,
)
from .permissions import (
    CLAUDE_RESOLVED_WRAPPERS,
    CLAUDE_TOOL_PATTERNS,
    bucket_patterns,
    glob_variants,
    literal_directories,
    secret_name_variants,
    tool_patterns,
)


class ClaudeTranslator:
    def __init__(self, ctx: SyncContext) -> None:
        self.ctx = ctx

    def translate(self, sources: SourceBundle) -> list[Artifact]:
        artifacts: list[Artifact] = []
        artifacts.extend(self._global(sources))
        artifacts.extend(self._rules(sources))
        artifacts.extend(self._skills(sources))
        artifacts.extend(self._commands(sources))
        artifacts.extend(self._agents(sources))
        artifacts.extend(self._permissions(sources))
        return artifacts

    def _global(self, sources: SourceBundle) -> list[Artifact]:
        if not sources.globals:
            return []
        source = sources.globals[0]
        return [
            TextArtifact(self.ctx.claude / "CLAUDE.md", source.body.rstrip() + "\n")
        ]

    def _rules(self, sources: SourceBundle) -> list[Artifact]:
        # User-scoped rules with `paths` do not load: claude-code#21858.
        root = self.ctx.claude / "rules"
        artifacts: list[Artifact] = [
            TextArtifact(root / f"{rule.stem}.md", rule.body.rstrip() + "\n")
            for rule in sources.rules
        ]
        artifacts.append(
            ManagedRootArtifact(
                root, {f"{rule.stem}.md" for rule in sources.rules}, "file"
            )
        )
        return artifacts

    def _skills(self, sources: SourceBundle) -> list[Artifact]:
        root = self.ctx.claude / "skills"
        artifacts: list[Artifact] = []
        for skill in sources.skills:
            meta = skill_meta_for(skill, "claude")
            artifacts.append(
                TreeArtifact(
                    skill.source_dir,
                    root / skill.source_dir.name,
                    skill_tree_artifact_override(skill, meta=meta),
                )
            )
        artifacts.append(
            ManagedRootArtifact(
                root, {skill.source_dir.name for skill in sources.skills}, "dir"
            )
        )
        return artifacts

    def _commands(self, sources: SourceBundle) -> list[Artifact]:
        root = self.ctx.claude / "commands"
        artifacts: list[Artifact] = []
        for command in sources.commands:
            meta = command_meta_for(command, "claude")
            artifacts.append(
                TextArtifact(
                    root / command.path.name, render_markdown(meta, command.body)
                )
            )
        artifacts.append(
            ManagedRootArtifact(
                root, {command.path.name for command in sources.commands}, "file"
            )
        )
        return artifacts

    def _agents(self, sources: SourceBundle) -> list[Artifact]:
        root = self.ctx.claude / "agents"
        artifacts: list[Artifact] = []
        for agent in sources.agents:
            meta = claude_agent_meta(agent)
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
        buckets = bucket_patterns(
            permissions,
            glob_variants,
            lambda wrapper: f"{wrapper} *",
            CLAUDE_RESOLVED_WRAPPERS,
        )
        tools = permissions.tools
        ask = buckets["ask"] + list(secret_name_variants(permissions.secret_names))
        deny = [f"Bash({pattern})" for pattern in buckets["deny"]] + [
            f"{operation}({path})"
            for path in permissions.secret_paths
            for operation in ("Read", "Edit")
        ]
        values = [
            (
                ("permissions", "allow"),
                [f"Bash({pattern})" for pattern in buckets["allow"]]
                + list(tool_patterns(tools, CLAUDE_TOOL_PATTERNS, "allow")),
            ),
            (
                ("permissions", "ask"),
                [f"Bash({pattern})" for pattern in ask]
                + list(tool_patterns(tools, CLAUDE_TOOL_PATTERNS, "ask")),
            ),
        ]
        deny += list(tool_patterns(tools, CLAUDE_TOOL_PATTERNS, "deny"))
        if deny:
            values.append((("permissions", "deny"), deny))
        if directories := literal_directories(permissions.workspace):
            values.append((("permissions", "additionalDirectories"), directories))
        return [
            NativeConfigArtifact(
                "claude",
                tuple(values),
            )
        ]
