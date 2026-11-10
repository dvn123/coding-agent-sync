package unwrap

import (
	"fmt"
	"slices"
	"strings"
)

// The verdict for one command: its words, with the run-time parts Wild, in
// every shape zsh may run them, against the rules.

// verdict is what the zsh analysis concludes about one command or script:
// "allow", a certain "deny" or "ask" with its message, "advise" (a deny in
// Claude, with how to write it so the rules can read it), or "" with why the
// rules cannot decide it.
type verdict struct {
	decision, message string
}

func undecided(why string) verdict { return verdict{"", why} }

// A command with more words zsh may drop is judged in too many shapes.
const maxVanishing = 6

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

// templatesOf joins words with the input `xargs` adds read as mark, and once
// more without it when it stands alone.
func templatesOf(words []string, mark string) []string {
	join := func(list []string) string {
		out := make([]string, len(list))
		for i, word := range list {
			out[i] = strings.ReplaceAll(word, XargsInput, mark)
		}
		return strings.Join(out, " ")
	}
	kept := join(words)
	dropped := join(slices.DeleteFunc(slices.Clone(words), func(word string) bool { return word == XargsInput }))
	if dropped == kept {
		return []string{kept}
	}
	return []string{kept, dropped}
}

// shown is words as a message spells them, with each run-time part as `…`.
func shown(words []string) string {
	return strings.ReplaceAll(strings.Join(words, " "), Wild, "…")
}

// certain is the guard every value of a template must meet, if any: a deny
// before an ask.
func (r Rules) certain(spelling []string, template string) (verdict, bool) {
	for _, rule := range r.Deny.candidates(template) {
		if MustMatch(rule.Pattern, template) {
			return verdict{"deny", Denial(shown(spelling), rule.Reason)}, true
		}
	}
	for _, rule := range r.Ask.candidates(template) {
		if MustMatch(rule.Pattern, template) {
			return verdict{"ask", "shell-unwrap: `" + shown(spelling) + "` needs approval"}, true
		}
	}
	return verdict{}, false
}

// possible is the first guard some value of a template may meet, if any. A
// rule that starts with `*` is about the script's text, such as a variable
// it names, which a run-time value cannot add to it, so Decide judges it on
// the text alone. A rule for a Head other than the command's reads the
// selector's opaque words, as does a rule about text inside a word where the
// grammar bounds where settings are read; see selected.
func (r Rules) possible(template, opaque string, found selector) (string, bool) {
	for _, guard := range []struct {
		kind  string
		rules bucket
	}{{"deny", r.Deny}, {"ask", r.Ask}} {
		for _, rule := range guard.rules.candidates(template) {
			against := template
			head := ruleHead(rule.Pattern, found.Prefix)
			if found.Settings && textRule(rule.Pattern) || found.Head != "" && head != "" && head != found.Head {
				against = opaque
			}
			if !strings.HasPrefix(rule.Pattern, "*") && MayMatch(rule.Pattern, against) {
				return fmt.Sprintf("%s rule `%s` may match", guard.kind, rule.Pattern), true
			}
		}
	}
	return "", false
}

// mayMeet is why some value of the guards' shape of a command, or of a
// spelling of it, may meet a deny or ask rule, or "". read marks the
// run-time words a program reads as operands; see positional and operands.
func (r Rules) mayMeet(guards []string, read func([]string) ([]string, string)) string {
	for _, candidate := range append([][]string{guards}, Spellings(guards)...) {
		plain := templatesOf(read(candidate))
		opaque := plain
		found, ok := selected(candidate)
		if ok {
			opaque = templatesOf(found.Opaque, Opaque)
		}
		for index, template := range plain {
			if why, ok := r.possible(template, opaque[index], found); ok {
				return why
			}
		}
	}
	return ""
}

