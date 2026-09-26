"""Decision oracle for compiled command permissions.

Lowering multiplies every source rule into target glob patterns. This module
models how each target matches a command against those patterns, generates
commands from the source rules, and records every decision, so a change to the
lowering can be shown to leave each target's decisions unchanged.

The OpenCode model ports its `Wildcard.match` and last-match evaluation. The
Claude model reproduces the matching the live `deny-reach` scenarios pin: the
wildcard grammar, the peeling of assignments and wrappers before deny and ask
rules, and the retry behind a bare `xargs`.
"""

from __future__ import annotations

import collections
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Annotated

import typer

from .sources import CommandPermission, PermissionSource, SourceBundle, load_sources
from .targets import claude, opencode
from .targets.permissions import fold_edit_write

UNMATCHED = ("ask", "allow")
BUCKETS = ("deny", "ask", "allow")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
REDIRECT = re.compile(r"(?:\d+|&)?(?:>>|>&|>\||<>|<&|>|<)(.*)", re.S)
# Claude peels these before matching every rule, with the options each takes
# a value for; `timeout` and `nice` also take a leading positional.
CLAUDE_ALWAYS_PEELED = {
    "timeout": {"-k", "-s", "--kill-after", "--signal"},
    "time": set(),
    "nice": {"-n", "--adjustment"},
    "nohup": set(),
    "stdbuf": set(),
    "command": set(),
    "builtin": set(),
    "noglob": set(),
}
# Claude also peels these before deny and ask rules only.
CLAUDE_DENY_PEELED = {
    "env": {"-u", "-C", "--unset", "--chdir"},
    "sudo": {"-u", "-g", "-U", "-C", "-D", "-h", "-p", "-r", "-R", "-t", "-T"},
    "exec": {"-a"},
    "nocorrect": set(),
}
# Natural spellings of each wrapper in front of a command.
WRAPPER_SPELLINGS = {
    "timeout": ("timeout 30", "timeout -k 5 30"),
    "env": ("env", "env FOO=1", "env -u HOME"),
    "command": ("command",),
    "xargs": ("xargs", "xargs -n 1"),
    "nice": ("nice", "nice -n 5"),
    "nohup": ("nohup",),
    "time": ("time",),
    "stdbuf": ("stdbuf -oL",),
}
EXTRA_PREFIXES = ("FOO=1", "sudo", "> /dev/null", "2>&1")
# Directories a probe runs a program from.
PROGRAM_DIRECTORIES = (
    "/bin/",
    "/usr/bin/",
    "/usr/local/bin/",
    "/opt/homebrew/bin/",
    "/sbin/",
    "/usr/sbin/",
)


# Matchers


@cache
def _opencode_regex(pattern: str) -> re.Pattern[str]:
    escaped = re.sub(r"[.+^${}()|\[\]\\]", lambda m: "\\" + m.group(0), pattern)
    escaped = escaped.replace("*", ".*").replace("?", ".")
    if escaped.endswith(" .*"):
        escaped = escaped[:-3] + "( .*)?"
    return re.compile(escaped, re.S)


def opencode_match(pattern: str, text: str) -> bool:
    return _opencode_regex(pattern).fullmatch(text) is not None


@cache
def _claude_regex(pattern: str) -> re.Pattern[str]:
    pattern = re.sub(r"[ \t]+", " ", pattern.strip())
    escaped = re.sub(r"[.+?^${}()|\[\]\\'\"]", lambda m: "\\" + m.group(0), pattern)
    escaped = escaped.replace("*", ".*")
    if escaped.endswith(" .*") and pattern.count("*") == 1:
        escaped = escaped[:-3] + "( .*)?"
    return re.compile(escaped, re.S)


def claude_match(pattern: str, text: str) -> bool:
    if "*" not in pattern:
        return pattern == text
    return _claude_regex(pattern).fullmatch(text) is not None


