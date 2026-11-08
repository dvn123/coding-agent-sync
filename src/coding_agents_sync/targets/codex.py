import json
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

import tomlkit
import yaml
from pydantic import ConfigDict, Field

from ..ir import applies_to, resolve
from ..models import SyncContext
from ..output.plan import (
    Diagnostic,
    NativeValue,
    OwnedFile,
    OwnedTree,
    Plan,
)
from ..sources import (
    AgentSource,
    McpServerSource,
    PermissionSource,
    SkillSource,
    SourceBundle,
    StrictModel,
    resolve_model_policy,
)
from .claude import shell_entries as claude_shell_entries
from .permissions import (
    UNWRAP_HOOK,
    UNWRAP_TIMEOUT_SECONDS,
    literal_directories,
    unwrap_rules_file,
)
from .support import (
    bundled_files,
    collision_error,
    declared_trees,
    frontmatter,
    mcp_servers,
    mcp_tool_decisions,
    native_patch,
    raw_files,
    shared_skill,
    skill_collisions,
)


class CodexPrefixRule(StrictModel):
    # A list token is a set of alternatives for that position.
    pattern: list[str | Annotated[list[str], Field(min_length=1)]] = Field(min_length=1)
    decision: Literal["allow", "prompt", "forbidden"] = "allow"
    justification: str | None = None


class CodexRuleNative(StrictModel):
    rules: list[CodexPrefixRule] = Field(default_factory=list)


class CodexPermissionNative(StrictModel):
    # BUILT_IN_PERMISSION_PROFILE_* in codex-rs/protocol/src/models.rs. Unset
    # selects the compiled profile, which stays defined either way.
    default_permissions: (
        Literal[":read-only", ":workspace", ":danger-full-access"] | None
    ) = None


class CodexSkillInterface(StrictModel):
    display_name: str | None = None
    short_description: str | None = None
    icon_small: str | None = None
    icon_large: str | None = None
    brand_color: str | None = None
    default_prompt: str | None = None


class CodexSkillPolicy(StrictModel):
    allow_implicit_invocation: bool | None = None
    products: list[Literal["chatgpt", "codex", "atlas"]] | None = None


class CodexSkillTool(StrictModel):
    type: Literal["mcp"] = "mcp"
    value: str = Field(min_length=1)
    description: str | None = None
    transport: Literal["stdio", "streamable_http"] | None = None
    command: str | None = None
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
    personality: Literal["none", "friendly", "pragmatic"] | None = None
    # A model catalog tier id, `fast` and `flex` among them; the set is open.
    service_tier: str | None = None
    features: CodexAgentFeatures | None = None
    skills: CodexAgentSkills | None = None
    nickname_candidates: list[str] | None = None


def codex_skill(
    skill: SkillSource, root: Path
) -> tuple[tuple[OwnedTree, ...], list[Diagnostic]]:
    """The skill's tree below `root`, if its native block validates."""
    resolved, diagnostics = resolve("codex", [skill], CodexSkillNative)
    if not resolved:
        return (), diagnostics
    native = resolved[0].native
    meta, issues = frontmatter(
        skill.path,
        {
            "name": skill.name,
            "description": skill.description,
            **({"license": skill.license} if skill.license else {}),
            **({"metadata": skill.metadata} if skill.metadata else {}),
        },
        native.model_copy(update={"openai": None}),
        resolved[0].raw,
    )
    diagnostics.extend(issues)
    try:
        tree = _tree(
            skill.source_dir.name,
            skill.body,
            root,
            meta,
            native.openai,
            explicit_only=skill.disable_model_invocation,
            source_dir=skill.source_dir,
        )
    except (ValueError, yaml.YAMLError) as exc:
        return (), [*diagnostics, Diagnostic("error", str(exc), skill.path)]
    return (tree,), diagnostics


