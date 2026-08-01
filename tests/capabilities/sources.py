from __future__ import annotations


def source_document(
    kind: str, identifier: str, name: str, description: str, body: str, extra: str = ""
) -> str:
    return (
        "---\n"
        "schema: coding-agents/v4\n"
        f"kind: {kind}\n"
        f"id: {identifier}\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{extra}"
        "---\n"
        f"{body}\n"
    )