def _peel(words: list[str], table: dict[str, set[str]]) -> list[str] | None:
    """Drop one leading wrapper and its options, or None when there is none."""
    if not words or (options := table.get(words[0])) is None:
        return None
    index = 1
    while index < len(words) and words[index].startswith("-"):
        index += 2 if words[index] in options else 1
    if words[0] == "timeout" and index < len(words):
        index += 1
    return words[index:]


@cache
def claude_candidates(command: str, *, deny: bool) -> set[str]:
    """Every spelling Claude tests a rule against, whitespace normalized."""
    tables = (
        (CLAUDE_ALWAYS_PEELED, CLAUDE_DENY_PEELED) if deny else (CLAUDE_ALWAYS_PEELED,)
    )
    table = {name: options for table in tables for name, options in table.items()}
    found = {" ".join(command.split())}
    pending = [command.split()]
    while pending:
        words = pending.pop()
        peeled = None
        if deny and words and ASSIGNMENT.match(words[0]):
            peeled = words[1:]
        elif (
            deny
            and words[:2]
            and words[0] == "env"
            and len(words) > 2
            and words[1] in ("-S", "--split-string")
        ):
            peeled = " ".join(words[2:]).strip("'\"").split()
        elif deny and words and (head := _shell_words(words[0])[0][0]) != words[0]:
            # A quoted or escaped program name: `\rm`, `"rm"`.
            peeled = [head, *words[1:]]
        else:
            peeled = _peel(words, table)
        if peeled and (text := " ".join(peeled)) not in found:
            found.add(text)
            pending.append(peeled)
    return found


@dataclass(frozen=True)
class _Wrapper:
    values: frozenset[str] = frozenset()
    positionals: int = 0
    lookups: frozenset[str] = frozenset()
    assignments: bool = False


# A port of the opencode-unwrap plugin's `peel.ts`, which OpenCode runs over
# every shell command; `test_audit` replays the plugin's own cases.
UNWRAP_WRAPPERS = {
    "builtin": _Wrapper(),
    "command": _Wrapper(lookups=frozenset({"-v", "-V"})),
    "doas": _Wrapper(frozenset({"-a", "-C", "-u"})),
    "env": _Wrapper(
        frozenset(
            [
                "-a",
                "-C",
                "-P",
                "-S",
                "-u",
                "--argv0",
                "--chdir",
                "--split-string",
                "--unset",
            ]
        ),
        assignments=True,
    ),
    "exec": _Wrapper(frozenset({"-a"})),
    "nice": _Wrapper(frozenset({"-n", "--adjustment"})),
    "nocorrect": _Wrapper(),
    "noglob": _Wrapper(),
    "nohup": _Wrapper(),
    "stdbuf": _Wrapper(frozenset({"-e", "-i", "-o", "--error", "--input", "--output"})),
    "sudo": _Wrapper(
        assignments=True,
        values=frozenset(
            [
                "-C",
                "-D",
                "-g",
                "-h",
                "-p",
                "-R",
                "-r",
                "-T",
                "-t",
                "-U",
                "-u",
                "--chdir",
                "--chroot",
                "--close-from",
                "--command-timeout",
                "--group",
                "--host",
                "--other-user",
                "--prompt",
                "--role",
                "--type",
                "--user",
            ]
        ),
    ),
    "time": _Wrapper(frozenset({"-f", "-o", "--format", "--output"})),
    "timeout": _Wrapper(
        frozenset({"-k", "--kill-after", "-s", "--signal"}), positionals=1
    ),
    "xargs": _Wrapper(
        frozenset(
            [
                "-a",
                "-d",
                "-E",
                "-I",
                "-J",
                "-L",
                "-n",
                "-P",
                "-R",
                "-S",
                "-s",
                "--arg-file",
                "--delimiter",
                "--max-args",
                "--max-chars",
                "--max-lines",
                "--max-procs",
                "--process-slot-var",
            ]
        )
    ),
}


