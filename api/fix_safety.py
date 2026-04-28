import re


_ALLOWED_VERBS = {"create", "insert", "update", "delete", "alter"}
_BLOCKED_PATTERNS = [
    re.compile(r"\bdrop\b", re.IGNORECASE),
    re.compile(r"\btruncate\b", re.IGNORECASE),
    re.compile(r"\bgrant\b", re.IGNORECASE),
    re.compile(r"\brevoke\b", re.IGNORECASE),
    re.compile(r"\balter\s+system\b", re.IGNORECASE),
    re.compile(r"\bcopy\b\s+.+\bprogram\b", re.IGNORECASE),
]


def _normalize_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("Each query must be a SQL string.")

    normalized = query.strip()
    if not normalized:
        raise ValueError("Queries cannot be empty.")

    body = normalized[:-1].strip() if normalized.endswith(";") else normalized
    if ";" in body:
        raise ValueError("Each list item must contain exactly one SQL statement.")

    return normalized


def _extract_verb(query: str) -> str:
    verb = query.split(None, 1)[0].lower()
    if verb not in _ALLOWED_VERBS:
        raise ValueError(
            "Unsupported SQL verb. Only CREATE, INSERT, UPDATE, DELETE, and ALTER are allowed."
        )
    return verb


def validate_fix_queries(queries: list[str]) -> None:
    if not isinstance(queries, list) or not queries:
        raise ValueError("queries must be a non-empty list of SQL strings.")

    for query in queries:
        normalized = _normalize_query(query)
        for pattern in _BLOCKED_PATTERNS:
            if pattern.search(normalized):
                raise ValueError("Safety violation: dangerous SQL statements are not allowed.")
        _extract_verb(normalized)
