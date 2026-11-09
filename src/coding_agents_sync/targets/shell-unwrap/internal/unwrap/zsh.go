package unwrap

import (
	"fmt"
	"maps"
	"os"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"mvdan.cc/sh/v3/syntax"
)

// The simple commands a script runs, as zsh will run them.
//
// An allow must rest on what zsh executes, so this parses with mvdan/sh's
// zsh dialect and turns each simple command into a list of words in which
// quoting is removed and every part zsh decides at run time is Wild. It
// declines, with the reason, anything whose effect it cannot bound: zsh
// flags, indirection, arithmetic, assignments outside a command, functions,
// redirects that write or read files, and syntax it does not model.
//
// It assumes the zsh that Claude Code runs on macOS, which test/oracle.test.ts
// pins: EXTENDED_GLOB, MAGIC_EQUAL_SUBST, KSH_GLOB, BARE_GLOB_QUAL,
// GLOB_SUBST, IGNORE_BRACES, RC_QUOTES and ALL_EXPORT off; EQUALS, NOMATCH and
// INTERACTIVE_COMMENTS on.

// Decline leaves the decision to the host, saying why.
type Decline struct{ Why string }

func (d Decline) Error() string { return d.Why }

func decline(format string, args ...any) { panic(Decline{fmt.Sprintf(format, args...)}) }

// ZshWord is one word of a command; Vanishes when zsh may drop it, as it
// does an empty unquoted `$x`, Splits when it may become several words not
// all sharing its literal start, as an unquoted `$(…)` does, and Globs when
// it may become several words that do, as `src/*` does.
type ZshWord struct {
	Text     string
	Vanishes bool
	Splits   bool
	Globs    bool
}

// Characters zsh and its parser may read differently: controls other than tab
// and newline, and Unicode blanks and format characters.
var unsafeRanges = [][2]rune{
	{0x0, 0x8}, {0xb, 0x1f}, {0x7f, 0xa0}, {0x1680, 0x1680}, {0x2000, 0x200f}, {0x2028, 0x202f},
	{0x205f, 0x2064}, {0x3000, 0x3000}, {0xfeff, 0xfeff},
}

// Expansion operators whose result is only text: defaults, alternates, and
// pattern removal. Assigning (`:=`) and erroring (`:?`) forms are left out.
var textOperators = []string{":-", "-", ":+", "+", "#", "##", "%", "%%"}

// Lower-case parameters the shell itself reads or sets: zsh 5.9's special
// ones (`${(k)parameters[(R)*special*]}` with its modules loaded), those
// pattern matching and `read` set, and the proxy variables tools read.
var zshSpecial = []string{
	"aliases", "argv", "builtins", "cdpath", "commands", "dirstack", "dis_aliases", "dis_builtins",
	"dis_functions", "dis_functions_source", "dis_galiases", "dis_patchars", "dis_reswords", "dis_saliases",
	"epochtime", "errnos", "fignore", "fpath", "funcfiletrace", "funcsourcetrace", "funcstack", "functions",
	"functions_source", "functrace", "galiases", "histchars", "history", "historywords", "jobdirs", "jobstates",
	"jobtexts", "keymaps", "langinfo", "mailpath", "manpath", "mapfile", "module_path", "modules", "nameddirs",
	"options", "parameters", "patchars", "path", "pipestatus", "prompt", "psvar", "reswords", "saliases",
	"signals", "status", "sysparams", "termcap", "terminfo", "userdirs", "usergroups", "watch", "widgets",
	"zsh_eval_context", "zsh_scheduled_events",
	"match", "mbegin", "mend", "reply",
	"all_proxy", "ftp_proxy", "http_proxy", "https_proxy", "no_proxy",
}

var (
	variableName = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	// Brace expansion turns one word into several.
	braceList = regexp.MustCompile(`(^|[^\\])\{[^{}]*(,|\.\.)[^{}]*\}`)
	digits    = regexp.MustCompile(`^\d+$`)
	dupTarget = regexp.MustCompile(`^(\d+|-)$`)
)

