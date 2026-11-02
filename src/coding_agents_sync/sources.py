import re
from itertools import combinations
from pathlib import Path
from typing import Annotated, Any, Literal, get_args

import yaml
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    RootModel,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from .io import load_yaml, split_frontmatter

SCHEMA = "coding-agents/v1"

type ToolName = Literal["claude", "cursor", "opencode", "codex"]
TOOL_NAMES: frozenset[str] = frozenset(get_args(ToolName.__value__))
RESERVED_RULE_STEMS: frozenset[str] = frozenset({"coding-agents-global"})


class SourceSchemaError(ValueError):
    pass


def _parse_yaml(path: Path, text: str, *, first_line: int = 1) -> Any:
    """Parse strict YAML, reporting errors at their line in `path`."""
    try:
        return load_yaml(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"{path}:{mark.line + first_line}" if mark else str(path)
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise SourceSchemaError(f"{where}: {problem}") from None


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _unique[T](value: tuple[T, ...]) -> tuple[T, ...]:
    if len(set(value)) != len(value):
        raise ValueError("entries must be unique")
    return value


Unique = AfterValidator(_unique)
# Non-empty, without leading or trailing whitespace, on one line.
type Line = Annotated[str, StringConstraints(pattern=r"^\S(?:.*\S)?$")]
type NonEmpty = Annotated[str, StringConstraints(min_length=1)]
# One literal argv token. The alphabet includes ~ so a rule can match
# home-relative command text exactly as the agent types it
# (`~/.config/tool/run.sh`); tokens are compared literally and the tilde is
# never expanded by any target.
type Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_./,@%+=~-]+$")]
type Decision = Literal["allow", "ask", "deny"]
# An MCP server or tool name every target spells unchanged in its rules.
type McpName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]+$")]
type Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORT_LEVELS: frozenset[str] = frozenset(get_args(Effort.__value__))


class TargetBlock(StrictModel):
    """One target's typed escape hatch, raw fields, and loss acknowledgement."""

    native: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    # A dotted portable path, such as `workspace.ask`, to the reason it is lost.
    omit: dict[Annotated[str, StringConstraints(pattern=r"^\w+(?:\.\w+)*$")], Line] = (
        Field(default_factory=dict)
    )


class TargetBlocks(RootModel[dict[ToolName, TargetBlock]]):
    model_config = ConfigDict(frozen=True)


class CommandExecution(StrictModel):
    agent: str | None = None
    subtask: bool = False


# One shell word, quoted as typed (`IFS=$'\t'`), with no whitespace or glob
# character, so its pattern matches that exact text and nothing wider.
# A literal value only, so its pattern matches that exact text: a wildcard
# value would span into the command.
type AssignmentToken = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*=[^\s*?]*$")
]
# A leading token that is not option-shaped is a subcommand and belongs in a
# rule; tolerating it would open a hole at an arbitrary argument position.
type OptionToken = Annotated[
    str, StringConstraints(pattern=r"^-[A-Za-z0-9_./,@%+=~-]*$")
]
# Literal text anywhere in the segment, edged by at most one space a side.
type LiteralText = Annotated[
    str, StringConstraints(pattern=r"^ ?[^\s*?()](?:[^*?()]*[^\s*?()])? ?$")
]


class CommandPermission(StrictModel):
    """A command authorization narrowed by optional independent predicates.

    Predicates are named for the region they constrain: `subcommand` pins the
    head, `tail` matches token sequences after it, and `text` matches literal
    text anywhere in the segment. The leading-option region between a command
    and its subcommand is not a rule predicate; it is the per-command
    `options` vocabulary, which the compiler expands into every rule's heads.
    `reason`, on a deny only, is what the agent is told to do instead; it is
    not a predicate.
    """

    command: Token
    subcommand: tuple[Token, ...] = ()
    tail: Annotated[
        tuple[Annotated[tuple[Token, ...], Field(min_length=1)], ...], Unique
    ] = ()
    text: Annotated[tuple[LiteralText, ...], Unique] = ()
    exact: bool = False
    reason: Line | None = None

    @field_validator("subcommand", "text", mode="before")
    @classmethod
    def _wrap_scalar(cls, value: Any) -> Any:
        return (value,) if isinstance(value, str) else value

    @field_validator("tail", mode="before")
    @classmethod
    def _wrap_single_tokens(cls, value: Any) -> Any:
        if isinstance(value, list | tuple):
            return [(item,) if isinstance(item, str) else item for item in value]
        return value

    @property
    def predicates(self) -> tuple[Any, ...]:
        return (
            self.command,
            self.subcommand,
            frozenset(self.tail),
            frozenset(self.text),
            self.exact,
        )

    def narrows(self, other: CommandPermission) -> bool:
        """Return whether every match of this rule also matches `other`."""

        # Entries are alternatives, so an unconstrained field is broadest and a
        # subset of alternatives is narrower than a superset.
        def within(inner: frozenset[Any], outer: frozenset[Any]) -> bool:
            return not outer or bool(inner and inner <= outer)

        if self.command != other.command:
            return False
        if other.exact:
            # An exact rule admits nothing after its subcommand.
            if not self.exact or self.subcommand != other.subcommand:
                return False
        elif self.subcommand[: len(other.subcommand)] != other.subcommand:
            return False
        return within(frozenset(self.tail), frozenset(other.tail)) and within(
            frozenset(self.text), frozenset(other.text)
        )


