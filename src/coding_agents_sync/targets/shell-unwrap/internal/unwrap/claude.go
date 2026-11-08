package unwrap

import (
	"fmt"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
)

// MaxCommand is the length past which Claude Code prompts for a command it
// cannot fully analyse.
const MaxCommand = 10_000

// Allowed is the reason an allow carries.
const Allowed = "shell-unwrap: every command matches an allow rule"

// Decision is a PreToolUse decision.
type Decision struct {
	Decision string `json:"permissionDecision"`
	Reason   string `json:"permissionDecisionReason"`
}

// Commands whose arguments run, write, or delete by themselves, which Claude
// Code prompts for even under a matching allow.
var (
	findActions  = []string{"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls", "-files0-from"}
	fdActions    = []string{"-x", "-X", "--exec", "--exec-batch"}
	neverAllowed = []string{"tee", "watch", "setsid", "ionice", "flock", "pushd"}
)

// Builtins that change what later commands in the same shell run, which a
// rule about one command cannot judge. `cd` is judged by its target instead.
var stateful = []string{
	"alias", "autoload", "bindkey", "cd", "chdir", "declare", "disable", "emulate", "enable", "eval", "exec",
	"export", "fc", "float", "functions", "getopts", "hash", "integer", "let", "limit", "local", "popd", "pushd",
	"r", "readonly", "rehash", "sched", "setopt", "trap", "typeset", "ulimit", "umask", "unalias", "unfunction",
	"unhash", "unset", "unsetopt", "vared", "zformat", "zle", "zmodload", "zparseopts", "zregexparse", "zstat",
	"zstyle",
}

var (
	setFlags  = regexp.MustCompile(`^[-+][euvx]+$`)
	setOption = regexp.MustCompile(`^[-+][euvx]*o$`)
	setNames  = []string{"errexit", "nounset", "pipefail", "verbose", "xtrace"}
	bundledV  = regexp.MustCompile(`^-[A-Za-z]*v`)
	bundledP  = regexp.MustCompile(`^-[A-Za-z]*P`)
	quoting   = regexp.MustCompile(`[\s'"\\]`)
)

// A command with more words zsh may drop is judged in too many shapes.
const maxVanishing = 6

// runsItself is whether the program runs, writes, or deletes by its own
// arguments in a way Claude Code prompts for even under a matching allow.
func runsItself(spelling string) bool {
	values := Values(spelling)
	if len(values) == 0 {
		return false
	}
	name := Basename(values[0])
	if slices.Contains(neverAllowed, name) {
		return true
	}
	actions := map[string][]string{"find": findActions, "fd": fdActions}[name]
	for _, argument := range values[1:] {
		option, _, _ := strings.Cut(argument, "=")
		if slices.Contains(actions, option) {
			return true
		}
	}
	return false
}

// templates are what a peeled spelling stands for: its words joined, with
// the input `xargs` adds read as any arguments or none.
func templates(spelling string) []string {
	values := Values(spelling)
	wild := func(list []string) string {
		out := make([]string, len(list))
		for i, value := range list {
			out[i] = strings.ReplaceAll(value, XargsInput, Wild)
		}
		return strings.Join(out, " ")
	}
	kept := wild(values)
	dropped := wild(slices.DeleteFunc(slices.Clone(values), func(value string) bool { return value == XargsInput }))
	if dropped == kept {
		return []string{kept}
	}
	return []string{kept, dropped}
}

// render spells words so Words reads them back unchanged.
func render(values []string) string {
	out := make([]string, len(values))
	for i, value := range values {
		switch {
		case value == "":
			out[i] = "''"
		case quoting.MatchString(value):
			out[i] = "'" + strings.ReplaceAll(value, "'", `'\''`) + "'"
		default:
			out[i] = value
		}
	}
	return strings.Join(out, " ")
}

