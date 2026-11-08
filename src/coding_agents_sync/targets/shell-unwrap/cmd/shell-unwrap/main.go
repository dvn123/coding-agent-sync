// Command shell-unwrap applies shell permission rules to wrapped commands as
// a Claude Code, Codex or Cursor hook, and serves the OpenCode plugin and the
// Pi extension. The host is the first argument rather than read from the
// payload: a payload that fails to parse cannot name its host, and the hosts
// fail differently, Claude and Cursor by asking and Codex by denying.
//
//	shell-unwrap claude|codex|cursor           a hook: event on stdin, decision on stdout
//	shell-unwrap opencode review|explain|batch the OpenCode plugin's review, JSON in and out
//	shell-unwrap spellings|transparent         the peel of each argument, or of each command in a JSON array on stdin
//	shell-unwrap decide HOST SETTINGS          a host's decision for each {script, cwd} in a JSON array on stdin
//	shell-unwrap why SETTINGS CWD              each stdin line's decision, its message, and why it is not allowed
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/dvn123/shell-unwrap/internal/unwrap"
)

const (
	reasonsFile = "shell-unwrap.json"
	failed      = "shell-unwrap: the hook failed, so its guards were not checked"
)

var hosts = map[string]func() error{
	"claude":      claude,
	"codex":       codex,
	"cursor":      cursor,
	"opencode":    opencode,
	"spellings":   spellings,
	"transparent": transparent,
	"decide":      decide,
	"why":         why,
}

func main() {
	if len(os.Args) < 2 || hosts[os.Args[1]] == nil {
		fmt.Fprintln(os.Stderr, "usage: shell-unwrap claude|codex|cursor|opencode|spellings|transparent|decide|why")
		os.Exit(2)
	}
	if err := hosts[os.Args[1]](); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// here is the directory of the binary, with symlinks resolved.
func here() (string, error) {
	path, err := os.Executable()
	if err != nil {
		return "", err
	}
	if path, err = filepath.EvalSymlinks(path); err != nil {
		return "", err
	}
	return filepath.Dir(path), nil
}

func write(value any) error {
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetEscapeHTML(false)
	return encoder.Encode(value)
}

type hookEvent struct {
	ToolName  string `json:"tool_name"`
	ToolInput struct {
		Command *string `json:"command"`
	} `json:"tool_input"`
	Cwd            string `json:"cwd"`
	PermissionMode string `json:"permission_mode"`
}

type hookOutput struct {
	HookEventName string `json:"hookEventName"`
	unwrap.Decision
}

func readEvent(event any) error {
	data, err := io.ReadAll(os.Stdin)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, event)
}

// claude is the Claude Code PreToolUse hook. It reads the rules from the
// Claude root, the parent of the `hooks/` directory it sits in.
func claude() error {
	var event hookEvent
	decision := func() *unwrap.Decision {
		if err := readEvent(&event); err != nil {
			return &unwrap.Decision{Decision: "ask", Reason: failed}
		}
		if event.ToolName != "Bash" || event.ToolInput.Command == nil {
			return nil
		}
		dir, err := here()
		if err != nil {
			return &unwrap.Decision{Decision: "ask", Reason: failed}
		}
		root := filepath.Dir(dir)
		rules, err := unwrap.LoadRules(filepath.Join(root, "settings.json"), filepath.Join(root, reasonsFile))
		if err != nil {
			return &unwrap.Decision{Decision: "ask", Reason: failed}
		}
		cwd := event.Cwd
		if cwd == "" {
			cwd, _ = os.Getwd()
		}
		return rules.Decide(*event.ToolInput.Command, cwd)
	}()
	// Bypass mode skips Claude Code's prompts, but not a hook's ask. Its denies
	// still apply, as Claude Code's own deny rules do.
	if decision == nil || decision.Decision == "ask" && event.PermissionMode == "bypassPermissions" {
		return nil
	}
	return write(map[string]hookOutput{"hookSpecificOutput": {"PreToolUse", *decision}})
}

