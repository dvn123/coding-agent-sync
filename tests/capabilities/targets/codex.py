from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path


def write_config(
    codex_home: Path,
    cwd: Path,
    base_url: str,
    *,
    approval: str = "on-request",
    skills: Iterable[tuple[Path, bool]] = (),
) -> None:
    lines = [
        'model = "gpt-5.2"',
        'model_provider = "probe"',
        f"approval_policy = {json.dumps(approval)}",
        'sandbox_mode = "danger-full-access"',
        'web_search = "disabled"',
        'model_reasoning_effort = "low"',
        'shell_environment_policy.inherit = "none"',
        "",
        "[model_providers.probe]",
        'name = "Local deterministic probe"',
        f"base_url = {json.dumps(f'{base_url}/v1')}",
        'env_key = "CODEX_CAPABILITY_KEY"',
        'wire_api = "responses"',
        "requires_openai_auth = false",
        "",
        f"[projects.{json.dumps(str(cwd))}]",
        'trust_level = "trusted"',
    ]
    for path, enabled in skills:
        lines += [
            "",
            "[[skills.config]]",
            f"path = {json.dumps(str(path))}",
            f"enabled = {str(enabled).lower()}",
        ]
    (codex_home / "config.toml").write_text("\n".join(lines))
