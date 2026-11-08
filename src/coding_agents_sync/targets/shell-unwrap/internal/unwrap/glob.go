package unwrap

import (
	"regexp"
	"strings"
	"sync"
)

// Claude Code's Bash rule globs, matched against concrete text, and judged
// against command templates.
//
// A template is a command's text with every part the shell decides at run
// time (a parameter, a command substitution, a glob) replaced by Wild, which
// stands for any text: none, one word, or several. A rule may match a
// template when some value of its Wilds makes the rule match, and must match
// it when every value does. A deny or ask guards an allow only if it cannot
// match, and an allow carries only if it must.

var (
	regexes      sync.Map // glob -> *regexp.Regexp
	alternations sync.Map // rule -> []string
)

// Glob compiles a Claude Code Bash glob: `*` matches any text, `:*` reads as
// ` *`, and a trailing ` *` that is the rule's only wildcard also matches the
// bare command.
func Glob(glob string) *regexp.Regexp {
	if cached, ok := regexes.Load(glob); ok {
		return cached.(*regexp.Regexp)
	}
	pattern := glob
	if strings.HasSuffix(pattern, ":*") {
		pattern = strings.TrimSuffix(pattern, ":*") + " *"
	}
	var body string
	if strings.HasSuffix(pattern, " *") && strings.Count(pattern, "*") == 1 {
		body = regexp.QuoteMeta(strings.TrimSuffix(pattern, " *")) + "( .*)?"
	} else {
		parts := strings.Split(pattern, "*")
		for i, part := range parts {
			parts[i] = regexp.QuoteMeta(part)
		}
		body = strings.Join(parts, ".*")
	}
	compiled := regexp.MustCompile(`(?s)^(?:` + body + `)$`)
	regexes.Store(glob, compiled)
	return compiled
}

// alternatives are the plain patterns a rule stands for, with quoting removed
// so they compare with a command's words.
func alternatives(rule string) []string {
	if cached, ok := alternations.Load(rule); ok {
		return cached.([]string)
	}
	pattern := rule
	if strings.HasSuffix(pattern, ":*") {
		pattern = strings.TrimSuffix(pattern, ":*") + " *"
	}
	if strings.ContainsAny(pattern, `'"\`) {
		pattern = strings.Join(Values(pattern), " ")
	}
	found := []string{pattern}
	if strings.HasSuffix(pattern, " *") && strings.Count(pattern, "*") == 1 {
		found = append(found, strings.TrimSuffix(pattern, " *"))
	}
	alternations.Store(rule, found)
	return found
}

// ends are the literal text before the first and after the last of mark.
func ends(text, mark string) (string, string) {
	first, last := strings.Index(text, mark), strings.LastIndex(text, mark)
	if first == -1 {
		return text, text
	}
	return text[:first], text[last+len(mark):]
}

// compatible is whether one of a and b is a prefix (or, with suffix, a
// suffix) of the other.
func compatible(a, b string, suffix bool) bool {
	if len(a) > len(b) {
		a, b = b, a
	}
	if suffix {
		return strings.HasSuffix(b, a)
	}
	return strings.HasPrefix(b, a)
}

// intersects is whether some value of the template's Wilds makes pattern
// match it.
func intersects(pattern, template string) bool {
	// Both match a common text only if their literal ends agree.
	patternHead, patternTail := ends(pattern, "*")
	templateHead, templateTail := ends(template, Wild)
	if !compatible(patternHead, templateHead, false) || !compatible(patternTail, templateTail, true) {
		return false
	}
	m, n := len(pattern), len(template)
	// reach[i][j]: pattern[i:] and template[j:] share a match.
	reach := make([]bool, (m+1)*(n+1))
	at := func(i, j int) bool { return reach[i*(n+1)+j] }
	for i := m; i >= 0; i-- {
		for j := n; j >= 0; j-- {
			var ok bool
			switch {
			case i < m && pattern[i] == '*':
				ok = at(i+1, j) || (j < n && at(i, j+1))
			case j < n && template[j] == Wild[0]:
				ok = at(i, j+1) || (i < m && at(i+1, j))
			case i < m && j < n:
				ok = pattern[i] == template[j] && at(i+1, j+1)
			default:
				ok = i == m && j == n
			}
			reach[i*(n+1)+j] = ok
		}
	}
	return at(0, 0)
}

// covers is whether pattern matches the template for every value of its
// Wilds.
func covers(pattern, template string) bool {
	// The pattern's literal ends must lie in the template's literal ends.
	patternHead, patternTail := ends(pattern, "*")
	templateHead, templateTail := ends(template, Wild)
	if !strings.HasPrefix(templateHead, patternHead) || !strings.HasSuffix(templateTail, patternTail) {
		return false
	}
	m, n := len(template), len(pattern)
	// reach[i][j]: pattern[j:] matches all of template[i:].
	reach := make([]bool, (m+1)*(n+1))
	at := func(i, j int) bool { return reach[i*(n+1)+j] }
	for i := m; i >= 0; i-- {
		for j := n; j >= 0; j-- {
			var ok bool
			switch {
			case j < n && pattern[j] == '*':
				ok = at(i, j+1) || (i < m && at(i+1, j))
			case i < m && j < n:
				ok = template[i] != Wild[0] && template[i] == pattern[j] && at(i+1, j+1)
			default:
				ok = i == m && j == n
			}
			reach[i*(n+1)+j] = ok
		}
	}
	return at(0, 0)
}

// MayMatch is whether some value of the template's Wilds makes rule match.
func MayMatch(rule, template string) bool {
	if !strings.Contains(template, Wild) {
		for _, pattern := range alternatives(rule) {
			if Glob(pattern).MatchString(template) {
				return true
			}
		}
		return false
	}
	for _, pattern := range alternatives(rule) {
		if intersects(pattern, template) {
			return true
		}
	}
	return false
}

// MustMatch is whether rule matches the template for every value of its
// Wilds.
func MustMatch(rule, template string) bool {
	if !strings.Contains(template, Wild) {
		return MayMatch(rule, template)
	}
	for _, pattern := range alternatives(rule) {
		if covers(pattern, template) {
			return true
		}
	}
	return false
}
