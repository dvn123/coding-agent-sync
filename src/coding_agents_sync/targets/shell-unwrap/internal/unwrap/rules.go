package unwrap

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
)

// Rule is one Bash rule pattern and the reason its deny names, if any.
type Rule struct {
	Pattern string
	Reason  string
}

// bucket holds rules by the literal first word of their pattern; "" holds
// the rest.
type bucket map[string][]Rule

// Rules is a host's Bash rules and the directories Claude Code lets commands
// work in.
type Rules struct {
	Allow, Ask, Deny bucket
	Directories      []string
	// Paths are the Read and Edit rules, and Root the settings file's
	// directory, which anchors a rule written `/path`.
	Paths []PathRule
	Root  string
}

// Permissions is the `permissions` object of Claude Code's settings, and of
// the `shell-unwrap.json` the other hosts read.
type Permissions struct {
	Allow                 []string `json:"allow"`
	Ask                   []string `json:"ask"`
	Deny                  []string `json:"deny"`
	AdditionalDirectories []string `json:"additionalDirectories"`
}

// Reasons says why a deny rule exists, by the rule's exact pattern.
type Reasons map[string]string

func head(pattern string) string {
	first, _, _ := strings.Cut(pattern, " ")
	if strings.Contains(first, "*") {
		return ""
	}
	return first
}

func bashPatterns(entries []string) []string {
	var found []string
	for _, entry := range entries {
		switch {
		case entry == "Bash":
			found = append(found, "*")
		case strings.HasPrefix(entry, "Bash(") && strings.HasSuffix(entry, ")"):
			found = append(found, entry[len("Bash("):len(entry)-1])
		}
	}
	return found
}

func newBucket(entries []string, reasons Reasons) bucket {
	found := bucket{}
	for _, pattern := range bashPatterns(entries) {
		key := head(pattern)
		found[key] = append(found[key], Rule{Pattern: pattern, Reason: reasons[pattern]})
	}
	return found
}

// BuildRules indexes a permissions object's Bash rules.
func BuildRules(permissions Permissions, reasons Reasons) Rules {
	directories := make([]string, 0, len(permissions.AdditionalDirectories))
	for _, directory := range permissions.AdditionalDirectories {
		directories = append(directories, real(directory))
	}
	paths := append(pathRules("allow", permissions.Allow), pathRules("ask", permissions.Ask)...)
	return Rules{
		Allow:       newBucket(permissions.Allow, reasons),
		Ask:         newBucket(permissions.Ask, reasons),
		Deny:        newBucket(permissions.Deny, reasons),
		Directories: directories,
		Paths:       append(paths, pathRules("deny", permissions.Deny)...),
	}
}

// ReadReasons reads `{"reasons": {"<rule pattern>": "<reason>"}}`. Reasons
// are advice, so a missing or unreadable file leaves them out rather than
// failing the caller, which would drop the wrapped-command denies with it.
func ReadReasons(path string) Reasons {
	var file struct {
		Reasons map[string]any `json:"reasons"`
	}
	data, err := os.ReadFile(path)
	if err != nil || json.Unmarshal(data, &file) != nil {
		return Reasons{}
	}
	reasons := Reasons{}
	for pattern, reason := range file.Reasons {
		if text, ok := reason.(string); ok {
			reasons[pattern] = text
		}
	}
	return reasons
}

// LoadRules reads the `permissions` of a settings file and the reasons file.
func LoadRules(settings, reasons string) (Rules, error) {
	var file struct {
		Permissions Permissions `json:"permissions"`
	}
	data, err := os.ReadFile(settings)
	if err != nil {
		return Rules{}, err
	}
	if err := json.Unmarshal(data, &file); err != nil {
		return Rules{}, err
	}
	rules := BuildRules(file.Permissions, ReadReasons(reasons))
	rules.Root = filepath.Dir(settings)
	return rules, nil
}

// matching is the rules in b whose glob matches text.
func (b bucket) matching(text string) []Rule {
	word, _, _ := strings.Cut(text, " ")
	var found []Rule
	for _, rule := range append(append([]Rule{}, b[word]...), b[""]...) {
		if Glob(rule.Pattern).MatchString(text) {
			found = append(found, rule)
		}
	}
	return found
}

// candidates are the rules a template may meet: its program's, those without
// one, or all when the program is decided at run time.
func (b bucket) candidates(template string) []Rule {
	program, _, _ := strings.Cut(template, " ")
	if strings.Contains(program, Wild) {
		var all []Rule
		for _, rules := range b {
			all = append(all, rules...)
		}
		return all
	}
	return append(append([]Rule{}, b[program]...), b[""]...)
}

// Denial is the message for a denied command.
func Denial(command, reason string) string {
	message := "`" + command + "` is denied by the shell rules"
	if reason != "" {
		message += ": " + reason
	}
	return message
}

// Denied is the denial for the first of command's spellings a deny rule
// matches, or "".
func (r Rules) Denied(command string) string {
	for _, spelling := range append([]string{command}, Spellings(command)...) {
		found := r.Deny.matching(spelling)
		if len(found) == 0 {
			continue
		}
		for _, rule := range found {
			if rule.Reason != "" {
				return Denial(spelling, rule.Reason)
			}
		}
		return Denial(spelling, "")
	}
	return ""
}

// Asked is the first of command's spellings an ask rule matches, or "".
func (r Rules) Asked(command string) string {
	for _, spelling := range append([]string{command}, Spellings(command)...) {
		if len(r.Ask.matching(spelling)) > 0 {
			return spelling
		}
	}
	return ""
}

// real is a path with `~` expanded and symlinks resolved as far as it exists.
func real(path string) string {
	home, _ := os.UserHomeDir()
	if path == "~" || strings.HasPrefix(path, "~/") {
		path = home + path[1:]
	}
	absolute, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	return resolve(absolute)
}

// maxLinks is how many symlinks resolve follows, as the kernel stops at 40.
const maxLinks = 40

// resolve follows every symlink in an absolute path, component by
// component, including one whose target does not exist yet: writing through
// it creates the target. Components past the first that does not exist stay
// as written.
func resolve(absolute string) string {
	pending := strings.Split(strings.TrimPrefix(absolute, "/"), "/")
	done := "/"
	for hops := 0; len(pending) > 0; {
		part := pending[0]
		pending = pending[1:]
		switch part {
		case "", ".":
			continue
		case "..":
			done = filepath.Dir(done)
			continue
		}
		next := filepath.Join(done, part)
		info, err := os.Lstat(next)
		if err != nil {
			return filepath.Join(append([]string{next}, pending...)...)
		}
		if info.Mode()&os.ModeSymlink == 0 {
			done = next
			continue
		}
		hops++
		target, err := os.Readlink(next)
		if err != nil || hops > maxLinks {
			return filepath.Join(append([]string{next}, pending...)...)
		}
		if filepath.IsAbs(target) {
			done = "/"
		}
		pending = append(strings.Split(strings.TrimPrefix(target, "/"), "/"), pending...)
	}
	return done
}

func inside(target string, directories []string) bool {
	for _, root := range directories {
		prefix := root
		if !strings.HasSuffix(prefix, "/") {
			prefix += "/"
		}
		if target == root || strings.HasPrefix(target, prefix) {
			return true
		}
	}
	return false
}
