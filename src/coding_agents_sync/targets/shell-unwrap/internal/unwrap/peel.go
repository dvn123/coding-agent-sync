// Package unwrap applies shell permission rules to the commands a script
// runs, wrapped or not.
package unwrap

import (
	"regexp"
	"slices"
	"strings"
)

// Every spelling of a command with its launch prefixes peeled away.
//
// A rule names the program it is about, so a deny for `rm *` must also meet
// `timeout 5 rm -rf x`, `FOO=1 rm -rf x` and `/bin/rm -rf x`. Peeling yields
// the words each of those runs: past an assignment, a program's path, and a
// wrapper's options, as programs.yaml describes each wrapper. The guards
// judge every spelling; an allow is carried only through the policy's
// transparent wrappers (see Policy.Peel).

// Wild stands for any text the shell decides at run time; see glob.go.
const Wild = "\x00"

// OneWord stands for run-time text that stays inside its word, such as the
// rest of each name a glob like `src/*` expands to. Only the deny and ask
// guards' templates carry it; see Judge.
const OneWord = "\x01"

// Positional stands for run-time text after a literal `--`, which a program
// that ends its options there reads as operands: any words, none of them an
// option. Only the guards' templates carry it; see positional.
const Positional = "\x03"

// wilds are the run-time markers.
const wilds = Wild + OneWord + Positional

// XargsInput marks what `xargs` appends to the command it runs, or puts in
// place of its replacement string: the command must pass with that input in
// place, so an exact rule never carries it and a guard must judge it.
const XargsInput = "<xargs-input>"

var (
	assignment = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*\+?=`)
	// A redirect before the command, as the guards' fallback split leaves it
	// in a piece of text: `> out rm`, `2>&1 rm`. An empty target group means
	// the target is the next word.
	redirectPrefix = regexp.MustCompile(`(?s)^(?:\d+|&)?(?:>>|>&|>\||<>|<&|>|<)(.*)$`)
	// A plain value: nothing to expand, quote, or split.
	plainValue = regexp.MustCompile(`^[A-Za-z0-9_.,:/@%+-]*$`)
)

// Basename is a program's name without its directory.
func Basename(program string) string {
	return program[strings.LastIndexByte(program, '/')+1:]
}

// Longer letter runs are single-dash long options (`-chdir`, `-version`), not
// bundles of short flags.
const maxBundle = 4

var (
	joinedFlag = regexp.MustCompile(`(?s)^(--?[A-Za-z0-9][A-Za-z0-9-]*)=(.+)$`)
	shortFlag  = regexp.MustCompile(`(?s)^-[A-Za-z].`)
	letters    = regexp.MustCompile(`^-[A-Za-z]+$`)
	upper      = regexp.MustCompile(`[A-Z]`)
)

// flagReadings are the readings of one flag a rule can match: a bundle of
// short flags split (`-fu` as `-f -u`), a value detached from its flag
// (`--method=POST`, `-X=POST`, `-n1`, `-XPOST`), and both readings of a short
// bundle whose tail could be a value (`-oL` as `-o -L` and `-o L`).
func flagReadings(flag string) ([2][]string, bool) {
	if joined := joinedFlag.FindStringSubmatch(flag); joined != nil {
		reading := []string{joined[1], joined[2]}
		return [2][]string{reading, reading}, true
	}
	if !shortFlag.MatchString(flag) || strings.HasPrefix(flag, "--") {
		return [2][]string{}, false
	}
	value := []string{flag[:2], flag[2:]}
	valued := upper.MatchString(flag[2:])
	if !letters.MatchString(flag) {
		return [2][]string{value, value}, true
	}
	if len(flag)-1 > maxBundle {
		return [2][]string{value, value}, valued
	}
	split := make([]string, 0, len(flag)-1)
	for _, letter := range flag[1:] {
		split = append(split, "-"+string(letter))
	}
	if valued {
		return [2][]string{split, value}, true
	}
	return [2][]string{split, split}, true
}

// flagSpellings are the words with every flag in each of its readings, when
// any changes. A word with blanks or quotes in it is a message, not a flag.
func flagSpellings(words []string) [][]string {
	variants := [2][]string{{words[0]}, {words[0]}}
	changed := false
	for _, word := range words[1:] {
		readings, ok := flagReadings(word)
		if !ok || strings.ContainsAny(word, " \t\n'\"\\") {
			readings = [2][]string{{word}, {word}}
		} else {
			changed = true
		}
		for j := range variants {
			variants[j] = append(variants[j], readings[j]...)
		}
	}
	if !changed {
		return nil
	}
	if slices.Equal(variants[0], variants[1]) {
		return variants[:1]
	}
	return variants[:]
}

// peelOnce is the spellings one prefix layer below words.
func peelOnce(words []string) [][]string {
	if len(words) == 0 {
		return nil
	}
	head := words[0]
	if redirect := redirectPrefix.FindStringSubmatch(head); redirect != nil {
		next := 2
		if redirect[1] != "" {
			next = 1
		}
		if next < len(words) {
			return [][]string{words[next:]}
		}
		return nil
	}
	if assignment.MatchString(head) {
		if len(words) > 1 {
			return [][]string{words[1:]}
		}
		return nil
	}
	var found [][]string
	// `/bin/rm` runs `rm`.
	if program := Basename(head); program != "" && program != head {
		found = append(found, append([]string{program}, words[1:]...))
	}
	parsed := parse(words)
	if parsed != nil && parsed.Command != -1 && parsed.Grammar.Runs.KeepsLauncher {
		parsed = nil
	}
	if parsed == nil || parsed.Command == -1 && !parsed.runsOption() {
		if Basename(head) == head {
			// Rules name the bare program, so only its own spelling needs the
			// flags read.
			found = append(found, flagSpellings(words)...)
		}
		return found
	}
	if parsed.Command != -1 {
		found = append(found, words[parsed.Command:])
		if parsed.Grammar.Runs.Input != nil {
			found = append(found, runsWith(parsed, words))
		}
	}
	// `env -S 'rm -rf x'` runs its split string as the command.
	for _, option := range parsed.Options {
		if parsed.Grammar.role(option.Name) == "command" && option.Takes {
			split, ok := ShellWords(option.Value)
			if !ok {
				split = strings.Fields(option.Value)
			}
			found = append(found, append(split, words[option.ValueAt+1:]...))
			break
		}
	}
	return found
}

// runsOption is whether an option runs a command line (`env -S`).
func (parsed *Parsed) runsOption() bool {
	return slices.ContainsFunc(parsed.Options, func(option Option) bool {
		return parsed.Grammar.role(option.Name) == "command" && option.Takes
	})
}

// Spellings is every distinct peeled spelling of words, excluding words
// itself, in the order a depth-first peel finds them.
func Spellings(words []string) [][]string {
	key := func(list []string) string { return strings.Join(list, "\x1f") }
	seen := map[string]bool{key(words): true}
	var order [][]string
	pending := [][]string{words}
	for len(pending) > 0 {
		last := pending[len(pending)-1]
		pending = pending[:len(pending)-1]
		for _, spelling := range peelOnce(last) {
			if len(spelling) == 0 || seen[key(spelling)] {
				continue
			}
			seen[key(spelling)] = true
			order = append(order, spelling)
			pending = append(pending, spelling)
		}
	}
	return order
}