// codex is the Codex PreToolUse hook, which the Pi extension also runs. It
// only denies: Codex rejects an ask from this hook. It denies when it cannot
// read its rules or fails, except in bypass mode.
func codex() error {
	var event hookEvent
	reason := func() string {
		if err := readEvent(&event); err != nil {
			return failed
		}
		if event.ToolName != "Bash" || event.ToolInput.Command == nil {
			return ""
		}
		rules, err := besideRules()
		if err != nil {
			return failed
		}
		return rules.CodexDenial(*event.ToolInput.Command, workingDirectory(event.Cwd))
	}()
	if reason == "" || reason == failed && event.PermissionMode == "bypassPermissions" {
		return nil
	}
	return write(map[string]hookOutput{"hookSpecificOutput": {"PreToolUse", unwrap.Decision{Decision: "deny", Reason: reason}}})
}

// besideRules reads the rules and reasons from the `shell-unwrap.json` beside
// the binary.
func besideRules() (unwrap.Rules, error) {
	dir, err := here()
	if err != nil {
		return unwrap.Rules{}, err
	}
	path := filepath.Join(dir, reasonsFile)
	return unwrap.LoadRules(path, path)
}

type cursorVerdict struct {
	Permission   string `json:"permission"`
	UserMessage  string `json:"user_message"`
	AgentMessage string `json:"agent_message"`
}

// cursor is the Cursor beforeShellExecution hook. Cursor never reads an allow
// from it, so it only adds prompts and refusals, and asks when it cannot
// judge, so its guards fail closed.
func cursor() error {
	decision := func() *unwrap.Decision {
		ask := func(message string) *unwrap.Decision { return &unwrap.Decision{Decision: "ask", Reason: message} }
		var event struct {
			Command *string `json:"command"`
			Cwd     string  `json:"cwd"`
		}
		if err := readEvent(&event); err != nil {
			return ask("shell-unwrap: the hook failed")
		}
		if event.Command == nil {
			return ask("shell-unwrap: the hook received no command")
		}
		rules, err := besideRules()
		if err != nil {
			return ask("shell-unwrap: the hook failed")
		}
		return rules.CursorDecision(*event.Command, workingDirectory(event.Cwd))
	}()
	if decision == nil {
		return write(struct{}{})
	}
	return write(cursorVerdict{decision.Decision, decision.Reason, decision.Reason})
}

// opencode serves the OpenCode plugin: `review` takes an Evaluation and
// answers a Verdict; `explain` takes resources, rules and reasons and answers
// the reason OpenCode's own deny carries; `batch` reviews many events under
// one set of rules and answers each one's effect, for tools that replay it.
func opencode() error {
	if len(os.Args) < 3 {
		return fmt.Errorf("usage: shell-unwrap opencode review|explain|batch")
	}
	switch os.Args[2] {
	case "review":
		var event unwrap.Evaluation
		if err := readEvent(&event); err != nil {
			return err
		}
		return write(unwrap.Review(event))
	case "explain":
		var input struct {
			Resources []string              `json:"resources"`
			Rules     []unwrap.OpenCodeRule `json:"rules"`
			Reasons   unwrap.Reasons        `json:"reasons"`
		}
		if err := readEvent(&input); err != nil {
			return err
		}
		return write(map[string]string{"reason": unwrap.Explain(input.Resources, input.Rules, input.Reasons)})
	case "batch":
		var input struct {
			Rules   []unwrap.OpenCodeRule `json:"rules"`
			Reasons unwrap.Reasons        `json:"reasons"`
			Events  []unwrap.Evaluation   `json:"events"`
		}
		if err := readEvent(&input); err != nil {
			return err
		}
		effects := make([]string, len(input.Events))
		for i, event := range input.Events {
			event.Rules, event.Reasons = input.Rules, input.Reasons
			if event.Rules == nil {
				event.Rules = []unwrap.OpenCodeRule{}
			}
			effects[i] = event.Effect
			if verdict := unwrap.Review(event); verdict.Effect != "" {
				effects[i] = verdict.Effect
			}
		}
		return write(effects)
	}
	return fmt.Errorf("unknown opencode operation %q", os.Args[2])
}