type CommandPermissionEntry = (
    Annotated[tuple[str, ...], Field(min_length=1)] | CommandPermission
)


BUCKET_STRICTNESS = {"allow": 0, "ask": 1, "deny": 2}
type Vocabulary[T] = Annotated[tuple[T, ...], Field(min_length=1), Unique]


class CommandPermissions(StrictModel):
    """The merged corpus: three decision buckets and the option vocabulary."""

    allow: tuple[CommandPermission, ...] = ()
    ask: tuple[CommandPermission, ...] = ()
    deny: tuple[CommandPermission, ...] = ()
    options: dict[Token, Vocabulary[OptionToken]] = Field(default_factory=dict)
    assignments: dict[Token, Vocabulary[AssignmentToken]] = Field(default_factory=dict)

    @property
    def buckets(self) -> tuple[tuple[str, tuple[CommandPermission, ...]], ...]:
        """Buckets in emission order: least strict first.

        OpenCode resolves the last matching key, so this order is also the
        precedence contract on that target.
        """
        return (("allow", self.allow), ("ask", self.ask), ("deny", self.deny))

    def option_tokens(self, command: str) -> tuple[str, ...]:
        return self.options.get(command, ())

    def assignment_tokens(self, command: str) -> tuple[str, ...]:
        return self.assignments.get(command, ())

    @model_validator(mode="after")
    def _validate_rules(self) -> CommandPermissions:
        """Reject contradictions, duplicates, and looser-inside-stricter pairs."""
        named = [(bucket, rule) for bucket, rules in self.buckets for rule in rules]
        for bucket, rule in named:
            if rule.exact and (rule.tail or rule.text):
                raise ValueError(
                    "exact rules match nothing after the subcommand, so "
                    "tail and text can never hold"
                )
            if rule.tail and rule.text:
                raise ValueError(
                    "tail with text lowers to one pattern that fixes their "
                    "order, which is not the conjunction the model promises; "
                    "use one field, or one rule for each"
                )
            if rule.reason is not None and bucket != "deny":
                raise ValueError("only a deny rule carries a reason")
        for (left_name, left), (right_name, right) in combinations(named, 2):
            if left.predicates == right.predicates:
                raise ValueError(
                    f"duplicate permission rule: {left_name} and {right_name} "
                    f"both match {describe_rule(left)}"
                )
            if left.narrows(right):
                inner, outer = (left_name, left), (right_name, right)
            elif right.narrows(left):
                inner, outer = (right_name, right), (left_name, left)
            else:
                continue
            # Only a stricter rule may narrow a looser one. The reverse is
            # unreachable wherever precedence is by category rather than by
            # specificity, and same-bucket containment is redundant.
            if BUCKET_STRICTNESS[inner[0]] <= BUCKET_STRICTNESS[outer[0]]:
                raise ValueError(
                    f"permission rules overlap: {outer[0]} {describe_rule(outer[1])} "
                    f"contains {inner[0]} {describe_rule(inner[1])}; only a "
                    "stricter rule may narrow a looser one"
                )
        commanded = {rule.command for _, rule in named}
        for field in ("options", "assignments"):
            if unused := sorted(set(getattr(self, field)) - commanded):
                raise ValueError(
                    f"{field} declared for {unused[0]}, which has no permission rule"
                )
        return self