def _tree(
    directory: str,
    body: str,
    root: Path,
    meta: dict[str, Any],
    openai: CodexSkillMetadata | None,
    *,
    explicit_only: bool,
    source_dir: Path | None = None,
) -> OwnedTree:
    files, executables = bundled_files(source_dir) if source_dir else ({}, frozenset())
    if explicit_only or openai is not None:
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
        if explicit_only:
            policy = metadata.setdefault("policy", {})
            if not isinstance(policy, dict):
                raise ValueError("agents/openai.yaml policy must contain a mapping")
            policy["allow_implicit_invocation"] = False
        files[metadata_path] = yaml.safe_dump(
            metadata, sort_keys=False, allow_unicode=True
        ).encode()

    header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).rstrip()
    files[Path("SKILL.md")] = f"---\n{header}\n---\n\n{body.rstrip()}\n".encode()
    return OwnedTree(
        root / directory,
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

    def listed(values: list[str]) -> str:
        return "[" + ", ".join(quoted(value) for value in values) + "]"

    def token(value: str | list[str]) -> str:
        return quoted(value) if isinstance(value, str) else listed(value)

    def order(item: tuple[str, CodexPrefixRule]) -> tuple[Any, ...]:
        source, rule = item
        tokens = [
            (value,) if isinstance(value, str) else tuple(value)
            for value in rule.pattern
        ]
        return (source, tokens)

    for _, rule in sorted(rules, key=order):
        pattern = "[" + ", ".join(token(item) for item in rule.pattern) + "]"
        args = [f"pattern={pattern}", f"decision={quoted(rule.decision)}"]
        if rule.justification:
            args.append(f"justification={quoted(rule.justification)}")
        lines.append(f"prefix_rule({', '.join(args)})")
    return ("\n".join(lines) + "\n").encode()


CODEX_DECISIONS: dict[str, Literal["prompt", "forbidden"]] = {
    "ask": "prompt",
    "deny": "forbidden",
}


def _command_rules(
    permissions: PermissionSource | None,
) -> tuple[list[tuple[str, CodexPrefixRule]], list[Diagnostic]]:
    """Lower the ask and deny buckets to `prompt` and `forbidden` prefix rules.

    Codex matches one flat argv by literal prefix, with no wildcard, so a rule
    projects as its head and each tail appended directly, bare and behind any
    declared wrapper. The option-hole and tail-after-arguments spellings fall to
    Codex's own approval flow. Text and exact predicates have no prefix form;
    shell-unwrap's hook carries such denies (`_unwrap_hook`), and such asks
    fall to Codex's approval flow. A rule's `reason` becomes the
    `justification` Codex shows on rejection.
    Allows never project: a Codex `allow` rule runs the command outside the
    sandbox, while an unlisted command already runs inside it.

    Codex takes a list token as alternatives for one position, so the wrappers
    share one lead token, and patterns that differ only in their last token
    with the same decision and reason merge into one rule.
    """
    if permissions is None:
        return [], []
    diagnostics: list[Diagnostic] = []
    wrappers = sorted(permissions.wrappers)
    leads: list[tuple[str | tuple[str, ...], ...]] = [()]
    if wrappers:
        leads.append((wrappers[0] if len(wrappers) == 1 else tuple(wrappers),))
    lasts: dict[
        tuple[Literal["prompt", "forbidden"], str | None, tuple[Any, ...]], set[str]
    ] = {}
    dropped: list[str] = []
    for bucket, decision in CODEX_DECISIONS.items():
        for rule in getattr(permissions.commands, bucket):
            if rule.text or rule.exact:
                if bucket == "ask":
                    dropped.append(" ".join((rule.command, *rule.subcommand)))
                continue
            head = (rule.command, *rule.subcommand)
            for *stem, last in [(*head, *tail) for tail in sorted(rule.tail)] or [head]:
                for lead in leads:
                    key = (decision, rule.reason, (*lead, *stem))
                    lasts.setdefault(key, set()).add(last)
    rules = [
        (
            "permissions",
            CodexPrefixRule(
                pattern=[
                    *(list(item) if isinstance(item, tuple) else item for item in stem),
                    last.pop() if len(last) == 1 else sorted(last),
                ],
                decision=decision,
                justification=reason,
            ),
        )
        for (decision, reason, stem), last in lasts.items()
    ]
    if dropped:
        diagnostics.append(
            Diagnostic(
                "warning",
                "Codex prefix rules cannot hold a text predicate or an exact "
                "match, and its hooks cannot ask, so these asks fall to its "
                f"approval flow: {', '.join(dropped)}",
                permissions.paths[0],
            )
        )
    return rules, diagnostics


# Codex prefix rules miss a wrapped, bundled, or joined spelling and hold no
# text rule. shell-unwrap's hook bundle, a `Bash` PreToolUse command, denies a
# command inside a call when any peeled spelling of it matches a deny, text
# rules included. Codex rejects an ask from a PreToolUse hook, so asks stay
# prefix rules. The hook denies when it cannot run, so the guards fail closed.
# Codex runs it only once the user trusts it in `/hooks`.
UNWRAP_HOOK_PATH = Path("hooks/shell-unwrap.js")
UNWRAP_UNAVAILABLE = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            "shell-unwrap could not run, so its guards were not checked"
        ),
    }
}


