from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import AliasChoices, ConfigDict, Field, model_validator

from ..models import SyncContext
from ..patches import (
    Patch,
    PatchError,
    Pointer,
    display_pointer,
    validate_generated_conflicts,
)
from ..plan import Diagnostic, NativePatch, NativeValue, OwnedFile, OwnedTree, Plan
from ..sources import (
    CommandPermission,
    SkillSource,
    SourceBundle,
    StrictModel,
    describe_rule,
)
from .permissions import (
    CURSOR_TOOL_FLAGS,
    CURSOR_TOOL_PATTERNS,
    cursor_shell_variants,
    rule_patterns,
    tool_patterns,
)
from .support import (
    block,
    frontmatter,
    markdown,
    native_patch,
    omissions,
    raw_files,
    strict_native,
    unhandled_target_block,
)


class CursorRuleNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    description: str | None = None
    globs: list[str] = Field(default_factory=list)
    always_apply: bool = Field(
        True,
        serialization_alias="alwaysApply",
        validation_alias=AliasChoices("alwaysApply", "always_apply"),
    )

    @model_validator(mode="after")
    def _validate_scope(self) -> CursorRuleNative:
        if self.always_apply and self.globs:
            raise ValueError("scoped Cursor rules must set always_apply: false")
        return self


class CursorSkillNative(StrictModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    name: str | None = None
    description: str | None = None
    paths: list[str] | None = None
    disable_model_invocation: bool | None = Field(
        None,
        serialization_alias="disable-model-invocation",
        validation_alias=AliasChoices(
            "disable-model-invocation", "disable_model_invocation"
        ),
    )
    metadata: dict[str, Any] | None = None


def _tree(skill: SkillSource, root: Path, meta: dict[str, Any]) -> OwnedTree:
    files = {
        path.relative_to(skill.source_dir): path.read_bytes()
        for path in skill.source_dir.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    files[Path("SKILL.md")] = markdown(meta, skill.body)
    return OwnedTree(
        root / skill.source_dir.name,
        tuple(sorted(files.items(), key=lambda item: item[0].as_posix())),
        root,
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
    if not (permissions := sources.permissions):
        return (), ()
    policy_omit = block(permissions, "cursor").omit
    tools_omitted = "tools" in policy_omit
    secrets_omitted = "secret_paths" in policy_omit
    commands = permissions.commands
    has_rules = bool(commands.allow or commands.ask or commands.deny)
    if not has_rules and tools_omitted and secrets_omitted:
        return (), ()

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

    # An ask that narrows an allow carves a dangerous variant out of the
    # allow's patterns. The CLI deny channel claws the variant back; Desktop
    # has no deny channel, so the allow rules the variant rides leave its
    # allowlist and prompt per invocation, which is ask-equivalent, while
    # the rest of the family stays allowlisted. A narrowing ask the token
    # matcher cannot express (a text predicate) drops the same rules from
    # the CLI allowlist too, rather than letting the variant ride them.
    # An ask that shares an allow's subcommand prefix without provably
    # narrowing it is a guarded overlap: it is clawed back the same way,
    # with a warning, because the overlap would otherwise ride the allow.
    narrowing = tuple(
        rule
        for rule in commands.ask
        if any(rule.narrows(allowed) for allowed in allowed_rules)
    )
    overlap = tuple(
        rule
        for rule in commands.ask
        if rule not in narrowing
        and any(
            _prefixes(rule, allowed, commands.option_tokens(rule.command))
            for allowed in allowed_rules
        )
    )
    diagnostics.extend(
        Diagnostic(
            "warning",
            f"guarded overlap {describe_rule(rule)}: the ask shares an "
            "allow's subcommand prefix but does not provably narrow it",
        )
        for rule in overlap
    )

    def colliding(ask: CommandPermission) -> frozenset[CommandPermission]:
        options = commands.option_tokens(ask.command)
        return frozenset(
            allowed
            for allowed in allowed_rules
            if ask.narrows(allowed)
            or _prefixes(ask, allowed, options)
            or _rides_options(ask, allowed, options)
        )

    clawback: list[str] = []
    unlowered: set[CommandPermission] = set()
    desktop_excluded: set[CommandPermission] = set()
    for rule in (*narrowing, *overlap):
        hit = colliding(rule)
        desktop_excluded |= hit
        if forms := rule_patterns(permissions, cursor_shell_variants, (rule,)):
            clawback.extend(forms)
        else:
            # The variant has no token-matcher form to claw back, so the
            # allow rules it rides cannot keep their CLI wildcards either.
            unlowered |= hit
    allow = rule_patterns(
        permissions,
        cursor_shell_variants,
        tuple(rule for rule in allowed_rules if rule not in unlowered),
    )
    desktop_allow = rule_patterns(
        permissions,
        cursor_shell_variants,
        tuple(rule for rule in allowed_rules if rule not in desktop_excluded),
    )

    cli: list[NativeValue] = [
        NativeValue(
            "cursor-cli", root / "cli-config.json", ("approvalMode",), "allowlist"
        )
    ]
    if allowed := [f"Shell({pattern})" for pattern in allow]:
        if not tools_omitted:
            allowed.extend(
                tool_patterns(permissions.tools, CURSOR_TOOL_PATTERNS, "allow")
            )
        cli.append(
            NativeValue(
                "cursor-cli",
                root / "cli-config.json",
                ("permissions", "allow"),
                allowed,
            )
        )
    if not tools_omitted:
        cli.extend(
            NativeValue(
                "cursor-cli",
                root / "cli-config.json",
                (flag,),
                permissions.tools[tool] == "allow",
            )
            for tool, flag in CURSOR_TOOL_FLAGS.items()
            if tool in permissions.tools
        )
    denied = []
    if not secrets_omitted:
        denied.extend(
            f"{operation}({path})"
            for path in permissions.secret_paths
            for operation in ("Read", "Write")
        )
    deny_forms: list[str] = []
    for rule in commands.deny:
        if forms := rule_patterns(permissions, cursor_shell_variants, (rule,)):
            deny_forms.extend(forms)
        else:
            # Only a text predicate lowers to nothing: exact rules project,
            # and exact+tail/text is a schema error. Degradation is safe
            # (the command prompts), but the drop is not silent.
            diagnostics.append(
                Diagnostic(
                    "warning",
                    f"omitted deny {describe_rule(rule)}: Cursor's token "
                    "matcher cannot hold a text predicate",
                )
            )
    denied.extend(f"Shell({pattern})" for pattern in (*deny_forms, *clawback))
    if not tools_omitted:
        denied.extend(tool_patterns(permissions.tools, CURSOR_TOOL_PATTERNS, "deny"))
    if denied:
        cli.append(
            NativeValue(
                "cursor-cli",
                root / "cli-config.json",
                ("permissions", "deny"),
                list(dict.fromkeys(denied)),
            )
        )
    if not desktop_allow:
        return tuple(cli), tuple(diagnostics)
    return (
        tuple(
            (
                *cli,
                NativeValue(
                    "cursor-desktop",
                    root / "permissions.json",
                    ("approvalMode",),
                    "allowlist",
                ),
                NativeValue(
                    "cursor-desktop",
                    root / "permissions.json",
                    ("terminalAllowlist",),
                    desktop_allow,
                ),
            )
        ),
        tuple(diagnostics),
    )


def _touches(left: Pointer, right: Pointer) -> bool:
    return left[: min(len(left), len(right))] == right[: min(len(left), len(right))]


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
                if _touches(operation.pointer, ("terminalAllowlist",)):
                    raise PatchError(
                        "cursor-desktop patch cannot contribute command permissions at "
                        f"{display_pointer(operation.pointer)}"
                    )


def compile_cursor(ctx: SyncContext, sources: SourceBundle) -> Plan:
    root = ctx.cursor
    rules_root = root / "rules"
    skills_root = root / "skills"
    trees = [
        OwnedTree(
            rules_root, manifest_root=rules_root, manifest_mode="file", declaration=True
        ),
        OwnedTree(
            skills_root,
            manifest_root=skills_root,
            manifest_mode="dir",
            declaration=True,
        ),
        OwnedTree(
            root / "agents",
            manifest_root=root / "agents",
            manifest_mode="file",
            retired=True,
        ),
        OwnedTree(
            root / "plugins" / "local",
            manifest_root=root / "plugins" / "local",
            manifest_mode="dir",
            retired=True,
        ),
    ]
    files: list[OwnedFile] = []
    diagnostics: list[Diagnostic] = []
    if sources.globals:
        global_source = sources.globals[0]
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
        files.append(
            OwnedFile(
                root / "AGENTS.md",
                None,
                retire_if=(global_source.body.rstrip() + "\n").encode(),
            )
        )
    for rule in sources.rules:
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
    for command in sources.commands:
        value = block(command, "cursor")
        diagnostics.extend(unhandled_target_block(command.path, "cursor", value))
        diagnostics.extend(omissions(command.path, "cursor", value, {"command"}))
    for agent in sources.agents:
        value = block(agent, "cursor")
        diagnostics.extend(unhandled_target_block(agent.path, "cursor", value))
        diagnostics.extend(omissions(agent.path, "cursor", value, {"agent"}))
    if permissions := sources.permissions:
        value = block(permissions, "cursor")
        diagnostics.extend(
            unhandled_target_block(permissions.paths[0], "cursor", value)
        )
        required = {
            *({"tools"} if permissions.tools else set()),
            *(
                {"workspace"}
                if permissions.workspace.allow or permissions.workspace.ask
                else set()
            ),
            *({"secret_paths"} if permissions.secret_paths else set()),
            *({"secret_names"} if permissions.secret_names else set()),
        }
        diagnostics.extend(omissions(permissions.paths[0], "cursor", value, required))
        for path, targets, intent in permissions.command_targets:
            value = targets.root.get("cursor", type(value)())
            diagnostics.extend(unhandled_target_block(path, "cursor", value))
            diagnostics.extend(
                omissions(
                    path,
                    "cursor",
                    value,
                    {"commands.deny"} if "deny" in intent else set(),
                )
            )
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
