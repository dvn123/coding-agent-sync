package unwrap

import (
	"strings"

	"mvdan.cc/sh/v3/syntax"
)

// Words of source text as rules read them, through mvdan/sh: quoting is
// removed, and a part decided at run time keeps its source text (`$x`,
// `$(…)`), so a rule about text still sees it. The zsh analysis has its own
// reading, with run-time parts marked; this one serves the guards, which read
// what a script says rather than what it runs.

// ShellWords are the words of text when it is one simple command, its
// assignments first, or false.
func ShellWords(text string) ([]string, bool) {
	file, err := syntax.NewParser(syntax.Variant(syntax.LangZsh)).Parse(strings.NewReader(text), "")
	if err != nil || len(file.Stmts) != 1 {
		return nil, false
	}
	call, ok := file.Stmts[0].Cmd.(*syntax.CallExpr)
	if !ok {
		return nil, false
	}
	return callWords(call, text), true
}

// callWords are a simple command's assignments and words; see ShellWords.
func callWords(call *syntax.CallExpr, source string) []string {
	var words []string
	for _, assign := range call.Assigns {
		if assign.Value == nil || assign.Array != nil || assign.Index != nil {
			words = append(words, source[assign.Pos().Offset():assign.End().Offset()])
			continue
		}
		op := "="
		if assign.Append {
			op = "+="
		}
		words = append(words, assign.Name.Value+op+wordValue(assign.Value, source))
	}
	for _, arg := range call.Args {
		words = append(words, wordValue(arg, source))
	}
	return words
}

// wordValue is a word with its quoting removed.
func wordValue(word *syntax.Word, source string) string {
	var out strings.Builder
	for _, part := range word.Parts {
		switch part := part.(type) {
		case *syntax.Lit:
			out.WriteString(unescaped(part.Value))
		case *syntax.SglQuoted:
			out.WriteString(singleQuoted(part))
		case *syntax.DblQuoted:
			for _, inner := range part.Parts {
				if lit, ok := inner.(*syntax.Lit); ok {
					out.WriteString(doubleQuoted(lit.Value))
				} else {
					out.WriteString(source[inner.Pos().Offset():inner.End().Offset()])
				}
			}
		default:
			out.WriteString(source[part.Pos().Offset():part.End().Offset()])
		}
	}
	return out.String()
}

// unescaped is unquoted literal text with each backslash escape read as the
// character it escapes, and a backslash before a newline dropped.
func unescaped(text string) string {
	if !strings.Contains(text, `\`) {
		return text
	}
	var out strings.Builder
	for index := 0; index < len(text); index++ {
		if text[index] == '\\' && index+1 < len(text) {
			index++
			if text[index] == '\n' {
				continue
			}
		}
		out.WriteByte(text[index])
	}
	return out.String()
}

// TextWords are text's words, quoting removed, or its blank-split text when
// it is not one simple command.
func TextWords(text string) []string {
	if words, ok := ShellWords(text); ok {
		return words
	}
	return strings.Fields(text)
}

// Peeled is every spelling the guards read in a command's text, peeled, as
// the text a rule reads; its own words come first when quoting changes them.
func Peeled(command string) []string {
	var found []string
	for _, spelling := range spelled(TextWords(command)) {
		if spelling != command {
			found = append(found, spelling)
		}
	}
	return found
}

// singleQuoted is a `'…'` or `$'…'` string's text.
func singleQuoted(part *syntax.SglQuoted) string {
	if part.Dollar {
		return ansiC(part.Value)
	}
	return part.Value
}