// Assigned declines a variable an allowed command may not set: one the shell
// reads, an exported one, or one a program reads from the environment.
func Assigned(name string) {
	if !variableName.MatchString(name) || name == "_" || slices.Contains(zshSpecial, name) || shellReads.MatchString(name) {
		decline("assignment to %s", name)
	}
	if _, exported := os.LookupEnv(name); exported {
		decline("assignment to exported %s", name)
	}
}

// Upper-case parameters zsh reads or sets, whether or not it marks them
// special (`TMPPREFIX` places here-document files, `NULLCMD` runs for a bare
// redirect), from zshparam(1).
var shellReads = regexp.MustCompile(`^(?:[!#$*?@0-9-]|ARGC|ARGV0|BAUD|CDPATH|COLUMNS|CORRECT_IGNORE(?:_FILE)?|` +
	`DIRSTACKSIZE|EGID|ENV|EPOCH[A-Z]*|ERRNO|EUID|FCEDIT|FIGNORE|FPATH|FUNCNEST|GID|HIST[A-Z_]*|HOME|HOST|IFS|` +
	`KEYBOARD_HACK|KEYTIMEOUT|LANG|LANGUAGE|LC_[A-Z]*|LINENO|LINES|LISTMAX|LOGCHECK|LOGNAME|MACHTYPE|MAIL[A-Z]*|` +
	`MANPATH|MATCH|MBEGIN|MEND|MODULE_PATH|NULLCMD|OLDPWD|OPTARG|OPTIND|OSTYPE|PATH|PERIOD|POSTEDIT|PPID|PROMPT[A-Z0-9_]*|` +
	`PS[1-4]|PSVAR|PWD|RANDOM|READNULLCMD|REPLY|REPORT[A-Z]*|RPROMPT[0-9]*|RPS[12]|SAVEHIST|SECONDS|SHELL|SHLVL|` +
	`SPROMPT|STTY|TERM[A-Z_]*|TIMEFMT|TMOUT|TMPPREFIX|TRY_BLOCK_[A-Z]*|TTY[A-Z]*|UID|USER|USERNAME|VENDOR|WATCH[A-Z]*|` +
	`WORDCHARS|ZBEEP|ZDOTDIR|ZLE_[A-Z_]*|ZSH_[A-Z_]*)$`)

// Step is one thing a script does, in order: a simple command, or a file a
// redirect reads or writes. Conditional marks one that may be skipped or
// runs in a subshell.
type Step struct {
	Words       []ZshWord
	Access      *Access
	Conditional bool
}

// Access is a file a redirect reads or writes.
type Access struct {
	Path        string
	Read, Write bool
}

type analysis struct {
	steps []Step
	// sites counts the places that may set each variable: loop headers,
	// `read`, `printf -v` and `print -v`.
	sites map[string]int
	// bound holds the value of each loop variable while its body is judged.
	bound map[string]string
	// passes is how many times loop bodies are judged, at most maxPasses.
	passes int
	// conditional counts the enclosing constructs that may skip a statement
	// or run it in a subshell, after which an assignment it makes may not
	// hold.
	conditional int
}

// inside judges walk one level deeper in conditional.
func (a *analysis) inside(walk func()) {
	a.conditional++
	walk()
	a.conditional--
}

// A loop over literal words is judged once per word; past this many bodies
// its variable reads as Wild instead.
const maxPasses = 64

