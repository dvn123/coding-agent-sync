package unwrap

import (
	"fmt"
	"path/filepath"
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
	// Advice is a deny that only stands in for a prompt, with the advice
	// that would avoid it; bypass mode drops it as it does an ask.
	Advice bool `json:"-"`
}

// Verdict is the zsh analysis's conclusion for script: a certain deny for any
// command or file, else a certain ask, else allowed when every step is, else
// undecided with the first reason.
func (r Rules) Verdict(script, cwd string) verdict {
	return r.verdictFrom(script, cwd, cwd)
}

// verdictFrom is Verdict for a script that starts in dir ("" when not known),
// such as one a `hyperfine` after a `cd` runs; cwd stays the session's, which
// anchors rules.
func (r Rules) verdictFrom(script, cwd, dir string) verdict {
	if len(script) > MaxCommand {
		return undecided("longer than Claude Code analyses")
	}
	steps, err := StepsIn(script, dir)
	if err != nil {
		return withHint(undecided(err.Error()))
	}
	if len(steps) == 0 {
		return undecided("no command")
	}
	var ask, advice, pending, rewrite *verdict
	keep := func(result verdict) {
		switch {
		case result.decision == "ask" && ask == nil:
			ask = &result
		case result.decision == "advise" && advice == nil:
			advice = &result
		case result.decision == "" && pending == nil:
			pending = &result
		case result.decision == "restructure" && rewrite == nil:
			rewrite = &result
		}
	}
	for _, step := range steps {
		var result verdict
		switch {
		case step.Access != nil:
			result = r.access(*step.Access, step.Dir, cwd)
		case step.Words[0].Text == "cd":
			result = r.cd(step, cwd)
		default:
			step.Words = asTest(step.Words)
			words := rawTexts(step.Words)
			result = r.Judge(step.Words)
			if result.decision == "" {
				result.message = fmt.Sprintf("`%s`: %s", shown(words), result.message)
			}
			// A word zsh may drop moves what each argument reads as.
			forms, ok := shapes(step.Words)
			if !ok {
				forms = [][]string{words}
			}
			var read []verdict
			for _, form := range forms {
				read = append(read, r.arguments(form, result, cwd, step.Dir))
			}
			result = worst(read)
			someAllowed := slices.ContainsFunc(read, func(form verdict) bool { return form.decision == "allow" })
			if someAllowed && (result.decision == "deny" || result.decision == "ask") {
				result = undecided("a deny or ask rule matches only some of the ways zsh may run it")
			}
			if git, ok := gitVerdict(words, step.Dir); ok && result.decision != "deny" && result.decision != "ask" {
				result = git
			}
		}
		if result.decision == "deny" {
			return result
		}
		keep(withHint(result))
	}
	// A step no rewrite settles keeps the whole command with the host.
	switch {
	case ask != nil:
		return *ask
	case advice != nil:
		return *advice
	case pending != nil:
		return *pending
	case rewrite != nil:
		return *rewrite
	}
	return verdict{"allow", Allowed}
}

// rewrites are the reasons an undecided command is undecided only because of
// how it is written, each with how to write it so the rules can read it. A
// reason may follow the command it is about, as "`cmd`: reason".
var rewrites = map[string]string{
	"dynamic program":                                 "name the program literally, as `uv run x` rather than `$tool x`",
	"git from a run-time directory":                   "give git a literal directory, as `git -C ~/repo status`",
	"git with a run-time -C or an explicit --git-dir": "give git a literal directory, as `git -C ~/repo status`",
	"cd to a run-time directory":                      "cd to a literal path",
	"cd from a run-time directory":                    "cd to a literal path",
	"a redirect to a run-time path":                   "redirect to a literal path",
	"a redirect relative to a run-time directory":     "redirect to an absolute literal path",
	runTimeValue: "spell the values out, as `for f in a.txt b.txt`, or run one command per value",
}

// withHint is an undecided result whose reason a rewrite settles marked
// "restructure", its message followed by the rewrite; any other result as is.
func withHint(result verdict) verdict {
	if result.decision != "" {
		return result
	}
	for reason, rewrite := range rewrites {
		if result.message == reason || strings.HasSuffix(result.message, "`: "+reason) {
			return verdict{"restructure", result.message + "; " + rewrite}
		}
	}
	return result
}

