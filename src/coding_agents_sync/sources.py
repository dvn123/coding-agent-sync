from __future__ import annotations

import re
from collections.abc import Callable
from itertools import combinations
from pathlib import Path
from typing import Annotated, Any, Literal

import frontmatter
import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    computed_field,
    field_validator,
    model_validator,
)

from .metadata import to_snake

SCHEMA = "coding-agents/v4"
type SourceKind = Literal["global", "rule", "skill", "command", "agent"]

TOOL_NAMES: frozenset[str] = frozenset({"claude", "cursor", "opencode", "codex"})
COMMON_KEYS: frozenset[str] = frozenset({"schema", "kind", "id", "name", "description"})
RESERVED_RULE_STEMS: frozenset[str] = frozenset({"coding-agents-global"})


class SourceSchemaError(ValueError):
    pass


def _reject_duplicate_yaml_keys(path: Path, node: yaml.Node) -> None:
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key, value in node.value:
            if not isinstance(key, yaml.ScalarNode):
                raise SourceSchemaError(
                    f"{path}:{key.start_mark.line + 1}: mapping key must be scalar"
                )
            if key.value in seen:
                raise SourceSchemaError(
                    f"{path}:{key.start_mark.line + 1}: "
                    f"duplicate YAML key `{key.value}`"
                )
            seen.add(key.value)
            _reject_duplicate_yaml_keys(path, value)
    elif isinstance(node, yaml.SequenceNode):
        for value in node.value:
            _reject_duplicate_yaml_keys(path, value)


def _reject_normalized_yaml_key_collisions(
    path: Path, value: Any, *, target_block: bool = False
) -> None:
    if target_block:
        return
    if isinstance(value, dict):
        seen: set[str] = set()
        for key, child in value.items():
            normalized = to_snake(str(key))
            if normalized in seen:
                raise SourceSchemaError(
                    f"{path}: normalized YAML key collision `{normalized}`"
                )
            seen.add(normalized)
            _reject_normalized_yaml_key_collisions(
                path, child, target_block=key == "targets"
            )
    elif isinstance(value, list):
        for child in value:
            _reject_normalized_yaml_key_collisions(path, child)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TargetBlock(StrictModel):
    """One target's typed escape hatch, raw fields, and loss acknowledgement."""

    native: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    omit: dict[str, str] = Field(default_factory=dict)

    @field_validator("omit")
    @classmethod
    def _validate_omissions(cls, value: dict[str, str]) -> dict[str, str]:
        for path, reason in value.items():
            if (
                not path
                or path != path.strip()
                or not all(
                    part and part.replace("_", "").isalnum() for part in path.split(".")
                )
                or not isinstance(reason, str)
                or not reason.strip()
                or reason != reason.strip()
            ):
                raise ValueError(
                    "omit must map a dotted portable path to a non-empty trimmed reason"
                )
        return value


class TargetBlocks(RootModel[dict[str, TargetBlock]]):
    model_config = ConfigDict(frozen=True)

    @model_validator(mode="after")
    def _validate_targets(self) -> TargetBlocks:
        if unknown := set(self.root) - TOOL_NAMES:
            raise ValueError(f"unknown targets: {sorted(unknown)}")
        return self


class CommandExecution(StrictModel):
    agent: str | None = None
    subtask: bool = False


def _portable_tokens(value: tuple[str, ...]) -> bool:
    return bool(value) and all(
        token
        and token == token.strip()
        and re.fullmatch(r"[A-Za-z0-9_./,@%+=-]+", token) is not None
        for token in value
    )


