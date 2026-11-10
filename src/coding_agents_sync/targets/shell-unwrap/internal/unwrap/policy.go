package unwrap

import (
	"slices"
	"strings"
)

// Policy is what the rules leave to judgement, read from shell-unwrap.json:
// the wrappers that carry an allow to what they run, each with the options
// that keep it the same command ("*" for every option its grammar reads);
// the assignments that change nothing a rule decides, as `NAME=value`, or
// `NAME` for any plain value; the shell options a script may set; and what
// the Claude hook does with a command it cannot judge only because of how it
// is written: "deny" or "ask" with how to rewrite it, or "" to leave it to
// Claude.
type Policy struct {
	Wrappers     map[string][]string `json:"wrappers"`
	Assignments  []string            `json:"assignments"`
	ShellOptions []string            `json:"shell_options"`
	Restructure  string              `json:"restructure"`
}

// A wrapper named by path runs only from a system directory: `/tmp/timeout`
// could be anything.
var systemDirs = []string{"", "/bin/", "/usr/bin/"}

// harmless is whether an assignment word changes nothing a rule decides.
func (p Policy) harmless(word string) bool {
	name, value, found := strings.Cut(word, "=")
	if !found || !assignment.MatchString(word) {
		return false
	}
	return slices.Contains(p.Assignments, word) || slices.Contains(p.Assignments, name) && plainValue.MatchString(value)
}

// wrapper is the options that keep program transparent, when the policy lists
// it and it runs from a system directory.
func (p Policy) wrapper(program string) ([]string, bool) {
	name := Basename(program)
	options, ok := p.Wrappers[name]
	return options, ok && slices.Contains(systemDirs, program[:len(program)-len(name)])
}

// permits is whether every option and operand of a parsed transparent wrapper
// keeps it the same command: a listed option with a literal value, and a
// literal operand that is not an assignment, which could change what runs.
func permits(options []string, words []string, parsed *Parsed) bool {
	for _, option := range parsed.Options {
		listed := slices.Contains(options, "*") || slices.Contains(options, option.Name)
		if !listed || runTime(option.Name) || runTime(option.Value) {
			return false
		}
	}
	for _, index := range parsed.Operands {
		if runTime(words[index]) || assignment.MatchString(words[index]) {
			return false
		}
	}
	return true
}

// Peel is the command a transparent wrapper or a harmless assignment at the
// head of words runs, one layer down, or false.
func (p Policy) Peel(words []string) ([]string, bool) {
	if len(words) < 2 {
		return nil, false
	}
	if p.harmless(words[0]) {
		return words[1:], true
	}
	options, ok := p.wrapper(words[0])
	if !ok {
		return nil, false
	}
	parsed := parse(words)
	if parsed == nil || parsed.Command == -1 || parsed.Lookup || !permits(options, words, parsed) {
		return nil, false
	}
	return runsWith(parsed, words), true
}

// runsWith is the command a parsed wrapper runs, with the input xargs adds
// in place of its replacement string, or after it.
func runsWith(parsed *Parsed, words []string) []string {
	inner := slices.Clone(words[parsed.Command:])
	input := parsed.Grammar.Runs.Input
	if input == nil {
		return inner
	}
	replaced := input.replacement(parsed.Options)
	if replaced == "" {
		return append(inner, XargsInput)
	}
	for index, word := range inner {
		inner[index] = strings.ReplaceAll(word, replaced, XargsInput)
	}
	return inner
}

// replacement is the string xargs replaces with its input (`-I R`, `-JR`,
// `-i`, `--replace=R`), or "" when it appends the input instead.
func (input *Input) replacement(options []Option) string {
	for _, option := range options {
		if slices.Contains(input.Replace, option.Name) {
			return option.Value
		}
		for _, braces := range input.ReplaceBraces {
			switch {
			case option.Name == braces && option.Takes:
				return option.Value
			case option.Name == braces:
				return "{}"
			case !strings.HasPrefix(braces, "--") && strings.HasPrefix(option.Name, braces):
				return option.Name[len(braces):]
			}
		}
	}
	return ""
}
