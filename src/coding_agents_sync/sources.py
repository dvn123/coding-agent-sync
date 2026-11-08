import re
import stat
from itertools import combinations, product
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

type ToolName = Literal["claude", "cursor", "opencode", "codex", "pi"]
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
MCP_NAME = re.compile(r"[A-Za-z0-9_-]+")
type McpName = Annotated[str, StringConstraints(pattern=rf"^{MCP_NAME.pattern}$")]
type Effort = Literal["low", "medium", "high", "xhigh", "max"]
EFFORT_LEVELS: frozenset[str] = frozenset(get_args(Effort.__value__))


class TargetBlock(StrictModel):
    """One target's typed escape hatch, raw fields, and loss acknowledgement."""

    native: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    # Dotted portable paths, such as `workspace.ask`, this target loses; the
    # reason is the limitation's.
    omit: Annotated[
        tuple[Annotated[str, StringConstraints(pattern=r"^\w+(?:\.\w+)*$")], ...],
        Unique,
    ] = ()


class TargetBlocks(RootModel[dict[ToolName, TargetBlock]]):
    model_config = ConfigDict(frozen=True)


class CommandExecution(StrictModel):
    agent: str | None = None
    subtask: bool = False


# A leading token that is not option-shaped is a subcommand and belongs in a
# rule; tolerating it would open a hole at an arbitrary argument position.
type OptionToken = Annotated[
    str, StringConstraints(pattern=r"^-[A-Za-z0-9_./,@%+=~-]*$")
]
# Options a well-known CLI takes before its subcommand that pick where or how
# it runs, not what it does, so an allow carries over them. Asks and denies
# need no table: they match any leading options. The corpus `options` adds to
# these for its own tools.
PROGRAM_OPTIONS: dict[str, tuple[str, ...]] = {
    "aws": ("--profile", "--region"),
    "databricks": ("--profile", "-p", "--host"),
    "gcloud": (
        "--account",
        "--configuration",
        "--format",
        "--project",
        "--quiet",
        "-q",
        "--verbosity",
    ),
    "gh": ("-R",),
    "git": ("-C", "--no-pager"),
    "kubectl": ("--context", "--namespace", "-n"),
    "mise": ("--locked",),
    "terraform": ("-chdir",),
    "uv": ("--frozen", "--locked"),
    "wt": ("-C",),
}
# Literal text anywhere in the segment, edged by at most one space a side.
type LiteralText = Annotated[
    str, StringConstraints(pattern=r"^ ?[^\s*?()](?:[^*?()]*[^\s*?()])? ?$")
]