// judge is the verdict for one shape of a command. A guard every value of
// some spelling meets decides it, a deny before an ask. A guard some value
// may meet, or a command that changes the shell, leaves it undecided.
// Otherwise it is allowed when it reaches an allow rule through the policy's
// transparent wrappers, or is a harmless `export`. guards is the same shape
// with the run-time parts that stay inside their word marked OneWord, which
// only the test for a guard some value may meet reads.
func (r Rules) judge(command, guards []string) (result verdict) {
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
	for _, candidate := range append([][]string{command}, Spellings(command)...) {
		for _, template := range templatesOf(candidate, Wild) {
			if found, ok := r.certain(candidate, template); ok && (guarded == nil || found.decision == "deny" && guarded.decision == "ask") {
				guarded = &found
			}
		}
	}
	maybe := r.mayMeet(guards, positional)
	switch {
	case guarded != nil:
		return *guarded
	case maybe != "" && r.mayMeet(guards, operands) == "":
		// Only words that may be options keep it from running, and `--`
		// before them settles that, so the agent is told how.
		return verdict{"advise", useOperands}
	case maybe != "":
		return undecided(maybe)
	}
	mayBeAllowed := false
	for spelling, ok := command, true; ok; spelling, ok = r.Policy.Peel(spelling) {
		r.Policy.keepsShell(spelling)
		if spelling[0] == "export" {
			return verdict{"allow", Allowed}
		}
		allowed := true
		for _, template := range templatesOf(spelling, Wild) {
			allowed = allowed && slices.ContainsFunc(r.Allow.candidates(template), func(rule Rule) bool {
				return MustMatch(rule.Pattern, template)
			})
		}
		if allowed {
			return verdict{"allow", Allowed}
		}
		for _, template := range templatesOf(spelling, Wild) {
			if slices.ContainsFunc(r.Allow.candidates(template), func(rule Rule) bool { return MayMatch(rule.Pattern, template) }) {
				mayBeAllowed = true
			}
		}
	}
	if mayBeAllowed {
		return undecided(runTimeValue)
	}
	return undecided("no allow rule covers every value")
}

// runTimeValue is why a command an allow rule covers for some of its
// run-time values, but not all, is left undecided.
const runTimeValue = "no allow rule covers every run-time value"

// insideWords marks OneWord the run-time parts that zsh keeps inside one
// argument which cannot be an option, so they can only stand for text within
// it: a word that cannot split and begins with literal text other than `-`
// (a glob like `src/*` expands only to names that begin `src/`). A path an
// option takes (git -C) is marked Opaque: no rule can read it. Any other
// run-time word may be an option such as `-XPOST`, which a rule matches only
// once spelled out, so it stays Wild.
func insideWords(list []ZshWord) []ZshWord {
	out := slices.Clone(list)
	for index, word := range out {
		prefix, _, found := strings.Cut(word.Text, Wild)
		if found && !word.Splits && prefix != "" && !strings.HasPrefix(prefix, "-") {
			out[index].Text = strings.ReplaceAll(word.Text, Wild, OneWord)
		}
	}
	start := slices.IndexFunc(out, func(word ZshWord) bool { return !assignment.MatchString(word.Text) })
	if start == -1 {
		return out
	}
	words := make([]string, 0, len(out)-start)
	for _, word := range out[start:] {
		words = append(words, word.Text)
	}
	parsed := parse(words)
	if parsed == nil {
		return out
	}
	for _, option := range parsed.Options {
		switch grammar(words[0]).role(option.Name) {
		case "directory", "path", "repository":
			if path := &out[start+option.ValueAt]; option.ValueAt != option.At && !path.Splits && !path.Globs {
				path.Text = strings.ReplaceAll(path.Text, Wild, Opaque)
			}
		}
	}
	return out
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
	// The same shapes, in the same order, as the guards read them.
	guarded, _ := shapes(insideWords(list))
	var results []verdict
	for index, words := range found {
		result := r.judge(words, guarded[index])
		if result.decision == "" {
			return result
		}
		results = append(results, result)
	}
	allowed := func(result verdict) bool { return result.decision == "allow" }
	advised := func(result verdict) bool { return result.decision == "advise" }
	switch {
	case !slices.ContainsFunc(results, func(result verdict) bool { return !allowed(result) }):
		return results[0]
	case !slices.ContainsFunc(results, func(result verdict) bool { return !allowed(result) && !advised(result) }):
		// `--` settles every shape, as each is the same words or fewer.
		return results[slices.IndexFunc(results, advised)]
	case slices.ContainsFunc(results, allowed):
		return undecided("a deny or ask rule matches only some of the ways zsh may run it")
	}
	if index := slices.IndexFunc(results, func(result verdict) bool { return result.decision == "deny" }); index != -1 {
		return results[index]
	}
	return results[0]
}
