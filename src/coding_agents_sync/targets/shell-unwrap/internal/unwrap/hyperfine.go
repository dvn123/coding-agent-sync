package unwrap

import (
	"slices"
	"strings"
)

// hyperfine's options, from `hyperfine --help` 1.19. Each command it times,
// and each setup, prepare, cleanup and conclude command, is a script its
// shell runs (or, with `-N`, a command it splits itself, which the shell
// reading covers).
var (
	hyperfineScripts = []string{"-s", "--setup", "-p", "--prepare", "-c", "--cleanup", "--conclude"}
	hyperfineValues  = []string{
		"-w", "--warmup", "-m", "--min-runs", "-M", "--max-runs", "-r", "--runs", "-u", "--time-unit", "--style",
		"--sort", "--min-benchmarking-time", "-n", "--command-name", "-S", "--shell", "--output", "--input",
	}
	hyperfineFlags = []string{"-N", "-i", "--ignore-failure", "--show-output", "-h", "--help", "-V", "--version"}
	// The shells `-S` may name; each runs the script with `-c`.
	hyperfineShells = []string{"none", "sh", "bash", "zsh", "dash"}
	// `--output` values that write no file.
	hyperfineStreams = []string{"null", "pipe", "inherit"}
)

// hyperfine is the verdict for a `hyperfine` step, judged by the scripts it
// runs, the file `--input` reads and the files it writes, or false when the
// step is not one. Parameter scans and lists put run-time values in the
// scripts, so they are left to Claude, as is any option it does not know.
func (r Rules) hyperfine(words []string, cwd string, place directory) (verdict, bool) {
	if len(words) == 0 || words[0] != "hyperfine" {
		return verdict{}, false
	}
	var scripts []string
	var files []Access
	for index := 1; index < len(words); index++ {
		word := words[index]
		if strings.ContainsAny(word, wilds) {
			return undecided("hyperfine with a run-time argument"), true
		}
		if !strings.HasPrefix(word, "-") || word == "-" {
			scripts = append(scripts, word)
			continue
		}
		name, value, joined := strings.Cut(word, "=")
		if !joined && !strings.HasPrefix(word, "--") && len(word) > 2 {
			name, value, joined = word[:2], word[2:], true
		}
		switch {
		case slices.Contains(hyperfineFlags, name) && !joined:
			continue
		case strings.HasPrefix(name, "--export-"):
			// --export-asciidoc, -csv, -json, -markdown, -orgmode write a file.
		case !slices.Contains(hyperfineScripts, name) && !slices.Contains(hyperfineValues, name):
			return undecided("hyperfine option " + name), true
		}
		if !joined {
			if index++; index == len(words) {
				return undecided("hyperfine option " + name + " without its value"), true
			}
			value = words[index]
			if strings.ContainsAny(value, wilds) {
				return undecided("hyperfine with a run-time argument"), true
			}
		}
		switch {
		case slices.Contains(hyperfineScripts, name):
			scripts = append(scripts, value)
		case name == "-S" || name == "--shell":
			if !slices.Contains(hyperfineShells, value) {
				return undecided("hyperfine with the shell " + value), true
			}
		case name == "--input" && value != "null":
			files = append(files, Access{Path: value, Read: true})
		case strings.HasPrefix(name, "--export-") || name == "--output" && !slices.Contains(hyperfineStreams, value):
			files = append(files, Access{Path: value, Write: true})
		}
	}
	results := make([]verdict, 0, len(scripts)+len(files))
	for _, access := range files {
		results = append(results, r.access(access, place, cwd))
	}
	for _, script := range scripts {
		if !place.known {
			return undecided("hyperfine from a run-time directory"), true
		}
		results = append(results, r.verdictFrom(script, cwd, place))
	}
	if len(scripts) == 0 {
		return undecided("hyperfine with no command"), true
	}
	return worst(results), true
}

// worst is a deny among results, else an ask, else advice, else the first undecided,
// else allowed.
func worst(results []verdict) verdict {
	for _, decision := range []string{"deny", "ask", "advise", ""} {
		if index := slices.IndexFunc(results, func(result verdict) bool { return result.decision == decision }); index != -1 {
			return results[index]
		}
	}
	return verdict{"allow", Allowed}
}