def _shell_words(text: str) -> list[tuple[str, int, int]]:
    """`(value, start, end)` per word, honouring quotes and backslashes."""
    found: list[tuple[str, int, int]] = []
    index = 0
    while index < len(text):
        while index < len(text) and text[index].isspace():
            index += 1
        if index >= len(text):
            break
        start, value = index, ""
        while index < len(text) and not text[index].isspace():
            char = text[index]
            if char == "'":
                close = text.find("'", index + 1)
                stop = len(text) if close == -1 else close
                value += text[index + 1 : stop]
                index = stop + 1
            elif char == '"':
                index += 1
                while index < len(text) and text[index] != '"':
                    if text[index] == "\\" and index + 1 < len(text):
                        index += 1
                    value += text[index]
                    index += 1
                index += 1
            elif char == "\\" and index + 1 < len(text):
                value += text[index + 1]
                index += 2
            else:
                value += char
                index += 1
        found.append((value, start, min(index, len(text))))
    return found


def _wrapped(words: list[tuple[str, int, int]], wrapper: _Wrapper) -> int | None:
    index, positionals = 1, wrapper.positionals
    while index < len(words):
        value = words[index][0]
        if value == "--":
            return index + 1 if index + 1 < len(words) else None
        if value in wrapper.lookups:
            return None
        if value.startswith("-") and value != "-":
            index += 2 if value in wrapper.values else 1
        elif wrapper.assignments and ASSIGNMENT.match(value):
            index += 1
        elif positionals > 0:
            positionals -= 1
            index += 1
        else:
            return index
    return None


def _peel_once(text: str) -> list[str]:
    words = _shell_words(text)
    if not words:
        return []
    head, head_start, head_end = words[0]
    raw = text[head_start:head_end]
    if redirect := REDIRECT.match(raw):
        rest = words[1 if redirect[1] else 2 :]
        return [text[rest[0][1] :]] if rest else []
    if ASSIGNMENT.match(head):
        return [text[words[1][1] :]] if len(words) > 1 else []
    found: list[str] = []
    program = head[head.rfind("/") + 1 :]
    if program and program != raw:
        found.append(program + text[head_end:])
    if wrapper := UNWRAP_WRAPPERS.get(program):
        if (index := _wrapped(words, wrapper)) is not None:
            found.append(text[words[index][1] :])
        if program == "env":
            split = next(
                (
                    i
                    for i, word in enumerate(words)
                    if word[0] in ("-S", "--split-string")
                ),
                -1,
            )
            if split > 0 and split + 1 < len(words):
                value, _, end = words[split + 1]
                found.append(value + text[end:])
    return [spelling for spelling in (item.strip() for item in found) if spelling]


def unwrap_spellings(command: str) -> list[str]:
    """Every distinct peeled spelling of `command`, excluding itself."""
    original = command.strip()
    seen, pending = {original: None}, [original]
    while pending:
        for spelling in _peel_once(pending.pop()):
            if spelling not in seen:
                seen[spelling] = None
                pending.append(spelling)
    del seen[original]
    return list(seen)


# Commands


def _heads(rule: CommandPermission, options: Sequence[str]) -> list[list[str]]:
    heads = [[rule.command, *rule.subcommand]]
    if rule.subcommand:
        for option in options:
            heads += [
                [rule.command, option, *rule.subcommand],
                [rule.command, option, "v", *rule.subcommand],
            ]
    return heads


def _bodies(rule: CommandPermission, head: list[str]) -> list[str]:
    start = " ".join(head)
    if rule.exact:
        return [start]
    stems = (
        [f"{start} {' '.join(seq)}" for seq in rule.tail]
        + [f"{start} a {' '.join(seq)} b" for seq in rule.tail]
        if rule.tail
        else [start, f"{start} arg"]
    )
    if not rule.text:
        return stems
    return [
        body
        for stem in stems
        for text in rule.text
        for body in (
            f"{stem} a{text}b",
            *((f"{stem} a{text.rstrip()}",) if text.endswith(" ") else ()),
        )
    ]


