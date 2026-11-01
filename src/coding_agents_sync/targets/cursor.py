from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from ..models import SyncContext
from ..patches import (
    Patch,
    PatchError,
    display_pointer,
    touches,
    validate_generated_conflicts,
)
from ..plan import Diagnostic, NativePatch, NativeValue, OwnedFile, OwnedTree, Plan
from ..sources import (
    CommandPermission,
    SkillSource,
    SourceBundle,
    StrictModel,
    describe_rule,
    resolve_model_policy,
)
from .permissions import (
    cursor_shell_variants,
    has_directories,
)
from .support import (
    applies_to,
    block,
    bundled_files,
    command_target_diagnostics,
    declared_trees,
    frontmatter,
    markdown,
    native_patch,
    omissions,
    raw_files,
    strict_native,
    unhandled_target_block,
    with_skill_scripts,
)


class CursorRuleNative(StrictModel):
    description: str | None = None
    globs: list[str] = Field(default_factory=list)
    always_apply: bool = Field(True, alias="alwaysApply")

    @model_validator(mode="after")
    def _validate_scope(self) -> CursorRuleNative:
        if self.always_apply and self.globs:
            raise ValueError("scoped Cursor rules must set alwaysApply: false")
        return self


class CursorSkillNative(StrictModel):
    name: str | None = None
    description: str | None = None
    paths: list[str] | None = None
    disable_model_invocation: bool | None = Field(
        None, alias="disable-model-invocation"
    )
    metadata: dict[str, Any] | None = None


class CursorAgentNative(StrictModel):
    model: str | None = None
    readonly: bool | None = None


def _agent_file(meta: dict[str, str | bool], body: str) -> bytes:
    """Desktop reads agent frontmatter line by line as `key: value`, not as
    YAML: a value must sit bare on one line, and quotes would stay literal."""
    lines = (
        f"{key}: {str(value).lower() if isinstance(value, bool) else value}"
        for key, value in meta.items()
    )
    return f"---\n{'\n'.join(lines)}\n---\n\n{body.rstrip()}\n".encode()


def _tree(skill: SkillSource, root: Path, meta: dict[str, Any]) -> OwnedTree:
    files, executables = bundled_files(skill.source_dir)
    files[Path("SKILL.md")] = markdown(meta, skill.body)
    return OwnedTree(
        root / skill.source_dir.name,
        tuple(sorted(files.items(), key=lambda item: item[0].as_posix())),
        root,
        executables=executables,
    )


INTERPRETER_COMMANDS = frozenset({"bash", "eval", "sh", "zsh"})


def _prefixes(
    rule: CommandPermission, allowed: CommandPermission, options: tuple[str, ...]
) -> bool:
    if rule.command != allowed.command or allowed.exact:
        # An exact allow admits nothing beyond its subcommand, so no
        # non-narrowing ask can overlap it.
        return False
    if rule.subcommand[: len(allowed.subcommand)] == allowed.subcommand:
        return True
    # An ask may embed the allow's leading-option vocabulary: with `-C`
    # declared, `git -C foo status` ends with `status` and rides the
    # emitted option-hole head `git:-C * status`.
    return bool(
        allowed.subcommand
        and rule.subcommand
        and rule.subcommand[0] in options
        and rule.subcommand[-len(allowed.subcommand) :] == allowed.subcommand
    )


def _rides_options(
    ask: CommandPermission, allowed: CommandPermission, options: tuple[str, ...]
) -> bool:
    """The allow's option-hole heads can carry the ask's gated variant.

    With `--profile` declared, an allow whose subcommand ends with the ask's
    behind option tokens emits heads like `prog:--profile * status *`, which
    the gated variant `prog --profile prod status --flag` rides even though
    the ask never names the option.
    """
    if allowed.exact or len(allowed.subcommand) <= len(ask.subcommand):
        return False
    lead = allowed.subcommand[: len(allowed.subcommand) - len(ask.subcommand)]
    return allowed.subcommand[len(lead) :] == ask.subcommand and all(
        token in options for token in lead
    )


