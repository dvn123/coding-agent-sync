package unwrap

import (
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strings"
)

// gitVerdict judges where each git a command runs would find its repository.
// A directory shaped like a bare repository is used as one, so its config,
// which nothing reviewed (core.fsmonitor, core.pager, hooks), runs commands:
// that is what Claude Code's check on `cd` beside `git` guards against, and
// `git -C` reaches the same directories. It asks for such a directory and
// leaves a git whose directory is decided at run time to Claude. ok is false
// when the command runs no git, or none in such a directory.
func gitVerdict(spelling, dir string) (verdict, bool) {
	for _, candidate := range append([]string{spelling}, Spellings(spelling)...) {
		values := Values(candidate)
		if len(values) == 0 || Basename(values[0]) != "git" {
			continue
		}
		if dir == "" {
			return undecided("git from a run-time directory"), true
		}
		repository, known := gitDirectory(values[1:], dir)
		if !known {
			return undecided("git with a run-time -C or an explicit --git-dir"), true
		}
		if bare := bareRepository(repository); bare != "" {
			return verdict{"ask", fmt.Sprintf("shell-unwrap: git would use %s as a bare repository, whose config can run commands", bare)}, true
		}
	}
	return verdict{}, false
}

// gitDirectory is where git starts looking for its repository: the working
// directory, moved by each leading `-C`, which git applies in order.
func gitDirectory(args []string, cwd string) (string, bool) {
	dir := cwd
	for index := 0; index < len(args); index++ {
		arg := args[index]
		switch {
		case !strings.HasPrefix(arg, "-"):
			return dir, true
		case arg == "-C":
			index++
			if index >= len(args) || strings.Contains(args[index], Wild) || strings.Contains(args[index], XargsInput) {
				return "", false
			}
			if next := args[index]; filepath.IsAbs(next) {
				dir = next
			} else if next != "" {
				dir = filepath.Join(dir, next)
			}
		case arg == "--git-dir" || strings.HasPrefix(arg, "--git-dir="):
			// An explicit repository, bare or not, is git's to trust and
			// beyond this check.
			return "", false
		case arg == "-c" || arg == "--namespace" || arg == "--work-tree" || arg == "--config-env":
			index++
		}
	}
	return dir, true
}

// bareRepository is the directory git would use as a bare repository when
// started in dir, or "". Git takes the first directory up from dir whose
// `.git` is a gitfile or a git directory, or that is itself a git directory;
// it passes over a `.git` directory that is not one.
func bareRepository(dir string) string {
	for current := real(dir); ; {
		dotGit := filepath.Join(current, ".git")
		if info, err := os.Stat(dotGit); err == nil && (!info.IsDir() || gitDirectoryShape(dotGit)) {
			return ""
		}
		if gitDirectoryShape(current) {
			return current
		}
		parent := filepath.Dir(current)
		if parent == current {
			return ""
		}
		current = parent
	}
}

// gitDirectoryShape is git's own test for a git directory: a HEAD file and
// objects and refs directories, which a `commondir` file may place in another
// directory, as a linked worktree's does. That other directory's config is
// the one git reads, so any `commondir` counts, wherever it points.
func gitDirectoryShape(dir string) bool {
	head, err := os.Stat(filepath.Join(dir, "HEAD"))
	if err != nil || !head.Mode().IsRegular() {
		return false
	}
	if _, err := os.Lstat(filepath.Join(dir, "commondir")); err == nil {
		return true
	}
	for _, name := range []string{"objects", "refs"} {
		if info, err := os.Stat(filepath.Join(dir, name)); err != nil || !info.IsDir() {
			return false
		}
	}
	return true
}

// Opaque stands for run-time text that no text rule can read: it matches a
// rule's `*` but none of its literal text.
const Opaque = "\x02"

// Git's options before the subcommand that take the next word: a path, or a
// value that may set configuration.
var (
	gitPathOptions  = []string{"-C", "--git-dir", "--work-tree", "--namespace"}
	gitValueOptions = []string{"-c", "--config-env"}
)

// Subcommands that read settings or transports from their own arguments:
// `git config` sets them, and these take a URL, which may name `ext::`.
var gitReadsArguments = []string{"config", "clone", "fetch", "pull", "push", "ls-remote", "remote", "submodule", "archive"}