// access is the verdict for a file a redirect run in dir reads or writes.
func (r Rules) access(access Access, dir, cwd string) verdict {
	if runTime(access.Path) {
		return undecided("a redirect to a run-time path")
	}
	path := access.Path
	if !filepath.IsAbs(path) {
		if dir == "" {
			return undecided("a redirect relative to a run-time directory")
		}
		path = filepath.Join(dir, path)
	}
	return r.fileVerdict(access, path, cwd)
}

// cd is the verdict for a `cd`: allowed when it moves to a known directory
// inside the working directories (the session's and `additionalDirectories`)
// or one the Edit rules allow. The analysis follows where it moves, so what
// runs there is judged as usual, relative paths against it: commands by their
// rules, redirects by the Read and Edit rules, git by its repository.
func (r Rules) cd(step Step, cwd string) verdict {
	list := step.Words
	if len(list) != 2 {
		return undecided("cd with options")
	}
	target := list[1]
	// `cd -` returns to $OLDPWD, and `cd -2` or `cd +1` reads the directory
	// stack: neither names its directory.
	if target.Vanishes || strings.Contains(target.Text, Wild) || strings.HasPrefix(target.Text, "-") || strings.HasPrefix(target.Text, "+") {
		return undecided("cd to a run-time directory")
	}
	path := target.Text
	if !filepath.IsAbs(path) {
		if step.Pwd == "" {
			return undecided("cd from a run-time directory")
		}
		path = filepath.Join(step.Pwd, path)
	}
	// A directory counts when a file in it may be read without asking.
	if !r.pathAllowed("Read", filepath.Join(real(path), "file"), cwd) {
		return undecided("cd outside the working directories")
	}
	// cd is a builtin that changes the shell, which Judge declines, so its own
	// allow rule is checked here.
	command := strings.Join(rawTexts(list), " ")
	if !slices.ContainsFunc(r.Allow.candidates(command), func(rule Rule) bool { return MustMatch(rule.Pattern, command) }) {
		return undecided("no allow rule covers `cd`")
	}
	return verdict{"allow", Allowed}
}

// Why is the reason script is not decided by the zsh analysis, or "".
func (r Rules) Why(script, cwd string) string {
	if result := r.Verdict(script, cwd); result.decision == "" || result.decision == "restructure" {
		return result.message
	}
	return ""
}

// Certain is the deny or ask the zsh analysis concludes for script, or nil:
// the hosts that take no allow from this hook use it beside their guards.
func (r Rules) Certain(script, cwd string) *Decision {
	result := r.Verdict(script, cwd)
	// Advice to use `--` stays with Claude, where the command would prompt;
	// these hosts' own rules decide it.
	if result.decision != "deny" && result.decision != "ask" {
		return nil
	}
	return &Decision{Decision: result.decision, Reason: result.message}
}

// Decide is the Claude Code decision for script, or nil to leave it to
// Claude: the guards on every command as written, then the zsh analysis.
func (r Rules) Decide(script, cwd string) *Decision {
	all := Guarded(script)
	for _, command := range all {
		if message := r.Denied(command); message != "" {
			return &Decision{Decision: "deny", Reason: message}
		}
	}
	for _, command := range all {
		if spelling := r.Asked(command); spelling != "" {
			return &Decision{Decision: "ask", Reason: "shell-unwrap: `" + spelling + "` needs approval"}
		}
	}
	result := r.Verdict(script, cwd)
	switch result.decision {
	case "":
		return nil
	case "advise":
		// Denied with the advice, since otherwise it would prompt.
		return &Decision{Decision: "deny", Reason: result.message, Advice: true}
	case "restructure":
		// Left to Claude unless the policy asks for the rewrite; either way it
		// would prompt, so bypass mode drops it as it does the advice.
		if r.Policy.Restructure != "deny" && r.Policy.Restructure != "ask" {
			return nil
		}
		return &Decision{Decision: r.Policy.Restructure, Reason: "shell-unwrap: " + result.message, Advice: true}
	}
	return &Decision{Decision: result.decision, Reason: result.message}
}

// asTest is `[ … ]` spelled as the `test …` it is, so the rules for `test`
// judge it; any other command is returned as is.
func asTest(list []ZshWord) []ZshWord {
	last := len(list) - 1
	if last < 1 || list[0].Text != "[" || list[last].Text != "]" {
		return list
	}
	return append([]ZshWord{{Text: "test"}}, list[1:last]...)
}

// rawTexts are the words' texts with their run-time parts still Wild, for
// the checks that judge them; texts is for messages.
func rawTexts(list []ZshWord) []string {
	out := make([]string, len(list))
	for i, word := range list {
		out[i] = word.Text
	}
	return out
}
