package unwrap

import (
	"fmt"
	"slices"
	"strings"
)

// What a command's arguments do by themselves, as programs.yaml gives each a
// role: a file read or written is judged by the Read and Edit rules, an
// option that acts (`find -exec`) leaves the command to Claude, and the
// scripts a program runs (`hyperfine`) carry an allow in place of a rule of
// its own when the policy lists that program as transparent. Each layer a
// transparent wrapper peels is read the same way.

// roles is what one layer's arguments do: the files they read or write, the
// scripts they run, why an acting option leaves it to Claude, and why its
// scripts cannot stand in for a rule.
type roles struct {
	files    []Access
	scripts  []string
	acts     string
	declined string
}

// rolesOf reads a command's arguments by its grammar.
func rolesOf(words []string) roles {
	var found roles
	parsed := parse(words)
	if parsed == nil {
		return found
	}
	program, name := parsed.Grammar, Basename(words[0])
	decline := func(why string) {
		if found.declined == "" {
			found.declined = why
		}
	}
	add := func(role, value string) {
		switch role {
		case "script":
			if runTime(value) {
				decline(fmt.Sprintf("`%s` runs a run-time script", name))
			}
			found.scripts = append(found.scripts, value)
		case "read", "write":
			found.files = append(found.files, Access{Path: value, Read: role == "read", Write: role == "write"})
		}
	}
	acting := fmt.Sprintf("`%s` runs, writes or deletes by its arguments", name)
	for _, option := range parsed.Options {
		choices, limited := program.Choices[option.Name]
		switch {
		case acts(program, option.Name):
			found.acts = acting
		case slices.Contains(program.Declines, option.Name), program.Closed && !program.known(option.Name),
			limited && !slices.Contains(choices, option.Value):
			decline(fmt.Sprintf("`%s` with the option %s", name, option.Name))
		case option.Takes && !slices.Contains(program.Except[option.Name], option.Value):
			add(program.role(option.Name), option.Value)
		}
	}
	for _, index := range parsed.Operands {
		// find reads its expression after `--` too, which ends only its own
		// options (-H, -L, -P).
		if !program.EndsOptions && slices.Contains(program.Acts, words[index]) {
			found.acts = acting
		}
		add(program.Operands, words[index])
	}
	return found
}

// acts is whether an option acts by its arguments, alone or as a letter of
// a bundle of short flags (`fd -Hx`).
func acts(program *Program, option string) bool {
	if slices.Contains(program.Acts, option) {
		return true
	}
	if strings.HasPrefix(option, "--") {
		return false
	}
	for _, letter := range option[1:] {
		if slices.Contains(program.Acts, "-"+string(letter)) {
			return true
		}
	}
	return false
}

// arguments is the verdict for a command whose rules came to ruled, once its
// arguments are read: through every layer the policy's transparent wrappers
// peel, a file must pass the Read and Edit rules and an acting option leaves
// it to Claude; a transparent program the rules do not decide is judged by
// the scripts it runs instead.
func (r Rules) arguments(words []string, ruled verdict, cwd, dir string) verdict {
	if ruled.decision == "deny" || ruled.decision == "ask" {
		return ruled
	}
	results := []verdict{ruled}
	for layer, ok := words, true; ok; layer, ok = r.Policy.Peel(layer) {
		found := rolesOf(layer)
		if found.acts != "" {
			return undecided(found.acts)
		}
		for _, access := range found.files {
			results = append(results, r.access(access, dir, cwd))
		}
		// An option the grammar declines leaves even an allowed command to
		// Claude, though a file it reads or writes may still deny it.
		if found.declined != "" {
			return worst(append([]verdict{undecided(found.declined)}, results...))
		}
		if _, transparent := r.Policy.wrapper(layer[0]); ruled.decision != "" || !transparent || found.scripts == nil {
			continue
		}
		if dir == "" {
			return undecided(fmt.Sprintf("`%s` from a run-time directory", Basename(layer[0])))
		}
		// Its scripts stand in for the rule that did not decide it.
		results = results[1:]
		for _, script := range found.scripts {
			results = append(results, r.verdictFrom(script, cwd, dir))
		}
	}
	return worst(results)
}

// worst is a deny among results, else an ask, else advice, else the first
// undecided, else the first a rewrite would settle, else allowed.
func worst(results []verdict) verdict {
	for _, decision := range []string{"deny", "ask", "advise", "", "restructure"} {
		if index := slices.IndexFunc(results, func(result verdict) bool { return result.decision == decision }); index != -1 {
			return results[index]
		}
	}
	return verdict{"allow", Allowed}
}
