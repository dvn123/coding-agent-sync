package unwrap

import (
	"slices"
	"strings"
)

// What a run-time word can be, read from the program's grammar: an operand
// after a `--` that ends its options, or an argument that no rule for
// another subcommand can read.

// optionsEnd is the index of a `--` that ends a program's options, or -1. It
// must not be an option's value: in `rg -e -- $f` it is the pattern -e takes,
// and $f may still be `--pre=sh`. So the word before it is literal, and, for
// a program whose value options programs.yaml does not list, not an option.
func optionsEnd(words []string) int {
	parsed := parse(words)
	if parsed == nil || !parsed.Grammar.EndsOptions || parsed.End == -1 {
		return -1
	}
	before := words[parsed.End-1]
	if runTime(before) || parsed.Grammar.Values == nil && strings.HasPrefix(before, "-") {
		return -1
	}
	return parsed.End
}

// positional marks Positional the run-time parts after a `--` that ends the
// program's options, which it reads as operands, so they hold no option a
// guard could name. It returns the mark for the input `xargs` appends:
// Positional when it lands after that `--`, else Wild.
func positional(words []string) ([]string, string) {
	end := optionsEnd(words)
	if end == -1 {
		return words, Wild
	}
	out := slices.Clone(words)
	input := Wild
	for index := end + 1; index < len(out); index++ {
		out[index] = mark(out[index], Positional)
		if strings.Contains(out[index], XargsInput) {
			input = Positional
		}
	}
	return out, input
}

// useOperands is the deny for a command that only a run-time word that may be
// an option keeps from running: `--` before such words settles it.
const useOperands = "shell-unwrap: a run-time word may be read as an option; put run-time paths and patterns after `--` (`rg -n x -- \"$f\"`, `xargs rg -l x --`)"

// operands is positional as if a `--` stood before the run-time words: each
// run-time word of a program that ends its options at `--` read as an
// operand, except one that starts with a literal `-`, an option `--` would not
// move. It asks whether such a `--` would settle the guards.
func operands(words []string) ([]string, string) {
	program := grammar(words[0])
	if program == nil || !program.EndsOptions || slices.Contains(words, "--") {
		return positional(words)
	}
	out := slices.Clone(words)
	for index := 1; index < len(out); index++ {
		if !strings.HasPrefix(out[index], "-") {
			out[index] = mark(out[index], Positional)
		}
	}
	return out, Positional
}

// mark is a word with its run-time parts marked as.
func mark(word, as string) string {
	for _, from := range []string{Wild, OneWord} {
		word = strings.ReplaceAll(word, from, as)
	}
	return word
}

// selector is what decides which of a program's rules a command can meet:
// Prefix names the grammar (`git`, `uv run`), Head the literal word after it
// that selects what runs (git's subcommand, the command uv run starts), and
// Opaque the words with every run-time part after Head, and every path
// before it, marked Opaque, since a rule for another Head cannot read them.
// Settings is whether the grammar bounds where settings are read (git's
// reads_settings), so a rule about text inside a word cannot read them either.
type selector struct {
	Opaque       []string
	Prefix, Head string
	Settings     bool
}

// selected is the selector of a command, or false when nothing literal
// selects what runs, or a setting before it holds a run-time value. Git's
// subcommand counts only when it is a builtin, which no alias can replace,
// that reads no settings from its arguments.
func selected(words []string) (selector, bool) {
	parsed := parse(words)
	if parsed == nil || parsed.Subcommand == -1 || runTime(words[parsed.Subcommand]) {
		return selector{}, false
	}
	program, subcommand := grammar(words[0]), words[parsed.Subcommand]
	out := slices.Clone(words)
	for _, option := range parsed.Options {
		if option.At > parsed.Subcommand {
			break
		}
		switch program.role(option.Name) {
		case "setting":
			if runTime(option.Value) {
				return selector{}, false
			}
		case "directory", "path", "repository":
			out[option.ValueAt] = mark(out[option.ValueAt], Opaque)
		}
	}
	found := selector{Opaque: out, Prefix: Basename(words[0]), Settings: len(program.ReadsSettings) > 0}
	from := parsed.Subcommand + 1
	switch {
	case slices.Contains(program.Builtins, subcommand) && !slices.Contains(program.ReadsSettings, subcommand):
		found.Head = subcommand
	case parsed.Command != -1 && !runTime(words[parsed.Command]):
		found.Prefix += " " + subcommand
		found.Head = words[parsed.Command]
		from = parsed.Command + 1
	default:
		return selector{}, false
	}
	for index := from; index < len(out); index++ {
		out[index] = mark(out[index], Opaque)
	}
	return found, true
}

// ruleHead is the Head a rule for prefix names, the first literal word after
// its options (`credential` in `git -* credential fill *`, `python` in
// `uv run * python - *`), or "".
func ruleHead(pattern, prefix string) string {
	tokens, words := strings.Fields(pattern), strings.Fields(prefix)
	if len(words) == 0 || len(tokens) < len(words) || !slices.Equal(tokens[:len(words)], words) {
		return ""
	}
	for _, token := range tokens[len(words):] {
		switch {
		case token == "*" || strings.HasPrefix(token, "-"):
			continue
		case strings.ContainsAny(token, "*?["):
			return ""
		default:
			return token
		}
	}
	return ""
}

// textRule is whether a rule matches text inside a word, as a `text`
// predicate compiles (`git*core.pager*`, `git -* config*alias.*`), rather
// than whole words (`git -* push * --force *`).
func textRule(pattern string) bool {
	for _, token := range strings.Fields(pattern) {
		if token != "*" && token != "-*" && strings.Contains(token, "*") {
			return true
		}
	}
	return false
}

// runTime is whether a value holds any part decided at run time.
func runTime(value string) bool {
	return strings.ContainsAny(value, wilds) || strings.Contains(value, XargsInput)
}