def _permission_values(
    sources: SourceBundle, root: Path
) -> tuple[tuple[NativeValue, ...], tuple[Diagnostic, ...]]:
    """Lower allows to Desktop's `terminalAllowlist`, which the CLI also reads.

    Cursor's shared allowlist has no deny channel, so every allow an ask or a
    deny narrows or overlaps leaves it, and the guarded command prompts. Wrapped
    spellings (`timeout 30 git status`) are not listed and prompt too.
    """
    if not (permissions := sources.permissions):
        return (), ()
    commands = permissions.commands
    diagnostics: list[Diagnostic] = []
    # An allowlisted interpreter carries its payload as an argument, so
    # Shell(sh) allows everything the interpreter runs. Never project one.
    allowed_rules = tuple(
        rule for rule in commands.allow if rule.command not in INTERPRETER_COMMANDS
    )
    diagnostics.extend(
        Diagnostic(
            "warning",
            f"omitted allow {describe_rule(rule)}: "
            "Cursor cannot confine an interpreter payload",
        )
        for rule in commands.allow
        if rule.command in INTERPRETER_COMMANDS
    )

    def collides(guard: CommandPermission, allowed: CommandPermission) -> bool:
        options = commands.option_tokens(guard.command)
        return (
            guard.narrows(allowed)
            or _prefixes(guard, allowed, options)
            or _rides_options(guard, allowed, options)
        )

    guards = (*commands.ask, *commands.deny)
    # A guard that shares an allow's subcommand prefix without provably
    # narrowing it still withholds the allow, with a warning.
    diagnostics.extend(
        Diagnostic(
            "warning",
            f"guarded overlap {describe_rule(guard)}: the guard shares an "
            "allow's subcommand prefix but does not provably narrow it",
        )
        for guard in guards
        if not any(guard.narrows(allowed) for allowed in allowed_rules)
        and any(
            _prefixes(guard, allowed, commands.option_tokens(guard.command))
            for allowed in allowed_rules
        )
    )
    allowlist = list(
        dict.fromkeys(
            pattern
            for rule in allowed_rules
            if not any(collides(guard, rule) for guard in guards)
            for pattern in cursor_shell_variants(
                rule, commands.option_tokens(rule.command)
            )
        )
    )
    if not allowlist:
        return (), tuple(diagnostics)
    path = root / "permissions.json"
    return (
        NativeValue("cursor-desktop", path, ("approvalMode",), "allowlist"),
        NativeValue("cursor-desktop", path, ("terminalAllowlist",), allowlist),
    ), tuple(diagnostics)


def _validate_patches(
    patches: list[NativePatch], generated: tuple[NativeValue, ...]
) -> None:
    values = {
        target: {
            value.pointer: value.value for value in generated if value.target == target
        }
        for target in {patch.target for patch in patches}
    }
    for patch in patches:
        validate_generated_conflicts(Patch(patch.operations), values[patch.target])
        if patch.target == "cursor-desktop":
            for operation in patch.operations:
                if touches(operation.pointer, ("terminalAllowlist",)):
                    raise PatchError(
                        "cursor-desktop patch cannot contribute command permissions at "
                        f"{display_pointer(operation.pointer)}"
                    )