// countSites records every place in file that may set a variable.
func countSites(file *syntax.File) map[string]int {
	sites := map[string]int{}
	literal := func(word *syntax.Word) string {
		if word == nil {
			return ""
		}
		return word.Lit()
	}
	syntax.Walk(file, func(node syntax.Node) bool {
		switch node := node.(type) {
		case *syntax.ForClause:
			if loop, ok := node.Loop.(*syntax.WordIter); ok {
				sites[loop.Name.Value]++
			}
		case *syntax.CallExpr:
			if len(node.Args) == 0 {
				for _, assign := range node.Assigns {
					sites[assign.Name.Value]++
				}
				return true
			}
			program := Basename(literal(node.Args[0]))
			for index, arg := range node.Args[1:] {
				value := literal(arg)
				switch {
				case value == "" && arg != nil:
					// A run-time word may name anything; such commands are
					// declined, so nothing is lost by not binding.
				case program == "read" && !strings.HasPrefix(value, "-"):
					name, _, _ := strings.Cut(value, "?")
					sites[name]++
				case (program == "printf" || program == "print") && strings.HasPrefix(value, "-v"):
					name := strings.TrimPrefix(value, "-v")
					if name == "" && index+2 < len(node.Args) {
						name = literal(node.Args[index+2])
					}
					sites[name]++
				}
			}
		}
		return true
	})
	return sites
}

// literalItems are a loop's items when every one is a single literal word.
func (a *analysis) literalItems(items []*syntax.Word) ([]string, bool) {
	var values []string
	for _, item := range items {
		word := a.word(item, false)
		if word.Vanishes || strings.Contains(word.Text, Wild) {
			return nil, false
		}
		values = append(values, word.Text)
	}
	return values, true
}

func (a *analysis) stmts(list []*syntax.Stmt) {
	for _, stmt := range list {
		a.stmt(stmt)
	}
}

func (a *analysis) stmt(stmt *syntax.Stmt) {
	if stmt.Coprocess {
		decline("coprocess")
	}
	for _, redirect := range stmt.Redirs {
		a.redirect(redirect)
	}
	if stmt.Cmd == nil {
		return
	}
	if stmt.Background || stmt.Disown {
		a.inside(func() { a.command(stmt.Cmd) })
		return
	}
	a.command(stmt.Cmd)
}

// Targets that are streams rather than files.
var streams = regexp.MustCompile(`^/dev/(?:null|stdin|stdout|stderr|tty|fd/\d+)$`)

func (a *analysis) redirect(redirect *syntax.Redirect) {
	if redirect.N != nil && !digits.MatchString(redirect.N.Value) {
		decline("redirect that names a variable")
	}
	op := redirect.Op.String()
	switch op {
	case "<<", "<<-":
		if redirect.Hdoc != nil {
			a.word(redirect.Hdoc, true)
		}
		return
	case "<<<":
		a.word(redirect.Word, true)
		return
	}
	// zsh globs and tilde-expands a target as it does an argument.
	target := a.word(redirect.Word, false).Text
	if (op == ">&" || op == "<&") && dupTarget.MatchString(target) || streams.MatchString(target) {
		return
	}
	access := &Access{Path: target, Read: op == "<" || op == "<>" || op == "<&", Write: op != "<" && op != "<&"}
	a.steps = append(a.steps, Step{Access: access, Conditional: a.conditional > 0})
}

func (a *analysis) command(node syntax.Command) {
	switch node := node.(type) {
	case *syntax.CallExpr:
		if len(node.Args) == 0 {
			a.assign(node.Assigns)
			return
		}
		a.steps = append(a.steps, Step{Words: a.call(node), Conditional: a.conditional > 0})
	case *syntax.BinaryCmd:
		// The left of `&&` always runs; anything else may be skipped, and a
		// pipeline runs its parts in subshells.
		if node.Op.String() == "&&" {
			a.stmt(node.X)
		} else {
			a.inside(func() { a.stmt(node.X) })
		}
		a.inside(func() { a.stmt(node.Y) })
	case *syntax.Block:
		a.stmts(node.Stmts)
	case *syntax.Subshell:
		a.inside(func() { a.stmts(node.Stmts) })
	case *syntax.IfClause:
		a.inside(func() {
			for clause := node; clause != nil; clause = clause.Else {
				a.stmts(clause.Cond)
				a.stmts(clause.Then)
			}
		})
	case *syntax.WhileClause:
		a.inside(func() {
			a.stmts(node.Cond)
			a.stmts(node.Do)
		})
	case *syntax.ForClause:
		a.inside(func() { a.loop(node) })
	case *syntax.CaseClause:
		a.word(node.Word, true)
		a.inside(func() {
			for _, item := range node.Items {
				for _, pattern := range item.Patterns {
					a.word(pattern, false)
				}
				a.stmts(item.Stmts)
			}
		})
	case *syntax.DeclClause:
		a.export(node)
	case *syntax.TestClause:
		a.test(node.X)
	case *syntax.ArithmCmd:
		a.arithmetic(node.X)
	case *syntax.TimeClause:
		if node.Stmt != nil {
			a.stmt(node.Stmt)
		}
	default:
		decline("%T", node)
	}
}

