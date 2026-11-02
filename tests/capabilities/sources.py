def source_document(description: str, body: str, extra: str = "") -> str:
    return f"---\ndescription: {description}\n{extra}---\n{body}\n"