def compile_cursor(ctx: SyncContext, sources: SourceBundle) -> Plan:
    root = ctx.cursor
    sources = with_skill_scripts(sources, "cursor", root, ctx.home)
    rules_root = root / "rules"
    skills_root = root / "skills"
    agents_root = root / "agents"
    trees = declared_trees(
        root, (("rules", "file"), ("skills", "dir"), ("agents", "file"))
    )
    files: list[OwnedFile] = []
    diagnostics: list[Diagnostic] = []
    for global_source in sources.globals[:1]:
        if not applies_to(global_source, "cursor"):
            continue
        value = block(global_source, "cursor")
        diagnostics.extend(unhandled_target_block(global_source.path, "cursor", value))
        diagnostics.extend(omissions(global_source.path, "cursor", value, set()))
        meta = {"description": global_source.name, "globs": "", "alwaysApply": True}
        files.append(
            OwnedFile(
                rules_root / "coding-agents-global.mdc",
                markdown(meta, global_source.body),
                rules_root,
            )
        )
    for rule in sources.rules:
        if not applies_to(rule, "cursor"):
            continue
        value = block(rule, "cursor")
        native, issues = strict_native(rule.path, "cursor", value, CursorRuleNative)
        diagnostics.extend(issues)
        diagnostics.extend(omissions(rule.path, "cursor", value, set()))
        if native:
            meta = {
                "description": native.description or rule.description.strip(),
                "globs": ",".join(native.globs),
                "alwaysApply": native.always_apply,
            }
            overlap = set(meta) & set(value.raw)
            if overlap:
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "raw target fields shadow canonical or typed fields: "
                        f"{', '.join(sorted(overlap))}",
                        rule.path,
                    )
                )
            else:
                meta.update(value.raw)
                if value.raw:
                    diagnostics.append(
                        Diagnostic(
                            "warning",
                            "using unvalidated raw target fields",
                            rule.path,
                        )
                    )
            files.append(
                OwnedFile(
                    rules_root / f"{rule.stem}.mdc",
                    markdown(meta, rule.body),
                    rules_root,
                )
            )
    for skill in sources.skills:
        if not applies_to(skill, "cursor"):
            continue
        value = block(skill, "cursor")
        native, issues = strict_native(skill.path, "cursor", value, CursorSkillNative)
        diagnostics.extend(issues)
        diagnostics.extend(
            omissions(
                skill.path,
                "cursor",
                value,
                {"license"} if skill.license else set(),
            )
        )
        if native:
            meta, issues = frontmatter(
                skill.path,
                {
                    "name": skill.name,
                    "description": skill.description,
                    **({"paths": skill.paths} if skill.paths else {}),
                    **(
                        {"disable-model-invocation": True}
                        if skill.disable_model_invocation
                        else {}
                    ),
                    **({"metadata": skill.metadata} if skill.metadata else {}),
                },
                native,
                value.raw,
            )
            diagnostics.extend(issues)
            trees.append(_tree(skill, skills_root, meta))
    # Cursor's own migration turns a slash command into a skill the model
    # cannot invoke by itself.
    for command in sources.commands:
        if not applies_to(command, "cursor"):
            continue
        value = block(command, "cursor")
        native, issues = strict_native(command.path, "cursor", value, CursorSkillNative)
        diagnostics.extend(issues)
        delegated = command.execution.agent or command.execution.subtask
        diagnostics.extend(
            omissions(
                command.path, "cursor", value, {"execution"} if delegated else set()
            )
        )
        if native:
            meta, issues = frontmatter(
                command.path,
                {
                    "name": command.stem,
                    "description": command.description,
                    "disable-model-invocation": True,
                },
                native,
                value.raw,
            )
            diagnostics.extend(issues)
            trees.append(
                OwnedTree(
                    skills_root / command.stem,
                    ((Path("SKILL.md"), markdown(meta, command.body)),),
                    skills_root,
                )
            )
    for agent in sources.agents:
        if not applies_to(agent, "cursor"):
            continue
        value = block(agent, "cursor")
        native, issues = strict_native(agent.path, "cursor", value, CursorAgentNative)
        diagnostics.extend(issues)
        diagnostics.extend(
            omissions(
                agent.path,
                "cursor",
                value,
                {
                    *({"effort"} if agent.effort else set()),
                    *({"color"} if agent.color else set()),
                },
            )
        )
        if value.raw:
            diagnostics.append(
                Diagnostic("error", "cursor raw fields are not valid here", agent.path)
            )
        policy = resolve_model_policy(sources, agent, "cursor")
        if policy is not None and native and native.model is not None:
            diagnostics.append(
                Diagnostic(
                    "error", "model policy owns Cursor fields: model", agent.path
                )
            )
        if native:
            model = native.model if policy is None else policy.model
            meta: dict[str, str | bool] = {"name": agent.name}
            if description := " ".join(agent.description.split()):
                meta["description"] = description
            if model:
                meta["model"] = model
            if native.readonly:
                meta["readonly"] = True
            if agent.background:
                meta["background"] = True
            files.append(
                OwnedFile(
                    agents_root / agent.path.name,
                    _agent_file(meta, agent.body),
                    agents_root,
                    structured=False,
                )
            )
    if permissions := sources.permissions:
        value = block(permissions, "cursor")
        diagnostics.extend(
            unhandled_target_block(permissions.paths[0], "cursor", value)
        )
        required = {
            *({"tools"} if permissions.tools else set()),
            *({"workspace"} if has_directories(permissions.workspace) else set()),
            *({"secret_paths"} if permissions.secret_paths else set()),
            *({"secret_names"} if permissions.secret_names else set()),
            *({"unmatched"} if permissions.unmatched != "ask" else set()),
        }
        diagnostics.extend(omissions(permissions.paths[0], "cursor", value, required))
        diagnostics.extend(command_target_diagnostics(permissions, "cursor"))
    native_values, permission_diagnostics = _permission_values(sources, root)
    diagnostics.extend(permission_diagnostics)
    patches = []
    for target, path, name in (
        ("cursor-cli", root / "cli-config.json", "cursor-cli-config"),
        ("cursor-desktop", root / "permissions.json", "cursor-permissions"),
        ("cursor", root / "settings.json", "cursor-settings"),
        ("cursor-mcp", root / "mcp.json", "cursor-mcp"),
    ):
        patch, issues = native_patch(
            config_root=ctx.config_root, target=target, path=path, patch_name=name
        )
        patches.append(patch)
        diagnostics.extend(issues)
    _validate_patches(patches, native_values)
    raw, raw_tree, issues = raw_files(
        config_root=ctx.config_root,
        target="cursor",
        root=root,
        excluded=(
            Path("cli-config.json"),
            Path("permissions.json"),
            Path("settings.json"),
            Path("mcp.json"),
        ),
    )
    return Plan(
        tuple((*files, *raw)),
        tuple((*trees, raw_tree)),
        native_values,
        tuple(patches),
        tuple((*diagnostics, *issues)),
    )