def probe_commands(permissions: PermissionSource) -> list[str]:
    commands = permissions.commands
    spellings = [
        *(
            s
            for w in permissions.wrappers
            for s in WRAPPER_SPELLINGS.get(w, (w, f"{w} -x 1"))
        ),
        *EXTRA_PREFIXES,
    ]
    probes: list[str] = []
    for _, rules in commands.buckets:
        for rule in rules:
            base = _bodies(rule, [rule.command, *rule.subcommand])
            probes += base
            for head in _heads(rule, commands.option_tokens(rule.command))[1:]:
                probes += _bodies(rule, head)
            for body in base:
                probes += [f"{prefix} {body}" for prefix in spellings]
                probes += [f"{directory}{body}" for directory in PROGRAM_DIRECTORIES]
                probes.append(f"\\{body}")
                probes += [f"echo {body}", f'git commit -m "{body}"', f"{body}x"]
            if rule.tail or rule.text:
                probes.append(f"{' '.join([rule.command, *rule.subcommand])} arg")
    return list(dict.fromkeys(probes))


# Compilation


def _with_unmatched(sources: SourceBundle, unmatched: str) -> SourceBundle:
    permissions = sources.permissions
    assert permissions is not None
    workspace = permissions.workspace.model_copy(update={"unmatched": unmatched})
    return sources.model_copy(
        update={
            "permissions": permissions.model_copy(
                update={"unmatched": unmatched, "workspace": workspace}
            )
        }
    )


def compiled(sources: SourceBundle) -> dict[str, object]:
    permissions = sources.permissions
    assert permissions is not None
    claude_tools = fold_edit_write(permissions.tools, "Claude")[0]
    opencode_tools = fold_edit_write(permissions.tools, "OpenCode")[0]
    settings = dict(claude._permission_values(sources, claude_tools))
    (value,) = opencode._permissions(sources, Path("opencode.json"), opencode_tools)
    return {
        "claude": {
            key: settings.get(("permissions", key), [])
            for key in ("allow", "ask", "deny")
        },
        "opencode": [
            (rule["resource"], rule["effect"])
            for rule in value.value
            if rule["action"] == "shell"
        ],
    }


def decisions(config_root: Path) -> dict[str, dict[str, str]]:
    sources = load_sources(config_root)
    assert sources.permissions is not None
    probes = probe_commands(sources.permissions)
    result: dict[str, dict[str, str]] = {}
    for unmatched in UNMATCHED:
        config = compiled(_with_unmatched(sources, unmatched))
        claude_permissions = config["claude"]
        shell_rules = config["opencode"]
        assert isinstance(claude_permissions, dict) and isinstance(shell_rules, list)
        claude_rules = ClaudeRules(claude_permissions)
        result[f"claude/{unmatched}"] = {p: claude_rules.verdict(p) for p in probes}
        opencode_rules = OpenCodeRules(shell_rules)
        result[f"opencode/{unmatched}"] = {
            p: opencode_rules.decision(p) for p in probes
        }
    return result


def _words(pattern: str) -> frozenset[str]:
    """The words every match contains whole."""
    segments = [segment.split(" ") for segment in re.split(r"[*?]", pattern)]
    words = [
        word
        for index, parts in enumerate(segments)
        for position, word in enumerate(parts)
        if word
        and (position > 0 or index == 0)
        and (position < len(parts) - 1 or index == len(segments) - 1)
    ]
    return frozenset(words)


class _Index[T]:
    """Rules grouped by their longest required word, so a command is tested
    only against rules whose required words it contains."""

    def __init__(self, entries: Sequence[tuple[str, T]]) -> None:
        self.grouped: dict[str | None, list[tuple[frozenset[str], T]]] = (
            collections.defaultdict(list)
        )
        for pattern, entry in entries:
            required = _words(pattern)
            anchor = max(required, key=len, default=None)
            self.grouped[anchor].append((required, entry))

    def lookup(self, texts: Iterable[str]) -> list[T]:
        words = {word for text in texts for word in text.split()}
        return [
            entry
            for word in (None, *words)
            for required, entry in self.grouped.get(word, ())
            if required <= words
        ]


