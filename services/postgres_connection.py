import fnmatch
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import psycopg2


_POSTGRES_DSN_PATTERN = re.compile(
    r"^postgres(ql)?://"
    r"[^/\s:@]+"
    r"(?::[^/\s@]*)?"
    r"@"
    r"[^/\s:@]+"
    r"(?::\d{1,5})?"
    r"/"
    r"[^?\s#]+"
    r"(?:\?.*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PostgresDsnInfo:
    scheme: str
    host: str | None
    port: int | None
    database: str | None


def looks_like_postgres_dsn(connection_string: str) -> bool:
    if not isinstance(connection_string, str):
        return False
    s = connection_string.strip()
    if not s or s != connection_string:
        return False
    return bool(_POSTGRES_DSN_PATTERN.fullmatch(s))


def parse_postgres_dsn_info(connection_string: str) -> PostgresDsnInfo:
    p = urlparse(connection_string)
    host = p.hostname
    port = p.port
    database = (p.path or "").lstrip("/") or None
    return PostgresDsnInfo(
        scheme=p.scheme or "",
        host=host,
        port=port,
        database=database,
    )


def redact_postgres_dsn(connection_string: str) -> str:
    try:
        p = urlparse(connection_string)
        host = p.hostname or "?"
        port = f":{p.port}" if p.port else ""
        db = (p.path or "").lstrip("/") or "?"
        return f"{p.scheme or 'postgresql'}://***:***@{host}{port}/{db}"
    except Exception:
        return "postgresql://***:***@?/ ?"


def _get_allowed_db_hosts() -> list[str] | None:
    raw = os.environ.get("QUALITYLAB_ALLOWED_DB_HOSTS", "").strip()
    if not raw:
        return None
    return [h.strip() for h in raw.split(",") if h.strip()]


def assert_valid_postgres_dsn(connection_string: str) -> None:
    if not looks_like_postgres_dsn(connection_string):
        raise ValueError(
            "Invalid PostgreSQL connection string format. "
            "Expected: postgresql://user:password@host:port/database"
        )
    info = parse_postgres_dsn_info(connection_string)
    if info.scheme.lower() not in ("postgresql", "postgres"):
        raise ValueError("Invalid PostgreSQL connection string scheme (use postgresql://).")
    if not info.host or not info.database:
        raise ValueError(
            "Invalid PostgreSQL connection string. Host and database are required."
        )
    allowed = _get_allowed_db_hosts()
    if allowed is not None:
        host = info.host or ""
        if not any(fnmatch.fnmatch(host, pattern) for pattern in allowed):
            raise ValueError(
                f"Database host '{host}' is not in the allowed hosts list. "
                "Set QUALITYLAB_ALLOWED_DB_HOSTS to include this host."
            )


def test_postgres_connection(connection_string: str, *, timeout_s: int = 3) -> None:
    """
    Validate DSN and ensure we can connect + run a trivial query.
    Raises ValueError for invalid format and RuntimeError for connectivity failures.
    """
    assert_valid_postgres_dsn(connection_string)
    try:
        conn = psycopg2.connect(connection_string, connect_timeout=timeout_s)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                cur.fetchone()
        finally:
            conn.close()
    except ValueError:
        raise
    except Exception as exc:
        safe = redact_postgres_dsn(connection_string)
        raise RuntimeError(f"Could not connect to PostgreSQL ({safe}): {exc}") from exc

