package unwrap

import (
	"regexp"
	"slices"
	"strings"

	"mvdan.cc/sh/v3/syntax"
)

// The commands the deny and ask guards judge. A guard needs every command it
// can find, not a reason to allow, so this reads the script as zsh and as
// bash and keeps the commands either finds, skipping only a command whose
// program is decided at run time. When neither parses it, it judges each
// piece between shell operators.

var (
	operators = regexp.MustCompile("\\$\\(|[;&|\n()`{}]+")
	keywords  = regexp.MustCompile(`^(?:\s*(?:if|then|else|elif|do|while|until|!)\s+)+`)
)

// Scan is every command either dialect finds in script, and whether either
// parsed it.
func Scan(script string) ([]string, bool) {
	var found []string
	parsed := false
	for _, variant := range []syntax.LangVariant{syntax.LangZsh, syntax.LangBash} {
		file, err := syntax.NewParser(syntax.Variant(variant)).Parse(strings.NewReader(script), "")
		if err != nil {
			continue
		}
		parsed = true
		syntax.Walk(file, func(node syntax.Node) bool {
			text, ok := "", false
			switch node := node.(type) {
			case *syntax.CallExpr:
				text, ok = callText(node, script)
			case *syntax.DeclClause:
				// `export`, `local` and the like, whose assignments name
				// variables as a command's words do.
				text, ok = script[node.Pos().Offset():node.End().Offset()], true
			}
			if ok && !slices.Contains(found, text) {
				found = append(found, text)
			}
			return true
		})
	}
	return found, parsed
}

// Guarded is every command for the guards to judge: those Scan finds, or,
// when neither dialect parses the script, each piece between shell operators.
func Guarded(script string) []string {
	if found, parsed := Scan(script); parsed {
		return found
	}
	var found []string
	for _, piece := range operators.Split(script, -1) {
		if piece = strings.TrimSpace(keywords.ReplaceAllString(piece, "")); piece != "" {
			found = append(found, piece)
		}
	}
	return found
}

// callText is one simple command as its rules see it: its assignments and
// words as written, without its redirects. A bare assignment is its own
// text, which only a rule about the script's text, such as a variable it
// names, can match.
func callText(call *syntax.CallExpr, script string) (string, bool) {
	if len(call.Args) > 0 && dynamicWord(call.Args[0]) || len(call.Args) == 0 && len(call.Assigns) == 0 {
		return "", false
	}
	var parts []string
	for _, assign := range call.Assigns {
		parts = append(parts, script[assign.Pos().Offset():assign.End().Offset()])
	}
	for _, arg := range call.Args {
		parts = append(parts, script[arg.Pos().Offset():arg.End().Offset()])
	}
	return strings.Join(parts, " "), true
}

func dynamicWord(word *syntax.Word) bool {
	dynamic := false
	syntax.Walk(word, func(node syntax.Node) bool {
		switch node.(type) {
		case *syntax.ParamExp, *syntax.CmdSubst, *syntax.ProcSubst, *syntax.ArithmExp:
			dynamic = true
		}
		return !dynamic
	})
	return dynamic
}
