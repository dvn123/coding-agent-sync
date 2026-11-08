import pytest
from capabilities.audit import ClaudeRules, OpenCodeRules

# The live `deny-reach` scenarios in test_claude_permissions, replayed against
# the model: a bare deny with everything else allowed.
DENY_REACH = {
    "probe arg": True,
    "echo probe arg": False,
    "FOO=bar probe arg": True,
    "env probe arg": True,
    "env FOO=bar probe arg": True,
    "env -u HOME probe arg": True,
    "env -S 'probe arg'": True,
    "xargs probe arg": True,
    "xargs -n 1 probe arg": False,
    "xargs -I{} probe {}": False,
    "sudo probe arg": True,
    "timeout 30 probe arg": True,
    "nice -n 5 probe arg": True,
    "\\probe arg": True,
    '"probe" arg': True,
    "> /dev/null probe arg": False,
    "2>&1 probe arg": False,
    "/bin/probe arg": True,
    "/bin/probe": True,
    "/usr/bin/probe arg": True,
    "env /bin/probe arg": True,
    "/bin/time probe arg": True,
    "/bin/nohup probe arg": True,
    "/bin/command probe arg": True,
    "/bin/builtin probe arg": True,
    "/bin/noglob probe arg": True,
    "/bin/env probe arg": True,
    "/bin/sudo probe arg": True,
    "/bin/xargs probe arg": True,
    "/bin/timeout 30 probe arg": True,
    "/usr/bin/nice -n 5 probe arg": True,
    "/bin/stdbuf -oL probe arg": True,
    "/usr/bin/env probe arg": True,
    "/bin/xargs -n 1 probe arg": False,
}


@pytest.mark.parametrize(("command", "denied"), DENY_REACH.items())
def test_claude_model_matches_live_deny_reach(command: str, denied: bool) -> None:
    rules = ClaudeRules({"allow": ["Bash"], "deny": ["Bash(probe *)"]})
    assert (rules.verdict(command) == "deny") is denied


def test_claude_model_adds_the_hook_deny() -> None:
    rules = ClaudeRules({"allow": ["Bash"], "deny": ["Bash(probe *)"]})
    assert rules.verdict("xargs -n 1 probe arg") == "allow"
    assert rules.decision("xargs -n 1 probe arg") == "deny"
    assert rules.decision("echo probe arg") == "allow"


# The peel itself is pinned by shell-unwrap's own tests; the model asks the
# vendored bundle for it.
def test_opencode_model_carries_over_a_peeled_deny_or_ask() -> None:
    rules = OpenCodeRules([("*", "allow"), ("git push *", "ask"), ("rm *", "deny")])
    assert rules.decision("timeout 30 rm -rf x") == "deny"
    assert rules.decision("FOO=1 git push origin") == "ask"
    assert rules.decision("echo rm -rf x") == "allow"
    assert OpenCodeRules([("*", "ask")]).decision("FOO=1 ls") == "ask"
    assert rules.native("timeout 30 rm -rf x") == "allow"