class CommandPermission(StrictModel):
    """A command authorization narrowed by optional independent predicates.

    Predicates are named for the region they constrain: `subcommand` pins the
    head, `tail` matches token sequences after it, and `text` matches literal
    text anywhere in the segment. The leading-option region between a command
    and its subcommand is not a rule predicate; it is the per-command
    `options` vocabulary, which the compiler expands into every rule's heads.
    """

    command: str
    subcommand: tuple[str, ...] = ()
    tail: tuple[tuple[str, ...], ...] = ()
    text: tuple[str, ...] = ()
    exact: bool = False

    @field_validator("command")
    @classmethod
    def _validate_command(cls, value: str) -> str:
        if not _portable_tokens((value,)):
            raise ValueError("command must be one portable literal argv token")
        return value

    @field_validator("subcommand")
    @classmethod
    def _validate_subcommand(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value and not _portable_tokens(value):
            raise ValueError("subcommand must contain portable literal argv tokens")
        return value

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

    @field_validator("tail")
    @classmethod
    def _validate_tail(
        cls, value: tuple[tuple[str, ...], ...]
    ) -> tuple[tuple[str, ...], ...]:
        if any(not _portable_tokens(sequence) for sequence in value):
            raise ValueError("tail must contain non-empty portable literal argv tokens")
        if len(set(value)) != len(value):
            raise ValueError("tail must be unique")
        return value

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not text or text != text.strip() or any(c in text for c in "*?()")
            for text in value
        ):
            raise ValueError(
                "text must contain non-empty literal text without "
                "wildcards or parentheses"
            )
        if len(set(value)) != len(value):
            raise ValueError("text must be unique")
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


class CommandPermissions(StrictModel):
    """The merged corpus: three decision buckets and the option vocabulary."""

    allow: tuple[CommandPermission, ...] = ()
    ask: tuple[CommandPermission, ...] = ()
    deny: tuple[CommandPermission, ...] = ()
    options: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @property
    def buckets(self) -> tuple[tuple[str, tuple[CommandPermission, ...]], ...]:
        """Buckets in emission order: least strict first.

        OpenCode resolves the last matching key, so this order is also the
        precedence contract on that target.
        """
        return (("allow", self.allow), ("ask", self.ask), ("deny", self.deny))

    def option_tokens(self, command: str) -> tuple[str, ...]:
        return self.options.get(command, ())

    @field_validator("options")
    @classmethod
    def _validate_options(
        cls, value: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        for command, tokens in value.items():
            if not _portable_tokens((command,)):
                raise ValueError(
                    f"options key {command!r} must be one portable literal argv token"
                )
            if not tokens or not _portable_tokens(tokens):
                raise ValueError(
                    f"options for {command} must be non-empty portable literal tokens"
                )
            if len(set(tokens)) != len(tokens):
                raise ValueError(f"options for {command} must be unique")
            if any(not token.startswith("-") for token in tokens):
                # A leading token that is not option-shaped is a subcommand and
                # belongs in a rule; tolerating it would open a hole at an
                # arbitrary argument position.
                raise ValueError(
                    f"options for {command} must be leading option tokens "
                    "beginning with '-'"
                )
        return value

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
            del bucket
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


PERMISSION_TOOLS = ("read", "edit", "write", "webfetch", "websearch")
PERMISSION_DECISIONS = ("allow", "ask", "deny")


class WorkspacePermissions(StrictModel):
    """Directories the agent may reach outside the current project."""

    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_directories(self) -> WorkspacePermissions:
        seen = [*self.allow, *self.ask]
        if len(seen) != len(set(seen)):
            raise ValueError(
                "workspace directories must be unique across allow and ask"
            )
        if any(not path or path != path.strip() for path in seen):
            raise ValueError("workspace directories must be non-empty and trimmed")
        if any(not path.startswith(("~/", "/")) for path in seen):
            raise ValueError("workspace directories must be absolute or ~-relative")
        return self


class PermissionPolicyDocument(StrictModel):
    schema_: Literal["coding-agents/v4"] = Field(alias="schema")
    kind: Literal["permission-policy"]
    id: str
    name: str
    description: str = ""
    wrappers: tuple[str, ...] = ()
    tools: dict[str, str] = Field(default_factory=dict)
    workspace: WorkspacePermissions = Field(default_factory=WorkspacePermissions)
    secret_paths: tuple[str, ...] = ()
    secret_names: tuple[str, ...] = ()
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, value: dict[str, str]) -> dict[str, str]:
        for tool, decision in value.items():
            if tool not in PERMISSION_TOOLS:
                raise ValueError(
                    f"unknown tool {tool!r}; expected one of {PERMISSION_TOOLS}"
                )
            if decision not in PERMISSION_DECISIONS:
                raise ValueError(
                    f"tool {tool} decision must be one of "
                    f"{PERMISSION_DECISIONS}, got {decision!r}"
                )
        return value

    @field_validator("wrappers")
    @classmethod
    def _validate_wrappers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("wrappers must be unique")
        if value and not _portable_tokens(value):
            raise ValueError("wrappers must be portable literal argv tokens")
        return value

    @field_validator("secret_names")
    @classmethod
    def _validate_secret_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("secret_names must be unique")
        if any(
            not name or name != name.strip() or any(c in name for c in "*?()")
            for name in value
        ):
            raise ValueError(
                "secret_names must contain non-empty literal environment "
                "variable names without wildcards or parentheses"
            )
        return value

    @field_validator("secret_paths")
    @classmethod
    def _validate_secret_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("secret_paths must be unique")
        if any(
            not path
            or path != path.strip()
            or any(character.isspace() or character in "()" for character in path)
            for path in value
        ):
            raise ValueError(
                "secret_paths must contain non-empty native path patterns "
                "without whitespace or parentheses"
            )
        return value


