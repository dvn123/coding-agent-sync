package unwrap

import (
	"regexp"
	"slices"
	"strings"
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
	bundledV  = regexp.MustCompile(`^-[A-Za-z]*v`)
	bundledP  = regexp.MustCompile(`^-[A-Za-z]*P`)
)

// keepsShell declines a command that changes the shell later commands run in,
// other than setting the shell options the policy allows.
func (p Policy) keepsShell(words []string) {
	start := slices.IndexFunc(words, func(word string) bool { return !assignment.MatchString(word) })
	if start == -1 {
		return
	}
	name, args := Basename(words[start]), words[start+1:]
	switch {
	case name == "setopt" && len(args) > 0 && !slices.ContainsFunc(args, func(arg string) bool { return !p.shellOption(arg) }):
	case name == "export":
		// Variables a leading assignment may carry, which zsh exports.
		for _, arg := range args {
			if !p.harmless(arg) {
				variable, _, _ := strings.Cut(arg, "=")
				decline("export of %s", variable)
			}
		}
	case slices.Contains(stateful, name):
		decline("%s changes the shell", name)
	case name == "set":
		if !p.plainSet(args) {
			decline("set changes shell options")
		}
	case name == "read" || name == "printf" || name == "print":
		for _, variable := range setsVariables(name, args) {
			Assigned(variable)
		}
	}
}

// shellOption is whether a setopt argument names an option the policy allows,
// which zsh reads ignoring case and underscores, with an optional `no` to
// unset it.
func (p Policy) shellOption(arg string) bool {
	name := strings.ToLower(strings.ReplaceAll(arg, "_", ""))
	return slices.Contains(p.ShellOptions, name) || slices.Contains(p.ShellOptions, strings.TrimPrefix(name, "no"))
}

// plainSet is whether `set` changes only the positional parameters and the
// options the policy allows, by name or by the -e, -u, -v and -x flags.
func (p Policy) plainSet(args []string) bool {
	flags := map[rune]string{'e': "errexit", 'u': "nounset", 'v': "verbose", 'x': "xtrace"}
	for index := 0; index < len(args); index++ {
		arg := args[index]
		switch {
		case arg == "--" || arg == "-" || !strings.HasPrefix(arg, "-") && !strings.HasPrefix(arg, "+"):
			return true
		case setOption.MatchString(arg):
			index++
			if index >= len(args) || !slices.Contains(p.ShellOptions, args[index]) {
				return false
			}
		case !setFlags.MatchString(arg):
			return false
		}
		for _, flag := range strings.TrimRight(arg[1:], "o") {
			if !slices.Contains(p.ShellOptions, flags[flag]) {
				return false
			}
		}
	}
	return true
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