def describe_rule(rule: CommandPermission) -> str:
    parts = [rule.command, *rule.subcommand]
    for sequence in sorted(rule.tail):
        parts.append(f"+{' '.join(sequence)}")
    for text in sorted(rule.text):
        parts.append(f"~{text}")
    if rule.exact:
        parts.append("(exact)")
    return " ".join(parts)


type PermissionTool = Literal["read", "edit", "write", "webfetch", "websearch"]
# A Windows directory outside the home, forward-slashed like every other path.
WINDOWS_DRIVE = re.compile(r"[A-Za-z]:/")
type Directory = Annotated[
    str, StringConstraints(pattern=r"^(?:~/|/|[A-Za-z]:/)(?:.*\S)?$")
]
# A literal environment variable name, matched anywhere in a command.
type SecretName = Annotated[
    str, StringConstraints(pattern=r"^[^\s*?()](?:[^*?()]*[^\s*?()])?$")
]
# A `**/`-anywhere or `~/`-home pattern without whitespace or parentheses.
type SecretPath = Annotated[str, StringConstraints(pattern=r"^(?:\*\*/|~/)[^\s()]*$")]


class WorkspacePermissions(StrictModel):
    """Directories the agent may reach outside the current project."""

    unmatched: Literal["ask", "allow"] = "ask"
    allow: tuple[Directory, ...] = ()
    ask: tuple[Directory, ...] = ()
    deny: tuple[Directory, ...] = ()

    @model_validator(mode="after")
    def _validate_directories(self) -> WorkspacePermissions:
        _unique((*self.allow, *self.ask, *self.deny))
        return self


class PermissionPolicyDocument(StrictModel):
    schema_: Literal["coding-agents/v1"] = Field(alias="schema")
    kind: Literal["permission-policy"]
    id: NonEmpty
    name: NonEmpty
    description: str = ""
    wrappers: Annotated[tuple[Token, ...], Unique] = ()
    unmatched: Literal["ask", "allow"] = "ask"
    tools: dict[PermissionTool, Decision] = Field(default_factory=dict)
    workspace: WorkspacePermissions = Field(default_factory=WorkspacePermissions)
    secret_paths: Annotated[tuple[SecretPath, ...], Unique] = ()
    secret_names: Annotated[tuple[SecretName, ...], Unique] = ()
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))


class PermissionRulesDocument(StrictModel):
    schema_: Literal["coding-agents/v1"] = Field(alias="schema")
    kind: Literal["permission-rules"]
    id: NonEmpty
    name: NonEmpty
    description: str = ""
    options: dict[Token, Vocabulary[OptionToken]] = Field(default_factory=dict)
    assignments: dict[Token, Vocabulary[AssignmentToken]] = Field(default_factory=dict)
    allow: tuple[CommandPermissionEntry, ...] = ()
    ask: tuple[CommandPermissionEntry, ...] = ()
    deny: tuple[CommandPermissionEntry, ...] = ()
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))


def _resolve_permission_entries(
    entries: tuple[CommandPermissionEntry, ...],
) -> tuple[CommandPermission, ...]:
    """Expand `- [git, status]` into the predicate form."""
    return tuple(
        entry
        if isinstance(entry, CommandPermission)
        else CommandPermission(command=entry[0], subcommand=tuple(entry[1:]))
        for entry in entries
    )


class SourceModel(StrictModel):
    """A Markdown source: its frontmatter fields, its path, and its body."""

    path: Path
    body: str
    schema_: Literal["coding-agents/v1"] = Field(alias="schema")
    id: NonEmpty
    name: NonEmpty
    description: Annotated[str, BeforeValidator(lambda value: value or "")] = ""
    # Restrict which targets receive this artifact. Absent or empty means all.
    only: Annotated[tuple[ToolName, ...], Unique] = ()
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))
    # Source-author bookkeeping, never compiled.
    internal: Any = Field(default=None, exclude=True)

    @property
    def stem(self) -> str:
        return self.path.stem

    @model_validator(mode="after")
    def _validate_only(self) -> SourceModel:
        if self.only and (extra := set(self.targets.root) - set(self.only)):
            raise ValueError(f"targets outside only: {sorted(extra)}")
        return self


class GlobalSource(SourceModel):
    kind: Literal["global"]