// export judges an `export` that only sets harmless variables to literal
// values (`export GIT_OPTIONAL_LOCKS=0`): every later command inherits them,
// so each must be one an allowed command may carry, as a leading assignment
// would. Any other declaration changes what later commands run.
func (a *analysis) export(node *syntax.DeclClause) {
	if node.Variant.Value != "export" || len(node.Args) == 0 {
		decline("%s declaration", node.Variant.Value)
	}
	for _, assign := range node.Args {
		if assign.Name == nil || assign.Naked || assign.Append || assign.Array != nil || assign.Index != nil || assign.Value == nil {
			decline("export of a run-time or bare name")
		}
		value := a.word(assign.Value, true).Text
		pair := assign.Name.Value + "=" + value
		if !slices.Contains(harmlessValues, pair) && !(slices.Contains(safeNames, assign.Name.Value) && plainValue.MatchString(value)) {
			decline("export of %s", assign.Name.Value)
		}
	}
}

// loop judges a for loop's body, once per item when its variable can hold
// a known value.
func (a *analysis) loop(node *syntax.ForClause) {
	loop, ok := node.Loop.(*syntax.WordIter)
	if !ok {
		decline("arithmetic loop")
	}
	name := loop.Name.Value
	Assigned(name)
	values, literal := a.literalItems(loop.Items)
	// The variable keeps one known value per pass only when nothing else
	// sets it and zsh, not the user, picks the values (`select` asks).
	if !literal || node.Select || a.sites[name] != 1 || a.passes+len(values) > maxPasses {
		a.stmts(node.Do)
		return
	}
	outer := a.bound
	for _, value := range values {
		a.passes++
		a.bound = maps.Clone(outer)
		a.bound[name] = value
		a.stmts(node.Do)
	}
	a.bound = outer
}

// assign judges a statement that only sets variables. Each must be one an
// allowed command may set; it holds a known value afterwards when the
// statement always runs, its value is literal, and nothing else sets it.
func (a *analysis) assign(assigns []*syntax.Assign) {
	for _, assign := range assigns {
		if assign.Array != nil || assign.Index != nil || assign.Naked || assign.Append {
			decline("array or appending assignment")
		}
		name := assign.Name.Value
		Assigned(name)
		value := ""
		if assign.Value != nil {
			value = a.assignedValue(assign.Value)
		}
		if a.conditional == 0 && a.sites[name] == 1 && !strings.Contains(value, Wild) {
			a.bound[name] = value
		}
	}
}

// assignedValue is an assignment's value: no splitting or globbing, but a
// leading `~/` names the home directory, and any other `~` reads as Wild.
func (a *analysis) assignedValue(word *syntax.Word) string {
	text := a.word(word, true).Text
	if len(word.Parts) > 0 {
		if lit, ok := word.Parts[0].(*syntax.Lit); ok && strings.HasPrefix(lit.Value, "~") {
			rest := strings.TrimPrefix(text, "~")
			if rest != "" && !strings.HasPrefix(rest, "/") {
				return Wild
			}
			home, _ := os.UserHomeDir()
			text = home + rest
		}
	}
	for _, part := range word.Parts {
		if lit, ok := part.(*syntax.Lit); ok && strings.Contains(lit.Value, ":~") {
			return Wild
		}
	}
	return text
}

func (a *analysis) test(node syntax.TestExpr) {
	switch node := node.(type) {
	case *syntax.Word:
		a.word(node, true)
	case *syntax.BinaryTest:
		a.test(node.X)
		a.test(node.Y)
	case *syntax.UnaryTest:
		a.test(node.X)
	case *syntax.ParenTest:
		a.test(node.X)
	}
}