// Built-in subcommands (`git --list-cmds=builtins`, 2.51), which no alias can
// replace: git ignores an alias that hides one. Any other word may be an alias
// for anything, so its arguments stay as they are.
var gitBuiltins = []string{
	"add", "am", "annotate", "apply", "archive", "bisect", "blame", "branch", "bundle", "cat-file", "check-attr",
	"check-ignore", "check-mailmap", "check-ref-format", "checkout", "cherry", "cherry-pick", "clean", "clone",
	"commit", "commit-graph", "commit-tree", "config", "count-objects", "describe", "diff", "diff-files", "diff-index",
	"diff-tree", "difftool", "fetch", "for-each-ref", "format-patch", "fsck", "gc", "grep", "hash-object", "help",
	"init", "log", "ls-files", "ls-remote", "ls-tree", "merge", "merge-base", "merge-tree", "mv", "name-rev", "notes",
	"pull", "push", "range-diff", "read-tree", "rebase", "reflog", "remote", "repack", "replace", "reset", "restore",
	"rev-list", "rev-parse", "revert", "rm", "shortlog", "show", "show-branch", "show-ref", "sparse-checkout", "stash",
	"status", "submodule", "switch", "symbolic-ref", "tag", "update-index", "update-ref", "var", "verify-commit",
	"verify-tag", "whatchanged", "worktree", "write-tree",
}

// gitArguments is a git command's words for the text rules (see textRule):
// git reads settings only from -c and --config-env before the subcommand, from
// `git config`, and from a transport URL, so when the subcommand is literal
// and reads none from its arguments, the run-time parts of those arguments and
// of the paths git takes before it are marked Opaque. ok is false for any
// other command, or when -c or --config-env holds a run-time value. The same
// words serve a rule for another subcommand (see ruleSubcommand), since git
// reads its subcommand once: after `log`, no argument makes it `credential`.
func gitArguments(values []string) ([]string, string, bool) {
	if len(values) == 0 || Basename(values[0]) != "git" {
		return nil, "", false
	}
	out := slices.Clone(values)
	opaque := func(index int) {
		for _, mark := range []string{Wild, OneWord} {
			out[index] = strings.ReplaceAll(out[index], mark, Opaque)
		}
	}
	index := 1
subcommand:
	for ; index < len(out); index++ {
		arg := out[index]
		switch {
		case runTime(arg):
			return nil, "", false
		case !strings.HasPrefix(arg, "-"):
			break subcommand
		case slices.Contains(gitPathOptions, arg) && index+1 < len(out):
			index++
			opaque(index)
			continue
		case slices.Contains(gitValueOptions, arg):
			index++
			if index < len(out) && runTime(out[index]) {
				return nil, "", false
			}
			continue
		}
	}
	if index >= len(out) || !slices.Contains(gitBuiltins, out[index]) || slices.Contains(gitReadsArguments, out[index]) {
		return nil, "", false
	}
	for after := index + 1; after < len(out); after++ {
		opaque(after)
	}
	return out, out[index], true
}

// ruleSubcommand is the subcommand a rule for program (`git`, `uv run`) names,
// the first literal word after its options (`credential` in
// `git -* credential fill *`, `python` in `uv run * python - *`), or "".
func ruleSubcommand(pattern, program string) string {
	tokens, prefix := strings.Fields(pattern), strings.Fields(program)
	if len(prefix) == 0 || len(tokens) < len(prefix) || !slices.Equal(tokens[:len(prefix)], prefix) {
		return ""
	}
	for _, token := range tokens[len(prefix):] {
		switch {
		case token == "*" || strings.HasPrefix(token, "-"):
			continue
		case strings.ContainsAny(token, "*?["):
			return ""
		default:
			return token
		}
	}
	return ""
}

// textRule is whether a rule matches text inside a word, as a `text`
// predicate compiles (`git*core.pager*`, `git -* config*alias.*`), rather
// than whole words (`git -* push * --force *`).
func textRule(pattern string) bool {
	for _, token := range strings.Fields(pattern) {
		if token != "*" && token != "-*" && strings.Contains(token, "*") {
			return true
		}
	}
	return false
}

// runTime is whether a value holds any part decided at run time.
func runTime(value string) bool {
	return strings.ContainsAny(value, wilds) || strings.Contains(value, XargsInput)
}
