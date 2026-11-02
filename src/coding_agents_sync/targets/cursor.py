from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, model_validator

from ..ir import Bare, applies_to, resolve
from ..models import SyncContext
from ..output.patches import (
    Patch,
    PatchError,
    display_pointer,
    touches,
    validate_generated_conflicts,
)
from ..output.plan import (
    Diagnostic,
    NativePatch,
    NativeValue,
    OwnedFile,
    OwnedTree,
    Plan,
)
from ..sources import (
    CommandPermission,
    McpServerSource,
    SkillSource,
    SourceBundle,
    StrictModel,
    describe_rule,
    resolve_model_policy,
)
from .permissions import cursor_shell_variants, denied_paths
from .support import (
    collision_error,
    command_target_diagnostics,
    declared_trees,
    frontmatter,
    markdown,
    mcp_servers,
    mcp_tool_decisions,
    native_patch,
    raw_files,
    shared_skill,
    skill_collisions,
    skill_tree,
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
    # Tool names the agent may use; Desktop reads them comma-separated.
    tools: list[Annotated[str, Field(pattern=r"^[^,\n]+$")]] | None = None


def _agent_file(meta: dict[str, str | bool], body: str) -> bytes:
    """Desktop reads agent frontmatter line by line as `key: value`, not as
    YAML: a value must sit bare on one line, and quotes would stay literal."""
    lines = (
        f"{key}: {str(value).lower() if isinstance(value, bool) else value}"
        for key, value in meta.items()
    )
    return f"---\n{'\n'.join(lines)}\n---\n\n{body.rstrip()}\n".encode()


def cursor_skill(
    skill: SkillSource, root: Path
) -> tuple[tuple[OwnedTree, ...], list[Diagnostic]]:
    """The skill's tree below `root`, if its native block validates."""
    resolved, diagnostics = resolve("cursor", [skill], CursorSkillNative)
    if not resolved:
        return (), diagnostics
    meta, issues = frontmatter(
        skill.path,
        {
            "name": skill.name,
            "description": skill.description,
            **({"license": skill.license} if skill.license else {}),
            **({"paths": skill.paths} if skill.paths else {}),
            **(
                {"disable-model-invocation": True}
                if skill.disable_model_invocation
                else {}
            ),
            **({"metadata": skill.metadata} if skill.metadata else {}),
        },
        resolved[0].native,
        resolved[0].raw,
    )
    return (skill_tree(skill, root, meta),), [*diagnostics, *issues]


def _ignore_pattern(path: str) -> str | None:
    """A denied path in `.cursorignore` syntax, relative to the home directory.

    Cursor's agent reads a `.cursorignore` in every ancestor of the workspace,
    so one at home covers each workspace below it, for its file tools and its
    sandboxed shell alike. An absolute path outside home has no such ancestor.
    """
    if path.startswith("**/"):
        return path
    if path.startswith("~/"):
        return "/" + path.removeprefix("~/")
    return None


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


def _terminal_allowlist(
    sources: SourceBundle,
) -> tuple[list[str], tuple[Diagnostic, ...]]:
    """Lower allows to Desktop's `terminalAllowlist`, which the CLI also reads.

    Cursor's shared allowlist has no deny channel, so every allow an ask or a
    deny narrows or overlaps leaves it, and the guarded command prompts. Wrapped
    spellings (`timeout 30 git status`) are not listed and prompt too.
    """
    if not (permissions := sources.permissions):
        return [], ()
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
    return allowlist, tuple(diagnostics)


def _permission_values(
    sources: SourceBundle,
    servers: list[tuple[McpServerSource, dict[str, Any]]],
    path: Path,
) -> tuple[tuple[NativeValue, ...], tuple[Diagnostic, ...]]:
    """Lower allows to Desktop's allowlists. An unlisted command or MCP tool
    asks; Desktop has no deny list."""
    terminal, diagnostics = _terminal_allowlist(sources)
    mcp = [
        f"{server}:{tool}"
        for server, tool, decision in mcp_tool_decisions(servers)
        if decision == "allow"
    ]
    values = [
        NativeValue("cursor-desktop", path, (key,), allowlist)
        for key, allowlist in (("terminalAllowlist", terminal), ("mcpAllowlist", mcp))
        if allowlist
    ]
    if values:
        values.insert(
            0, NativeValue("cursor-desktop", path, ("approvalMode",), "allowlist")
        )
    return tuple(values), diagnostics


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
    globals_, issues = resolve("cursor", sources.globals[:1])
    diagnostics.extend(issues)
    for global_ in globals_:
        meta = {"description": global_.source.name, "globs": "", "alwaysApply": True}
        files.append(
            OwnedFile(
                rules_root / "coding-agents-global.mdc",
                markdown(meta, global_.source.body),
                rules_root,
            )
        )
    rules, issues = resolve("cursor", sources.rules, CursorRuleNative)
    diagnostics.extend(issues)
    for resolved in rules:
        rule, native = resolved.source, resolved.native
        meta, issues = frontmatter(
            rule.path,
            {
                "description": native.description or rule.description.strip(),
                "globs": ",".join(native.globs),
                "alwaysApply": native.always_apply,
            },
            Bare(),
            resolved.raw,
        )
        diagnostics.extend(issues)
        files.append(
            OwnedFile(
                rules_root / f"{rule.stem}.mdc", markdown(meta, rule.body), rules_root
            )
        )
    for skill in sources.skills:
        if applies_to(skill, "cursor") and not shared_skill(skill):
            tree, issues = cursor_skill(skill, skills_root)
            diagnostics.extend(issues)
            trees.extend(tree)
    # Cursor's own migration turns a slash command into a skill the model
    # cannot invoke by itself.
    collisions = skill_collisions(sources, "cursor")
    commands, issues = resolve("cursor", sources.commands, CursorSkillNative)
    diagnostics.extend(issues)
    for resolved in commands:
        command = resolved.source
        if command.stem in collisions:
            diagnostics.append(collision_error(command))
            continue
        meta, issues = frontmatter(
            command.path,
            {
                "name": command.stem,
                "description": command.description,
                "disable-model-invocation": True,
            },
            resolved.native,
            resolved.raw,
        )
        diagnostics.extend(issues)
        trees.append(
            OwnedTree(
                skills_root / command.stem,
                ((Path("SKILL.md"), markdown(meta, command.body)),),
                skills_root,
            )
        )
    agents, issues = resolve("cursor", sources.agents, CursorAgentNative, raw=False)
    diagnostics.extend(issues)
    for resolved in agents:
        agent, native = resolved.source, resolved.native
        policy = resolve_model_policy(sources, agent, "cursor")
        if policy is not None and native.model is not None:
            diagnostics.append(
                Diagnostic(
                    "error", "model policy owns Cursor fields: model", agent.path
                )
            )
        model = native.model if policy is None else policy.model
        meta: dict[str, str | bool] = {"name": agent.name}
        if description := " ".join(agent.description.split()):
            meta["description"] = description
        if model:
            meta["model"] = model
        if native.readonly:
            meta["readonly"] = True
        if native.tools:
            meta["tools"] = ", ".join(native.tools)
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
        _, issues = resolve("cursor", [permissions])
        diagnostics.extend(issues)
        diagnostics.extend(command_target_diagnostics(permissions, "cursor"))
        if ignored := [
            pattern
            for path in denied_paths(permissions)
            if (pattern := _ignore_pattern(path))
        ]:
            lines = ("# Generated by coding-agents sync", *ignored, "")
            files.append(
                OwnedFile(
                    ctx.home / ".cursorignore", "\n".join(lines).encode(), ctx.home
                )
            )
    servers, issues = mcp_servers(sources, "cursor")
    diagnostics.extend(issues)
    native_values, permission_diagnostics = _permission_values(
        sources, servers, root / "permissions.json"
    )
    diagnostics.extend(permission_diagnostics)
    native_values = (
        *native_values,
        *(
            NativeValue(
                "cursor-mcp", root / "mcp.json", ("mcpServers", server.id, key), value
            )
            for server, fields in servers
            for key, value in fields.items()
        ),
    )
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