// shapes are the word lists a command stands for, with each word zsh may drop
// kept and dropped.
func shapes(list []ZshWord) ([][]string, bool) {
	var vanishing []int
	for i, word := range list {
		if word.Vanishes {
			vanishing = append(vanishing, i)
		}
	}
	if len(vanishing) > maxVanishing {
		return nil, false
	}
	var found [][]string
	for mask := 0; mask < 1<<len(vanishing); mask++ {
		var values []string
		for i, word := range list {
			bit := slices.Index(vanishing, i)
			if bit == -1 || mask&(1<<bit) == 0 {
				values = append(values, word.Text)
			}
		}
		found = append(found, values)
	}
	return found, true
}

// setsVariables are the variables `read`, `printf -v`, or `print -v` sets.
func setsVariables(program string, args []string) []string {
	// A run-time word may be an option such as `-v`, up to the first word
	// that is plainly not one; every word `read` takes may be a name.
	for _, arg := range args {
		if strings.Contains(arg, Wild) {
			decline("run-time option or name for %s", program)
		}
		if program != "read" && (arg == "--" || !strings.HasPrefix(arg, "-")) {
			break
		}
	}
	var names []string
	if program == "read" {
		for _, arg := range args {
			if !strings.HasPrefix(arg, "-") && !digits.MatchString(arg) {
				name, _, _ := strings.Cut(arg, "?")
				names = append(names, name)
			}
		}
		return names
	}
	for index := 0; index < len(args); index++ {
		arg := args[index]
		if arg == "--" || !strings.HasPrefix(arg, "-") {
			break
		}
		switch {
		case program == "print" && bundledP.MatchString(arg):
			decline("print -P runs $(...) through prompt substitution")
		case arg == "-v":
			index++
			if index < len(args) {
				names = append(names, args[index])
			} else {
				names = append(names, "")
			}
		case bundledV.MatchString(arg):
			decline("bundled -v")
		}
	}
	return names
}

// plainSet is whether `set` changes only error handling, tracing, or the
// positional parameters.
func plainSet(args []string) bool {
	for index := 0; index < len(args); index++ {
		arg := args[index]
		switch {
		case arg == "--" || arg == "-" || !strings.HasPrefix(arg, "-") && !strings.HasPrefix(arg, "+"):
			return true
		case setOption.MatchString(arg):
			index++
			if index >= len(args) || !slices.Contains(setNames, args[index]) {
				return false
			}
		case !setFlags.MatchString(arg):
			return false
		}
	}
	return true
}

// keepsShell declines a command that changes the shell later commands run in.
func keepsShell(spelling string) {
	values := Values(spelling)
	start := slices.IndexFunc(values, func(value string) bool { return !assignment.MatchString(value) })
	if start == -1 {
		return
	}
	name, args := Basename(values[start]), values[start+1:]
	switch {
	case slices.Contains(stateful, name):
		decline("%s changes the shell", name)
	case name == "set":
		if !plainSet(args) {
			decline("set changes shell options")
		}
	case name == "read" || name == "printf" || name == "print":
		for _, variable := range setsVariables(name, args) {
			Assigned(variable)
		}
	}
}

// verdict is what the zsh analysis concludes about one command or script:
// "allow", a certain "deny" or "ask" with its message, or "" with why the
// rules cannot decide it.
type verdict struct {
	decision, message string
}

func undecided(why string) verdict { return verdict{"", why} }

// certain is the guard every value of a template must meet, if any: a deny
// before an ask.
func (r Rules) certain(spelling, template string) (verdict, bool) {
	shown := strings.ReplaceAll(spelling, Wild, "…")
	for _, rule := range r.Deny.candidates(template) {
		if MustMatch(rule.Pattern, template) {
			return verdict{"deny", Denial(shown, rule.Reason)}, true
		}
	}
	for _, rule := range r.Ask.candidates(template) {
		if MustMatch(rule.Pattern, template) {
			return verdict{"ask", "shell-unwrap: `" + shown + "` needs approval"}, true
		}
	}
	return verdict{}, false
}

