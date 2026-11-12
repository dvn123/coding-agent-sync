import functools
import hashlib
import itertools
import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from ..output.plan import OwnedFile
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
# it matches a deny or ask rule, so copies behind them are dead. It peels
# `xargs` only bare; shell-unwrap's hook peels it with its options too, and
# asks when it cannot run, so `xargs` needs no copy either.
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
    "xargs",
)
CLAUDE_PEELED_PREFIXES = frozenset(
    prefix for wrapper in CLAUDE_PEELED_WRAPPERS for prefix in wrapper_prefixes(wrapper)
)
# A hole for any options between a program and its subcommand, which an ask or
# deny matches whatever they are.
ANY_OPTIONS = ("-",)
# Wrappers that run their argument as given, so an allowed whole program is
# allowed behind them too where the target cannot see through them itself.
ALLOW_WRAPPERS = ("command", "xargs")
INTERPRETER_COMMANDS = frozenset({"bash", "eval", "sh", "zsh"})
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


def whole_program(rule: CommandPermission) -> bool:
    """Whether the rule takes its program with any arguments."""
    return not (rule.subcommand or rule.tail or rule.text or rule.exact)


def bucket_entries(
    permissions: PermissionSource,
    lower: Callable[[CommandPermission, Sequence[str], str], Sequence[str]],
    peeled: frozenset[str] = frozenset(),
    *,
    wrapped_allows: bool = False,
) -> Iterator[tuple[str, str, str]]:
    """Lower each bucket as `(bucket, origin, pattern)`.

    `origin` names the step that produced the pattern, for the audit. An ask
    or deny also lands with a hole for any options before its subcommand, and
    behind each declared wrapper the target does not peel: `peeled` are the
    wrapper prefixes it strips before it matches an ask or deny. An allow
    carries over only its command's
    option vocabulary and gets no wrapper form, since a blanket `env *` would
    allow any payload the wrapper carries. With `wrapped_allows`, an allowed
    whole program also lands behind `command` and `xargs`, which run it as
    given, unless the target peels that wrapper itself.
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
        guard = bucket != "allow"
        for rule in rules:
            options = ANY_OPTIONS if guard else commands.option_tokens(rule.command)
            for origin, patterns in (
                ("base", lower(rule, (), "")),
                ("option", lower(rule, options, "")),
            ):
                yield from ((bucket, origin, pattern) for pattern in patterns)
            if guard:
                for origin, prefix in wrapped:
                    if prefix not in peeled:
                        yield from (
                            (bucket, origin, pattern)
                            for pattern in lower(rule, options, prefix)
                        )
            elif (
                wrapped_allows
                and whole_program(rule)
                and rule.command not in INTERPRETER_COMMANDS
            ):
                for wrapper in ALLOW_WRAPPERS:
                    if f"{wrapper} " not in peeled:
                        yield from (
                            (bucket, wrapper, pattern)
                            for pattern in lower(rule, (), f"{wrapper} ")
                        )


def deny_reasons(
    permissions: PermissionSource,
    entries: Callable[[PermissionSource], Iterable[tuple[str, str, str]]],
) -> dict[str, str]:
    """Each pattern `entries` emits for a deny rule that carries a reason, to
    that reason."""
    commands = permissions.commands
    reasons: dict[str, str] = {}
    for rule in commands.deny:
        if rule.reason is None:
            continue
        alone = permissions.model_copy(
            update={
                "commands": commands.model_copy(
                    update={"allow": (), "ask": (), "deny": (rule,)}
                )
            }
        )
        for _bucket, _origin, pattern in entries(alone):
            reasons.setdefault(pattern, rule.reason)
    return reasons


# shell-unwrap's plugin and hook read deny reasons from the target's root.
UNWRAP_REASONS = Path("shell-unwrap.json")


def unwrap_policy(permissions: PermissionSource) -> dict[str, Any]:
    """What shell-unwrap judges beyond the rules: the wrappers that carry an
    allow with their transparent options, the harmless assignments, the shell
    options a script may set, what to do with a command it cannot judge only
    because of how it is written, and whether a command that would prompt is
    denied instead; empty parts left out."""
    policy = {
        "wrappers": {
            name: list(options) for name, options in permissions.wrappers.items()
        },
        "assignments": list(permissions.assignments),
        "shell_options": list(permissions.shell_options),
        "restructure": permissions.restructure,
        "prompts": permissions.prompts,
    }
    return {key: value for key, value in policy.items() if value}


def unwrap_reasons_file(
    root: Path,
    permissions: PermissionSource,
    entries: Callable[[PermissionSource], Iterable[tuple[str, str, str]]],
) -> list[OwnedFile]:
    """The reasons and policy file, or nothing when there is neither."""
    extras = {
        "reasons": deny_reasons(permissions, entries),
        "policy": unwrap_policy(permissions),
    }
    if not (extras := {key: value for key, value in extras.items() if value}):
        return []
    text = json.dumps(extras, indent=2, sort_keys=True) + "\n"
    return [OwnedFile(root / UNWRAP_REASONS, text.encode(), root)]


# shell-unwrap's hook: one Go binary, the Claude, Codex, and Cursor hooks by
# host argument, which Pi's extension runs as its Codex host and OpenCode's
# plugin asks for its reviews. Its source and dependencies are vendored beside
# this module, so it is reviewable and builds offline; it is built once per
# source and Go version, and cached.
UNWRAP_SOURCE = Path(__file__).with_name("shell-unwrap")
UNWRAP_NAME = "shell-unwrap.exe" if os.name == "nt" else "shell-unwrap"
UNWRAP_HOOK_PATH = Path("hooks") / UNWRAP_NAME
UNWRAP_TIMEOUT_SECONDS = 10


@functools.cache
def unwrap_peeled_prefixes() -> frozenset[str]:
    """The prefixes of every wrapper shell-unwrap's hook sees through: its
    grammar names the command the program, or one of its subcommands, runs, or
    the scripts it runs. The hook peels those for its guards whatever the
    policy, and judges a policy wrapper's scripts, so guard copies behind them
    are dead where the hook runs and asks when it cannot."""
    grammar = yaml.safe_load(
        (UNWRAP_SOURCE / "internal/unwrap/programs.yaml").read_text()
    )

    def sees_through(program: Mapping[str, Any]) -> bool:
        return "runs" in program or program.get("operands") == "script"

    return frozenset(
        prefix
        for name, program in grammar.items()
        if sees_through(program)
        or any(map(sees_through, program.get("subcommands", {}).values()))
        for prefix in wrapper_prefixes(name)
    )


# Claude Code and Codex send hook input as compact JSON, and a quote inside a
# string value is escaped, so only the field itself matches.
UNWRAP_BYPASS = '"permission_mode":"bypassPermissions"'
# Files beside the Go source that the binary is not built from.
UNWRAP_SHIMS = frozenset({"opencode.js", "pi.js"})


class UnwrapBuildError(RuntimeError):
    """The shell-unwrap binary could not be built."""


def _go(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "CGO_ENABLED": "0", "GOFLAGS": "-mod=vendor", "GOPROXY": "off"}
    try:
        return subprocess.run(
            ["go", *args],
            cwd=UNWRAP_SOURCE,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError:
        raise UnwrapBuildError(
            "shell-unwrap's hook builds with Go 1.26 or later; `go` is not on PATH"
        ) from None
    except subprocess.CalledProcessError as error:
        raise UnwrapBuildError(
            f"`go {' '.join(args)}` failed in {UNWRAP_SOURCE}:\n{error.stderr}"
        ) from None


@functools.cache
def unwrap_binary_path() -> Path:
    """The shell-unwrap binary for this machine, built from the vendored source
    once per source and Go version."""
    digest = hashlib.sha256(_go("env", "GOVERSION").stdout.encode())
    for path in sorted(UNWRAP_SOURCE.rglob("*")):
        relative = path.relative_to(UNWRAP_SOURCE)
        if path.is_file() and relative.as_posix() not in UNWRAP_SHIMS:
            digest.update(relative.as_posix().encode() + b"\0" + path.read_bytes())
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    binary = (
        cache
        / "coding-agents-sync/shell-unwrap"
        / digest.hexdigest()[:16]
        / UNWRAP_NAME
    )
    if not binary.is_file():
        binary.parent.mkdir(parents=True, exist_ok=True)
        building = binary.with_name(f".{UNWRAP_NAME}.{os.getpid()}")
        flags = "-s -w -buildid="
        _go(
            "build",
            "-trimpath",
            f"-ldflags={flags}",
            "-o",
            str(building),
            "./cmd/shell-unwrap",
        )
        building.replace(binary)
    return binary


def unwrap_binary() -> bytes:
    """The shell-unwrap binary's bytes, to install in a target."""
    return unwrap_binary_path().read_bytes()


