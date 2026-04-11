"""
guidelines_store.py — Versioned, file-based persistence layer for agent guidelines.

Each database gets one JSON file under guidelines_store/{database_id}.json.
Every pipeline run appends a new version entry — old versions are never overwritten.

Public API
----------
    store = GuidelinesStore(connection_string)
    prior = store.load_latest()        # -> dict | None
    store.save_version(guidelines, schema_snapshot)

File format
-----------
{
  "database_id": "localhost_5432_mydb",
  "versions": [
    {
      "version": 1,
      "created_at": "<ISO timestamp>",
      "schema_snapshot": "<md5 hash>",
      "guidelines": {
        "schema_integrity_validator": "...",
        "column_validator": "...",
        "row_level_validator": "...",
        "cross_row_validator": "...",
        "data_fix_planner": "..."
      }
    }
  ],
  "latest_version": 1
}
"""

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_STORE_DIR = os.path.join(os.path.dirname(__file__), "guidelines_store")


# ---------------------------------------------------------------------------
# Database identifier helpers
# ---------------------------------------------------------------------------

def _derive_database_id(connection_string: str) -> str:
    """
    Build a stable, filesystem-safe key from the DSN.

    Uses only host, port, and database name — credentials are never included.
    Example: 'postgresql://user:pass@localhost:5432/mydb' → 'localhost_5432_mydb'
    """
    try:
        parsed = urlparse(connection_string)
        host = parsed.hostname or "unknown"
        port = str(parsed.port or 5432)
        dbname = (parsed.path or "/unknown").lstrip("/") or "unknown"
        raw = f"{host}_{port}_{dbname}"
        # Make filesystem-safe: replace any non-alphanumeric/dash/dot/underscore char
        safe = re.sub(r"[^a-zA-Z0-9._-]", "_", raw)
        return safe
    except Exception:
        # Ultimate fallback — hash the stripped connection string
        stripped = re.sub(r"://[^@]+@", "://***@", connection_string)
        return hashlib.md5(stripped.encode()).hexdigest()[:16]


def compute_schema_snapshot(schema_info: str) -> str:
    """
    Compute a lightweight MD5 hash of schema info text.

    Typically called with a sorted, normalised string of 'table.column:type' lines
    so that the snapshot changes only when the schema actually changes.
    """
    return hashlib.md5(schema_info.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# File locking (simple .lock file — good enough for single-machine use)
# ---------------------------------------------------------------------------

class _FileLock:
    """Minimal advisory file lock using a .lock sentinel file."""

    TIMEOUT = 30       # seconds to wait before giving up
    POLL_INTERVAL = 0.2

    def __init__(self, path: str):
        self._lock_path = path + ".lock"

    def __enter__(self):
        deadline = time.monotonic() + self.TIMEOUT
        while True:
            try:
                # O_CREAT | O_EXCL is atomic on POSIX
                fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    # Stale lock — forcibly remove and retry
                    logger.warning(
                        "GuidelinesStore: stale lock detected at %s — removing.",
                        self._lock_path,
                    )
                    try:
                        os.remove(self._lock_path)
                    except OSError:
                        pass
                    continue
                time.sleep(self.POLL_INTERVAL)

    def __exit__(self, *_):
        try:
            os.remove(self._lock_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# GuidelinesStore
# ---------------------------------------------------------------------------

class GuidelinesStore:
    """
    Manages read/write access to the versioned guidelines file for one database.

    Usage::

        store = GuidelinesStore(connection_string)
        prior = store.load_latest()  # None on first run
        # ... run pipeline ...
        store.save_version(
            guidelines={
                "schema_integrity_validator": "...",
                "column_validator": "...",
                "row_level_validator": "...",
                "cross_row_validator": "...",
                "data_fix_planner": "...",
            },
            schema_snapshot="<md5 hash>",
        )
    """

    def __init__(self, connection_string: str):
        self.database_id = _derive_database_id(connection_string)
        self._file_path = os.path.join(_STORE_DIR, f"{self.database_id}.json")
        os.makedirs(_STORE_DIR, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_latest(self) -> dict[str, Any] | None:
        """
        Load the most recent guidelines version for this database.

        Returns the guidelines dict from the latest version entry,
        or None if no file exists or the file is unreadable.

        The returned dict has the shape::

            {
                "schema_integrity_validator": "...",
                "column_validator": "...",
                "row_level_validator": "...",
                "cross_row_validator": "...",
                "data_fix_planner": "...",
                "_version": 2,
                "_created_at": "2026-01-01T00:00:00+00:00",
                "_schema_snapshot": "<md5>",
            }
        """
        if not os.path.exists(self._file_path):
            return None

        try:
            with open(self._file_path, encoding="utf-8") as fh:
                data = json.load(fh)

            latest_num = data.get("latest_version", 0)
            if latest_num == 0 or not data.get("versions"):
                return None

            # Find the version entry without loading all into memory
            # (versions list is typically tiny, but we iterate once)
            entry = next(
                (v for v in data["versions"] if v.get("version") == latest_num),
                None,
            )
            if entry is None:
                logger.warning(
                    "GuidelinesStore: latest_version=%s not found in versions array for %s",
                    latest_num,
                    self.database_id,
                )
                return None

            guidelines = dict(entry.get("guidelines", {}))
            guidelines["_version"] = entry.get("version")
            guidelines["_created_at"] = entry.get("created_at")
            guidelines["_schema_snapshot"] = entry.get("schema_snapshot")
            return guidelines

        except Exception as exc:
            logger.warning(
                "GuidelinesStore: could not read %s — proceeding without prior guidelines. Error: %s",
                self._file_path,
                exc,
            )
            return None

    def save_version(
        self,
        guidelines: dict[str, str],
        schema_snapshot: str,
    ) -> int:
        """
        Append a new version entry to the guidelines file.

        Uses a file lock + atomic write (tmp file → os.replace) to be
        safe against concurrent pipeline runs.

        Returns the new version number.
        """
        with _FileLock(self._file_path):
            # Read current state (if any)
            data = self._read_raw()

            if data is None:
                data = {
                    "database_id": self.database_id,
                    "versions": [],
                    "latest_version": 0,
                }

            next_version = data["latest_version"] + 1
            entry = {
                "version": next_version,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "schema_snapshot": schema_snapshot,
                "guidelines": guidelines,
            }
            data["versions"].append(entry)
            data["latest_version"] = next_version

            # Atomic write: write to .tmp then rename
            tmp_path = self._file_path + ".tmp"
            try:
                with open(tmp_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self._file_path)
            except Exception:
                # Clean up temp file on error
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
                raise

            logger.info(
                "GuidelinesStore: saved version %s for database '%s'.",
                next_version,
                self.database_id,
            )
            return next_version

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_raw(self) -> dict | None:
        """Read and parse the JSON file. Returns None if absent or unreadable."""
        if not os.path.exists(self._file_path):
            return None
        try:
            with open(self._file_path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            logger.warning(
                "GuidelinesStore: failed to read %s: %s", self._file_path, exc
            )
            return None
