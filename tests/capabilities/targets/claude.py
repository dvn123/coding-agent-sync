from __future__ import annotations

from capabilities.harness import Paths, sanitized_env


def environment(paths: Paths, base_url: str, **extra: str) -> dict[str, str]:
    return sanitized_env(
        {
            "HOME": str(paths.home),
            "XDG_CONFIG_HOME": str(paths.root / "xdg"),
            "CLAUDE_CONFIG_DIR": str(paths.config),
            "TMPDIR": str(paths.tmp),
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_API_KEY": "capability-placeholder",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_TELEMETRY": "1",
        }
        | extra
    )