// spellings prints each argument's peel as a map, or, with no arguments, the
// peel of each command in the JSON array on stdin as an array in order.
func spellings() error {
	if len(os.Args) == 2 {
		var commands []string
		if err := readEvent(&commands); err != nil {
			return err
		}
		found := make([][]string, len(commands))
		for i, command := range commands {
			found[i] = append([]string{}, unwrap.Spellings(command)...)
		}
		return write(found)
	}
	found := map[string][]string{}
	for _, command := range os.Args[2:] {
		found[command] = append([]string{}, unwrap.Spellings(command)...)
	}
	return write(found)
}

// transparent prints the layer each argument's transparent wrapper runs, as
// a map, or, with no arguments, that of each command in the JSON array on
// stdin as an array in order; null where none.
func transparent() error {
	layer := func(command string) *string {
		if inner, ok := unwrap.Transparent(command); ok {
			return &inner
		}
		return nil
	}
	if len(os.Args) == 2 {
		var commands []string
		if err := readEvent(&commands); err != nil {
			return err
		}
		found := make([]*string, len(commands))
		for i, command := range commands {
			found[i] = layer(command)
		}
		return write(found)
	}
	found := map[string]*string{}
	for _, command := range os.Args[2:] {
		found[command] = layer(command)
	}
	return write(found)
}

// decide judges each `{"script", "cwd"}` in the JSON array on stdin as the
// host does under a settings or rules file, and prints the decisions in
// order: allow, ask, or deny, or null to leave the command to the host.
func decide() error {
	if len(os.Args) < 4 {
		return fmt.Errorf("usage: shell-unwrap decide claude|codex|cursor SETTINGS < items")
	}
	host, settings := os.Args[2], os.Args[3]
	rules, err := unwrap.LoadRules(settings, filepath.Join(filepath.Dir(settings), reasonsFile))
	if err != nil {
		return err
	}
	var items []struct {
		Script string `json:"script"`
		Cwd    string `json:"cwd"`
	}
	if err := readEvent(&items); err != nil {
		return err
	}
	decisions := make([]*string, len(items))
	for i, item := range items {
		var found *unwrap.Decision
		switch host {
		case "claude":
			found = rules.Decide(item.Script, item.Cwd)
		case "cursor":
			found = rules.CursorDecision(item.Script, item.Cwd)
		case "codex":
			if reason := rules.CodexDenial(item.Script, item.Cwd); reason != "" {
				found = &unwrap.Decision{Decision: "deny", Reason: reason}
			}
		default:
			return fmt.Errorf("unknown host %q", host)
		}
		if found != nil {
			decisions[i] = &found.Decision
		}
	}
	return write(decisions)
}

// why judges each stdin line as a Claude Code command under a settings file
// and prints the decision and, for a command not allowed, why.
func why() error {
	if len(os.Args) < 4 {
		return fmt.Errorf("usage: shell-unwrap why SETTINGS CWD < commands")
	}
	rules, err := unwrap.LoadRules(os.Args[2], filepath.Join(filepath.Dir(os.Args[2]), reasonsFile))
	if err != nil {
		return err
	}
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 1<<20), 1<<24)
	encoder := json.NewEncoder(os.Stdout)
	for scanner.Scan() {
		var script string
		if err := json.Unmarshal(scanner.Bytes(), &script); err != nil {
			return err
		}
		decision, message, reason := "abstain", "", ""
		if d := rules.Decide(script, os.Args[3]); d != nil {
			decision, message = d.Decision, d.Reason
		}
		if decision != "allow" {
			reason = rules.Why(script, os.Args[3])
		}
		if err := encoder.Encode(map[string]string{"decision": decision, "message": message, "why": reason}); err != nil {
			return err
		}
	}
	return scanner.Err()
}

// workingDirectory is the event's working directory, or the hook's own.
func workingDirectory(cwd string) string {
	if cwd == "" {
		cwd, _ = os.Getwd()
	}
	return cwd
}
