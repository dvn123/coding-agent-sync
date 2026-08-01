from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence

from ..sources import CommandPermission, PermissionSource, WorkspacePermissions

CLAUDE_RESOLVED_WRAPPERS = frozenset(
    {"timeout", "time", "nice", "nohup", "stdbuf", "command", "noglob", "builtin"}
)
CLAUDE_TOOL_PATTERNS = {
    "read": "Read(**)",
    "edit": "Edit(**)",
    "write": "Write(**)",
    "webfetch": "WebFetch(*)",
    "websearch": "WebSearch(*)",
}
CURSOR_TOOL_PATTERNS = {"read": "Read(**)", "write": "Write(**)"}
CURSOR_TOOL_FLAGS = {"websearch": "autoAcceptWebSearch"}


def _wrapper_prefixes(wrapper: str | None) -> tuple[str, ...]:
    return ("",) if wrapper is None else (f"{wrapper} ", f"{wrapper} * ")


def _glob_heads(
    rule: CommandPermission, options: Sequence[str], wrapper: str | None
) -> tuple[str, ...]:
    prefixes = _wrapper_prefixes(wrapper)
    if not rule.subcommand:
        return tuple(f"{prefix}{rule.command}" for prefix in prefixes)
    subcommand = " ".join(rule.subcommand)
    return tuple(
        head
        for prefix in prefixes
        for head in (
            f"{prefix}{rule.command} {subcommand}",
            *(f"{prefix}{rule.command} {option}* {subcommand}" for option in options),
        )
    )


def glob_variants(
    rule: CommandPermission,
    options: Sequence[str] = (),
    wrapper: str | None = None,
    *,
    optional_trailing: bool = False,
) -> tuple[str, ...]:
    heads = _glob_heads(rule, options, wrapper)
    if rule.exact:
        return heads
    prefixes = heads
    if rule.tail:
        prefixes = tuple(
            variant
            for head in heads
            for sequence in sorted(rule.tail)
            for variant in (
                f"{head} {' '.join(sequence)}",
                f"{head} * {' '.join(sequence)}",
            )
        )
    if rule.text:
        return tuple(
            dict.fromkeys(
                f"{prefix}*{text}*" for prefix in prefixes for text in sorted(rule.text)
            )
        )
    return tuple(
        dict.fromkeys(
            variant
            for prefix in prefixes
            for variant in (
                (f"{prefix} *",)
                if optional_trailing or "*" not in prefix
                else (f"{prefix} *", prefix)
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
        return ()
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
    return tuple(
        native[tool]
        for tool, chosen in sorted(tools.items())
        if chosen == decision and tool in native
    )


def external_directory_map(workspace: WorkspacePermissions) -> dict[str, str]:
    entries = {"*": "ask"}
    for decision in ("allow", "ask"):
        for directory in getattr(workspace, decision):
            entries[f"{directory}/**"] = decision
    return entries


def literal_directories(workspace: WorkspacePermissions) -> list[str]:
    return [
        directory
        for directory in workspace.allow
        if not any(character in directory for character in "*?[")
    ]


def secret_name_variants(names: Iterable[str]) -> tuple[str, ...]:
    return tuple(f"*{name}*" for name in names)


def bucket_patterns(
    permissions: PermissionSource,
    lower: Callable[[CommandPermission, Sequence[str], str | None], Sequence[str]],
    blanket: Callable[[str], str],
    resolved_wrappers: frozenset[str] = frozenset(),
) -> dict[str, list[str]]:
    commands = permissions.commands
    wrappers = tuple(w for w in permissions.wrappers if w not in resolved_wrappers)
    patterns: dict[str, list[str]] = {}
    for bucket, rules in commands.buckets:
        emitted = (
            [blanket(wrapper) for wrapper in wrappers] if bucket == "allow" else []
        )
        for rule in rules:
            options = commands.option_tokens(rule.command)
            emitted.extend(lower(rule, options, None))
            if bucket != "allow":
                for wrapper in wrappers:
                    emitted.extend(lower(rule, options, wrapper))
        patterns[bucket] = list(dict.fromkeys(emitted))
    return patterns
