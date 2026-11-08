import shlex
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, model_validator

from ..ir import Bare, applies_to, resolve
from ..models import SyncContext
from ..output.patches import (
    PatchError,
    display_pointer,
    touches,
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
    McpServerSource,
    PermissionSource,
    SkillSource,
    SourceBundle,
    StrictModel,
    describe_rule,
    resolve_model_policy,
)
from .claude import shell_entries as claude_shell_entries
from .permissions import (
    ALLOW_WRAPPERS,
    INTERPRETER_COMMANDS,
    UNWRAP_HOOK,
    UNWRAP_HOOK_PATH,
    UNWRAP_TIMEOUT_SECONDS,
    cursor_shell_variants,
    denied_paths,
    unwrap_command,
    unwrap_rules_file,
    whole_program,
)
from .support import (
    collision_error,
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


def _terminal_allowlist(
    sources: SourceBundle,
) -> tuple[list[str], tuple[Diagnostic, ...]]:
    """Lower allows to Desktop's `terminalAllowlist`, which the CLI also reads.

    The allowlist has no deny channel; shell-unwrap's hook carries the asks and
    denies (`_unwrap_hook`), so every allow lands as written. An allowed whole
    program also lands behind `command` and `xargs`; other wrapped spellings
    (`timeout 30 git status`) are not listed and prompt.
    """
    if not (permissions := sources.permissions):
        return [], ()
    commands = permissions.commands
    # An allowlisted interpreter carries its payload as an argument, so
    # Shell(sh) allows everything the interpreter runs. Never project one.
    diagnostics = tuple(
        Diagnostic(
            "warning",
            f"omitted allow {describe_rule(rule)}: "
            "Cursor cannot confine an interpreter payload",
        )
        for rule in commands.allow
        if rule.command in INTERPRETER_COMMANDS
    )
    allowed = [
        rule for rule in commands.allow if rule.command not in INTERPRETER_COMMANDS
    ]
    allowlist = list(
        dict.fromkeys(
            (
                *(
                    pattern
                    for rule in allowed
                    for pattern in cursor_shell_variants(
                        rule, commands.option_tokens(rule.command)
                    )
                ),
                *(
                    f"{wrapper} {rule.command}"
                    for rule in allowed
                    if whole_program(rule)
                    for wrapper in ALLOW_WRAPPERS
                ),
            )
        )
    )
    return allowlist, diagnostics


# Cursor's allowlists cannot ask or deny. shell-unwrap's hook bundle, a
# `beforeShellExecution` hook in the user `hooks.json` that Desktop and the CLI
# both load, denies or asks for any command inside a call that a guard
# matches, peeled as on Claude, and leaves the rest to the allowlist. It reads
# Claude's `Bash(...)` patterns and asks when it cannot run, so the guards
# fail closed. Desktop can refuse user-local plugins, so it is not one.
# Desktop starts from the Dock without the login shell's PATH.
BUN_PATH = (
    "$HOME/.bun/bin:$HOME/.local/share/mise/shims:/opt/homebrew/bin:/usr/local/bin"
)
UNWRAP_UNAVAILABLE = {
    "permission": "ask",
    "user_message": "shell-unwrap could not run, so its guards cannot be checked",
}


def _unwrap_hook(
    root: Path, home: Path, permissions: PermissionSource
) -> tuple[list[OwnedFile], list[NativeValue]]:
    """The hook and its rules, or nothing when no command is asked or denied."""
    script = root / UNWRAP_HOOK_PATH
    rules = unwrap_rules_file(
        script.with_suffix(".json"),
        root,
        permissions,
        claude_shell_entries,
        asks=True,
    )
    if rules is None:
        return [], []
    directory = script.parent
    located = (
        f'"$HOME"/{shlex.quote(directory.relative_to(home).as_posix())}'
        if directory.is_relative_to(home)
        else shlex.quote(directory.as_posix())
    )
    path = f'PATH="{BUN_PATH}:$PATH" '
    body = unwrap_command(located, "cursor", UNWRAP_UNAVAILABLE, env=path)
    hook = {
        "command": f"/bin/sh -c {shlex.quote(body)}",
        "timeout": UNWRAP_TIMEOUT_SECONDS,
        "failClosed": True,
    }
    hooks = root / "hooks.json"
    return [
        OwnedFile(script, UNWRAP_HOOK.read_bytes(), root, structured=False),
        rules,
    ], [
        NativeValue("cursor-hooks", hooks, ("version",), 1),
        NativeValue("cursor-hooks", hooks, ("hooks", "beforeShellExecution"), [hook]),
    ]


def _permission_values(
    sources: SourceBundle,
    servers: list[tuple[McpServerSource, dict[str, Any]]],
    path: Path,
) -> tuple[tuple[NativeValue, ...], tuple[Diagnostic, ...]]:
    """Lower allows to Desktop's allowlists. An unlisted command or MCP tool
    asks; Desktop has no deny list, so the hook plugin carries shell guards."""
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


def _validate_patches(patches: list[NativePatch]) -> None:
    for patch in patches:
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
        meta = {
            "description": global_.source.description,
            "globs": "",
            "alwaysApply": True,
        }
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
        if native.readonly or "edit" in agent.deny:
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
    hook_values: list[NativeValue] = []
    if permissions := sources.permissions:
        _, issues = resolve("cursor", [permissions])
        diagnostics.extend(issues)
        hook_files, hook_values = _unwrap_hook(root, ctx.home, permissions)
        files.extend(hook_files)
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
    # The CLI runs a command off the allowlist in its sandbox without asking;
    # Desktop keeps that setting in its own storage, out of reach.
    sandbox = (
        (
            NativeValue(
                "cursor-cli", root / "cli-config.json", ("sandbox", "mode"), "enabled"
            ),
        )
        if sources.permissions and sources.permissions.unmatched == "sandbox"
        else ()
    )
    native_values = (
        *native_values,
        *sandbox,
        *hook_values,
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
        ("cursor-hooks", root / "hooks.json", "cursor-hooks"),
    ):
        patch, issues = native_patch(
            config_root=ctx.config_root, target=target, path=path, patch_name=name
        )
        patches.append(patch)
        diagnostics.extend(issues)
    _validate_patches(patches)
    raw, raw_tree, issues = raw_files(
        config_root=ctx.config_root,
        target="cursor",
        root=root,
        excluded=(
            Path("cli-config.json"),
            Path("permissions.json"),
            Path("settings.json"),
            Path("mcp.json"),
            Path("hooks.json"),
        ),
    )
    return Plan(
        tuple((*files, *raw)),
        tuple((*trees, raw_tree)),
        native_values,
        tuple(patches),
        tuple((*diagnostics, *issues)),
    )
