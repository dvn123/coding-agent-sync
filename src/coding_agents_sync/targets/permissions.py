from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence

from ..sources import (
    WINDOWS_DRIVE,
    CommandPermission,
    Decision,
    PermissionSource,
    PermissionTool,
    WorkspacePermissions,
)


def wrapper_prefixes(wrapper: str) -> tuple[str, str]:
    """A wrapper right before the command, and with its arguments between."""
    return (f"{wrapper} ", f"{wrapper} * ")


# Claude strips an absolute program path and then peels these wrappers before
# it matches a deny or ask rule, so copies behind them are dead. `xargs` is
# peeled only bare: a deny retries itself behind `xargs `, but not behind
# `xargs` options.
CLAUDE_PEELED_WRAPPERS = (
    "timeout",
    "time",
    "nice",
    "nohup",
    "stdbuf",
    "command",
    "noglob",
    "builtin",
    "env",
    "sudo",
)
CLAUDE_PEELED_PREFIXES = frozenset(
    {
        *(
            prefix
            for wrapper in CLAUDE_PEELED_WRAPPERS
            for prefix in wrapper_prefixes(wrapper)
        ),
        "xargs ",
    }
)
# Claude cannot see past a leading redirect (`2>&1 rm -rf x`), so a command
# that starts with one is denied outright whenever a deny exists. Models do not
# write that shape, so this beats copying every deny behind every redirect.
CLAUDE_OPAQUE_COMMANDS = (
    "<*",
    ">*",
    "&>*",
    *(f"{fd}{operator}*" for fd in range(10) for operator in "<>"),
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
FOLDED_TOOLS: tuple[PermissionTool, ...] = ("edit", "write")
DECISION_STRICTNESS: tuple[Decision, ...] = ("deny", "ask", "allow")


def fold_edit_write(
    tools: Mapping[PermissionTool, Decision], target: str
) -> tuple[dict[PermissionTool, Decision], str | None]:
    """Resolve `edit` and `write` onto one decision, keeping the stricter one.

    Neither Claude nor OpenCode has a gate for writing alone: Claude's Edit
    rules cover every file-editing tool, and OpenCode's write tool asks for its
    `edit` permission. Returns the resolved map, and a note when it narrowed.
    """
    folded: dict[PermissionTool, Decision] = {
        tool: tools[tool] for tool in FOLDED_TOOLS if tool in tools
    }
    if len(set(folded.values())) <= 1:
        return dict(tools), None
    strictest = min(folded.values(), key=DECISION_STRICTNESS.index)
    spelled = ", ".join(f"{tool}: {value}" for tool, value in folded.items())
    resolved = dict(tools)
    for tool in folded:
        resolved[tool] = strictest
    return resolved, (
        f"{target} has no gate for writing alone, so {spelled} fold onto one "
        f"edit decision; applying the stricter {strictest}"
    )


def _home_spellings(command: str) -> tuple[str, ...]:
    """A glob matches the text as typed, and agents type a home path as `~/`
    or `$HOME/`, so a `~/` program lands under both."""
    if command.startswith("~/"):
        return (command, f"$HOME/{command.removeprefix('~/')}")
    return (command,)


def _quoted_spellings(token: str) -> tuple[str, ...]:
    """Agents quote path arguments (`api post '/api/2.0/x'`), and a glob
    matches the quotes, so a subcommand token holding a `/` lands in each
    quoting too."""
    if "/" in token:
        return (token, f"'{token}'", f'"{token}"')
    return (token,)


def _glob_heads(
    rule: CommandPermission, options: Sequence[str], prefix: str
) -> tuple[str, ...]:
    programs = _home_spellings(rule.command)
    if not rule.subcommand:
        return tuple(f"{prefix}{program}" for program in programs)
    subcommands = [
        " ".join(tokens)
        for tokens in itertools.product(*map(_quoted_spellings, rule.subcommand))
    ]
    return tuple(
        head
        for program in programs
        for subcommand in subcommands
        for head in (
            f"{prefix}{program} {subcommand}",
            *(f"{prefix}{program} {option}* {subcommand}" for option in options),
        )
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
    rule: CommandPermission, options: Sequence[str] = ()
) -> tuple[str, ...]:
    if rule.text:
        return ()
    program = rule.command
    if rule.subcommand:
        heads: tuple[tuple[str, ...], ...] = (
            rule.subcommand,
            *((option, *rule.subcommand) for option in options),
            *((option, "*", *rule.subcommand) for option in options),
        )
    elif rule.exact:
        # An empty head renders as `prog:`, the exact-bare form: the program
        # with no arguments, pinned by the bare-exact live scenarios.
        heads = ((),)
    elif not rule.tail:
        return (rule.command,)
    else:
        heads = ((),)
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
    # Cursor matches a plain entry as the whole command or a prefix of it at a
    # word boundary, so one `prog sub` covers the bare command and any
    # arguments. A hole needs the `prog:args` glob form, bare and with a
    # trailing `*`, because that `*` is not optional.
    return tuple(
        dict.fromkeys(
            variant
            for prefix in rendered
            for variant in (
                (f"{program}:{prefix}", f"{program}:{prefix} *")
                if "*" in prefix
                else (f"{program} {prefix}",)
            )
        )
    )


def tool_patterns(
    tools: Mapping[PermissionTool, Decision],
    native: Mapping[str, str],
    decision: str,
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
    file, so anywhere-patterns and absolute paths take the `//` root form.
    Windows paths match in POSIX form, `C:/x` as `/c/x`."""
    if WINDOWS_DRIVE.match(pattern):
        return f"//{pattern[0].lower()}{pattern[2:]}"
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


def bucket_entries(
    permissions: PermissionSource,
    lower: Callable[[CommandPermission, Sequence[str], str], Sequence[str]],
    peeled: frozenset[str] = frozenset(),
    *,
    peels_ask: bool = True,
) -> Iterator[tuple[str, str, str]]:
    """Lower each bucket as `(bucket, origin, pattern)`, with wrapped copies
    of every ask and deny rule.

    `origin` names the step that produced the pattern, for the audit. Allows
    get no wrapper form: a blanket `env *` would allow any payload the wrapper
    carries, so a wrapped allowed command falls to `unmatched`. `peeled` are
    wrapper prefixes the target strips before it matches a deny, and before an
    ask too unless `peels_ask` is false; such a target strips leading
    assignments there as well. Every other rule, allows included, lands behind
    each literal `NAME=value` its command declares in `assignments`: a fixed
    value spans nothing, unlike a wrapper's `*`.
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
        peels = bool(peeled) and (bucket == "deny" or (bucket == "ask" and peels_ask))
        for rule in rules:
            options = commands.option_tokens(rule.command)
            assigned = (
                []
                if peels
                else [f"{token} " for token in commands.assignment_tokens(rule.command)]
            )
            for origin, patterns in (
                ("base", lower(rule, (), "")),
                ("option", lower(rule, options, "")),
                *(
                    ("assignment", lower(rule, opts, prefix))
                    for prefix in assigned
                    for opts in ((), options)
                ),
            ):
                yield from ((bucket, origin, pattern) for pattern in patterns)
            if bucket != "allow":
                skip = peeled if peels else frozenset()
                for origin, prefix in wrapped:
                    if prefix not in skip:
                        yield from (
                            (bucket, origin, pattern)
                            for pattern in lower(rule, options, prefix)
                        )


def _glob(pattern: str) -> re.Pattern[str]:
    return re.compile(".*".join(map(re.escape, pattern.split("*"))), re.DOTALL)


def _uncovered(patterns: list[str]) -> list[str]:
    """Drop each pattern another one in the bucket already matches.

    A `*`-only glob covers another when it matches that one's text with each
    `*` read as a literal: its own `*` then absorbs whatever the other's
    stands for. Only a glob with a `*` before a word, as a `text` rule
    emits (`kubectl get secret*yaml*`), spans what token rules spell out,
    so only those are tried. An equivalent pair keeps both.
    """
    wide = [
        (pattern, max(pattern.split("*"), key=len), _glob(pattern))
        for pattern in patterns
        if re.search(r"\*[^\s*]", pattern)
    ]
    return [
        pattern
        for pattern in patterns
        if not any(
            other != pattern
            and literal in pattern
            and glob.fullmatch(pattern)
            and not _glob(pattern).fullmatch(other)
            for other, literal, glob in wide
        )
    ]


def bucket_patterns(entries: Iterable[tuple[str, str, str]]) -> dict[str, list[str]]:
    """Each bucket's distinct glob patterns, minus those another one covers.

    A covered pattern decides nothing: whatever it matches, the pattern that
    covers it matches with the same decision, and on OpenCode, whose last
    match wins, from the same contiguous bucket.
    """
    patterns: dict[str, dict[str, None]] = {"allow": {}, "ask": {}, "deny": {}}
    for bucket, _, pattern in entries:
        patterns[bucket][pattern] = None
    return {bucket: _uncovered(list(emitted)) for bucket, emitted in patterns.items()}