def _unwrap_hook(
    root: Path, permissions: PermissionSource
) -> tuple[list[OwnedFile], list[NativeValue]]:
    """The hook and its deny rules, or nothing when no command is denied."""
    script = root / UNWRAP_HOOK_PATH
    rules = unwrap_rules_file(
        script.with_suffix(".json"),
        root,
        permissions,
        claude_shell_entries,
        asks=False,
    )
    if rules is None:
        return [], []
    command = f"bun {shlex.quote(script.as_posix())} codex 2>/dev/null"
    fallback = shlex.quote(json.dumps(UNWRAP_UNAVAILABLE))
    hook = {
        "matcher": "Bash",
        "hooks": [
            {
                "type": "command",
                "command": f"{command} || printf %s {fallback}",
                "timeout": UNWRAP_TIMEOUT_SECONDS,
            }
        ],
    }
    return [
        OwnedFile(script, UNWRAP_HOOK.read_bytes(), root, structured=False),
        rules,
    ], [NativeValue("codex", root / "config.toml", ("hooks", "PreToolUse"), [hook])]


def _agent_toml(
    agent: AgentSource, native: CodexAgentNative, raw: Mapping[str, Any]
) -> bytes:
    values: dict[str, Any] = {
        "name": agent.name,
        "description": agent.description.strip(),
        "developer_instructions": agent.body.rstrip(),
    }
    if agent.effort:
        values["model_reasoning_effort"] = agent.effort
    values.update(native.model_dump(exclude_none=True))
    # A role's features apply to the spawned agent; its sandbox, web search,
    # and spawn depth stay the parent's, which `limits` records.
    if "shell" in agent.deny:
        values["features"] = {**values.get("features", {}), "shell_tool": False}
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


PROFILE = "coding-agents"


def _permission_profile(
    permissions: PermissionSource, selected: str, home: Path, config: Path
) -> list[NativeValue]:
    """Define a permissions profile that extends Codex's `:workspace` sandbox,
    and select `selected`, which is that profile unless the source names a
    built-in one.

    The profile inherits the workspace filesystem rules, denies reads of the
    secret paths and denied directories, and lets the allowed directories be
    written. Codex keeps the read denials when the user switches to another
    profile in the session. Network stays with the user's patch, at
    `/permissions/coding-agents/network`.
    """

    def absolute(path: str) -> str:
        if path.startswith("~/"):
            return str(home / path.removeprefix("~/"))
        # A `**/` secret path matches at any depth anywhere.
        return f"/{path}" if path.startswith("**/") else path

    # Every directory is writable when the policy allows unmatched ones;
    # otherwise only the listed ones. Deny entries hold either way.
    writable = (
        {":root": "write"}
        if permissions.workspace.unmatched == "allow"
        else {
            absolute(path): "write"
            for path in literal_directories(permissions.workspace)
        }
    )
    filesystem = {
        **writable,
        **{
            absolute(path): "deny"
            for path in (*permissions.workspace.deny, *permissions.secret_paths)
        },
    }
    profile = ("permissions", PROFILE)
    values = [
        NativeValue("codex", config, ("default_permissions",), selected),
        NativeValue(
            "codex", config, (*profile, "description"), permissions.description
        ),
        NativeValue("codex", config, (*profile, "extends"), ":workspace"),
        NativeValue("codex", config, (*profile, "filesystem"), filesystem),
    ]
    # Commands never see a secret variable; a command that names one is not
    # refused, since prefix rules cannot match text.
    if permissions.secret_names:
        values.append(
            NativeValue(
                "codex",
                config,
                ("shell_environment_policy", "exclude"),
                list(permissions.secret_names),
            )
        )
    return values


# Codex's per-tool approval modes, which outrank the server's
# `default_tools_approval_mode`. A disabled tool is never offered.
CODEX_APPROVAL_MODES = {"allow": "approve", "ask": "prompt"}


def _mcp_tool_values(
    servers: list[tuple[McpServerSource, dict[str, Any]]], config: Path
) -> list[NativeValue]:
    values = []
    disabled: dict[str, list[str]] = {}
    for server, tool, decision in mcp_tool_decisions(servers):
        if decision == "deny":
            disabled.setdefault(server, []).append(tool)
        else:
            values.append(
                NativeValue(
                    "codex",
                    config,
                    ("mcp_servers", server, "tools", tool, "approval_mode"),
                    CODEX_APPROVAL_MODES[decision],
                )
            )
    values.extend(
        NativeValue("codex", config, ("mcp_servers", server, "disabled_tools"), tools)
        for server, tools in disabled.items()
    )
    return values


