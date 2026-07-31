from __future__ import annotations

import functools
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from coding_agents_sync.artifacts import (
    Artifact,
    ManagedRootArtifact,
    NativeConfigArtifact,
    OpenCodeConfigArtifact,
    TextArtifact,
    TreeArtifact,
)
from coding_agents_sync.models import OpenCodeInstruction, SyncContext
from coding_agents_sync.sources import RuleSource, SourceBundle

from .common import (
    command_meta_for,
    opencode_agent_meta,
    render_markdown,
    skill_meta_for,
    skill_tree_artifact_override,
    target_meta,
)
from .permissions import (
    bucket_patterns,
    external_directory_map,
    glob_variants,
    secret_name_variants,
)


def home_relative(path: Path, home: Path) -> str:
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def opencode_instruction_item(rule: RuleSource, *, home: Path) -> OpenCodeInstruction:
    item: OpenCodeInstruction = home_relative(rule.path, home)
    instructions = target_meta(rule.targets, "opencode", "rule").get("instructions")
    return instructions if isinstance(instructions, str) else item


class OpenCodeTranslator:
    def __init__(self, ctx: SyncContext) -> None:
        self.ctx = ctx

    def translate(self, sources: SourceBundle) -> list[Artifact]:
        artifacts: list[Artifact] = []
        artifacts.extend(self._global(sources))
        artifacts.extend(self._config(sources))
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
            TextArtifact(self.ctx.opencode / "AGENTS.md", source.body.rstrip() + "\n")
        ]

    def _config(self, sources: SourceBundle) -> list[Artifact]:
        return [
            OpenCodeConfigArtifact(
                tuple(
                    opencode_instruction_item(rule, home=self.ctx.home)
                    for rule in sources.rules
                ),
            )
        ]

    def _skills(self, sources: SourceBundle) -> list[Artifact]:
        root = self.ctx.opencode / "skills"
        artifacts: list[Artifact] = [
            TreeArtifact(
                skill.source_dir,
                root / skill.source_dir.name,
                skill_tree_artifact_override(
                    skill, meta=skill_meta_for(skill, "opencode")
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

    def _commands(self, sources: SourceBundle) -> list[Artifact]:
        root = self.ctx.opencode / "commands"
        artifacts: list[Artifact] = []
        for command in sources.commands:
            meta = command_meta_for(command, "opencode")
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
        root = self.ctx.opencode / "agents"
        artifacts: list[Artifact] = []
        for agent in sources.agents:
            meta = opencode_agent_meta(agent)
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
            functools.partial(glob_variants, optional_trailing=True),
            lambda wrapper: f"{wrapper} *",
        )
        # `Permission.evaluate` applies the last matching key, so these three
        # blocks in this order are the whole precedence contract on this
        # target: a later block always beats an earlier one.
        bash: dict[str, str] = {"*": "ask"}

        def append(patterns: Iterable[str], decision: str) -> None:
            # `dict.update` keeps a repeated key at its first position, which
            # would leave a stricter decision sitting where a later allow could
            # still win the last match, so re-insert instead of overwriting.
            for pattern in patterns:
                bash.pop(pattern, None)
                bash[pattern] = decision

        append(buckets["allow"], "allow")
        append(buckets["ask"], "ask")
        append(secret_name_variants(permissions.secret_names), "ask")
        append(buckets["deny"], "deny")
        values: list[tuple[tuple[str, ...], Any]] = [(("permission", "bash"), bash)]
        tools = permissions.tools
        # Read, edit, and write are maps so secret paths can deny on top of the
        # portable decision; webfetch and websearch are plain scalars.
        for tool in ("read", "edit", "write"):
            decision = tools.get(tool)
            if decision is None and not permissions.secret_paths:
                continue
            # Secret paths deny whatever the portable decision is, and they
            # still deny when it is silent. An undeclared class gets no `*`
            # entry, so nothing here grants it and OpenCode's own default
            # stands.
            default = {"*": decision} if decision is not None else {}
            values.append(
                (
                    ("permission", tool),
                    {**default, **dict.fromkeys(permissions.secret_paths, "deny")},
                )
            )
        for tool in ("webfetch", "websearch"):
            if (decision := tools.get(tool)) is not None:
                values.append((("permission", tool), decision))
        if permissions.workspace.allow or permissions.workspace.ask:
            values.append(
                (
                    ("permission", "external_directory"),
                    external_directory_map(permissions.workspace),
                )
            )
        return [
            NativeConfigArtifact(
                "opencode",
                tuple(values),
            )
        ]
