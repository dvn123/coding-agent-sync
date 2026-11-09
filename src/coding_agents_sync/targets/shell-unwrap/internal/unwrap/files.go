package unwrap

import (
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"strings"
)

// Claude Code's Read and Edit rules, applied to the files a redirect reads
// or writes. A rule's path is anchored as Claude anchors it: `//path` at the
// root, `~/path` at the home directory, `/path` at the settings file's
// directory, and any other path at the session's working directory. Its `*`
// stays within one directory and `**` crosses them. A deny or ask is matched
// loosely, so it catches every file it could mean: ignoring case, as macOS
// volumes usually do, and at any depth when it names no directory, as
// gitignore does, and the directory's contents when it names one. An allow is
// matched exactly.

// PathRule is one Read or Edit rule.
type PathRule struct {
	Tool, Effect, Pattern string
}

var pathEntry = regexp.MustCompile(`^(Read|Edit)\((.+)\)$`)

func pathRules(effect string, entries []string) []PathRule {
	var found []PathRule
	for _, entry := range entries {
		if match := pathEntry.FindStringSubmatch(entry); match != nil {
			found = append(found, PathRule{Tool: match[1], Effect: effect, Pattern: match[2]})
		}
	}
	return found
}

// globBody is a path glob as a regular expression body.
func globBody(glob string) string {
	var body strings.Builder
	for index := 0; index < len(glob); index++ {
		switch {
		case strings.HasPrefix(glob[index:], "**/"):
			body.WriteString("(?:.*/)?")
			index += 2
		case strings.HasPrefix(glob[index:], "**"):
			body.WriteString(".*")
			index++
		case glob[index] == '*':
			body.WriteString("[^/]*")
		case glob[index] == '?':
			body.WriteString("[^/]")
		default:
			body.WriteString(regexp.QuoteMeta(glob[index : index+1]))
		}
	}
	return body.String()
}

// pattern is the rule as a regular expression over absolute paths. With
// resolved set, the rule's literal leading directories are resolved through
// their symlinks, as macOS's `/tmp` is `/private/tmp`, so it compares with a
// resolved path.
func (rule PathRule) pattern(settingsDir, cwd string, loose, resolved bool) *regexp.Regexp {
	glob := rule.Pattern
	var base string
	switch {
	case strings.HasPrefix(glob, "//"):
		base, glob = "/", glob[2:]
	case glob == "~" || strings.HasPrefix(glob, "~/"):
		home, _ := os.UserHomeDir()
		base, glob = home, strings.TrimPrefix(strings.TrimPrefix(glob, "~"), "/")
	case strings.HasPrefix(glob, "/"):
		base, glob = settingsDir, glob[1:]
	default:
		base, glob = cwd, strings.TrimPrefix(glob, "./")
	}
	glob = strings.TrimSuffix(glob, "/")
	parts := strings.Split(glob, "/")
	literal := 0
	for literal < len(parts) && !strings.ContainsAny(parts[literal], "*?[") {
		literal++
	}
	anchor := filepath.Join(append([]string{base}, parts[:literal]...)...)
	if resolved {
		anchor = real(anchor)
	}
	rest := strings.Join(parts[literal:], "/")
	body := regexp.QuoteMeta(anchor)
	if rest != "" {
		body = strings.TrimSuffix(body, "/") + "/" + globBody(rest)
	}
	if !loose {
		return regexp.MustCompile("^" + body + "$")
	}
	if !strings.Contains(glob, "/") {
		// A name alone matches at any depth below its anchor.
		if resolved {
			base = real(base)
		}
		body = regexp.QuoteMeta(strings.TrimSuffix(base, "/")) + "/(?:.*/)?" + globBody(glob)
	}
	return regexp.MustCompile("(?i)^" + body + "(?:/.*)?$")
}

// fileVerdict judges one access to an absolute path by the file rules. A
// deny or ask decides it when the path, as written or resolved, may meet the
// rule, as written or resolved. Otherwise the file the shell opens, the
// resolved path, needs an Edit allow to be written, and a Read allow or a
// working directory to be read.
func (r Rules) fileVerdict(access Access, path, cwd string) verdict {
	paths := []string{filepath.Clean(path), real(path)}
	var tools []string
	if access.Read {
		tools = append(tools, "Read")
	}
	if access.Write {
		tools = append(tools, "Edit")
	}
	shown := map[bool]string{true: "write", false: "read"}[access.Write]
	for _, effect := range []string{"deny", "ask"} {
		for _, rule := range r.Paths {
			if rule.Effect != effect || !slices.Contains(tools, rule.Tool) {
				continue
			}
			for _, resolved := range []bool{false, true} {
				pattern := rule.pattern(r.Root, cwd, true, resolved)
				if !slices.ContainsFunc(paths, pattern.MatchString) {
					continue
				}
				if effect == "deny" {
					return verdict{"deny", "shell-unwrap: the redirect to " + shown + " `" + path + "` is denied by `" + rule.Tool + "(" + rule.Pattern + ")`"}
				}
				return verdict{"ask", "shell-unwrap: the redirect to " + shown + " `" + path + "` needs approval"}
			}
		}
	}
	for _, tool := range tools {
		if !r.pathAllowed(tool, paths[1], cwd) {
			return undecided("no " + tool + " rule allows `" + path + "`")
		}
	}
	return verdict{"allow", Allowed}
}

// pathAllowed is whether an allow rule for tool matches the resolved path,
// or, for a read, it lies in a working directory or may be edited: a policy's
// glob directories (`/var/folders/*/*/T`) reach Claude only as Edit rules,
// since `additionalDirectories` takes literal paths.
func (r Rules) pathAllowed(tool, path, cwd string) bool {
	if tool == "Read" && (inside(path, append([]string{real(cwd)}, r.Directories...)) || r.pathAllowed("Edit", path, cwd)) {
		return true
	}
	for _, rule := range r.Paths {
		if rule.Effect == "allow" && rule.Tool == tool && rule.pattern(r.Root, cwd, false, true).MatchString(path) {
			return true
		}
	}
	return false
}
