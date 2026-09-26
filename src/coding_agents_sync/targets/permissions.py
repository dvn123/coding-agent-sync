from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

from ..sources import CommandPermission, PermissionSource, WorkspacePermissions


def wrapper_prefixes(wrapper: str) -> tuple[str, str]:
    """A wrapper right before the command, and with its arguments between."""
    return (f"{wrapper} ", f"{wrapper} * ")


# Claude peels these itself before it matches a deny or ask rule, so copies
# behind them are dead. `xargs` is peeled only bare: a deny retries itself
# behind `xargs `, but not behind `xargs` options.
CLAUDE_PEELED_PREFIXES = frozenset(
    {
        *(
            prefix
            for wrapper in (
                "timeout",
                "time",
                "nice",
                "nohup",
                "stdbuf",
                "command",
                "noglob",
                "builtin",
                "env",
            )
            for prefix in wrapper_prefixes(wrapper)
        ),
        "xargs ",
    }
)
# Claude does not resolve a program path, so `Bash(rm *)` misses `/bin/rm`.
# A bare `*/rm *` would also match `ls foo/rm bar`, so single-token
# denies get a copy per install directory instead.
PROGRAM_DIRECTORIES = (
    "/bin/",
    "/usr/bin/",
    "/usr/local/bin/",
    "/opt/homebrew/bin/",
    "/sbin/",
    "/usr/sbin/",
)
# Claude's file permission checks only consult Edit rules, and an Edit rule
# covers every file-editing tool, so `write` folds onto the same pattern as
# `edit`. A `Write(**)` rule parses but never matches.
CLAUDE_TOOL_PATTERNS = {
    "read": "Read(**)",
    "edit": "Edit(**)",
    "write": "Edit(**)",
    "webfetch": "WebFetch(*)",
    "websearch": "WebSearch(*)",
}
# Portable tool classes that collapse onto one gate on Claude and OpenCode, so
# they cannot carry different decisions there. Strictest first.
FOLDED_TOOLS = ("edit", "write")
DECISION_STRICTNESS = ("deny", "ask", "allow")


def fold_edit_write(
    tools: Mapping[str, str], target: str
) -> tuple[dict[str, str], str | None]:
    """Resolve `edit` and `write` onto one decision, keeping the stricter one.

    Neither Claude nor OpenCode has a gate for writing alone: Claude's Edit
    rules cover every file-editing tool, and OpenCode's write tool asks for its
    `edit` permission. Returns the resolved map, and a note when it narrowed.
    """
    folded = {tool: tools[tool] for tool in FOLDED_TOOLS if tool in tools}
    if len(set(folded.values())) <= 1:
        return dict(tools), None
    strictest = min(folded.values(), key=DECISION_STRICTNESS.index)
    spelled = ", ".join(f"{tool}: {value}" for tool, value in folded.items())
    return {**tools, **dict.fromkeys(folded, strictest)}, (
        f"{target} has no gate for writing alone, so {spelled} fold onto one "
        f"edit decision; applying the stricter {strictest}"
    )


CURSOR_TOOL_PATTERNS = {"read": "Read(**)", "write": "Write(**)"}
CURSOR_TOOL_FLAGS = {"websearch": "autoAcceptWebSearch"}
# The CLI's approvalMode, which is the only one `unmatched` reaches; Desktop
# is pinned to `allowlist` because it has no deny channel. The enum's third
# value, `auto-review`, hands unlisted commands to a model classifier, which no
# portable value asks for. `permissions.deny` binds under every value.
CURSOR_APPROVAL_MODES = {"ask": "allowlist", "allow": "unrestricted"}


def _glob_heads(
    rule: CommandPermission, options: Sequence[str], prefix: str
) -> tuple[str, ...]:
    if not rule.subcommand:
        return (f"{prefix}{rule.command}",)
    subcommand = " ".join(rule.subcommand)
    return (
        f"{prefix}{rule.command} {subcommand}",
        *(f"{prefix}{rule.command} {option}* {subcommand}" for option in options),
    )


