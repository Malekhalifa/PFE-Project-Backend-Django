import re


_ALLOWED_VERBS = {"create", "insert", "update", "delete", "alter"}
_TARGET_PATTERNS = {
    "create": re.compile(
        r"^create\s+table\s+(?:if\s+not\s+exists\s+)?(?P<table>[^\s(]+)",
        re.IGNORECASE,
    ),
    "insert": re.compile(
        r"^insert\s+into\s+(?P<table>[^\s(]+)",
        re.IGNORECASE,
    ),
    "update": re.compile(
        r"^update\s+(?P<table>[^\s(]+)",
        re.IGNORECASE,
    ),
    "delete": re.compile(
        r"^delete\s+from\s+(?P<table>[^\s(]+)",
        re.IGNORECASE,
    ),
    "alter": re.compile(
        r"^alter\s+table\s+(?:only\s+)?(?P<table>[^\s(]+)",
        re.IGNORECASE,
    ),
}


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


def _clean_identifier(identifier: str) -> str:
    token = identifier.rstrip(",")
    token = token.split(".")[-1]
    return token.strip('"')


def _extract_target_table(query: str, verb: str) -> str:
    pattern = _TARGET_PATTERNS[verb]
    match = pattern.match(query)
    if not match:
        raise ValueError(f"Could not determine the target table for {verb.upper()} query.")

    return _clean_identifier(match.group("table"))


def validate_fix_queries(queries: list[str]) -> None:
    if not isinstance(queries, list) or not queries:
        raise ValueError("queries must be a non-empty list of SQL strings.")

    for query in queries:
        normalized = _normalize_query(query)
        verb = _extract_verb(normalized)
        target_table = _extract_target_table(normalized, verb)

        if not target_table.endswith("_clean"):
            raise ValueError(
                "Safety violation: write queries must target only _clean tables. "
                f"Offending target table: {target_table}"
            )