def unwrap_command(
    binary: str,
    host: str,
    fallback: Mapping[str, Any],
    *,
    silent_in_bypass: bool = False,
) -> str:
    """The shell command that runs the hook binary at `binary`, a shell word.

    When it cannot run, the command prints `fallback`, or, with
    `silent_in_bypass`, nothing for an event in bypass permissions mode.
    """
    run = f"{binary} {host} 2>/dev/null"
    printed = f"printf %s {shlex.quote(json.dumps(fallback))}"
    if not silent_in_bypass:
        return f"{run} || {printed}"
    bypass = shlex.quote(UNWRAP_BYPASS)
    return (
        f'input=$(cat); printf %s "$input" | {run}'
        f" || case $input in *{bypass}*) ;; *) {printed} ;; esac"
    )


def unwrap_rules_file(
    path: Path,
    manifest_root: Path,
    permissions: PermissionSource,
    entries: Callable[[PermissionSource], Iterable[tuple[str, str, str]]],
    *,
    asks: bool,
) -> OwnedFile | None:
    """The `shell-unwrap.json` a hook reads beside itself, or None when nothing
    is guarded: the guards as Claude `Bash(...)` patterns, whose matching the
    hook shares, and the denies' reasons. Without `asks`, the deny rules only."""
    buckets = bucket_patterns(entries(permissions))
    guards = {
        "deny": [*buckets["deny"], *secret_name_variants(permissions.secret_names)]
    }
    if asks:
        guards["ask"] = buckets["ask"]
    if not any(guards.values()):
        return None
    rules = {
        "permissions": {
            bucket: [f"Bash({pattern})" for pattern in patterns]
            for bucket, patterns in guards.items()
        },
        "reasons": deny_reasons(permissions, entries),
    }
    if policy := unwrap_policy(permissions):
        rules["policy"] = policy
    text = json.dumps(rules, indent=2, sort_keys=True) + "\n"
    return OwnedFile(path, text.encode(), manifest_root)


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