class RuleSource(SourceModel):
    kind: Literal["rule"]

    @model_validator(mode="after")
    def _validate_stem(self) -> RuleSource:
        if self.stem in RESERVED_RULE_STEMS:
            raise ValueError(f"reserved rule filename `{self.path.name}`")
        return self


class SkillSource(SourceModel):
    kind: Literal["skill"]
    paths: list[str] = Field(default_factory=list)
    disable_model_invocation: bool = False
    license: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def source_dir(self) -> Path:
        return self.path.parent


class CommandSource(SourceModel):
    kind: Literal["command"]
    execution: CommandExecution = Field(default_factory=CommandExecution)


class AgentSource(SourceModel):
    kind: Literal["agent"]
    model_policy: str | None = None
    effort: Effort | None = None
    background: bool = False
    color: str | None = None


class ModelPolicyTarget(StrictModel):
    model: str | None = None
    effort: Effort | None = None


class CodexModelPolicyTarget(StrictModel):
    model: str | None = None
    # Codex accepts model-defined effort values beyond the portable vocabulary.
    effort: str | None = None


class CursorModelPolicyTarget(StrictModel):
    # A Cursor model slug; Cursor agent files have no effort field.
    model: str | None = None


class ModelPolicyProfile(StrictModel):
    claude: ModelPolicyTarget = Field(default_factory=ModelPolicyTarget)
    cursor: CursorModelPolicyTarget = Field(default_factory=CursorModelPolicyTarget)
    opencode: ModelPolicyTarget = Field(default_factory=ModelPolicyTarget)
    codex: CodexModelPolicyTarget | None = None


class ModelPolicyDocument(StrictModel):
    schema_: Literal["coding-agents/model-policy/v1"] = Field(alias="schema")
    default: str
    profiles: dict[str, ModelPolicyProfile]

    @model_validator(mode="after")
    def _validate_profiles(self) -> ModelPolicyDocument:
        if not self.profiles:
            raise ValueError("profiles must not be empty")
        if self.default not in self.profiles:
            raise ValueError(f"default profile {self.default!r} is not defined")
        return self


class PermissionSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    paths: tuple[Path, ...]
    id: str
    name: str
    description: str
    wrappers: tuple[str, ...]
    unmatched: Literal["ask", "allow"]
    tools: dict[PermissionTool, Decision]
    workspace: WorkspacePermissions
    secret_paths: tuple[str, ...]
    secret_names: tuple[str, ...]
    commands: CommandPermissions
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))
    command_targets: tuple[tuple[Path, TargetBlocks, frozenset[str]], ...] = ()


class McpServerSource(StrictModel):
    """One MCP server, registered under `id` by every target that receives it.

    A local server has `command` with optional `args` and `env`; a remote one
    has `url`. `tools` decides single tools by exact name; an undecided tool
    follows the target's own default.
    """

    path: Path
    schema_: Literal["coding-agents/v1"] = Field(alias="schema")
    kind: Literal["mcp-server"]
    id: McpName
    description: str = ""
    only: Annotated[tuple[ToolName, ...], Unique] = ()
    command: NonEmpty | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    url: NonEmpty | None = None
    tools: dict[McpName, Decision] = Field(default_factory=dict)
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))

    @model_validator(mode="after")
    def _validate_transport(self) -> McpServerSource:
        if (self.command is None) == (self.url is None):
            raise ValueError("set exactly one of command (local) or url (remote)")
        if self.url is not None and (self.args or self.env):
            raise ValueError("args and env apply only to a local server")
        if self.only and (extra := set(self.targets.root) - set(self.only)):
            raise ValueError(f"targets outside only: {sorted(extra)}")
        return self


class SourceBundle(BaseModel):
    model_config = ConfigDict(frozen=True)

    globals: tuple[GlobalSource, ...] = ()
    rules: tuple[RuleSource, ...] = ()
    skills: tuple[SkillSource, ...] = ()
    commands: tuple[CommandSource, ...] = ()
    agents: tuple[AgentSource, ...] = ()
    model_policy: ModelPolicyDocument | None = None
    permissions: PermissionSource | None = None
    mcp_servers: tuple[McpServerSource, ...] = ()


def _model_error(path: Path, exc: ValidationError) -> SourceSchemaError:
    return SourceSchemaError(f"{path}: {exc}")


