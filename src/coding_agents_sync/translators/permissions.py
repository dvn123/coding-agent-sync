from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence

from coding_agents_sync.sources import (
    CommandPermission,
    PermissionSource,
    WorkspacePermissions,
)

# Wrappers Claude resolves to the inner command before matching, taking the
# strictest verdict across the peeled and unpeeled forms. It therefore needs
# neither a blanket allow nor a wrapped guard for these, and emitting one
# would widen every wrapped command that has no rule at all.
CLAUDE_RESOLVED_WRAPPERS = frozenset(
    {"timeout", "time", "nice", "nohup", "stdbuf", "command", "noglob", "builtin"}
)


def _wrapper_prefixes(wrapper: str | None) -> tuple[str, ...]:
    """Text prefixes for a wrapped command.

    A wrapper may sit immediately before its payload (`env rm -rf .`) or carry
    its own arguments first (`env FOO=1 rm -rf .`). The hole cannot match the
    empty string between two literal spaces, so both shapes are emitted;
    covering only the holed one would leave the bare form to the blanket
    wrapper allow, which is exactly the guard being bypassed.
    """
    if wrapper is None:
        return ("",)
    return (f"{wrapper} ", f"{wrapper} * ")


def _glob_heads(
    rule: CommandPermission, options: Sequence[str], wrapper: str | None
) -> tuple[str, ...]:
    """Command heads for a text matcher, including the leading-option region.

    The option hole is written attached (`-C*`), so one pattern covers
    `-C /tmp`, `-C/tmp`, `--context=prod`, and a valueless `--no-pager`: these
    wildcards match spaces and the empty string alike. Options need the
    subcommand as a right anchor, so a rule without one takes the bare head.
    """
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
    """Lower a rule to Claude and OpenCode command-text globs.

    Entries within a field are alternatives; the two fields are a conjunction.
    `optional_trailing` is OpenCode, whose matcher makes a trailing ` *`
    optional for every pattern; Claude makes it optional only for a
    single-wildcard one, so it also needs the bare form.
    """
    heads = _glob_heads(rule, options, wrapper)
    if rule.exact:
        return heads
    prefixes = heads
    if rule.tail:
        # A sequence may sit at the leading or a later argument position.
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
    # A prefix that already holds a wildcard needs the bare form as well, to
    # match when the sequence ends the command.
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
    """Lower a rule to Cursor's `command[:args]` token matcher.

    Cursor's interior wildcard is verified in both the allow and the deny
    channel, so tail sequences and the leading-option region are expressible
    here. It is greedy and needs at least one token, so an option is emitted
    in both its valued and valueless shape, and the trailing `*` is not
    optional, so every prefix is emitted bare and trailing. Free text has no
    verified form, and neither does a zero-argument exact rule.
    """
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
        # Both the bare and the holed shape, for the reason in
        # `_wrapper_prefixes`: the interior `*` needs at least one token.
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


# Native spelling of each portable tool class. Cursor Agent has no Edit tool —
# its Write covers editing — and no verified WebSearch entry, so it takes a
# narrower map rather than an invented one.
CLAUDE_TOOL_PATTERNS = {
    "read": "Read(**)",
    "edit": "Edit(**)",
    "write": "Write(**)",
    "webfetch": "WebFetch(*)",
    "websearch": "WebSearch(*)",
}
# Cursor Agent recognises exactly two path entries, `Read(` and `Write(`. Its
# Write tool also edits, but mapping a portable edit-only grant to Write would
# widen it to file creation. `WebFetch` is only a protobuf message type there,
# not a permission entry, and web fetch is scoped by `webFetchDomainAllowlist`
# with no verified allow-all form, so neither `edit` nor `webfetch` reaches it.
CURSOR_TOOL_PATTERNS = {
    "read": "Read(**)",
    "write": "Write(**)",
}
# Web search is a boolean on Cursor Agent rather than an allowlist entry.
CURSOR_TOOL_FLAGS = {"websearch": "autoAcceptWebSearch"}


def tool_patterns(
    tools: Mapping[str, str], native: Mapping[str, str], decision: str
) -> tuple[str, ...]:
    """Native entries for every portable tool class set to `decision`."""
    return tuple(
        native[tool]
        for tool, chosen in sorted(tools.items())
        if chosen == decision and tool in native
    )


def external_directory_map(workspace: WorkspacePermissions) -> dict[str, str]:
    """OpenCode's `permission.external_directory`, ask-by-default.

    Reaching outside the project is the exception, so the fallback asks and
    only the declared roots widen it.
    """
    entries = {"*": "ask"}
    for decision in ("allow", "ask"):
        for directory in getattr(workspace, decision):
            entries[f"{directory}/**"] = decision
    return entries


def literal_directories(workspace: WorkspacePermissions) -> list[str]:
    """Workspace roots that name a real directory rather than a glob.

    Claude Code's `additionalDirectories` takes literal paths, so a glob root
    such as OpenCode's per-user scratch directory reaches OpenCode only.
    """
    return [
        directory
        for directory in workspace.allow
        if not any(character in directory for character in "*?[")
    ]


def secret_name_variants(names: Iterable[str]) -> tuple[str, ...]:
    """Ask on any textual reference to a secret-bearing variable."""
    return tuple(f"*{name}*" for name in names)


def bucket_patterns(
    permissions: PermissionSource,
    lower: Callable[[CommandPermission, Sequence[str], str | None], Sequence[str]],
    blanket: Callable[[str], str],
    resolved_wrappers: frozenset[str] = frozenset(),
) -> dict[str, list[str]]:
    """Lower every bucket, expanding options everywhere and wrappers on guards.

    An exec wrapper hides its payload from every matcher, so covering wrapped
    commands by cross-producing wrappers with the 660 allows would be both
    enormous and incomplete. One blanket allow per wrapper covers them all,
    and the asks and denies are re-emitted behind each wrapper so the guard
    still binds. A wrapper the target resolves natively needs neither.
    """
    commands = permissions.commands
    wrappers = tuple(w for w in permissions.wrappers if w not in resolved_wrappers)
    patterns: dict[str, list[str]] = {}
    for bucket, rules in commands.buckets:
        emitted: list[str] = []
        if bucket == "allow":
            emitted.extend(blanket(wrapper) for wrapper in wrappers)
        for rule in rules:
            options = commands.option_tokens(rule.command)
            emitted.extend(lower(rule, options, None))
            if bucket != "allow":
                for wrapper in wrappers:
                    emitted.extend(lower(rule, options, wrapper))
        patterns[bucket] = list(dict.fromkeys(emitted))
    return patterns