def glob_variants(
    rule: CommandPermission,
    options: Sequence[str] = (),
    prefix: str = "",
    *,
    optional_trailing: bool = False,
) -> tuple[str, ...]:
    heads = _glob_heads(rule, options, prefix)
    if rule.exact:
        return heads
    stems = heads
    if rule.tail:
        stems = tuple(
            variant
            for head in heads
            for sequence in sorted(rule.tail)
            for variant in (
                f"{head} {' '.join(sequence)}",
                f"{head} * {' '.join(sequence)}",
            )
        )
    if rule.text:
        # A trailing-space text also ends the command bare: Claude's final
        # ` *` matches an empty end only when it is the rule's sole wildcard.
        return tuple(
            dict.fromkeys(
                variant
                for stem in stems
                for text in sorted(rule.text)
                for variant in (
                    f"{stem}*{text}*",
                    *((f"{stem}*{text[:-1]}",) if text.endswith(" ") else ()),
                )
            )
        )
    return tuple(
        dict.fromkeys(
            variant
            for stem in stems
            for variant in (
                (f"{stem} *",)
                if optional_trailing or "*" not in stem
                else (f"{stem} *", stem)
            )
        )
    )


def cursor_shell_variants(
    rule: CommandPermission,
    options: Sequence[str] = (),
    wrapper: str | None = None,
) -> tuple[str, ...]:
    if rule.text:
        return ()
    program = wrapper or rule.command
    lead = (rule.command,) if wrapper else ()
    if rule.subcommand:
        subcommand = (*lead, *rule.subcommand)
        heads: tuple[tuple[str, ...], ...] = (
            subcommand,
            *((*lead, option, *rule.subcommand) for option in options),
            *((*lead, option, "*", *rule.subcommand) for option in options),
        )
    elif lead:
        heads = (lead,)
    elif rule.exact:
        # An empty head renders as `prog:`, the exact-bare form: the program
        # with no arguments, pinned by the bare-exact live scenarios.
        heads = ((),)
    elif not rule.tail:
        return (rule.command,)
    else:
        heads = ((),)
    if wrapper:
        heads = tuple(shaped for head in heads for shaped in (head, ("*", *head)))
    prefixes = heads
    if rule.tail:
        prefixes = tuple(
            variant
            for head in heads
            for sequence in sorted(rule.tail)
            for variant in ((*head, *sequence), (*head, "*", *sequence))
        )
    rendered = tuple(dict.fromkeys(" ".join(prefix) for prefix in prefixes))
    if rule.exact:
        return tuple(f"{program}:{prefix}" for prefix in rendered)
    return tuple(
        dict.fromkeys(
            variant
            for prefix in rendered
            for variant in (f"{program}:{prefix}", f"{program}:{prefix} *")
        )
    )


def tool_patterns(
    tools: Mapping[str, str], native: Mapping[str, str], decision: str
) -> tuple[str, ...]:
    # Deduplicated, because several portable classes can share one native
    # pattern.
    return tuple(
        dict.fromkeys(
            native[tool]
            for tool, chosen in sorted(tools.items())
            if chosen == decision and tool in native
        )
    )


def has_directories(workspace: WorkspacePermissions) -> bool:
    return bool(
        workspace.allow
        or workspace.ask
        or workspace.deny
        or workspace.unmatched != "ask"
    )


def external_directory_rules(
    workspace: WorkspacePermissions,
) -> list[tuple[str, list[str]]]:
    """OpenCode's own `*: ask` default sits before its built-in allows (tool
    output, tmp, config), so only a non-default decision is written over it.
    """
    return [
        *([(workspace.unmatched, ["*"])] if workspace.unmatched != "ask" else []),
        *(
            (
                decision,
                [f"{directory}/**" for directory in getattr(workspace, decision)],
            )
            for decision in ("allow", "ask", "deny")
        ),
    ]


def denied_paths(permissions: PermissionSource) -> tuple[str, ...]:
    """Secret paths plus every file below a denied workspace directory."""
    return (
        *permissions.secret_paths,
        *(f"{directory}/**" for directory in permissions.workspace.deny),
    )


def claude_path(pattern: str) -> str:
    """Claude anchors a bare pattern at the cwd and a `/` one at the settings
    file, so anywhere-patterns and absolute paths take the `//` root form."""
    if pattern.startswith("**/"):
        return f"//{pattern}"
    if pattern.startswith("/"):
        return f"/{pattern}"
    return pattern