func (a *analysis) call(node *syntax.CallExpr) []ZshWord {
	if len(node.Args) == 0 {
		decline("shell variable assignment")
	}
	var list []ZshWord
	for _, assign := range node.Assigns {
		if assign.Array != nil || assign.Index != nil || assign.Naked {
			decline("array assignment")
		}
		value := ""
		if assign.Value != nil {
			value = a.word(assign.Value, true).Text
		}
		op := "="
		if assign.Append {
			op = "+="
		}
		list = append(list, ZshWord{Text: assign.Name.Value + op + value})
	}
	for _, arg := range node.Args {
		list = append(list, a.word(arg, false))
	}
	program := list[len(node.Assigns)]
	if program.Vanishes || strings.Contains(program.Text, Wild) {
		decline("dynamic program")
	}
	return list
}

type text struct {
	strings.Builder
	literal, dynamic, splits, globs bool
}

// word is one word's text as zsh passes it, with Wild for what it decides at
// run time. quoted holds for a context that never splits or drops the word.
func (a *analysis) word(node *syntax.Word, quoted bool) ZshWord {
	var skeleton strings.Builder
	for _, part := range node.Parts {
		if lit, ok := part.(*syntax.Lit); ok {
			skeleton.WriteString(lit.Value)
		} else {
			skeleton.WriteString("x")
		}
	}
	out := &text{}
	for index, part := range node.Parts {
		a.part(part, index == 0, quoted, out)
	}
	// Its parts are walked for commands even when braces make it Wild.
	if !quoted && braceList.MatchString(skeleton.String()) {
		return ZshWord{Text: Wild, Vanishes: true}
	}
	return ZshWord{Text: out.String(), Vanishes: !quoted && out.dynamic && !out.literal, Splits: out.splits, Globs: out.globs}
}

func (a *analysis) part(part syntax.WordPart, first, quoted bool, out *text) {
	switch part := part.(type) {
	case *syntax.Lit:
		if quoted {
			out.WriteString(part.Value)
		} else {
			text := unquoted(part.Value, first)
			out.WriteString(text)
			// Glob characters, EQUALS expansion, or `~user`.
			out.globs = out.globs || strings.Contains(text, Wild)
		}
		out.literal = out.literal || part.Value != ""
	case *syntax.SglQuoted:
		if part.Dollar {
			out.WriteString(ansiC(part.Value))
		} else {
			out.WriteString(part.Value)
		}
		out.literal = true
	case *syntax.DblQuoted:
		if part.Dollar {
			decline(`$"" string`)
		}
		out.literal = true
		for _, inner := range part.Parts {
			if lit, ok := inner.(*syntax.Lit); ok {
				out.WriteString(doubleQuoted(lit.Value))
			} else {
				a.part(inner, false, true, out)
			}
		}
	case *syntax.ParamExp:
		if value, ok := a.bound[plainName(part)]; ok {
			// Empty, it vanishes like any empty unquoted parameter.
			out.WriteString(value)
			out.literal = out.literal || value != ""
			out.dynamic = out.dynamic || value == ""
			return
		}
		if value, ok := constant(part); ok {
			out.WriteString(value)
			out.literal = true
			return
		}
		a.parameter(part)
		out.WriteString(Wild)
		out.dynamic = true
		// zsh splits a plain parameter only under SH_WORD_SPLIT, off by
		// default as this analysis assumes throughout; any other form may
		// hold a `$(…)` or a flag that splits.
		out.splits = out.splits || !quoted && plainName(part) == ""
	case *syntax.CmdSubst:
		if part.TempFile || part.ReplyVar {
			decline("mksh substitution")
		}
		a.inside(func() { a.stmts(part.Stmts) })
		if path, ok := mktempPath(part.Stmts); ok {
			out.WriteString(path)
			out.literal = true
			return
		}
		out.WriteString(Wild)
		out.dynamic = true
		out.splits = out.splits || !quoted
	case *syntax.ProcSubst:
		a.inside(func() { a.stmts(part.Stmts) })
		out.WriteString(Wild)
		out.literal = true
	case *syntax.ExtGlob:
		out.WriteString(Wild)
		out.literal = true
		out.globs = true
	case *syntax.ArithmExp:
		// A number, so never an empty word.
		a.arithmetic(part.X)
		out.WriteString(Wild)
		out.literal = true
	default:
		decline("%T", part)
	}
}