class ClaudeRules:
    def __init__(self, permissions: dict[str, list[str]]) -> None:
        self.blanket = {
            bucket: "Bash" in permissions.get(bucket, ()) for bucket in BUCKETS
        }
        self.index = {
            bucket: _Index(
                [
                    (rule[5:-1], rule[5:-1])
                    for rule in permissions.get(bucket, ())
                    if rule.startswith("Bash(") and rule.endswith(")")
                ]
            )
            for bucket in BUCKETS
        }

    def verdict(self, command: str) -> str:
        """The rule class a command lands in: deny, ask, allow, or unmatched."""
        for bucket in BUCKETS:
            if self.blanket[bucket]:
                return bucket
            candidates = claude_candidates(command, deny=bucket != "allow")
            for pattern in self.index[bucket].lookup(candidates):
                retry = bucket != "allow" or pattern.rstrip().endswith("*")
                if any(
                    claude_match(pattern, text)
                    or (retry and claude_match(f"xargs {pattern}", text))
                    for text in candidates
                ):
                    return bucket
        return "unmatched"


class OpenCodeRules:
    def __init__(self, rules: Sequence[tuple[str, str]]) -> None:
        self.index = _Index(
            [
                (resource, (position, resource, effect))
                for position, (resource, effect) in enumerate(rules)
            ]
        )

    def native(self, command: str) -> str:
        """The last matching rule's effect; OpenCode's own default asks."""
        return next(
            (
                effect
                for _, resource, effect in sorted(
                    self.index.lookup((command,)), reverse=True
                )
                if opencode_match(resource, command)
            ),
            "ask",
        )

    def decision(self, command: str) -> str:
        """The native effect, or deny when the rules deny a spelling the
        opencode-unwrap plugin peels from the command."""
        effect = self.native(command)
        if effect != "deny" and any(
            self.native(spelling) == "deny" for spelling in unwrap_spellings(command)
        ):
            return "deny"
        return effect


# Attribution


def origins(config_root: Path) -> dict[str, collections.Counter[str]]:
    sources = load_sources(config_root)
    assert sources.permissions is not None
    result = {}
    for target, entries in (
        ("claude", claude.shell_entries(sources.permissions)),
        ("opencode", opencode.shell_entries(sources.permissions)),
    ):
        seen: set[tuple[str, str]] = set()
        counts: collections.Counter[str] = collections.Counter()
        for bucket, origin, pattern in entries:
            if (bucket, pattern) not in seen:
                seen.add((bucket, pattern))
                counts[f"{bucket} {origin}"] += 1
        result[target] = counts
    return result


main = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


@main.command("decisions")
def decisions_command(
    config_root: Annotated[Path, typer.Argument(file_okay=False)],
    output: Annotated[Path, typer.Argument(dir_okay=False)],
) -> None:
    """Write every probe command's decision per target and `unmatched` value."""
    output.write_text(json.dumps(decisions(config_root), indent=1, sort_keys=True))


@main.command("diff")
def diff_command(
    before: Annotated[Path, typer.Argument(dir_okay=False)],
    after: Annotated[Path, typer.Argument(dir_okay=False)],
) -> None:
    """List every decision that differs between two `decisions` outputs."""
    old, new = json.loads(before.read_text()), json.loads(after.read_text())
    changed = [
        (view, probe, old[view].get(probe), new[view].get(probe))
        for view in sorted(old.keys() | new.keys())
        for probe in sorted(old.get(view, {}).keys() | new.get(view, {}).keys())
        if old.get(view, {}).get(probe) != new.get(view, {}).get(probe)
    ]
    for view, probe, was, now in changed:
        typer.echo(f"{view}\t{was} -> {now}\t{probe}")
    raise typer.Exit(code=1 if changed else 0)


@main.command("origins")
def origins_command(
    config_root: Annotated[Path, typer.Argument(file_okay=False)],
) -> None:
    """Count every emitted shell pattern by bucket and the step that produced it."""
    for target, counts in origins(config_root).items():
        typer.echo(f"{target}: {sum(counts.values())}")
        for key, count in counts.most_common():
            typer.echo(f"  {key:24} {count}")