// possible is the first guard some value of a template may meet, if any. A
// rule that starts with `*` is about the script's text, such as a variable
// it names, which a run-time value cannot add to it, so Decide judges it on
// the text alone.
func (r Rules) possible(template string) (string, bool) {
	for _, guard := range []struct {
		kind  string
		rules bucket
	}{{"deny", r.Deny}, {"ask", r.Ask}} {
		for _, rule := range guard.rules.candidates(template) {
			if !strings.HasPrefix(rule.Pattern, "*") && MayMatch(rule.Pattern, template) {
				return fmt.Sprintf("%s rule `%s` may match", guard.kind, rule.Pattern), true
			}
		}
	}
	return "", false
}

// judge is the verdict for one shape of a command. A guard every value of
// some spelling meets decides it, a deny before an ask. A guard some value
// may meet, a command that changes the shell, or one that runs its own
// arguments leaves it undecided. Otherwise it is allowed when it reaches an
// allow rule through transparent wrappers.
func (r Rules) judge(command string) (result verdict) {
	defer func() {
		if recovered := recover(); recovered != nil {
			declined, ok := recovered.(Decline)
			if !ok {
				panic(recovered)
			}
			result = undecided(declined.Why)
		}
	}()
	var guarded *verdict
	maybe := ""
	for _, candidate := range append([]string{command}, Spellings(command)...) {
		for _, template := range templates(candidate) {
			if found, ok := r.certain(candidate, template); ok && (guarded == nil || found.decision == "deny" && guarded.decision == "ask") {
				guarded = &found
			}
			if why, ok := r.possible(template); ok && maybe == "" {
				maybe = why
			}
		}
	}
	switch {
	case guarded != nil:
		return *guarded
	case maybe != "":
		return undecided(maybe)
	}
	for spelling, ok := command, true; ok; spelling, ok = Transparent(spelling) {
		keepsShell(spelling)
		if runsItself(spelling) {
			return undecided(fmt.Sprintf("`%s` runs or writes by its arguments", Values(spelling)[0]))
		}
		allowed := true
		for _, template := range templates(spelling) {
			allowed = allowed && slices.ContainsFunc(r.Allow.candidates(template), func(rule Rule) bool {
				return MustMatch(rule.Pattern, template)
			})
		}
		if allowed {
			return verdict{"allow", Allowed}
		}
	}
	return undecided("no allow rule covers every value")
}

// Judge is the verdict for one command's words over every shape zsh may run
// them in. It is allowed when every shape is; when every shape meets a guard
// for certain it is denied if any shape is, else asked; any other mix depends
// on run-time values and stays undecided.
func (r Rules) Judge(list []ZshWord) verdict {
	found, ok := shapes(list)
	if !ok {
		return undecided("too many words zsh may drop")
	}
	var results []verdict
	for _, values := range found {
		result := r.judge(render(values))
		if result.decision == "" {
			return result
		}
		results = append(results, result)
	}
	allowed := func(result verdict) bool { return result.decision == "allow" }
	switch {
	case !slices.ContainsFunc(results, func(result verdict) bool { return !allowed(result) }):
		return results[0]
	case slices.ContainsFunc(results, allowed):
		return undecided("a deny or ask rule matches only some of the ways zsh may run it")
	}
	if index := slices.IndexFunc(results, func(result verdict) bool { return result.decision == "deny" }); index != -1 {
		return results[index]
	}
	return results[0]
}

// Verdict is the zsh analysis's conclusion for script: a certain deny for any
// command or file, else a certain ask, else allowed when every step is, else
// undecided with the first reason.
func (r Rules) Verdict(script, cwd string) verdict {
	if len(script) > MaxCommand {
		return undecided("longer than Claude Code analyses")
	}
	steps, err := Steps(script)
	if err != nil {
		return undecided(err.Error())
	}
	if len(steps) == 0 {
		return undecided("no command")
	}
	var ask, pending *verdict
	keep := func(result verdict) {
		switch {
		case result.decision == "ask" && ask == nil:
			ask = &result
		case result.decision == "" && pending == nil:
			pending = &result
		}
	}
	place := directory{path: cwd, known: true}
	for _, step := range steps {
		var result verdict
		switch {
		case step.Access != nil:
			result = r.access(*step.Access, place, cwd)
		case step.Words[0].Text == "cd":
			result = r.cd(step, cwd, &place)
		default:
			result = r.Judge(step.Words)
			if result.decision == "" {
				result.message = fmt.Sprintf("`%s`: %s", render(texts(step.Words)), result.message)
			}
			if git, ok := gitVerdict(render(texts(step.Words)), place); ok && result.decision != "deny" && result.decision != "ask" {
				result = git
			}
		}
		if result.decision == "deny" {
			return result
		}
		keep(result)
	}
	switch {
	case ask != nil:
		return *ask
	case pending != nil:
		return *pending
	}
	return verdict{"allow", Allowed}
}

