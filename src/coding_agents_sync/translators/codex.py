from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import tomlkit

from coding_agents_sync.artifacts import (
    Artifact,
    CodexAgentRegistration,
    CodexConfigArtifact,
    FileTreeArtifact,
    ManagedRootArtifact,
    OptionalTextArtifact,
    TextArtifact,
    TreeArtifact,
)
from coding_agents_sync.codex import (
    codex_command_skill_document,
    codex_command_skill_name,
    codex_skill_meta,
)
from coding_agents_sync.codex_rules_translator import translate_rules
from coding_agents_sync.metadata import markdown_document
from coding_agents_sync.models import SyncContext
from coding_agents_sync.sources import (
    AgentSource,
    SkillSource,
    SourceBundle,
)
from coding_agents_sync.target_config import codex_rule_fragments

from .common import codex_agent_meta, skill_meta_for


def codex_skill_frontmatter(skill: SkillSource) -> dict[str, Any]:
    return codex_skill_meta(skill_meta_for(skill, "codex"))


def codex_skill_override(skill: SkillSource) -> dict[Path, str]:
    return {
        Path("SKILL.md"): markdown_document(codex_skill_frontmatter(skill), skill.body)
    }


def resolve_codex_skill_names(
    sources: SourceBundle,
) -> tuple[list[str], dict[str, str]]:
    """Resolve skill and command-wrapper names against one namespace."""
    skill_names = [skill.source_dir.name for skill in sources.skills]
    current = set(skill_names)
    command_names: dict[str, str] = {}
    for command in sources.commands:
        resolved = codex_command_skill_name(command.stem, current)
        current.add(resolved)
        command_names[command.stem] = resolved
    return skill_names, command_names


def codex_skill_paths(sources: SourceBundle) -> tuple[str, ...]:
    skill_names, command_names = resolve_codex_skill_names(sources)
    names = skill_names + list(command_names.values())
    return tuple(f"~/.codex/skills/{name}" for name in names)


def codex_agent_toml(agent: AgentSource) -> str:
    meta: dict[str, Any] = {
        "name": agent.name,
        "description": agent.description.strip(),
        "developer_instructions": agent.body.rstrip(),
    }
    meta.update(codex_agent_meta(agent))
    doc = tomlkit.document()
    for key, value in meta.items():
        doc.add(key, value)
    return tomlkit.dumps(doc)


class CodexTranslator:
    def __init__(self, ctx: SyncContext) -> None:
        self.ctx = ctx

    def translate(self, sources: SourceBundle) -> list[Artifact]:
        artifacts: list[Artifact] = []
        artifacts.extend(self._global(sources))
        artifacts.extend(self._rules(sources))
        artifacts.extend(self._skills_and_commands(sources))
        artifacts.extend(self._agents(sources))
        artifacts.extend(self._config(sources))
        return artifacts

    def _global(self, sources: SourceBundle) -> list[Artifact]:
        if not sources.globals and not sources.rules:
            return []
        parts = [sources.globals[0].body.rstrip()] if sources.globals else []
        parts.extend(rule.body.rstrip() for rule in sources.rules)
        return [TextArtifact(self.ctx.codex / "AGENTS.md", "\n\n".join(parts) + "\n")]

    def _rules(self, sources: SourceBundle) -> list[Artifact]:
        rule_meta = [
            (rule.path.name, rule.targets.get("codex", {}).get("rules"))
            for rule in sources.rules
        ]
        codex_rules, warnings = translate_rules(rule_meta)
        for warning in warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        # Codex receives no user command permissions. Its exec policy is not a
        # containment boundary: it has no wildcards, and its rules lapse
        # entirely on any script carrying `$VAR` or a substitution.
        parts = [codex_rules.rstrip()] if codex_rules else []
        generated_rules = "\n".join(parts) + "\n" if parts else None
        root = self.ctx.codex / "rules"
        artifacts: list[Artifact] = [
            OptionalTextArtifact(
                root / "coding-agents.rules",
                generated_rules,
            )
        ]
        current = {"coding-agents.rules"} if generated_rules is not None else set()
        for filename, content in codex_rule_fragments(self.ctx.config_root).items():
            artifacts.append(TextArtifact(root / filename, content))
            current.add(filename)
        artifacts.append(ManagedRootArtifact(root, current, "file"))
        return artifacts

    def _skills_and_commands(self, sources: SourceBundle) -> list[Artifact]:
        root = self.ctx.codex / "skills"
        artifacts: list[Artifact] = []
        skill_names, command_names = resolve_codex_skill_names(sources)
        current_dirs: set[str] = set(skill_names)

        for skill in sources.skills:
            name = skill.source_dir.name
            artifacts.append(
                TreeArtifact(skill.source_dir, root / name, codex_skill_override(skill))
            )

        for command in sources.commands:
            skill_name = command_names[command.stem]
            current_dirs.add(skill_name)
            meta: dict[str, Any] = {"description": command.description}
            if command.execution.agent:
                meta["agent"] = command.execution.agent
            if command.execution.subtask:
                meta["subtask"] = True
            artifacts.append(
                FileTreeArtifact(
                    root / skill_name,
                    {
                        Path("SKILL.md"): codex_command_skill_document(
                            command.stem, meta, command.body, skill_name
                        )
                    },
                )
            )

        artifacts.append(ManagedRootArtifact(root, current_dirs, "dir"))
        return artifacts

    def _config(self, sources: SourceBundle) -> list[Artifact]:
        agents = tuple(
            CodexAgentRegistration(
                slug=agent.stem,
                description=agent.description.strip(),
                config_file=f"~/.codex/agents/{agent.stem}.toml",
            )
            for agent in sources.agents
        )
        return [
            CodexConfigArtifact(
                codex_skill_paths(sources),
                agents,
            )
        ]

    def _agents(self, sources: SourceBundle) -> list[Artifact]:
        root = self.ctx.codex / "agents"
        artifacts: list[Artifact] = [
            TextArtifact(root / f"{agent.stem}.toml", codex_agent_toml(agent))
            for agent in sources.agents
        ]
        artifacts.append(
            ManagedRootArtifact(
                root, {f"{agent.stem}.toml" for agent in sources.agents}, "file"
            )
        )
        return artifacts