def opencode_paths(pattern: str) -> tuple[str, ...]:
    """OpenCode checks a file inside the session directory by its relative
    path, which `**/x` (a regex needing a `/`) and an expanded `~/x` both miss,
    so each pattern also lands in its relative spellings."""
    if pattern.startswith("**/"):
        return (pattern, pattern.removeprefix("**/"))
    if pattern.startswith("~/"):
        relative = pattern.removeprefix("~/")
        return (pattern, f"**/{relative}", relative)
    return (pattern,)


def literal_directories(workspace: WorkspacePermissions) -> list[str]:
    return [
        directory
        for directory in workspace.allow
        if not any(character in directory for character in "*?[")
    ]


def secret_name_variants(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(f"*{name}*" for name in names)


def rule_patterns(
    permissions: PermissionSource,
    lower: Callable[[CommandPermission, Sequence[str], str | None], Sequence[str]],
    rules: Iterable[CommandPermission],
) -> list[str]:
    """Lower each rule bare and once per declared wrapper, with no blanket.

    A bare wrapper allow (`Shell(env)`) would match any payload the wrapper
    carries, so a wrapper form is only ever emitted attached to its rule.
    """
    commands = permissions.commands
    return list(
        dict.fromkeys(
            variant
            for rule in rules
            for wrapper in (None, *permissions.wrappers)
            for variant in lower(rule, commands.option_tokens(rule.command), wrapper)
        )
    )


def bucket_entries(
    permissions: PermissionSource,
    lower: Callable[[CommandPermission, Sequence[str], str], Sequence[str]],
    peeled: frozenset[str] = frozenset(),
    *,
    peels_ask: bool = True,
    resolves_paths: bool = False,
) -> Iterator[tuple[str, str, str]]:
    """Lower each bucket as `(bucket, origin, pattern)`, with wrapped copies
    of every ask and deny rule.

    `origin` names the step that produced the pattern, for the audit. Allows
    get no wrapper form: a blanket `env *` would allow any payload the wrapper
    carries, so a wrapped allowed command falls to `unmatched`. `peeled` are
    wrapper prefixes the target strips before it matches a deny, and before an
    ask too unless `peels_ask` is false. `resolves_paths` says it judges
    `/bin/rm` as `rm`.
    """
    commands = permissions.commands
    wrapped = [
        (wrapper, prefix)
        for wrapper in permissions.wrappers
        for prefix in wrapper_prefixes(wrapper)
    ]
    for bucket, rules in commands.buckets:
        # Under `unmatched: allow` an allow decides nothing on either glob
        # target: the blanket allow already covers it, and it never outranks
        # an ask or deny.
        if bucket == "allow" and permissions.unmatched == "allow":
            continue
        for rule in rules:
            options = commands.option_tokens(rule.command)
            for origin, patterns in (
                ("base", lower(rule, (), "")),
                ("option", bare := lower(rule, options, "")),
            ):
                yield from ((bucket, origin, pattern) for pattern in patterns)
            # Multi-token denies stay bare: a copy per directory for every
            # `git push` spelling would multiply the ruleset for little gain.
            if bucket == "deny" and not rule.subcommand and not resolves_paths:
                yield from (
                    (bucket, "directory", f"{directory}{pattern}")
                    for directory in PROGRAM_DIRECTORIES
                    for pattern in bare
                )
            if bucket != "allow":
                skip = peeled if bucket == "deny" or peels_ask else frozenset()
                for origin, prefix in wrapped:
                    if prefix in skip:
                        continue
                    yield from (
                        (bucket, origin, pattern)
                        for pattern in lower(rule, options, prefix)
                    )


def bucket_patterns(entries: Iterable[tuple[str, str, str]]) -> dict[str, list[str]]:
    patterns: dict[str, dict[str, None]] = {"allow": {}, "ask": {}, "deny": {}}
    for bucket, _, pattern in entries:
        patterns[bucket][pattern] = None
    return {bucket: list(emitted) for bucket, emitted in patterns.items()}