// Variables whose value a command cannot change before zsh expands them: the
// shell inherits them from the process that also runs this hook, and the
// analysis declines every way to assign them (they are exported).
var constants = []string{"HOME", "USER", "LOGNAME", "TMPDIR"}

// plainName is the name a plain `$x` or `${x}` expands, or "".
func plainName(node *syntax.ParamExp) string {
	plain := node.Flags == nil && !node.Excl && !node.Length && !node.Width && !node.IsSet && node.Split == 0 &&
		node.GlobSubst == 0 && node.RcExpand == 0 && node.NestedParam == nil && node.Index == nil &&
		len(node.Modifiers) == 0 && node.Slice == nil && node.Repl == nil && node.Names == 0 && node.Exp == nil
	if !plain || node.Param == nil {
		return ""
	}
	return node.Param.Value
}

// constant is the value of a plain `$HOME` or `${HOME}`, when it is one.
func constant(node *syntax.ParamExp) (string, bool) {
	name := plainName(node)
	if !slices.Contains(constants, name) {
		return "", false
	}
	value, ok := os.LookupEnv(name)
	return value, ok && value != ""
}

func (a *analysis) parameter(node *syntax.ParamExp) {
	if node.Flags != nil || node.Excl || node.NestedParam != nil || node.Names != 0 {
		decline("parameter flags or indirection")
	}
	a.arithmetic(node.Index)
	if node.Slice != nil {
		a.arithmetic(node.Slice.Offset)
		a.arithmetic(node.Slice.Length)
	}
	if node.Exp != nil {
		if op := node.Exp.Op.String(); !slices.Contains(textOperators, op) {
			decline("parameter operator %s", op)
		}
		if node.Exp.Word != nil {
			a.word(node.Exp.Word, true)
		}
	}
	if node.Repl != nil {
		for _, word := range []*syntax.Word{node.Repl.Orig, node.Repl.With} {
			if word != nil {
				a.word(word, true)
			}
		}
	}
}

// unquoted is an unquoted literal's text: backslashes escape, glob characters
// become Wild, a leading `~` names the home directory, and a leading `=` is
// EQUALS expansion, which reads as Wild: zsh replaces the word with a
// command's path, or stops the command when there is none.
func unquoted(value string, first bool) string {
	var out strings.Builder
	index := 0
	if first && strings.HasPrefix(value, "=") && len(value) > 1 {
		return Wild
	}
	if first && strings.HasPrefix(value, "~") {
		slash := strings.IndexByte(value, '/')
		end := len(value)
		if slash != -1 {
			end = slash
		}
		user := value[1:end]
		if strings.HasPrefix(user, "[") {
			decline("dynamic named directory")
		}
		if user == "" {
			home, _ := os.UserHomeDir()
			out.WriteString(home)
		} else {
			out.WriteString(Wild)
		}
		index = end
	}
	for index < len(value) {
		char := value[index]
		switch {
		case char == '\\' && index+1 < len(value):
			out.WriteByte(value[index+1])
			index += 2
		case strings.IndexByte("*?[]()|<>", char) != -1:
			if !strings.HasSuffix(out.String(), Wild) {
				out.WriteString(Wild)
			}
			index++
		default:
			out.WriteByte(char)
			index++
		}
	}
	return out.String()
}

var doubleQuotedEscape = regexp.MustCompile("\\\\([$`\"\\\\\n])")

// doubleQuoted is a double-quoted literal's text: a backslash escapes only
// `$`, a backquote, `"`, `\` and a newline.
func doubleQuoted(value string) string {
	return doubleQuotedEscape.ReplaceAllStringFunc(value, func(escape string) string {
		if escape[1] == '\n' {
			return ""
		}
		return escape[1:]
	})
}

