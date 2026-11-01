from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import tomlkit
import yaml
from pydantic import ConfigDict, Field

from ..models import SyncContext
from ..patches import Patch, validate_generated_conflicts
from ..plan import (
    Diagnostic,
    NativePatch,
    NativeValue,
    OwnedFile,
    OwnedTree,
    Plan,
)
from ..sources import (
    AgentSource,
    PermissionSource,
    SkillSource,
    SourceBundle,
    StrictModel,
    describe_rule,
    resolve_model_policy,
)
from .permissions import literal_directories
from .support import (
    applies_to,
    block,
    bundled_files,
    command_target_diagnostics,
    declared_trees,
    frontmatter,
    native_patch,
    omissions,
    raw_files,
    strict_native,
    unhandled_target_block,
)


class CodexPrefixRule(StrictModel):
    pattern: list[str] = Field(min_length=1)
    decision: Literal["allow", "prompt", "forbidden"] = "allow"
    justification: str | None = None


class CodexRuleNative(StrictModel):
    rules: list[CodexPrefixRule] = Field(default_factory=list)


class CodexSkillInterface(StrictModel):
    display_name: str | None = None
    short_description: str | None = None
    icon_small: str | None = None
    icon_large: str | None = None
    brand_color: str | None = None
    default_prompt: str | None = None


class CodexSkillPolicy(StrictModel):
    allow_implicit_invocation: bool | None = None


class CodexSkillTool(StrictModel):
    type: Literal["mcp"] = "mcp"
    value: str = Field(min_length=1)
    description: str | None = None
    transport: Literal["stdio", "streamable_http"] | None = None
    url: str | None = None


class CodexSkillDependencies(StrictModel):
    tools: list[CodexSkillTool] = Field(default_factory=list)


class CodexSkillMetadata(StrictModel):
    interface: CodexSkillInterface | None = None
    policy: CodexSkillPolicy | None = None
    dependencies: CodexSkillDependencies | None = None


class CodexSkillNative(StrictModel):
    name: str | None = None
    description: str | None = None
    license: str | None = None
    metadata: dict[str, Any] | None = None
    allowed_tools: list[str] | None = Field(None, alias="allowed-tools")
    openai: CodexSkillMetadata | None = None


# SandboxMode in codex-rs/protocol/src/config_types.rs. `external-sandbox`
# is excluded: that enum is SandboxModeRequirement, which config.toml rejects.


class CodexAgentFeatures(StrictModel):
    shell_tool: Literal[False] | None = None
    apps: Literal[False] | None = None
    plugins: Literal[False] | None = None
    memories: Literal[False] | None = None
    request_permissions_tool: Literal[False] | None = None


class CodexAgentSkill(StrictModel):
    path: str = Field(min_length=1)
    enabled: Literal[False] = False


class CodexBundledSkills(StrictModel):
    enabled: Literal[False] = False


class CodexAgentSkills(StrictModel):
    config: list[CodexAgentSkill] = Field(default_factory=list)
    bundled: CodexBundledSkills | None = None
    include_instructions: Literal[False] | None = None


class CodexAgentNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    description: str | None = None
    developer_instructions: str | None = None
    model: str | None = None
    # Not validated: the ReasoningEffort enum ends in Custom(String) for
    # "a model-defined effort value that this client does not know yet", so
    # the accepted set is deliberately open.
    model_reasoning_effort: str | None = None
    model_reasoning_summary: Literal["auto", "concise", "detailed", "none"] | None = (
        None
    )
    model_verbosity: Literal["low", "medium", "high"] | None = None
    features: CodexAgentFeatures | None = None
    skills: CodexAgentSkills | None = None
    nickname_candidates: list[str] | None = None


def _tree(
    skill: SkillSource,
    root: Path,
    meta: dict[str, Any],
    openai: CodexSkillMetadata | None,
) -> OwnedTree:
    files, executables = bundled_files(skill.source_dir)
    if skill.disable_model_invocation or openai is not None:
        metadata_path = Path("agents/openai.yaml")
        metadata = yaml.safe_load(files.get(metadata_path, b"{}"))
        if not isinstance(metadata, dict):
            raise ValueError("agents/openai.yaml must contain a mapping")
        for section, fields in (
            openai.model_dump(exclude_none=True).items() if openai else ()
        ):
            existing = metadata.setdefault(section, {})
            if not isinstance(existing, dict):
                raise ValueError(f"agents/openai.yaml {section} must contain a mapping")
            existing.update(fields)
        if skill.disable_model_invocation:
            policy = metadata.setdefault("policy", {})
            if not isinstance(policy, dict):
                raise ValueError("agents/openai.yaml policy must contain a mapping")
            policy["allow_implicit_invocation"] = False
        files[metadata_path] = yaml.safe_dump(
            metadata, sort_keys=False, allow_unicode=True
        ).encode()

    header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).rstrip()
    files[Path("SKILL.md")] = f"---\n{header}\n---\n\n{skill.body.rstrip()}\n".encode()
    return OwnedTree(
        root / skill.source_dir.name,
        tuple(sorted(files.items())),
        root,
        executables=executables,
    )


