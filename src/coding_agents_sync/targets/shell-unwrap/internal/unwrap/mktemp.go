package unwrap

import (
	"os"
	"path/filepath"
	"strings"

	"mvdan.cc/sh/v3/syntax"
)

// tempPath is the path a word that is only a `$(mktemp …)`, quoted or not,
// names when mktemp succeeds. When it fails the word is empty, so only a
// caller for which an empty word does no harm reads it as the path: a
// redirect target, which zsh then refuses to open, and a variable assigned
// on the left of an `&&`. Anywhere else the empty word may vanish or leave a
// suffix such as `$HOME/.zshrc` standing alone.
func tempPath(word *syntax.Word) (string, bool) {
	parts := word.Parts
	if len(parts) == 1 {
		if quoted, ok := parts[0].(*syntax.DblQuoted); ok && !quoted.Dollar {
			parts = quoted.Parts
		}
	}
	if len(parts) != 1 {
		return "", false
	}
	subst, ok := parts[0].(*syntax.CmdSubst)
	if !ok || subst.TempFile || subst.ReplyVar {
		return "", false
	}
	return mktempPath(subst.Stmts)
}

// mktempPath is the path a `$(mktemp …)` prints, when the substitution is one
// literal macOS `mktemp` call that names its directory: `mktemp`, `-d`, `-q`,
// `-u`, `-t PREFIX` (in $TMPDIR), or an absolute template. The random
// characters read as zeros: they are letters and digits after a literal
// prefix, so they never decide a rule. A relative template depends on the
// directory, and anything else on the command line, so those stay Wild.
func mktempPath(stmts []*syntax.Stmt) (string, bool) {
	if len(stmts) != 1 {
		return "", false
	}
	stmt := stmts[0]
	call, ok := stmt.Cmd.(*syntax.CallExpr)
	if !ok || stmt.Negated || stmt.Background || stmt.Coprocess || len(stmt.Redirs) > 0 || len(call.Assigns) > 0 {
		return "", false
	}
	args := make([]string, len(call.Args))
	for index, arg := range call.Args {
		if args[index] = arg.Lit(); args[index] == "" {
			return "", false
		}
	}
	if args[0] != "mktemp" {
		return "", false
	}
	prefix, template := "", ""
	for index := 1; index < len(args); index++ {
		arg := args[index]
		if !strings.HasPrefix(arg, "-") {
			if template != "" || !filepath.IsAbs(arg) {
				return "", false
			}
			template = arg
			continue
		}
		for position, letter := range arg[1:] {
			switch {
			case letter == 'd' || letter == 'q' || letter == 'u':
			case letter == 't' && position == len(arg)-2 && index+1 < len(args):
				index++
				prefix = args[index]
			default:
				return "", false
			}
		}
	}
	directory := os.Getenv("TMPDIR")
	if directory == "" {
		directory = "/tmp"
	}
	var path string
	switch {
	case template != "" && prefix != "":
		return "", false
	case template != "":
		trimmed := strings.TrimRight(template, "X")
		path = trimmed + strings.Repeat("0", len(template)-len(trimmed))
	case prefix != "":
		path = filepath.Join(directory, prefix+".00000000")
	default:
		path = filepath.Join(directory, "tmp.00000000")
	}
	if strings.ContainsAny(path, " \t\n") || strings.Contains(prefix, "/") {
		return "", false
	}
	return filepath.Clean(path), true
}
