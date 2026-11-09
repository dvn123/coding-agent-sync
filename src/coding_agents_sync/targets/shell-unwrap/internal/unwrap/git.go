package unwrap

import (
	"fmt"
	"os"
	"path/filepath"
)

// gitVerdict judges where each git a command runs would find its repository.
// A directory shaped like a bare repository is used as one, so its config,
// which nothing reviewed (core.fsmonitor, core.pager, hooks), runs commands:
// that is what Claude Code's check on `cd` beside `git` guards against, and
// `git -C` reaches the same directories. It asks for such a directory and
// leaves a git whose directory is decided at run time to Claude. ok is false
// when the command runs no git, or none in such a directory.
func gitVerdict(words []string, dir string) (verdict, bool) {
	for _, candidate := range append([][]string{words}, Spellings(words)...) {
		if len(candidate) == 0 || Basename(candidate[0]) != "git" {
			continue
		}
		if dir == "" {
			return undecided("git from a run-time directory"), true
		}
		repository, known := gitDirectory(candidate, dir)
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
// directory, moved by each `-C` before the subcommand (an option whose role
// is directory), which git applies in order. An explicit repository, bare or
// not, is git's to trust and beyond this check.
func gitDirectory(words []string, cwd string) (string, bool) {
	dir := cwd
	parsed := parse(words)
	for _, option := range parsed.Options {
		if parsed.Subcommand != -1 && option.At > parsed.Subcommand {
			break
		}
		switch parsed.Grammar.role(option.Name) {
		case "repository":
			return "", false
		case "directory":
			switch next := option.Value; {
			case !option.Takes || runTime(next):
				return "", false
			case filepath.IsAbs(next):
				dir = next
			case next != "":
				dir = filepath.Join(dir, next)
			}
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