class PermissionRulesDocument(StrictModel):
    schema_: Literal["coding-agents/v4"] = Field(alias="schema")
    kind: Literal["permission-rules"]
    id: str
    name: str
    description: str = ""
    options: dict[str, tuple[str, ...]] = Field(default_factory=dict)
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


class GlobalExtra(StrictModel):
    pass


class RuleExtra(StrictModel):
    pass


class SkillExtra(StrictModel):
    paths: list[str] = Field(default_factory=list)
    disable_model_invocation: bool = False
    license: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommandExtra(StrictModel):
    execution: CommandExecution = Field(default_factory=CommandExecution)


EFFORT_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})


class AgentExtra(StrictModel):
    effort: str | None = None
    background: bool = False
    color: str | None = None

    @field_validator("effort")
    @classmethod
    def _validate_effort(cls, value: str | None) -> str | None:
        if value is not None and value not in EFFORT_LEVELS:
            raise ValueError(f"effort must be one of {sorted(EFFORT_LEVELS)}")
        return value


class SourceModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: Path
    id: str
    name: str
    description: str
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))
    body: str

    @computed_field
    @property
    def stem(self) -> str:
        return self.path.stem


class GlobalSource(SourceModel):
    pass


class RuleSource(SourceModel):
    pass


class SkillSource(SourceModel):
    source_dir: Path = Field(default_factory=Path)
    paths: list[str] = Field(default_factory=list)
    disable_model_invocation: bool = False
    license: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CommandSource(SourceModel):
    execution: CommandExecution = Field(default_factory=CommandExecution)


class AgentSource(SourceModel):
    effort: str | None = None
    background: bool = False
    color: str | None = None


class PermissionSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    paths: tuple[Path, ...]
    id: str
    name: str
    description: str
    wrappers: tuple[str, ...]
    tools: dict[str, str]
    workspace: WorkspacePermissions
    secret_paths: tuple[str, ...]
    secret_names: tuple[str, ...]
    commands: CommandPermissions
    targets: TargetBlocks = Field(default_factory=lambda: TargetBlocks({}))
    command_targets: tuple[tuple[Path, TargetBlocks, bool], ...] = ()


