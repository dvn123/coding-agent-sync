package unwrap

// The decisions of the hosts that take no allow from the hook: their guards
// on every command as written, then the deny or ask the zsh analysis is
// certain of.

// CodexDenial is the reason Codex denies script, or "": Codex takes only a
// deny from the hook.
func (r Rules) CodexDenial(script, cwd string) string {
	for _, command := range Guarded(script) {
		if message := r.Denied(command); message != "" {
			return message
		}
	}
	if certain := r.Certain(script, cwd); certain != nil && certain.Decision == "deny" {
		return certain.Reason
	}
	return ""
}

// CursorDecision is Cursor's deny or ask for script, or nil to leave it to
// the allowlist. It asks for a script it cannot judge, so the guards fail
// closed.
func (r Rules) CursorDecision(script, cwd string) *Decision {
	if len(script) > MaxCommand {
		return &Decision{"ask", "shell-unwrap: the command is too long to check"}
	}
	found, parsed := Scan(script)
	if !parsed {
		return &Decision{"ask", "shell-unwrap: the command could not be parsed"}
	}
	for _, command := range found {
		if message := r.Denied(command); message != "" {
			return &Decision{"deny", message}
		}
	}
	for _, command := range found {
		if spelling := r.Asked(command); spelling != "" {
			return &Decision{"ask", "shell-unwrap: `" + spelling + "` needs approval"}
		}
	}
	return r.Certain(script, cwd)
}
