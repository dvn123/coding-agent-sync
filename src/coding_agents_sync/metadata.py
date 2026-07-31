from __future__ import annotations

import re
from typing import Any

import yaml


def to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def snake_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {to_snake(str(k)): snake_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [snake_keys(v) for v in value]
    return value


def markdown_document(meta: dict[str, Any], body: str) -> str:
    if not meta:
        return body.rstrip() + "\n"
    yaml_text = yaml.safe_dump(meta, sort_keys=True, allow_unicode=True).rstrip()
    return f"---\n{yaml_text}\n---\n\n{body.rstrip()}\n"