def load_markdown[S: SourceModel](path: Path, model: type[S]) -> S:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise SourceSchemaError(f"{path}: invalid Markdown source") from None
    header, body = split_frontmatter(text)
    meta = _parse_yaml(path, header, first_line=2) if header is not None else None
    if not isinstance(meta, dict):
        meta = {}
    if meta.get("schema") != SCHEMA:
        raise SourceSchemaError(f"{path}: missing or invalid schema `{SCHEMA}`")
    kind = model.model_fields["kind"].annotation
    if meta.get("kind") not in get_args(kind):
        raise SourceSchemaError(f"{path}: expected kind `{get_args(kind)[0]}`")
    try:
        return model.model_validate({**meta, "path": path, "body": body.strip("\n")})
    except ValidationError as exc:
        raise _model_error(path, exc) from exc


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        raise SourceSchemaError(f"{path}: invalid YAML") from None
    parsed = _parse_yaml(path, text)
    if not isinstance(parsed, dict):
        raise SourceSchemaError(f"{path}: source must be a mapping")
    return parsed


def load_model_policy(path: Path) -> ModelPolicyDocument:
    try:
        return ModelPolicyDocument.model_validate(_load_yaml(path))
    except ValidationError as exc:
        raise _model_error(path, exc) from exc


def resolve_model_policy(
    sources: SourceBundle, agent: AgentSource, target: str
) -> ModelPolicyTarget | None:
    if sources.model_policy is None:
        return None
    profile_name = agent.model_policy or sources.model_policy.default
    profile = sources.model_policy.profiles[profile_name]
    return getattr(profile, target)


def load_permissions(root: Path, local_root: Path | None = None) -> PermissionSource:
    """Load the committed corpus, then any machine-local command fragments.

    Local fragments merge before validation, so a local rule that conflicts
    with a committed one is rejected like any other overlap.
    """
    policy_path = root / "policy.yaml"
    if not policy_path.exists():
        raise SourceSchemaError(f"{policy_path}: missing permission policy")
    rule_paths = sorted((root / "commands").glob("*.yaml"))
    expected = {policy_path, *rule_paths}
    unknown = sorted(set(root.rglob("*.yaml")) - expected)
    if unknown:
        raise SourceSchemaError(
            f"{unknown[0]}: permission YAML must be policy.yaml or commands/*.yaml"
        )
    try:
        policy = PermissionPolicyDocument.model_validate(_load_yaml(policy_path))
    except ValidationError as exc:
        raise _model_error(policy_path, exc) from exc

    local_paths = (
        sorted(local_root.glob("*.yaml"))
        if local_root is not None and local_root.is_dir()
        else []
    )
    fragments: list[tuple[Path, PermissionRulesDocument]] = []
    for path in (*rule_paths, *local_paths):
        try:
            fragment = PermissionRulesDocument.model_validate(_load_yaml(path))
        except ValidationError as exc:
            raise _model_error(path, exc) from exc
        fragments.append((path, fragment))
    ids = [fragment.id for _, fragment in fragments]
    if len(ids) != len(set(ids)):
        raise SourceSchemaError(f"{root}: duplicate permission fragment id")

    def rule_key(rule: CommandPermission) -> tuple[Any, ...]:
        return (
            rule.command,
            rule.subcommand,
            tuple(sorted(rule.tail)),
            tuple(sorted(rule.text)),
            rule.exact,
        )

    def bucket(name: str) -> tuple[CommandPermission, ...]:
        return tuple(
            sorted(
                (
                    rule
                    for _, fragment in fragments
                    for rule in _resolve_permission_entries(getattr(fragment, name))
                ),
                key=rule_key,
            )
        )

    # A command's leading-option vocabulary has one home, so the fragment that
    # owns the command owns its options and a second declaration is an error
    # rather than a silent union.
    vocabularies: dict[str, dict[str, tuple[str, ...]]] = {
        "options": {},
        "assignments": {},
    }
    for field, vocabulary in vocabularies.items():
        owners: dict[str, Path] = {}
        for path, fragment in fragments:
            for command, tokens in getattr(fragment, field).items():
                if command in owners:
                    raise SourceSchemaError(
                        f"{path}: {field} for {command} are already declared in "
                        f"{owners[command]}; a command's {field.rstrip('s')} "
                        "vocabulary has one home"
                    )
                owners[command], vocabulary[command] = path, tokens

    try:
        commands = CommandPermissions(
            allow=bucket("allow"),
            ask=bucket("ask"),
            deny=bucket("deny"),
            options=dict(sorted(vocabularies["options"].items())),
            assignments=dict(sorted(vocabularies["assignments"].items())),
        )
    except ValidationError as exc:
        raise _model_error(root, exc) from exc
    return PermissionSource(
        paths=(policy_path, *rule_paths, *local_paths),
        id=policy.id,
        name=policy.name,
        description=policy.description,
        wrappers=policy.wrappers,
        unmatched=policy.unmatched,
        tools=policy.tools,
        workspace=policy.workspace,
        secret_paths=policy.secret_paths,
        secret_names=policy.secret_names,
        commands=commands,
        targets=policy.targets,
        command_targets=tuple(
            (
                path,
                fragment.targets,
                frozenset(
                    name
                    for name in ("options", "allow", "ask", "deny")
                    if getattr(fragment, name)
                ),
            )
            for path, fragment in fragments
        ),
    )