class SourceBundle(BaseModel):
    model_config = ConfigDict(frozen=True)

    globals: tuple[GlobalSource, ...] = ()
    rules: tuple[RuleSource, ...] = ()
    skills: tuple[SkillSource, ...] = ()
    commands: tuple[CommandSource, ...] = ()
    agents: tuple[AgentSource, ...] = ()
    permissions: PermissionSource | None = None


def _model_error(path: Path, exc: ValidationError) -> SourceSchemaError:
    return SourceSchemaError(f"{path}: {exc}")


def _split_frontmatter(
    path: Path, meta: dict[str, Any]
) -> tuple[dict[str, Any], TargetBlocks]:
    """Separate portable fields from exact-key target blocks.

    `raw` is intentionally never normalized. Target compilers own typed-field
    validation, while this layer only makes the three target block names and
    omission reasons structurally safe.
    """
    remaining = dict(meta)
    target_data = remaining.pop("targets", {})
    remaining.pop("internal", None)
    if not isinstance(target_data, dict):
        raise SourceSchemaError(f"{path}: `targets` must be a mapping")
    try:
        targets = TargetBlocks.model_validate(target_data)
    except ValidationError as exc:
        raise _model_error(path, exc) from exc
    return {
        key: value for key, value in remaining.items() if key not in COMMON_KEYS
    }, targets


