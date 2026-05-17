"""
Fix Planner post-processor: verification execution, contradiction detection, graph repair.

Runs AFTER the Fix Planner LLM agent completes. Zero LLM calls. Six passes:
  1. Run verification_queries against the live DB → populate card.verification_results
  2. Contradiction detector: override cards that dismiss real py_profiler evidence
  3. DELETE overlap detector: inject missing depends_on for same-table DELETE pairs
  4. source_rule backfill from reduced findings
  5. evidence_payload.column_name correction for grouped-FK expansion copies
  6. severity / source_type / confidence sync from source findings
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

import psycopg2
from psycopg2.extras import RealDictCursor

if TYPE_CHECKING:
    from .schemas import Finding, FixCard, FixPlan

logger = logging.getLogger(__name__)

_STATEMENT_TIMEOUT_MS = 5_000

# Phrases that indicate the LLM dismissed a finding without running a real check.
_DISMISS_PATTERNS = (
    "no action needed",
    "no nulls",
    "re-evaluation shows",
    "shows no",
    "no rows found",
    "0% null",
    "zero null",
    "no issues found",
    "result is 0",
    "count is 0",
)

# Minimum evidence null_ratio that triggers contradiction checking.
_NULL_RATIO_THRESHOLD = 0.05

# Regex to extract the WHERE predicate from a DELETE statement.
_DELETE_WHERE_RE = re.compile(
    r"DELETE\s+FROM\s+\S+\s+WHERE\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)

# Regex to extract the table name from a DELETE statement.
_DELETE_TABLE_RE = re.compile(
    r"DELETE\s+FROM\s+(\S+)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _open_conn(connection_string: str):
    conn = psycopg2.connect(connection_string)
    conn.autocommit = True
    return conn


def _run_select(connection_string: str, sql: str) -> list[dict] | None:
    """Execute a SELECT query. Returns list of row dicts, or None on error."""
    sql = sql.strip().rstrip(";")
    if not sql.upper().startswith("SELECT"):
        return None
    try:
        conn = _open_conn(connection_string)
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS};")
                cur.execute(sql)
                return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("fix_verifier: query failed — %s | sql=%.120s", exc, sql)
        return None


def _extract_count(rows: list[dict] | None) -> int | None:
    """Pull the first integer value from the first row of a query result."""
    if not rows:
        return None
    for v in rows[0].values():
        try:
            return int(v)
        except (TypeError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# Pass 1: execute verification_queries
# ---------------------------------------------------------------------------

def _run_verification_queries(cards: list[FixCard], connection_string: str) -> None:
    for card in cards:
        if not card.verification_queries:
            continue
        results: dict[str, object] = {}
        for sql in card.verification_queries:
            rows = _run_select(connection_string, sql)
            results[sql] = rows if rows is not None else {"error": "query failed or non-SELECT"}
        card.verification_results = results


# ---------------------------------------------------------------------------
# Pass 2: contradiction detection
# ---------------------------------------------------------------------------

def _is_dismissing(text: str) -> bool:
    t = (text or "").lower()
    return any(p in t for p in _DISMISS_PATTERNS)


def _contradiction_check(
    cards: list[FixCard],
    evidence_by_entity: dict[str, dict],
) -> None:
    for card in cards:
        ep = card.evidence_payload or evidence_by_entity.get(card.target) or {}
        null_ratio = ep.get("null_ratio")
        if not null_ratio or null_ratio < _NULL_RATIO_THRESHOLD:
            continue
        if not _is_dismissing(card.rationale):
            continue

        # Optionally cross-check with the real verification result.
        real_count: int | None = None
        if card.verification_results:
            for rows in card.verification_results.values():
                if isinstance(rows, list):
                    real_count = _extract_count(rows)
                    break

        logger.warning(
            "fix_verifier: contradiction detected — card='%s' target='%s' "
            "py_profiler null_ratio=%.2f but rationale dismisses finding. "
            "real_verification_count=%s. Overriding to investigate.",
            card.fix_id, card.target, null_ratio, real_count,
        )
        card.interaction_model = "investigate"
        card.risk_level = "low"
        card.rationale = (
            f"{card.rationale}\n\n"
            f"[SYSTEM OVERRIDE — fix_verifier] py_profiler measured null_ratio={null_ratio:.2f} "
            f"({int(null_ratio * 100)}% of rows). The rationale above may contain an inaccurate "
            f"verification claim (LLM did not execute a real query). "
            f"Real verification count: {real_count if real_count is not None else 'not available'}. "
            f"Inspect manually before acting."
        )


# ---------------------------------------------------------------------------
# Pass 3: DELETE overlap detector
# ---------------------------------------------------------------------------

def _extract_delete_table(sql: str) -> str | None:
    m = _DELETE_TABLE_RE.search(sql.strip())
    return m.group(1).strip().strip('"').lower() if m else None


def _extract_where_predicate(sql: str) -> str | None:
    m = _DELETE_WHERE_RE.search(sql.strip().rstrip(";"))
    return m.group(1).strip() if m else None


def _overlap_detection(cards: list[FixCard], connection_string: str) -> None:
    # Collect DELETE cards grouped by table.
    delete_cards: dict[str, list[FixCard]] = {}
    for card in cards:
        sql = (card.sql or "").strip().upper()
        if not sql.startswith("DELETE"):
            continue
        table = _extract_delete_table(card.sql)
        if table:
            delete_cards.setdefault(table, []).append(card)

    for table, group in delete_cards.items():
        if len(group) < 2:
            continue
        # Check every unordered pair.
        for i, card_a in enumerate(group):
            pred_a = _extract_where_predicate(card_a.sql)
            if not pred_a:
                continue
            for card_b in group[i + 1:]:
                # Skip if dependency already declared in either direction.
                if card_a.fix_id in card_b.depends_on or card_b.fix_id in card_a.depends_on:
                    continue
                pred_b = _extract_where_predicate(card_b.sql)
                if not pred_b:
                    continue
                overlap_sql = (
                    f'SELECT COUNT(*) AS n FROM "{table}" '
                    f"WHERE ({pred_a}) AND ({pred_b})"
                )
                rows = _run_select(connection_string, overlap_sql)
                count = _extract_count(rows)
                if count is None:
                    continue
                if count > 0:
                    # card_b depends on card_a (first card runs first, removes overlapping rows).
                    card_b.depends_on.append(card_a.fix_id)
                    logger.warning(
                        "fix_verifier: DELETE overlap detected — %d shared rows in '%s'. "
                        "Injected depends_on: '%s' → '%s'.",
                        count, table, card_b.fix_id, card_a.fix_id,
                    )


# ---------------------------------------------------------------------------
# Pass 4: source_rule backfill
# ---------------------------------------------------------------------------

def _backfill_source_rules(cards: list[FixCard], findings: list[Finding]) -> None:
    # Exact entity match (e.g. "groupe.admin_id" → "null_ratio_high").
    rule_map: dict[str, str] = {f.entity: f.rule for f in findings if f.rule}
    # Table-level fallback: first finding per table, used when card.target is table-only.
    table_rule_map: dict[str, str] = {}
    for f in findings:
        if f.rule:
            table = f.entity.split(".")[0]
            table_rule_map.setdefault(table, f.rule)

    filled = 0
    for card in cards:
        if not card.source_rule:
            card_table = card.target.split(".")[0]
            rule = rule_map.get(card.target) or table_rule_map.get(card_table)
            if rule:
                card.source_rule = rule
                filled += 1
    if filled:
        logger.info("fix_verifier: backfilled source_rule for %d card(s).", filled)


# ---------------------------------------------------------------------------
# Pass 5: evidence_payload column correction
# ---------------------------------------------------------------------------

# Severity ranking used to pick the highest-severity matching finding when
# multiple findings map to the same card target.
_SEV_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}

def _fix_evidence_payload_columns(cards: list[FixCard]) -> None:
    """Correct evidence_payload.column_name when it doesn't match card.target.

    Root cause: when the LLM groups multiple FK columns from one table into a
    single finding and then expands them into individual cards, it copies the
    first card's evidence_payload for all subsequent cards. card.target is
    always correct (the LLM sets it per-card); evidence_payload.column_name
    lags behind.
    """
    fixed = 0
    for card in cards:
        if not card.target or "." not in card.target:
            continue
        _, col = card.target.rsplit(".", 1)
        ep = card.evidence_payload
        if not ep or not isinstance(ep, dict):
            continue
        stored_col = ep.get("column_name")
        if stored_col and stored_col != col:
            ep["column_name"] = col
            fixed += 1
    if fixed:
        logger.info("fix_verifier: corrected evidence_payload.column_name on %d card(s).", fixed)


# ---------------------------------------------------------------------------
# Pass 6: severity / source_type / confidence sync from source findings
# ---------------------------------------------------------------------------

def _sync_severity_from_findings(cards: list[FixCard], findings: list[Finding]) -> None:
    """Sync severity, source_type, and confidence from the originating finding.

    Root cause: FixCard.severity defaults to "medium" and source_type defaults
    to "ai". The LLM frequently forgets to override these, causing critical/high
    findings to appear as medium-severity "ai" cards in the UI.

    Strategy: build exact-entity + table-level fallback maps (same as Pass 4).
    For each card still at the default ("medium"/"ai") or with confidence < the
    finding's confidence, pull the values from the highest-severity matching
    finding. Never downgrade (never reduce severity or confidence already set
    by the LLM to something higher than the finding).
    """
    # entity → highest-severity finding
    exact: dict[str, Finding] = {}
    table: dict[str, Finding] = {}

    for f in findings:
        existing_exact = exact.get(f.entity)
        if not existing_exact or _SEV_RANK.get(f.severity, 0) > _SEV_RANK.get(existing_exact.severity, 0):
            exact[f.entity] = f
        t = f.entity.split(".")[0]
        existing_table = table.get(t)
        if not existing_table or _SEV_RANK.get(f.severity, 0) > _SEV_RANK.get(existing_table.severity, 0):
            table[t] = f

    synced = 0
    for card in cards:
        finding = exact.get(card.target) or table.get(card.target.split(".")[0])
        if not finding:
            continue

        changed = False

        # Severity: sync when card is at default "medium" and finding is higher.
        if (
            card.severity == "medium"
            and _SEV_RANK.get(finding.severity, 0) > _SEV_RANK["medium"]
        ):
            card.severity = finding.severity  # type: ignore[assignment]
            changed = True

        # source_type: Finding has no source_type field; use confidence=1.0 as the
        # deterministic proxy (py_profiler and schema-snapshot findings always score 1.0).
        if card.source_type == "ai" and finding.confidence == 1.0:
            card.source_type = "deterministic"  # type: ignore[assignment]
            changed = True

        # confidence: take the max — never reduce what the LLM set.
        if finding.confidence > card.confidence:
            card.confidence = finding.confidence
            changed = True

        if changed:
            synced += 1

    if synced:
        logger.info(
            "fix_verifier: synced severity/source_type/confidence on %d card(s) from findings.",
            synced,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def enrich_fix_plan(
    fix_plan: FixPlan,
    findings: list[Finding],
    connection_string: str,
) -> FixPlan:
    """Run all six post-processing passes over the Fix Planner output.

    Mutates fix_plan.cards in-place and returns the same FixPlan object.
    Never raises — errors in individual passes are logged and skipped.
    """
    if not fix_plan.cards:
        return fix_plan

    cards = fix_plan.cards

    # Build entity→evidence map from findings for contradiction checking.
    evidence_by_entity: dict[str, dict] = {
        f.entity: f.evidence for f in findings if f.evidence
    }

    try:
        _run_verification_queries(cards, connection_string)
    except Exception as exc:
        logger.error("fix_verifier: pass 1 (verification) failed: %s", exc)

    try:
        _contradiction_check(cards, evidence_by_entity)
    except Exception as exc:
        logger.error("fix_verifier: pass 2 (contradiction) failed: %s", exc)

    try:
        _overlap_detection(cards, connection_string)
    except Exception as exc:
        logger.error("fix_verifier: pass 3 (overlap) failed: %s", exc)

    try:
        _backfill_source_rules(cards, findings)
    except Exception as exc:
        logger.error("fix_verifier: pass 4 (source_rule backfill) failed: %s", exc)

    try:
        _fix_evidence_payload_columns(cards)
    except Exception as exc:
        logger.error("fix_verifier: pass 5 (evidence_payload column fix) failed: %s", exc)

    try:
        _sync_severity_from_findings(cards, findings)
    except Exception as exc:
        logger.error("fix_verifier: pass 6 (severity/source_type/confidence sync) failed: %s", exc)

    return fix_plan