def _load_many[S: SourceModel](
    paths: list[Path], model: type[S]
) -> tuple[list[S], list[str]]:
    loaded = []
    errors = []
    for path in paths:
        try:
            loaded.append(load_markdown(path, model))
        except SourceSchemaError as exc:
            errors.append(str(exc))
    return loaded, errors


def load_sources(config_root: Path) -> SourceBundle:
    model_policy_path = config_root / "model-policy.yaml"
    model_policy: ModelPolicyDocument | None = None
    model_policy_errors: list[str] = []
    if model_policy_path.exists():
        try:
            model_policy = load_model_policy(model_policy_path)
        except SourceSchemaError as exc:
            model_policy_errors.append(str(exc))
    global_paths = (
        [config_root / "global" / "AGENTS.md"]
        if (config_root / "global" / "AGENTS.md").exists()
        else []
    )
    globals_, global_errors = _load_many(global_paths, GlobalSource)
    rules, rule_errors = _load_many(sorted(config_root.glob("rules/*.md")), RuleSource)
    skills, skill_errors = _load_many(
        sorted(config_root.glob("skills/*/SKILL.md")), SkillSource
    )
    commands, command_errors = _load_many(
        sorted(config_root.glob("commands/*.md")), CommandSource
    )
    agents, agent_errors = _load_many(
        sorted(config_root.glob("agents/*.md")), AgentSource
    )
    if model_policy is not None:
        for agent in agents:
            profile_name = agent.model_policy or model_policy.default
            if profile_name not in model_policy.profiles:
                model_policy_errors.append(
                    f"{agent.path}: unknown model policy {profile_name!r}"
                )
    elif any(agent.model_policy for agent in agents):
        model_policy_errors.append(
            f"{config_root / 'model-policy.yaml'}: required by an agent model_policy"
        )
    mcp_servers: list[McpServerSource] = []
    mcp_errors: list[str] = []
    for path in sorted(config_root.glob("mcp/*.yaml")):
        try:
            server = McpServerSource.model_validate({**_load_yaml(path), "path": path})
        except SourceSchemaError as exc:
            mcp_errors.append(str(exc))
            continue
        except ValidationError as exc:
            mcp_errors.append(str(_model_error(path, exc)))
            continue
        if server.id != path.stem:
            mcp_errors.append(f"{path}: id must match the file name")
        else:
            mcp_servers.append(server)
    permission_root = config_root / "permissions"
    permission_errors: list[str] = []
    permissions: list[PermissionSource] = []
    if permission_root.exists():
        try:
            permissions.append(
                load_permissions(permission_root, config_root / "permissions.local")
            )
        except SourceSchemaError as exc:
            permission_errors.append(str(exc))
    errors = (
        model_policy_errors
        + global_errors
        + rule_errors
        + skill_errors
        + command_errors
        + agent_errors
        + mcp_errors
        + permission_errors
    )
    if errors:
        joined = "\n".join(f"- {error}" for error in errors)
        raise SourceSchemaError(f"invalid coding-agent source(s):\n{joined}")
    return SourceBundle(
        globals=tuple(globals_),
        rules=tuple(rules),
        skills=tuple(skills),
        commands=tuple(commands),
        agents=tuple(agents),
        model_policy=model_policy,
        permissions=permissions[0] if permissions else None,
        mcp_servers=tuple(mcp_servers),
    )
