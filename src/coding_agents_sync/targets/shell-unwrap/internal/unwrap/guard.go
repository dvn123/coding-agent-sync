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

// Scan is the words of every command either dialect finds in script, and
// whether either parsed it.
func Scan(script string) ([][]string, bool) {
	var found [][]string
	parsed := false
	add := func(words []string) {
		if !slices.ContainsFunc(found, func(other []string) bool { return slices.Equal(other, words) }) {
			found = append(found, words)
		}
	}
	for _, variant := range []syntax.LangVariant{syntax.LangZsh, syntax.LangBash} {
		file, err := syntax.NewParser(syntax.Variant(variant)).Parse(strings.NewReader(script), "")
		if err != nil {
			continue
		}
		parsed = true
		syntax.Walk(file, func(node syntax.Node) bool {
			switch node := node.(type) {
			case *syntax.CallExpr:
				if len(node.Args) > 0 && !dynamicWord(node.Args[0]) || len(node.Args) == 0 && len(node.Assigns) > 0 {
					// A bare assignment is its own command, which only a
					// rule about the script's text can match.
					add(callWords(node, script))
				}
			case *syntax.DeclClause:
				// `export`, `local` and the like, whose assignments name
				// variables as a command's words do.
				add(strings.Fields(script[node.Pos().Offset():node.End().Offset()]))
			}
			return true
		})
	}
	return found, parsed
}

// Guarded is the words of every command for the guards to judge: those Scan
// finds, or, when neither dialect parses the script, each piece between
// shell operators.
func Guarded(script string) [][]string {
	if found, parsed := Scan(script); parsed {
		return found
	}
	var found [][]string
	for _, piece := range operators.Split(script, -1) {
		if piece = strings.TrimSpace(keywords.ReplaceAllString(piece, "")); piece == "" {
			continue
		}
		found = append(found, TextWords(piece))
	}
	return found
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