class CommandPermission(StrictModel):
    """A command authorization narrowed by optional independent predicates.

    Predicates are named for the region they constrain: `subcommand` pins the
    head, `tail` matches token sequences after it, and `text` matches literal
    text anywhere in the segment. The leading-option region between a command
    and its subcommand is not a rule predicate: an ask or deny matches any
    options there, and an allow carries over the command's options vocabulary.
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


BRACES = re.compile(r"\{([^{}]*)\}")


def _alternatives(text: str) -> list[str]:
    """`text` with each `{a,b}` group expanded, as a shell does. Whitespace
    around an alternative is dropped, so a long group may wrap."""
    choices = [
        [choice.strip() for choice in part.split(",")] if index % 2 else [part]
        for index, part in enumerate(BRACES.split(text))
    ]
    return ["".join(choice) for choice in product(*choices)]


def _sequences(value: Any) -> list[Any]:
    """The token sequences a written value stands for: a string splits on
    whitespace after brace expansion, and a list is one literal sequence."""
    if isinstance(value, str):
        return [tuple(text.split()) for text in _alternatives(value)]
    return [value]


def _listed(value: str | list[Any]) -> list[Any]:
    return [value] if isinstance(value, str) else value


def _expand_rule(entry: Any) -> list[Any]:
    """The `CommandPermission` inputs one written rule stands for.

    A bare string or list is a prefix. A mapping sets `prefix` or `exact`,
    plus optional `tail`, `text`, and `reason`.
    """
    if not isinstance(entry, dict):
        entry = {"prefix": entry}
    fields = dict(entry)
    if ("prefix" in fields) == ("exact" in fields):
        raise ValueError("a rule sets exactly one of prefix or exact")
    exact = "exact" in fields
    heads = _sequences(fields.pop("exact" if exact else "prefix"))
    if isinstance(tail := fields.get("tail"), str | list):
        fields["tail"] = [
            sequence for item in _listed(tail) for sequence in _sequences(item)
        ]
    if isinstance(text := fields.get("text"), str | list):
        fields["text"] = [
            alternative for item in _listed(text) for alternative in _alternatives(item)
        ]
    return [
        {
            **fields,
            "command": head[0] if head else "",
            "subcommand": tuple(head[1:]),
            "exact": exact,
        }
        for head in heads
    ]


BUCKET_STRICTNESS = {"allow": 0, "ask": 1, "deny": 2}
type Vocabulary[T] = Annotated[tuple[T, ...], Field(min_length=1), Unique]


class CommandPermissions(StrictModel):
    """The merged corpus: three decision buckets and the option vocabulary."""

    allow: tuple[CommandPermission, ...] = ()
    ask: tuple[CommandPermission, ...] = ()
    deny: tuple[CommandPermission, ...] = ()
    options: dict[Token, Vocabulary[OptionToken]] = Field(default_factory=dict)

    @property
    def buckets(self) -> tuple[tuple[str, tuple[CommandPermission, ...]], ...]:
        """Buckets in emission order: least strict first.

        OpenCode resolves the last matching key, so this order is also the
        precedence contract on that target.
        """
        return (("allow", self.allow), ("ask", self.ask), ("deny", self.deny))

    def option_tokens(self, command: str) -> tuple[str, ...]:
        """The options an allow for `command` carries over."""
        return tuple(
            dict.fromkeys(
                (*PROGRAM_OPTIONS.get(command, ()), *self.options.get(command, ()))
            )
        )

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
        if unused := sorted(set(self.options) - commanded):
            raise ValueError(
                f"options declared for {unused[0]}, which has no permission rule"
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


# Programs that run their argument as a command, which agents type in front of
# one; a target that cannot peel them gets ask and deny copies behind them.
DEFAULT_WRAPPERS = (
    "command",
    "env",
    "nice",
    "nohup",
    "stdbuf",
    "time",
    "timeout",
    "xargs",
)


# The decision for a shell command no rule matches. `sandbox` runs it in the
# target's own sandbox without asking, and asks where the target has none.
type Unmatched = Literal["ask", "allow", "sandbox"]


class PermissionPolicyDocument(StrictModel):
    description: str = ""
    wrappers: Annotated[tuple[Token, ...], Unique] = DEFAULT_WRAPPERS
    unmatched: Unmatched = "ask"
    tools: dict[PermissionTool, Decision] = Field(default_factory=dict)
    workspace: WorkspacePermissions = Field(default_factory=WorkspacePermissions)
    secret_paths: Annotated[tuple[SecretPath, ...], Unique] = ()
    secret_names: Annotated[tuple[SecretName, ...], Unique] = ()
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))


class PermissionRulesDocument(StrictModel):
    # False keeps the fragment but merges none of its rules.
    enabled: bool = True
    options: dict[Token, Vocabulary[OptionToken]] = Field(default_factory=dict)
    allow: tuple[CommandPermission, ...] = ()
    ask: tuple[CommandPermission, ...] = ()
    deny: tuple[CommandPermission, ...] = ()

    @field_validator("allow", "ask", "deny", mode="before")
    @classmethod
    def _expand_rules(cls, entries: Any) -> Any:
        if not isinstance(entries, list):
            return entries
        return [rule for entry in entries for rule in _expand_rule(entry)]


class SourceModel(StrictModel):
    """A Markdown source: its frontmatter fields, its path, and its body.

    Its directory gives its kind and its file name gives its name.
    """

    path: Path
    body: str
    description: Annotated[str, BeforeValidator(lambda value: value or "")] = ""
    # False keeps the source but delivers it nowhere, retiring deployed copies.
    enabled: bool = True
    # Restrict which targets receive this artifact. Absent or empty means all.
    only: Annotated[tuple[ToolName, ...], Unique] = ()
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))
    # Source-author bookkeeping, never compiled.
    internal: Any = Field(default=None, exclude=True)

    @property
    def stem(self) -> str:
        return self.path.stem

    @property
    def name(self) -> str:
        return self.stem

    @model_validator(mode="after")
    def _validate_only(self) -> SourceModel:
        if self.only and (extra := set(self.targets.root) - set(self.only)):
            raise ValueError(f"targets outside only: {sorted(extra)}")
        return self


class GlobalSource(SourceModel):
    pass


class RuleSource(SourceModel):
    @model_validator(mode="after")
    def _validate_stem(self) -> RuleSource:
        if self.stem in RESERVED_RULE_STEMS:
            raise ValueError(f"reserved rule filename `{self.path.name}`")
        return self


class SkillSource(SourceModel):
    paths: list[str] = Field(default_factory=list)
    disable_model_invocation: bool = False
    license: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def source_dir(self) -> Path:
        return self.path.parent

    @property
    def name(self) -> str:
        return self.source_dir.name

    @model_validator(mode="before")
    @classmethod
    def _accept_spec_name(cls, data: Any) -> Any:
        """Agent Skills frontmatter names the skill; it must be its directory."""
        if isinstance(data, dict) and "name" in data:
            data = dict(data)
            if data.pop("name") != Path(data["path"]).parent.name:
                raise ValueError("name must match the skill directory")
        return data


class CommandSource(SourceModel):
    execution: CommandExecution = Field(default_factory=CommandExecution)


# A tool class an agent may be kept from: `edit` covers every file-writing
# tool and `delegate` the tool that spawns another agent.
type AgentTool = Literal["shell", "edit", "webfetch", "websearch", "delegate"]


class AgentSource(SourceModel):
    model_policy: str | None = None
    effort: Effort | None = None
    background: bool = False
    color: str | None = None
    deny: Annotated[tuple[AgentTool, ...], Unique] = ()


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
    pi: ModelPolicyTarget = Field(default_factory=ModelPolicyTarget)
    codex: CodexModelPolicyTarget | None = None


class ModelPolicyDocument(StrictModel):
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
    description: str
    wrappers: tuple[str, ...]
    unmatched: Unmatched
    tools: dict[PermissionTool, Decision]
    workspace: WorkspacePermissions
    secret_paths: tuple[str, ...]
    secret_names: tuple[str, ...]
    commands: CommandPermissions
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))

    @property
    def unsandboxed(self) -> Literal["ask", "allow"]:
        """`unmatched` on a target with no sandbox, where `sandbox` asks."""
        return "ask" if self.unmatched == "sandbox" else self.unmatched


class McpServerSource(StrictModel):
    """One MCP server, registered under its file name by every target that
    receives it.

    A local server has `command` with optional `args` and `env`; a remote one
    has `url`. `tools` decides single tools by exact name; an undecided tool
    follows the target's own default. `enabled: false` keeps the server off
    until a session turns it on.
    """

    path: Path
    description: str = ""
    only: Annotated[tuple[ToolName, ...], Unique] = ()
    command: NonEmpty | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = Field(default_factory=dict)
    url: NonEmpty | None = None
    enabled: bool = True
    tools: dict[McpName, Decision] = Field(default_factory=dict)
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))

    @property
    def id(self) -> str:
        return self.path.stem

    @model_validator(mode="after")
    def _validate_transport(self) -> McpServerSource:
        if not MCP_NAME.fullmatch(self.id):
            raise ValueError(f"the file name must match {MCP_NAME.pattern}")
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
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise SourceSchemaError(f"{path}: frontmatter must be a mapping")
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
    if parsed is None:
        return {}
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
        if fragment.enabled:
            fragments.append((path, fragment))

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
                (rule for _, fragment in fragments for rule in getattr(fragment, name)),
                key=rule_key,
            )
        )

    # A command's leading-option vocabulary has one home, so the fragment that
    # owns the command owns its options and a second declaration is an error
    # rather than a silent union.
    options: dict[str, tuple[str, ...]] = {}
    owners: dict[str, Path] = {}
    for path, fragment in fragments:
        for command, tokens in fragment.options.items():
            if command in owners:
                raise SourceSchemaError(
                    f"{path}: options for {command} are already declared in "
                    f"{owners[command]}; a command's option vocabulary has one home"
                )
            owners[command], options[command] = path, tokens

    try:
        commands = CommandPermissions(
            allow=bucket("allow"),
            ask=bucket("ask"),
            deny=bucket("deny"),
            options=dict(sorted(options.items())),
        )
    except ValidationError as exc:
        raise _model_error(root, exc) from exc
    return PermissionSource(
        paths=(policy_path, *rule_paths, *local_paths),
        description=policy.description,
        wrappers=policy.wrappers,
        unmatched=policy.unmatched,
        tools=policy.tools,
        workspace=policy.workspace,
        secret_paths=policy.secret_paths,
        secret_names=policy.secret_names,
        commands=commands,
        targets=policy.targets,
    )


def with_home_paths(permissions: PermissionSource, home: Path) -> PermissionSource:
    """Each rule holding a `~/` token also with its tokens under the absolute
    path, the other spelling agents type. A home outside the token alphabet,
    such as a Windows drive's `C:/`, cannot be written in a rule and gets
    none."""
    prefix = f"{home.as_posix()}/"
    if not re.fullmatch(r"[A-Za-z0-9_./,@%+=~-]+", prefix):
        return permissions
    commands = permissions.commands

    def absolute(token: str) -> str:
        return prefix + token[2:] if token.startswith("~/") else token

    def home_relative(rule: CommandPermission) -> bool:
        tokens = (rule.command, *rule.subcommand, *(t for s in rule.tail for t in s))
        return any(token.startswith("~/") for token in tokens)

    def twinned(rules: tuple[CommandPermission, ...]) -> tuple[CommandPermission, ...]:
        return (
            *rules,
            *(
                rule.model_copy(
                    update={
                        "command": absolute(rule.command),
                        "subcommand": tuple(map(absolute, rule.subcommand)),
                        "tail": tuple(tuple(map(absolute, s)) for s in rule.tail),
                    }
                )
                for rule in rules
                if home_relative(rule)
            ),
        )

    return permissions.model_copy(
        update={
            "commands": commands.model_copy(
                update={
                    bucket: twinned(getattr(commands, bucket))
                    for bucket in ("allow", "ask", "deny")
                }
            )
        }
    )


def _load_many[S: SourceModel](
    paths: list[Path], model: type[S]
) -> tuple[list[S], list[str]]:
    loaded = []
    errors = []
    for path in paths:
        try:
            source = load_markdown(path, model)
        except SourceSchemaError as exc:
            errors.append(str(exc))
            continue
        if source.enabled:
            loaded.append(source)
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
    local_mcp_paths = sorted(config_root.glob("mcp.local/*.yaml"))
    for path in (*sorted(config_root.glob("mcp/*.yaml")), *local_mcp_paths):
        if path in local_mcp_paths and stat.S_IMODE(path.stat().st_mode) != 0o600:
            mcp_errors.append(f"{path}: local MCP server must use mode 0600")
            continue
        try:
            server = McpServerSource.model_validate({**_load_yaml(path), "path": path})
        except SourceSchemaError as exc:
            mcp_errors.append(str(exc))
            continue
        except ValidationError as exc:
            mcp_errors.append(str(_model_error(path, exc)))
            continue
        if any(other.id == server.id for other in mcp_servers):
            mcp_errors.append(f"{path}: MCP server {server.id!r} is already declared")
            continue
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
