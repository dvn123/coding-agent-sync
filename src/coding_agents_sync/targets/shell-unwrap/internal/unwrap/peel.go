// Package unwrap applies shell permission rules to the commands a script
// runs, wrapped or not.
package unwrap

import (
	"regexp"
	"slices"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Every spelling of a shell command with its launch prefixes peeled away.
//
// OpenCode matches a shell rule against a command's raw text, so a deny for
// `rm *` misses `timeout 5 rm -rf x`, `FOO=1 rm -rf x`, and `/bin/rm -rf x`.
// Peeling yields the text each of those commands actually runs, sliced from
// the original so its quoting is exactly what a rule would have matched.

// Wild stands for any text the shell decides at run time; see glob.go.
const Wild = "\x00"

// OneWord stands for run-time text that stays inside its word, such as the
// rest of each name a glob like `src/*` expands to. Only the deny and ask
// guards' templates carry it; see Judge.
const OneWord = "\x01"

// wilds are both run-time markers.
const wilds = Wild + OneWord

// XargsInput marks what `xargs` appends to the command it runs, or puts in
// place of its replacement string: the command must pass with that input in
// place, so an exact rule never carries it and a guard must judge it.
const XargsInput = "<xargs-input>"

// Word is one shell word: its value with quoting removed, and its byte span
// in the text it came from.
type Word struct {
	Value      string
	Start, End int
}

type wrapper struct {
	values      []string // options that consume the next word
	positionals int      // words between the options and the command
	lookups     []string // options that report on the command instead of running it
	assignments bool     // accepts NAME=value words before the command
}

var (
	assignment = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*\+?=`)
	// A redirect before the command: `> out rm`, `2>&1 rm`. An empty target
	// group means the target is the next word.
	redirectPrefix = regexp.MustCompile(`(?s)^(?:\d+|&)?(?:>>|>&|>\||<>|<&|>|<)(.*)$`)
	// A plain value: nothing to expand, quote, or split.
	plainValue = regexp.MustCompile(`^[A-Za-z0-9_.,:/@%+-]*$`)
)

// Option tables follow the GNU and BSD manuals; an unlisted option is taken as
// a flag without a value.
var wrappers = map[string]wrapper{
	"builtin":   {},
	"command":   {lookups: []string{"-v", "-V"}},
	"doas":      {values: []string{"-a", "-C", "-u"}},
	"env":       {values: []string{"-a", "-C", "-P", "-S", "-u", "--argv0", "--chdir", "--split-string", "--unset"}, assignments: true},
	"exec":      {values: []string{"-a"}},
	"nice":      {values: []string{"-n", "--adjustment"}},
	"nocorrect": {},
	"noglob":    {},
	"nohup":     {},
	"stdbuf":    {values: []string{"-e", "-i", "-o", "--error", "--input", "--output"}},
	"sudo": {assignments: true, values: []string{
		"-C", "-D", "-g", "-h", "-p", "-R", "-r", "-T", "-t", "-U", "-u", "--chdir", "--chroot", "--close-from",
		"--command-timeout", "--group", "--host", "--other-user", "--prompt", "--role", "--type", "--user",
	}},
	"time":    {values: []string{"-f", "-o", "--format", "--output"}},
	"timeout": {values: []string{"-k", "--kill-after", "-s", "--signal"}, positionals: 1},
	"xargs": {values: []string{
		"-a", "-d", "-E", "-I", "-J", "-L", "-n", "-P", "-R", "-S", "-s", "--arg-file", "--delimiter",
		"--max-args", "--max-chars", "--max-lines", "--max-procs", "--process-slot-var",
	}},
}

// Wrappers that change only when, how, or with how much environment the
// command runs, with the options that keep it so. `time -o FILE` writes a
// file, `env -C` moves the command, and an `env NAME=value` assignment can
// change which program runs, so the tables list what is safe rather than what
// is not.
var transparentOptions = map[string][]string{
	"builtin":   {},
	"command":   {},
	"env":       {"-i", "--ignore-environment", "-u", "--unset"},
	"nice":      {"-n", "--adjustment"},
	"nocorrect": {},
	"noglob":    {},
	"nohup":     {},
	"stdbuf":    {"-e", "-i", "-o", "--error", "--input", "--output"},
	"time":      {"-p"},
	"timeout":   {"-k", "--kill-after", "-s", "--signal", "--foreground", "--preserve-status", "-v", "--verbose"},
	"xargs": {
		"-0", "-d", "-E", "-I", "-J", "-L", "-n", "-P", "-r", "-s", "-t", "-x", "--delimiter", "--exit",
		"--max-args", "--max-chars", "--max-lines", "--max-procs", "--no-run-if-empty", "--null", "--verbose",
	},
}

// A wrapper named by path runs only from a system directory: `/tmp/timeout`
// could be anything.
var systemDirs = []string{"", "/bin/", "/usr/bin/"}

// Assignments that change nothing a rule decides, written as agents type them
// and as the zsh analysis renders them: the editor Git and gh would open, the
// separator `read` splits on, Python's bytecode cache, and Git's optional index
// lock.
var (
	harmlessRaw    = []string{"GIT_EDITOR=true", "GIT_OPTIONAL_LOCKS=0", "PYTHONDONTWRITEBYTECODE=1", "IFS=", "IFS=,", "IFS='|'", `IFS=$'\t'`}
	harmlessValues = []string{"GIT_EDITOR=true", "GIT_OPTIONAL_LOCKS=0", "PYTHONDONTWRITEBYTECODE=1", "IFS=", "IFS=,", "IFS=|", "IFS=\t"}
)

// Names Claude Code 2.1.319 lets an allow rule read past: locale, colour,
// terminal size, build and log switches. Its `ANTHROPIC_API_KEY` is left out:
// a credential typed into a command is never harmless.
var safeNames = []string{
	"GOEXPERIMENT", "GOOS", "GOARCH", "CGO_ENABLED", "GO111MODULE", "RUST_BACKTRACE", "RUST_LOG", "NODE_ENV",
	"PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG", "LANG",
	"LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_TIME", "CHARSET", "TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR", "TZ",
	"LS_COLORS", "LSCOLORS", "GREP_COLOR", "GREP_COLORS", "GCC_COLORS", "TIME_STYLE", "BLOCK_SIZE", "BLOCKSIZE",
	"COLUMNS", "LINES", "CLICOLOR", "CLICOLOR_FORCE", "CI", "DEBIAN_FRONTEND", "GIT_TERMINAL_PROMPT",
}

func isSpace(text string, index int) bool {
	r, _ := utf8.DecodeRuneInString(text[index:])
	return unicode.IsSpace(r)
}

// Words splits one command into words, honouring quotes and backslashes.
func Words(text string) []Word {
	var found []Word
	index := 0
	for index < len(text) {
		for index < len(text) && isSpace(text, index) {
			_, size := utf8.DecodeRuneInString(text[index:])
			index += size
		}
		if index >= len(text) {
			break
		}
		start := index
		var value strings.Builder
		for index < len(text) && !isSpace(text, index) {
			switch char := text[index]; {
			case char == '\'':
				end := strings.IndexByte(text[index+1:], '\'')
				stop := len(text)
				if end != -1 {
					stop = index + 1 + end
				}
				value.WriteString(text[index+1 : stop])
				index = stop + 1
			case char == '"':
				index++
				for index < len(text) && text[index] != '"' {
					if text[index] == '\\' && index+1 < len(text) {
						index++
					}
					value.WriteByte(text[index])
					index++
				}
				index++
			case char == '\\' && index+1 < len(text):
				value.WriteByte(text[index+1])
				index += 2
			default:
				value.WriteByte(char)
				index++
			}
		}
		found = append(found, Word{Value: value.String(), Start: start, End: min(index, len(text))})
	}
	return found
}

// Values is each word's value.
func Values(text string) []string {
	list := Words(text)
	values := make([]string, len(list))
	for i, word := range list {
		values[i] = word.Value
	}
	return values
}

// Basename is a program's name without its directory.
func Basename(program string) string {
	return program[strings.LastIndexByte(program, '/')+1:]
}

// wrapped is the index of the command a wrapper runs, or -1 when it runs none.
func wrapped(list []Word, w wrapper) int {
	index, positionals := 1, w.positionals
	for index < len(list) {
		value := list[index].Value
		switch {
		case value == "--":
			if index+1 < len(list) {
				return index + 1
			}
			return -1
		case slices.Contains(w.lookups, value):
			return -1
		case strings.HasPrefix(value, "-") && value != "-":
			if slices.Contains(w.values, value) {
				index += 2
			} else {
				index++
			}
		case w.assignments && assignment.MatchString(value):
			index++
		case positionals > 0:
			positionals--
			index++
		default:
			return index
		}
	}
	return -1
}

func harmless(raw, value string) bool {
	if slices.Contains(harmlessRaw, raw) || slices.Contains(harmlessValues, value) {
		return true
	}
	name, rest, found := strings.Cut(raw, "=")
	return found && slices.Contains(safeNames, name) && plainValue.MatchString(rest)
}

// permitted is whether a word before the wrapped command keeps the wrapper
// transparent.
func permitted(value string, options []string) bool {
	switch {
	case strings.ContainsAny(value, wilds): // a run-time value can move the command it runs
		return false
	case value == "--":
		return true
	case !strings.HasPrefix(value, "-"):
		return !assignment.MatchString(value)
	}
	name, _, _ := strings.Cut(value, "=")
	if slices.Contains(options, name) {
		return true
	}
	// A short option with its value joined: `-n1`, `-oL`, `-uNAME`.
	return !strings.HasPrefix(value, "--") && len(value) > 2 && slices.Contains(options, value[:2])
}

// Transparent is the command a transparent wrapper or a harmless assignment
// runs, or false when the command does not start with one or passes the
// wrapper an option outside its table.
func Transparent(command string) (string, bool) {
	text := strings.TrimSpace(command)
	list := Words(text)
	if len(list) == 0 {
		return "", false
	}
	head := list[0]
	raw := text[head.Start:head.End]
	if harmless(raw, head.Value) {
		if len(list) < 2 {
			return "", false
		}
		return strings.TrimSpace(text[list[1].Start:]), true
	}
	if raw != head.Value {
		return "", false
	}
	program := Basename(head.Value)
	options, ok := transparentOptions[program]
	if !ok || !slices.Contains(systemDirs, head.Value[:len(head.Value)-len(program)]) {
		return "", false
	}
	index := wrapped(list, wrappers[program])
	if index == -1 {
		return "", false
	}
	before := list[1:index]
	for _, word := range before {
		if !permitted(word.Value, options) {
			return "", false
		}
	}
	inner := strings.TrimSpace(text[list[index].Start:])
	if program == "xargs" {
		return xargsRuns(before, inner), true
	}
	return inner, true
}

// replacement is the string `xargs` replaces with its input (`-I R`, `-JR`,
// `--replace`), or "" when it appends the input instead.
func replacement(options []Word) string {
	for index, word := range options {
		value := word.Value
		switch {
		case value == "-I" || value == "-J":
			if index+1 < len(options) {
				return options[index+1].Value
			}
			return ""
		case value == "--replace" || value == "-i":
			return "{}"
		case strings.HasPrefix(value, "--replace="):
			return strings.TrimPrefix(value, "--replace=")
		case len(value) > 2 && (strings.HasPrefix(value, "-I") || strings.HasPrefix(value, "-J") || strings.HasPrefix(value, "-i")):
			return value[2:]
		}
	}
	return ""
}

// xargsRuns is the command `xargs` runs with its input marked.
func xargsRuns(options []Word, inner string) string {
	if replaced := replacement(options); replaced != "" {
		return strings.ReplaceAll(inner, replaced, XargsInput)
	}
	return inner + " " + XargsInput
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

// flagReadings are the readings of one unquoted flag a rule can match: a
// bundle of short flags split (`-fu` as `-f -u`), a value detached from its
// flag (`--method=POST`, `-X=POST`, `-n1`, `-XPOST`), and both readings of a
// short bundle whose tail could be a value (`-oL` as `-o -L` and `-o L`).
func flagReadings(raw string) ([2]string, bool) {
	if joined := joinedFlag.FindStringSubmatch(raw); joined != nil {
		reading := joined[1] + " " + joined[2]
		return [2]string{reading, reading}, true
	}
	if !shortFlag.MatchString(raw) || strings.HasPrefix(raw, "--") {
		return [2]string{}, false
	}
	value := raw[:2] + " " + raw[2:]
	valued := upper.MatchString(raw[2:])
	if !letters.MatchString(raw) {
		return [2]string{value, value}, true
	}
	if len(raw)-1 > maxBundle {
		return [2]string{value, value}, valued
	}
	parts := make([]string, 0, len(raw)-1)
	for _, letter := range raw[1:] {
		parts = append(parts, "-"+string(letter))
	}
	split := strings.Join(parts, " ")
	if valued {
		return [2]string{split, value}, true
	}
	return [2]string{split, split}, true
}

// flagSpellings is the command with every flag in each of its readings, when
// any changes.
func flagSpellings(text string, list []Word) []string {
	variants := [2]string{text, text}
	// Right to left, so earlier offsets stay valid.
	for i := len(list) - 1; i >= 1; i-- {
		word := list[i]
		raw := text[word.Start:word.End]
		if raw != word.Value {
			continue
		}
		readings, ok := flagReadings(raw)
		if !ok {
			continue
		}
		for j := range variants {
			variants[j] = variants[j][:word.Start] + readings[j] + variants[j][word.End:]
		}
	}
	var found []string
	for _, variant := range variants {
		if variant != text && !slices.Contains(found, variant) {
			found = append(found, variant)
		}
	}
	return found
}

// peelOnce is the spellings one prefix layer below text.
func peelOnce(text string) []string {
	list := Words(text)
	if len(list) == 0 {
		return nil
	}
	head := list[0]
	raw := text[head.Start:head.End]
	if redirect := redirectPrefix.FindStringSubmatch(raw); redirect != nil {
		next := 2
		if redirect[1] != "" {
			next = 1
		}
		if next < len(list) {
			return []string{strings.TrimSpace(text[list[next].Start:])}
		}
		return nil
	}
	if assignment.MatchString(head.Value) {
		if len(list) > 1 {
			return []string{strings.TrimSpace(text[list[1].Start:])}
		}
		return nil
	}
	var found []string
	// `/bin/rm`, `\rm`, and `"rm"` all run `rm`.
	program := Basename(head.Value)
	if program != "" && program != raw {
		found = append(found, program+text[head.End:])
	}
	if w, ok := wrappers[program]; ok {
		if index := wrapped(list, w); index != -1 {
			inner := text[list[index].Start:]
			found = append(found, inner)
			if program == "xargs" {
				found = append(found, xargsRuns(list[1:index], inner))
			}
		}
		// `env -S 'rm -rf x'` runs its split string as the command.
		if program == "env" {
			for i, word := range list {
				if i > 0 && (word.Value == "-S" || word.Value == "--split-string") {
					if i+1 < len(list) {
						payload := list[i+1]
						found = append(found, payload.Value+text[payload.End:])
					}
					break
				}
			}
		}
	} else if program == raw {
		// Rules name the bare program, so only its own spelling needs the flags read.
		found = append(found, flagSpellings(text, list)...)
	}
	var trimmed []string
	for _, spelling := range found {
		if spelling = strings.TrimSpace(spelling); spelling != "" {
			trimmed = append(trimmed, spelling)
		}
	}
	return trimmed
}

// Spellings is every distinct peeled spelling of command, excluding the
// command itself, in the order a depth-first peel finds them.
func Spellings(command string) []string {
	original := strings.TrimSpace(command)
	seen := map[string]bool{original: true}
	var order []string
	pending := []string{original}
	for len(pending) > 0 {
		last := pending[len(pending)-1]
		pending = pending[:len(pending)-1]
		for _, spelling := range peelOnce(last) {
			if !seen[spelling] {
				seen[spelling] = true
				order = append(order, spelling)
				pending = append(pending, spelling)
			}
		}
	}
	return order
}