def _validate_markdown_frontmatter(path: Path) -> None:
    """Reject duplicate YAML keys before python-frontmatter collapses them."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        raise SourceSchemaError(f"{path}: invalid Markdown source") from None
    if not lines or lines[0].strip() != "---":
        return
    for index, line in enumerate(lines[1:], 1):
        if line.strip() not in {"---", "..."}:
            continue
        try:
            node = yaml.compose("".join(lines[1:index]), Loader=yaml.SafeLoader)
        except yaml.YAMLError:
            raise SourceSchemaError(f"{path}: invalid YAML frontmatter") from None
        if node is not None:
            _reject_duplicate_yaml_keys(path, node)
        return


def _parse_common(
    path: Path, expected_kind: SourceKind
) -> tuple[dict[str, Any], dict[str, Any], TargetBlocks, str]:
    _validate_markdown_frontmatter(path)
    post = frontmatter.loads(path.read_text(encoding="utf-8"))
    meta = dict(post.metadata or {})

    if meta.get("schema") != SCHEMA:
        raise SourceSchemaError(f"{path}: missing or invalid schema `{SCHEMA}`")
    if meta.get("kind") != expected_kind:
        raise SourceSchemaError(f"{path}: expected kind `{expected_kind}`")
    for required in ("id", "name"):
        if not meta.get(required):
            raise SourceSchemaError(f"{path}: missing required field `{required}`")

    remaining, targets = _split_frontmatter(path, meta)

    common = {
        "id": str(meta["id"]),
        "name": str(meta["name"]),
        "description": str(meta.get("description") or ""),
    }
    return common, remaining, targets, (post.content or "").lstrip("\n")


def load_global(path: Path) -> GlobalSource:
    common, remaining, targets, body = _parse_common(path, "global")
    try:
        GlobalExtra.model_validate(remaining)
    except ValidationError as exc:
        raise _model_error(path, exc) from exc
    return GlobalSource(path=path, targets=targets, body=body, **common)


def load_rule(path: Path) -> RuleSource:
    if path.stem in RESERVED_RULE_STEMS:
        raise SourceSchemaError(f"{path}: reserved rule filename `{path.name}`")
    common, remaining, targets, body = _parse_common(path, "rule")
    try:
        RuleExtra.model_validate(remaining)
    except ValidationError as exc:
        raise _model_error(path, exc) from exc
    return RuleSource(path=path, targets=targets, body=body, **common)


def load_skill(path: Path) -> SkillSource:
    common, remaining, targets, body = _parse_common(path, "skill")
    try:
        extra = SkillExtra.model_validate(remaining)
    except ValidationError as exc:
        raise _model_error(path, exc) from exc
    return SkillSource(
        path=path,
        targets=targets,
        body=body,
        source_dir=path.parent,
        paths=extra.paths,
        disable_model_invocation=extra.disable_model_invocation,
        license=extra.license,
        metadata=extra.metadata,
        **common,
    )


def load_command(path: Path) -> CommandSource:
    common, remaining, targets, body = _parse_common(path, "command")
    try:
        extra = CommandExtra.model_validate(remaining)
    except ValidationError as exc:
        raise _model_error(path, exc) from exc
    return CommandSource(
        path=path, targets=targets, body=body, execution=extra.execution, **common
    )


def load_agent(path: Path) -> AgentSource:
    common, remaining, targets, body = _parse_common(path, "agent")
    try:
        extra = AgentExtra.model_validate(remaining)
    except ValidationError as exc:
        raise _model_error(path, exc) from exc
    return AgentSource(
        path=path,
        targets=targets,
        body=body,
        effort=extra.effort,
        background=extra.background,
        color=extra.color,
        **common,
    )


def _snake_source_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            to_snake(str(key)): (
                child if key == "targets" else _snake_source_keys(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_snake_source_keys(child) for child in value]
    return value


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        node = yaml.compose(text, Loader=yaml.SafeLoader)
        if node is not None:
            _reject_duplicate_yaml_keys(path, node)
        parsed = yaml.safe_load(text)
    except OSError, yaml.YAMLError:
        raise SourceSchemaError(f"{path}: invalid YAML") from None
    if not isinstance(parsed, dict):
        raise SourceSchemaError(f"{path}: source must be a mapping")
    _reject_normalized_yaml_key_collisions(path, parsed)
    return _snake_source_keys(parsed)


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
    if not policy.id or not policy.name:
        raise SourceSchemaError(f"{policy_path}: id and name must be non-empty")

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
        if not fragment.id or not fragment.name:
            raise SourceSchemaError(f"{path}: id and name must be non-empty")
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
        id=policy.id,
        name=policy.name,
        description=policy.description,
        wrappers=policy.wrappers,
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
                bool(
                    fragment.options or fragment.allow or fragment.ask or fragment.deny
                ),
            )
            for path, fragment in fragments
        ),
    )


def _load_many[T](
    paths: list[Path], loader: Callable[[Path], T]
) -> tuple[list[T], list[str]]:
    loaded = []
    errors = []
    for path in paths:
        try:
            loaded.append(loader(path))
        except SourceSchemaError as exc:
            errors.append(str(exc))
    return loaded, errors


def load_sources(config_root: Path) -> SourceBundle:
    global_paths = (
        [config_root / "global" / "AGENTS.md"]
        if (config_root / "global" / "AGENTS.md").exists()
        else []
    )
    globals_, global_errors = _load_many(global_paths, load_global)
    rules, rule_errors = _load_many(sorted(config_root.glob("rules/*.md")), load_rule)
    skills, skill_errors = _load_many(
        sorted(config_root.glob("skills/*/SKILL.md")), load_skill
    )
    commands, command_errors = _load_many(
        sorted(config_root.glob("commands/*.md")), load_command
    )
    agents, agent_errors = _load_many(
        sorted(config_root.glob("agents/*.md")), load_agent
    )
    permission_root = config_root / "permissions"
    permission_errors: list[str] = []
    permissions: list[PermissionSource] = []
    if (config_root / "permissions.yaml").exists():
        legacy = config_root / "permissions.yaml"
        permission_errors.append(f"{legacy}: legacy permission source is unsupported")
    if permission_root.exists():
        try:
            permissions.append(
                load_permissions(permission_root, config_root / "permissions.local")
            )
        except SourceSchemaError as exc:
            permission_errors.append(str(exc))
    errors = (
        global_errors
        + rule_errors
        + skill_errors
        + command_errors
        + agent_errors
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
        permissions=permissions[0] if permissions else None,
    )
