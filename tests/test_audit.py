from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agents_sync.audit import ClaudeRules, OpenCodeRules, unwrap_spellings

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
    "/usr/bin/env probe arg": False,
    "xargs probe arg": True,
    "xargs -n 1 probe arg": False,
    "xargs -I{} probe {}": False,
    "sudo probe arg": True,
    "timeout 30 probe arg": True,
    "nice -n 5 probe arg": True,
    "/usr/bin/nice -n 5 probe arg": False,
    "/bin/probe arg": False,
    "\\probe arg": True,
    '"probe" arg': True,
    "> /dev/null probe arg": False,
    "2>&1 probe arg": False,
}


@pytest.mark.parametrize(("command", "denied"), DENY_REACH.items())
def test_claude_model_matches_live_deny_reach(command: str, denied: bool) -> None:
    rules = ClaudeRules({"allow": ["Bash"], "deny": ["Bash(probe *)"]})
    assert (rules.verdict(command) == "deny") is denied


# The opencode-unwrap plugin's golden table (its `test/spellings.json`), copied
# with each vendored bundle: the port must peel every command to exactly the
# plugin's spellings.
SPELLINGS = json.loads(
    (Path(__file__).parent / "fixtures/opencode-unwrap/spellings.json").read_text()
)


@pytest.mark.parametrize(("command", "expected"), SPELLINGS.items())
def test_unwrap_port_peels_exactly_like_the_plugin(
    command: str, expected: list[str]
) -> None:
    assert unwrap_spellings(command) == expected


def test_opencode_model_carries_over_only_a_peeled_deny() -> None:
    rules = OpenCodeRules([("*", "allow"), ("git push *", "ask"), ("rm *", "deny")])
    assert rules.decision("timeout 30 rm -rf x") == "deny"
    assert rules.decision("FOO=1 git push origin") == "allow"
    assert rules.decision("echo rm -rf x") == "allow"
    assert rules.native("timeout 30 rm -rf x") == "allow"