// directory is where the script is at a step, if it is known: a `cd` that
// may be skipped, or that runs in a subshell, leaves it unknown.
type directory struct {
	path  string
	known bool
}

// access is the verdict for a file a redirect reads or writes.
func (r Rules) access(access Access, place directory, cwd string) verdict {
	if strings.Contains(access.Path, Wild) {
		return undecided("a redirect to a run-time path")
	}
	path := access.Path
	if !filepath.IsAbs(path) {
		if !place.known {
			return undecided("a redirect relative to a run-time directory")
		}
		path = filepath.Join(place.path, path)
	}
	return r.fileVerdict(access, path, cwd)
}

// cd is the verdict for a `cd`: allowed when it moves to a known directory
// inside the working ones.
func (r Rules) cd(step Step, cwd string, place *directory) verdict {
	list := step.Words
	moved := *place
	defer func() {
		// After a cd that may not run, or runs in a subshell, the directory is
		// not known.
		moved.known = moved.known && !step.Conditional
		*place = moved
	}()
	if len(list) != 2 {
		moved.known = false
		return undecided("cd with options")
	}
	target := list[1]
	// `cd -` returns to $OLDPWD, and `cd -2` or `cd +1` reads the directory
	// stack: neither names its directory.
	if target.Vanishes || strings.Contains(target.Text, Wild) || strings.HasPrefix(target.Text, "-") || strings.HasPrefix(target.Text, "+") {
		moved.known = false
		return undecided("cd to a run-time directory")
	}
	path := target.Text
	if !filepath.IsAbs(path) {
		if !place.known {
			moved.known = false
			return undecided("cd from a run-time directory")
		}
		path = filepath.Join(place.path, path)
	}
	moved.path = real(path)
	if !inside(moved.path, append([]string{real(cwd)}, r.Directories...)) {
		return undecided("cd outside the working directories")
	}
	return verdict{"allow", Allowed}
}

// Why is the reason script is not decided by the zsh analysis, or "".
func (r Rules) Why(script, cwd string) string {
	if result := r.Verdict(script, cwd); result.decision == "" {
		return result.message
	}
	return ""
}

func texts(list []ZshWord) []string {
	out := make([]string, len(list))
	for i, word := range list {
		out[i] = strings.ReplaceAll(word.Text, Wild, "…")
	}
	return out
}

// Certain is the deny or ask the zsh analysis concludes for script, or nil:
// the hosts that take no allow from this hook use it beside their guards.
func (r Rules) Certain(script, cwd string) *Decision {
	result := r.Verdict(script, cwd)
	if result.decision != "deny" && result.decision != "ask" {
		return nil
	}
	return &Decision{result.decision, result.message}
}

// Decide is the Claude Code decision for script, or nil to leave it to
// Claude: the guards on every command as written, then the zsh analysis.
func (r Rules) Decide(script, cwd string) *Decision {
	all := Guarded(script)
	for _, command := range all {
		if message := r.Denied(command); message != "" {
			return &Decision{"deny", message}
		}
	}
	for _, command := range all {
		if spelling := r.Asked(command); spelling != "" {
			return &Decision{"ask", "shell-unwrap: `" + spelling + "` needs approval"}
		}
	}
	result := r.Verdict(script, cwd)
	if result.decision == "" {
		return nil
	}
	return &Decision{result.decision, result.message}
}