var (
	ansiEscape = regexp.MustCompile(`(?s)\\(x[0-9A-Fa-f]{1,2}|[0-7]{1,3}|.)`)
	escapes    = map[string]string{
		"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v",
		`\`: `\`, "'": "'", `"`: `"`, "?": "?",
	}
	octal = regexp.MustCompile(`^[0-7]+$`)
)

// ansiC is a `$'…'` string's text; an escape it does not know reads as Wild.
func ansiC(value string) string {
	return ansiEscape.ReplaceAllStringFunc(value, func(match string) string {
		escape := match[1:]
		if text, ok := escapes[escape]; ok {
			return text
		}
		if strings.HasPrefix(escape, "x") && len(escape) > 1 {
			code, _ := strconv.ParseUint(escape[1:], 16, 8)
			return string(rune(code))
		}
		if octal.MatchString(escape) {
			code, _ := strconv.ParseUint(escape, 8, 8)
			return string(rune(code))
		}
		return Wild
	})
}

// Steps is every simple command script runs and every file its redirects
// read or write, in order, or the reason it declines.
func Steps(script string) (found []Step, err error) {
	for _, char := range script {
		for _, span := range unsafeRanges {
			if char >= span[0] && char <= span[1] {
				return nil, Decline{"control or Unicode blank characters"}
			}
		}
	}
	file, err := syntax.NewParser(syntax.Variant(syntax.LangZsh)).Parse(strings.NewReader(script), "")
	if err != nil {
		return nil, Decline{"unparseable as zsh"}
	}
	defer func() {
		if recovered := recover(); recovered != nil {
			declined, ok := recovered.(Decline)
			if !ok {
				panic(recovered)
			}
			found, err = nil, declined
		}
	}()
	a := &analysis{sites: countSites(file), bound: map[string]string{}}
	a.stmts(file.Stmts)
	return a.steps, nil
}

var numberLiteral = regexp.MustCompile(`^(?:0[xX][0-9A-Fa-f]+|[0-9]+(?:#[0-9A-Za-z]+)?|[0-9]*\.[0-9]+)$`)

// arithmetic declines an expression zsh's math could run anything for. zsh
// evaluates a variable's value as an expression too, so `$((x))` with x set
// to `PATH=5` assigns PATH: every operand must be a number, or a variable
// bound to one, and no operator may assign.
func (a *analysis) arithmetic(expr syntax.ArithmExpr) {
	switch expr := expr.(type) {
	case nil:
	case *syntax.Word:
		value, ok := a.numeric(expr)
		if !ok || !numberLiteral.MatchString(value) {
			decline("arithmetic over a value not known to be a number")
		}
	case *syntax.BinaryArithm:
		if op := expr.Op.String(); strings.HasSuffix(op, "=") && !slices.Contains([]string{"==", "!=", "<=", ">="}, op) {
			decline("arithmetic assignment")
		}
		a.arithmetic(expr.X)
		a.arithmetic(expr.Y)
	case *syntax.UnaryArithm:
		if op := expr.Op.String(); op == "++" || op == "--" {
			decline("arithmetic assignment")
		}
		a.arithmetic(expr.X)
	case *syntax.ParenArithm:
		a.arithmetic(expr.X)
	default:
		decline("arithmetic %T", expr)
	}
}

// numeric is an arithmetic operand's text: a literal, a variable name bound
// to a value, or a plain `$x` bound to one.
func (a *analysis) numeric(word *syntax.Word) (string, bool) {
	if literal := word.Lit(); literal != "" {
		if variableName.MatchString(literal) {
			value, ok := a.bound[literal]
			return value, ok
		}
		return literal, true
	}
	if len(word.Parts) == 1 {
		if param, ok := word.Parts[0].(*syntax.ParamExp); ok {
			value, ok := a.bound[plainName(param)]
			return value, ok
		}
	}
	return "", false
}