def compile_codex(ctx: SyncContext, sources: SourceBundle) -> Plan:
    root = ctx.codex
    files: list[OwnedFile] = []
    trees = declared_trees(
        root, (("rules", "file"), ("skills", "dir"), ("agents", "file"))
    )
    diagnostics: list[Diagnostic] = []
    globals_, issues = resolve("codex", sources.globals[:1])
    diagnostics.extend(issues)
    # Codex has no rules directory: every rule joins the global instructions,
    # and a rule's native prefix rules join the generated rules file.
    rules_, issues = resolve("codex", sources.rules, CodexRuleNative, raw=False)
    diagnostics.extend(issues)
    if parts := [item.source.body.rstrip() for item in (*globals_, *rules_)]:
        files.append(
            OwnedFile(root / "AGENTS.md", ("\n\n".join(parts) + "\n").encode(), root)
        )
    rules = [
        (rule.source.path.name, item) for rule in rules_ for item in rule.native.rules
    ]
    command_rules, issues = _command_rules(sources.permissions)
    rules.extend(command_rules)
    diagnostics.extend(issues)
    if content := _rule_text(rules):
        files.append(
            OwnedFile(root / "rules" / "coding-agents.rules", content, root / "rules")
        )
    for skill in sources.skills:
        if applies_to(skill, "codex") and not shared_skill(skill):
            tree, issues = codex_skill(skill, root / "skills")
            diagnostics.extend(issues)
            trees.extend(tree)
    # Codex has no custom prompts left, so a command becomes a skill that only
    # the user invokes.
    collisions = skill_collisions(sources, "codex")
    commands, issues = resolve("codex", sources.commands, CodexSkillNative)
    diagnostics.extend(issues)
    for resolved in commands:
        command, native = resolved.source, resolved.native
        if command.stem in collisions:
            diagnostics.append(collision_error(command))
            continue
        meta, issues = frontmatter(
            command.path,
            {"name": command.stem, "description": command.description},
            native.model_copy(update={"openai": None}),
            resolved.raw,
        )
        diagnostics.extend(issues)
        trees.append(
            _tree(
                command.stem,
                command.body,
                root / "skills",
                meta,
                native.openai,
                explicit_only=True,
            )
        )
    agents, issues = resolve("codex", sources.agents, CodexAgentNative)
    diagnostics.extend(issues)
    for resolved in agents:
        agent, native = resolved.source, resolved.native
        policy = resolve_model_policy(sources, agent, "codex")
        if policy is not None:
            conflicts = {"model", "model_reasoning_effort"} & (
                resolved.block.native.keys() | resolved.raw.keys()
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
            native = native.model_copy(
                update={
                    "model": policy.model,
                    "model_reasoning_effort": policy.effort,
                }
            )
        try:
            content = _agent_toml(agent, native, resolved.raw)
        except ValueError as exc:
            diagnostics.append(Diagnostic("error", str(exc), agent.path))
            continue
        files.append(
            OwnedFile(root / "agents" / f"{agent.stem}.toml", content, root / "agents")
        )
        if resolved.raw:
            diagnostics.append(
                Diagnostic("warning", "using unvalidated raw target fields", agent.path)
            )
    selected_profile = PROFILE
    hook_values: list[NativeValue] = []
    if permissions := sources.permissions:
        resolved_permissions, issues = resolve(
            "codex", [permissions], CodexPermissionNative, raw=False
        )
        diagnostics.extend(issues)
        for resolved in resolved_permissions:
            selected_profile = resolved.native.default_permissions or PROFILE
        if (
            permissions.unmatched == "sandbox"
            and selected_profile == ":danger-full-access"
        ):
            diagnostics.append(
                Diagnostic(
                    "error",
                    "unmatched: sandbox runs unlisted commands in Codex's sandbox, "
                    "which default_permissions :danger-full-access turns off",
                    permissions.paths[0],
                )
            )
        hook_files, hook_values = _unwrap_hook(root, permissions)
        files.extend(hook_files)
    # Codex discovers skills/ and agents/*.toml itself, so config.toml carries
    # no registration for them.
    servers, issues = mcp_servers(sources, "codex")
    diagnostics.extend(issues)
    websearch_denied = bool(
        sources.permissions and sources.permissions.tools.get("websearch") == "deny"
    )
    native_values = [
        *(
            _permission_profile(
                sources.permissions, selected_profile, ctx.home, root / "config.toml"
            )
            if sources.permissions
            else []
        ),
        *(
            (NativeValue("codex", root / "config.toml", ("web_search",), "disabled"),)
            if websearch_denied
            else ()
        ),
        *(
            NativeValue(
                "codex", root / "config.toml", ("mcp_servers", server.id, key), value
            )
            for server, fields in servers
            for key, value in {
                **fields,
                **({} if server.enabled else {"enabled": False}),
            }.items()
        ),
        *_mcp_tool_values(servers, root / "config.toml"),
        *hook_values,
    ]
    patch, issues = native_patch(
        config_root=ctx.config_root,
        target="codex",
        path=root / "config.toml",
        patch_name="codex",
    )
    diagnostics.extend(issues)
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