def _rule_text(rules: list[tuple[str, CodexPrefixRule]]) -> bytes | None:
    if not rules:
        return None
    lines = ["# Generated by coding-agents sync"]

    def quoted(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    for _, rule in sorted(rules, key=lambda item: (item[0], item[1].pattern)):
        pattern = "[" + ", ".join(quoted(item) for item in rule.pattern) + "]"
        args = [f"pattern={pattern}", f"decision={quoted(rule.decision)}"]
        if rule.justification:
            args.append(f"justification={quoted(rule.justification)}")
        lines.append(f"prefix_rule({', '.join(args)})")
    return ("\n".join(lines) + "\n").encode()


def _deny_rules(
    permissions: PermissionSource | None,
) -> tuple[list[tuple[str, CodexPrefixRule]], list[Diagnostic]]:
    """Lower the deny bucket to `forbidden` prefix rules.

    Codex matches one flat argv by literal prefix, with no wildcard, so a rule
    projects as its head and each tail appended directly, bare and behind each
    declared wrapper. The option-hole and tail-after-arguments spellings fall to
    Codex's own approval flow. Text and exact predicates have no prefix form.
    A rule's `reason` becomes the `justification` Codex shows on rejection.
    Allow and ask never project: an allow would skip Codex's sandbox approval
    and an ask would add prompts the sandbox does not need.
    """
    if permissions is None:
        return [], []
    rules: list[tuple[str, CodexPrefixRule]] = []
    diagnostics: list[Diagnostic] = []
    for rule in permissions.commands.deny:
        if rule.text or rule.exact:
            predicate = "a text predicate" if rule.text else "an exact match"
            diagnostics.append(
                Diagnostic(
                    "warning",
                    f"omitted deny {describe_rule(rule)}: Codex prefix rules "
                    f"cannot hold {predicate}",
                    permissions.paths[0],
                )
            )
            continue
        head = (rule.command, *rule.subcommand)
        patterns = [(*head, *sequence) for sequence in sorted(rule.tail)] or [head]
        rules.extend(
            (
                "permissions",
                CodexPrefixRule(
                    pattern=[*lead, *pattern],
                    decision="forbidden",
                    justification=rule.reason,
                ),
            )
            for pattern in patterns
            for lead in ((), *((wrapper,) for wrapper in permissions.wrappers))
        )
    return rules, diagnostics


def _agent_toml(
    agent: AgentSource, native: CodexAgentNative, raw: dict[str, Any]
) -> bytes:
    values: dict[str, Any] = {
        "name": agent.name,
        "description": agent.description.strip(),
        "developer_instructions": agent.body.rstrip(),
    }
    if agent.effort:
        values["model_reasoning_effort"] = agent.effort
    values.update(native.model_dump(exclude_none=True))
    overlap = set(raw) & set(values)
    if overlap:
        raise ValueError(
            "raw target fields shadow canonical or typed fields: "
            f"{', '.join(sorted(overlap))}"
        )
    values.update(raw)
    document = tomlkit.document()
    for key, value in values.items():
        document.add(key, value)
    return tomlkit.dumps(document).encode()


def _validate_patches(patch: NativePatch, generated: list[NativeValue]) -> None:
    validate_generated_conflicts(
        Patch(patch.operations),
        {value.pointer: value.value for value in generated},
    )


def compile_codex(ctx: SyncContext, sources: SourceBundle) -> Plan:
    root = ctx.codex
    files: list[OwnedFile] = []
    trees = declared_trees(
        root, (("rules", "file"), ("skills", "dir"), ("agents", "file"))
    )
    diagnostics: list[Diagnostic] = []
    globals_ = [item for item in sources.globals if applies_to(item, "codex")]
    rules_ = [item for item in sources.rules if applies_to(item, "codex")]
    if globals_ or rules_:
        if globals_:
            global_source = globals_[0]
            value = block(global_source, "codex")
            diagnostics.extend(
                unhandled_target_block(global_source.path, "codex", value)
            )
            diagnostics.extend(omissions(global_source.path, "codex", value, set()))
        parts = [globals_[0].body.rstrip()] if globals_ else []
        parts.extend(rule.body.rstrip() for rule in rules_)
        files.append(
            OwnedFile(root / "AGENTS.md", ("\n\n".join(parts) + "\n").encode(), root)
        )
    rules: list[tuple[str, CodexPrefixRule]] = []
    for rule in rules_:
        value = block(rule, "codex")
        native, issues = strict_native(rule.path, "codex", value, CodexRuleNative)
        diagnostics.extend(issues)
        if value.raw:
            diagnostics.append(
                Diagnostic("error", "codex raw rule fields are not valid", rule.path)
            )
        diagnostics.extend(omissions(rule.path, "codex", value, set()))
        if native:
            rules.extend((rule.path.name, item) for item in native.rules)
    deny_rules, issues = _deny_rules(sources.permissions)
    rules.extend(deny_rules)
    diagnostics.extend(issues)
    if content := _rule_text(rules):
        files.append(
            OwnedFile(root / "rules" / "coding-agents.rules", content, root / "rules")
        )
    for skill in sources.skills:
        if not applies_to(skill, "codex"):
            continue
        value = block(skill, "codex")
        native, issues = strict_native(skill.path, "codex", value, CodexSkillNative)
        diagnostics.extend(issues)
        diagnostics.extend(
            omissions(
                skill.path,
                "codex",
                value,
                {
                    *({"paths"} if skill.paths else set()),
                },
            )
        )
        if native:
            meta, issues = frontmatter(
                skill.path,
                {
                    "name": skill.name,
                    "description": skill.description.replace("<", "").replace(">", ""),
                    **({"license": skill.license} if skill.license else {}),
                    **({"metadata": skill.metadata} if skill.metadata else {}),
                },
                native.model_copy(update={"openai": None}),
                value.raw,
            )
            diagnostics.extend(issues)
            try:
                trees.append(_tree(skill, root / "skills", meta, native.openai))
            except (ValueError, yaml.YAMLError) as exc:
                diagnostics.append(Diagnostic("error", str(exc), skill.path))
    for command in sources.commands:
        if not applies_to(command, "codex"):
            continue
        value = block(command, "codex")
        diagnostics.extend(unhandled_target_block(command.path, "codex", value))
        diagnostics.extend(omissions(command.path, "codex", value, {"command"}))
    for agent in sources.agents:
        if not applies_to(agent, "codex"):
            continue
        value = block(agent, "codex")
        native, issues = strict_native(agent.path, "codex", value, CodexAgentNative)
        diagnostics.extend(issues)
        diagnostics.extend(
            omissions(
                agent.path,
                "codex",
                value,
                {
                    *({"background"} if agent.background else set()),
                    *({"color"} if agent.color else set()),
                },
            )
        )
        policy = resolve_model_policy(sources, agent, "codex")
        if policy is not None:
            conflicts = {"model", "model_reasoning_effort"} & (
                value.native.keys() | value.raw.keys()
            )
            if agent.effort:
                conflicts.add("effort")
            if conflicts:
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "model policy owns Codex fields: "
                        + ", ".join(sorted(conflicts)),
                        agent.path,
                    )
                )
            if native:
                native = native.model_copy(
                    update={
                        "model": policy.model,
                        "model_reasoning_effort": policy.effort,
                    }
                )
        if native:
            try:
                content = _agent_toml(agent, native, value.raw)
            except ValueError as exc:
                diagnostics.append(Diagnostic("error", str(exc), agent.path))
            else:
                files.append(
                    OwnedFile(
                        root / "agents" / f"{agent.stem}.toml", content, root / "agents"
                    )
                )
                if value.raw:
                    diagnostics.append(
                        Diagnostic(
                            "warning",
                            "using unvalidated raw target fields",
                            agent.path,
                        )
                    )
    if permissions := sources.permissions:
        value = block(permissions, "codex")
        diagnostics.extend(unhandled_target_block(permissions.paths[0], "codex", value))
        diagnostics.extend(
            omissions(
                permissions.paths[0],
                "codex",
                value,
                {
                    *({"tools"} if permissions.tools else set()),
                    *({"workspace.ask"} if permissions.workspace.ask else set()),
                    *({"workspace.deny"} if permissions.workspace.deny else set()),
                    *(
                        {"workspace.unmatched"}
                        if permissions.workspace.unmatched != "ask"
                        else set()
                    ),
                    *({"secret_paths"} if permissions.secret_paths else set()),
                    *({"secret_names"} if permissions.secret_names else set()),
                    *({"unmatched"} if permissions.unmatched != "ask" else set()),
                },
            )
        )
        diagnostics.extend(
            command_target_diagnostics(
                permissions,
                "codex",
                lambda intent: {
                    f"commands.{bucket}" for bucket in intent & {"allow", "ask"}
                },
            )
        )
    # Codex discovers skills/ and agents/*.toml itself, so config.toml carries
    # no registration for them.
    native_values: list[NativeValue] = []
    # Codex takes absolute roots, and applies them in `workspace-write` mode.
    if sources.permissions and (
        directories := literal_directories(sources.permissions.workspace)
    ):
        native_values.append(
            NativeValue(
                "codex",
                root / "config.toml",
                ("sandbox_workspace_write", "writable_roots"),
                [
                    str(ctx.home / d.removeprefix("~/")) if d.startswith("~/") else d
                    for d in directories
                ],
            )
        )
    patch, issues = native_patch(
        config_root=ctx.config_root,
        target="codex",
        path=root / "config.toml",
        patch_name="codex",
    )
    diagnostics.extend(issues)
    _validate_patches(patch, native_values)
    raw, raw_tree, raw_issues = raw_files(
        config_root=ctx.config_root,
        target="codex",
        root=root,
        excluded=(Path("config.toml"), Path("rules/coding-agents.rules")),
    )
    return Plan(
        tuple((*files, *raw)),
        tuple((*trees, raw_tree)),
        tuple(native_values),
        (patch,),
        tuple((*diagnostics, *raw_issues)),
    )
