package unwrap

import (
	"os"
	"path/filepath"
	"slices"
	"strings"

	"mvdan.cc/sh/v3/syntax"
)

// moves follows a command that changes the shell's directory: a `cd` to a
// literal directory that always runs and exists now moves it there, and any
// other leaves it unknown, since a failed cd stays where it was.
func (a *analysis) moves(words []ZshWord) {
	if len(words) == 0 || !slices.Contains([]string{"cd", "chdir", "pushd", "popd"}, words[0].Text) {
		return
	}
	known := a.dir
	a.dir = ""
	if words[0].Text != "cd" || len(words) != 2 || a.conditional > 0 {
		return
	}
	target := words[1]
	if target.Vanishes || strings.Contains(target.Text, Wild) || strings.HasPrefix(target.Text, "-") || strings.HasPrefix(target.Text, "+") {
		return
	}
	var moved string
	switch {
	case filepath.IsAbs(target.Text):
		moved = real(target.Text)
	case known != "":
		moved = real(filepath.Join(known, target.Text))
	}
	if info, err := os.Stat(moved); moved != "" && err == nil && info.IsDir() {
		a.dir = moved
	}
}

// globItems are a loop's items when each is a single literal word or a plain
// glob that names at least one file now, expanded as zsh would; see expand.
// The names are the files at the time the hook runs, the race every judged
// path already has.
func (a *analysis) globItems(items []*syntax.Word) ([]string, bool) {
	var values []string
	for _, item := range items {
		word := a.word(item, false)
		if !word.Vanishes && !strings.Contains(word.Text, Wild) {
			values = append(values, word.Text)
			continue
		}
		pattern, ok := a.globPattern(item)
		if !ok {
			return nil, false
		}
		names, ok := expand(pattern, a.dir)
		if !ok {
			return nil, false
		}
		values = append(values, names...)
	}
	return values, true
}

// globPattern is a word that only `*`, `?` and `[…]` in its unquoted
// literal parts make a glob, as a pattern for filepath.Glob: a leading `~/`
// reads as the home directory, and quoted parts (`"$PWD"/*`) match as they
// read, so each must hold no run-time text. Recursive `**`, negated classes,
// and anything zsh's options or qualifiers may read are left out.
func (a *analysis) globPattern(item *syntax.Word) (string, bool) {
	var pattern strings.Builder
	globs := false
	for index, part := range item.Parts {
		switch part := part.(type) {
		case *syntax.Lit:
			value := part.Value
			if index == 0 && strings.HasPrefix(value, "~/") {
				home, _ := os.UserHomeDir()
				pattern.WriteString(escapeGlob(home))
				value = value[1:]
			}
			if strings.ContainsAny(value, `\(){}|<>#^~!=`) || strings.Contains(value, "**") {
				return "", false
			}
			globs = globs || strings.ContainsAny(value, "*?[")
			pattern.WriteString(value)
		case *syntax.SglQuoted:
			if part.Dollar {
				return "", false
			}
			pattern.WriteString(escapeGlob(part.Value))
		case *syntax.DblQuoted:
			// Only literals and plain parameters, which a.word has already
			// walked, so nothing here adds a step twice.
			for _, inner := range part.Parts {
				if _, ok := inner.(*syntax.CmdSubst); ok {
					return "", false
				}
			}
			out := &text{}
			a.part(part, index == 0, false, out)
			if strings.ContainsAny(out.String(), wilds) {
				return "", false
			}
			pattern.WriteString(escapeGlob(out.String()))
		default:
			return "", false
		}
	}
	return pattern.String(), globs
}

// escapeGlob is text that filepath.Glob matches only as itself.
func escapeGlob(text string) string {
	return strings.NewReplacer(`\`, `\\`, "*", `\*`, "?", `\?`, "[", `\[`).Replace(text)
}

// expand is what zsh makes of pattern in dir: the matching names, sorted,
// without names that start with `.` unless the pattern spells the dot, and
// only directories, each with its `/`, for a pattern that ends in `/`.
// ok is false when nothing matches (zsh stops with an error, and the files
// may have changed by then), when a relative pattern meets an unknown
// directory, or when a name holds anything but plain characters.
func expand(pattern, dir string) ([]string, bool) {
	directories := strings.HasSuffix(pattern, "/")
	pattern = strings.TrimSuffix(pattern, "/")
	base := ""
	if !filepath.IsAbs(pattern) {
		if dir == "" {
			return nil, false
		}
		base = dir
	}
	matches, err := filepath.Glob(filepath.Join(base, pattern))
	if err != nil || len(matches) == 0 {
		return nil, false
	}
	parts := strings.Split(pattern, "/")
	var names []string
	for _, match := range matches {
		name := match
		if base != "" {
			name, _ = filepath.Rel(base, match)
		}
		if hidden(strings.Split(name, "/"), parts) {
			continue
		}
		if directories {
			if info, err := os.Stat(match); err != nil || !info.IsDir() {
				continue
			}
			name += "/"
		}
		if !plainValue.MatchString(name) || strings.HasPrefix(name, "-") {
			return nil, false
		}
		names = append(names, name)
	}
	if len(names) == 0 {
		return nil, false
	}
	slices.Sort(names)
	return names, true
}

// hidden is whether a name has a component starting with `.` that its
// pattern component does not spell, which zsh's globs skip by default.
func hidden(name, pattern []string) bool {
	if len(name) != len(pattern) {
		return true
	}
	for index, component := range name {
		if strings.HasPrefix(component, ".") && !strings.HasPrefix(pattern[index], ".") {
			return true
		}
	}
	return false
}
