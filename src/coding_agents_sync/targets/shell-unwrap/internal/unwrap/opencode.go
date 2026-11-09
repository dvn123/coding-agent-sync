package unwrap

import (
	"regexp"
	"runtime"
	"slices"
	"strings"
	"sync"
)

// OpenCode's rule evaluation, ported so a peeled spelling is judged exactly as
// OpenCode judges a command: `Wildcard.match` and the last-match-wins
// `Permission.evaluate` from OpenCode 2.2.6.

// OpenCodeRule is one OpenCode permission rule.
type OpenCodeRule struct {
	Action   string `json:"action"`
	Resource string `json:"resource"`
	Effect   string `json:"effect"`
}

var wildcards sync.Map // pattern -> *regexp.Regexp

// Match is OpenCode's `Wildcard.match`: `*` is any text, `?` any character,
// a trailing ` *` also matches the bare command, and `\` reads as `/`.
func Match(input, pattern string) bool {
	compiled, ok := wildcards.Load(pattern)
	if !ok {
		var body strings.Builder
		for _, char := range strings.ReplaceAll(pattern, `\`, "/") {
			switch char {
			case '*':
				body.WriteString(".*")
			case '?':
				body.WriteString(".")
			default:
				body.WriteString(regexp.QuoteMeta(string(char)))
			}
		}
		escaped := body.String()
		if strings.HasSuffix(escaped, " .*") {
			escaped = strings.TrimSuffix(escaped, " .*") + "( .*)?"
		}
		flags := "(?s)"
		if runtime.GOOS == "windows" {
			flags = "(?si)"
		}
		compiled = regexp.MustCompile(flags + "^" + escaped + "$")
		wildcards.Store(pattern, compiled)
	}
	return compiled.(*regexp.Regexp).MatchString(strings.ReplaceAll(input, `\`, "/"))
}

// deciding is the rule that decides resource: the last one matching.
func deciding(rules []OpenCodeRule, action, resource string) *OpenCodeRule {
	for i := len(rules) - 1; i >= 0; i-- {
		if Match(action, rules[i].Action) && Match(resource, rules[i].Resource) {
			return &rules[i]
		}
	}
	return nil
}

func evaluate(rules []OpenCodeRule, action, resource string) string {
	if rule := deciding(rules, action, resource); rule != nil {
		return rule.Effect
	}
	return "ask"
}

// Evaluation is OpenCode's `permission.evaluate` event, as the plugin passes
// it: Rules is nil until the plugin has loaded them.
type Evaluation struct {
	Action    string         `json:"action"`
	Resources []string       `json:"resources"`
	Effect    string         `json:"effect"`
	Rules     []OpenCodeRule `json:"rules"`
	Reasons   Reasons        `json:"reasons"`
	Policy    Policy         `json:"policy"`
}

// Verdict is the review's answer: the new effect and message, or a request
// for the rules when there is something to peel and they are not loaded.
type Verdict struct {
	Effect     string `json:"effect,omitempty"`
	Message    string `json:"message,omitempty"`
	NeedsRules bool   `json:"needsRules,omitempty"`
}

// Review denies a shell command when the rules deny any spelling peeled from
// it, so a wrapped command is never looser than the denied command it runs.
// The deny names the reason its rule carries. An allowed command asks when an
// ask rule matches a peeled spelling. Otherwise an asked command is allowed
// when every resource reaches an allow through transparent wrappers and
// harmless assignments only (`timeout 30 git status`); program paths, other
// assignments, and wrappers that change the command, its arguments, or its
// user never carry an allow, so `./gradlew` does not inherit anything from
// `gradlew`.
func Review(event Evaluation) Verdict {
	if event.Action != "shell" || event.Effect == "deny" {
		return Verdict{}
	}
	var peeled []string
	for _, resource := range event.Resources {
		for _, spelling := range Peeled(resource) {
			if !slices.Contains(peeled, spelling) {
				peeled = append(peeled, spelling)
			}
		}
	}
	if len(peeled) == 0 {
		return Verdict{}
	}
	if event.Rules == nil {
		return Verdict{NeedsRules: true}
	}
	for _, spelling := range peeled {
		if evaluate(event.Rules, "shell", spelling) == "deny" {
			return Verdict{Effect: "deny", Message: Denial(spelling, because(event.Rules, spelling, event.Reasons))}
		}
	}
	if event.Effect == "allow" {
		for _, spelling := range peeled {
			if rule := deciding(event.Rules, "shell", spelling); rule != nil && rule.Resource != "*" && rule.Effect == "ask" {
				return Verdict{Effect: "ask"}
			}
		}
		return Verdict{}
	}
	if event.Effect == "ask" {
		for _, resource := range event.Resources {
			// An allow is carried only where zsh fixes every word.
			if _, static := decoded(resource); !static || !passesOpenCode(event.Rules, event.Policy, resource) {
				return Verdict{}
			}
		}
		return Verdict{Effect: "allow"}
	}
	return Verdict{}
}

// passesOpenCode is whether command is allowed outright, or reaches an allow
// rule through transparent wrappers alone. Only the catch-all is looked
// through: a narrower rule that matches a spelling on the way decides it, so
// an ask written for `timeout * bq cp` still asks.
func passesOpenCode(rules []OpenCodeRule, policy Policy, command string) bool {
	if evaluate(rules, "shell", command) == "allow" {
		return true
	}
	for words, ok := TextWords(command), true; ok; words, ok = policy.Peel(words) {
		if rule := deciding(rules, "shell", strings.Join(words, " ")); rule != nil && rule.Resource != "*" {
			return rule.Effect == "allow"
		}
	}
	return false
}

// Explain is the reason for a shell command OpenCode's own rules denied.
// OpenCode runs `permission.evaluate` only for what its rules did not deny,
// and answers the rest with a bare "Permission denied", so this reads the
// blocked call's resources and deciding rules instead: the first resource
// whose deciding rule carries a reason names it.
func Explain(resources []string, rules []OpenCodeRule, reasons Reasons) string {
	for _, resource := range resources {
		if reason := because(rules, resource, reasons); reason != "" {
			return "`" + resource + "`: " + reason
		}
	}
	return ""
}

func because(rules []OpenCodeRule, resource string, reasons Reasons) string {
	if rule := deciding(rules, "shell", resource); rule != nil && rule.Effect == "deny" {
		return reasons[rule.Resource]
	}
	return ""
}

// decoded is a resource as zsh passes it, when zsh fixes every word: one
// simple command with no run-time part and no redirect to a file.
func decoded(resource string) (string, bool) {
	steps, err := Steps(resource)
	if err != nil || len(steps) != 1 || steps[0].Words == nil {
		return "", false
	}
	values := make([]string, 0, len(steps[0].Words))
	for _, word := range steps[0].Words {
		if word.Vanishes || strings.Contains(word.Text, Wild) {
			return "", false
		}
		values = append(values, word.Text)
	}
	return strings.Join(values, " "), true
}
