"""
learned_rules.py — Per-database structural rule accumulation.

Stores structural/logical violation findings as regression guards keyed by
database_id. Rules survive across runs and are pruned automatically after
PRUNE_AFTER_CLEAN_RUNS consecutive runs where they find 0 violations.

Only structural/logical rule types accumulate:
  - custom_rule_violation    (domain_rules.custom_rules breaches)
  - negative_or_zero_amounts (price/quantity/weight <= 0)
  - impossible_date          (date out of plausible range)
  - blank_required_field     (required text col is empty string)

Schema findings (nullable_fk, missing_pk, missing_fk_index) and statistical
findings (null_rate_high) are re-detected deterministically every run and are
NOT stored here — doing so would cause redundant duplicate checks.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

PRUNE_AFTER_CLEAN_RUNS = 3

_ACCUMULATE_RULES: frozenset[str] = frozenset({
    "custom_rule_violation",
    "business_rule_violation",
    "logical_inconsistency",
    "negative_or_zero_amounts",
    "negative_amount",
    "zero_amount",
    "impossible_date",
    "blank_required_field",
    "blank_required",
    "not_null_constraint",  # LLM variant for custom NOT NULL rule violations
})


def _rule_text_from_finding(finding: dict[str, Any]) -> str | None:
    """Derive a plain-English rule sentence from a finding dict."""
    rule = (finding.get("rule") or "").strip()
    table = (finding.get("table") or "").strip()
    if not table:
        return None

    evidence = finding.get("evidence") or {}

    if rule == "custom_rule_violation":
        hint = (finding.get("remediation_hint") or finding.get("fix") or "").strip()
        if not hint:
            # Try evidence.rule_description (LLM sometimes puts the original rule text here)
            hint = (evidence.get("rule_description") or "").strip()
        if not hint:
            col = (finding.get("column") or "").strip()
            hint = f"column '{col}' must satisfy custom rule" if col else "custom rule violation must not occur"
        return f"In table '{table}': {hint}"

    if rule in ("negative_or_zero_amounts", "negative_amount", "zero_amount"):
        cols = evidence.get("affected_columns") or []
        col = (finding.get("column") or "").strip()
        col_part = ", ".join(cols) if cols else col
        if col_part:
            return f"In table '{table}': {col_part} must be > 0."
        return f"In table '{table}': numeric columns must be > 0."

    if rule == "impossible_date":
        col = (finding.get("column") or "").strip()
        col_part = f" column '{col}'" if col else ""
        return (
            f"In table '{table}':{col_part} date values must be within plausible range "
            f"(last 100 years to next 5 years)."
        )

    if rule in ("blank_required_field", "blank_required"):
        col = (finding.get("column") or "").strip()
        col_part = f" column '{col}'" if col else ""
        return f"In table '{table}':{col_part} must not be blank or empty string."

    if rule == "not_null_constraint":
        col = (finding.get("column") or "").strip()
        hint = (finding.get("remediation_hint") or finding.get("fix") or "").strip()
        if hint:
            return f"In table '{table}': {hint}"
        col_part = f" column '{col}'" if col else ""
        return f"In table '{table}':{col_part} must not be null."

    if rule in ("business_rule_violation", "logical_inconsistency"):
        hint = (finding.get("remediation_hint") or finding.get("fix") or "").strip()
        if not hint:
            col = (finding.get("column") or "").strip()
            hint = f"column '{col}' must satisfy business rule" if col else "business rule violation must not occur"
        return f"In table '{table}': {hint}"

    return None


def load_learned_rules(database_id: str) -> list[dict[str, Any]]:
    """Load active learned rules for this database. Returns [] on any error."""
    if not database_id:
        return []
    try:
        from api.models import LearnedRule
        rows = list(
            LearnedRule.objects
            .filter(
                database_id=database_id,
                consecutive_clean_runs__lt=PRUNE_AFTER_CLEAN_RUNS,
            )
            .values("rule_text", "table_name", "rule_type")
            .order_by("table_name")
        )
        logger.info("learned_rules: loaded %d active rule(s) for %s", len(rows), database_id)
        return rows
    except Exception as exc:
        logger.warning("learned_rules: could not load for %s: %s", database_id, exc)
        return []


def save_learned_rules(
    database_id: str,
    pipeline_run_id: str,
    findings: list[dict[str, Any]],
) -> None:
    """
    Upsert learned rules from this run's structural findings.

    Rules that FIRED this run   → reset consecutive_clean_runs to 0.
    Rules that did NOT fire     → increment consecutive_clean_runs by 1.
    New qualifying findings     → insert with consecutive_clean_runs=0.
    """
    if not database_id:
        return
    try:
        from api.models import LearnedRule
        from django.db.models import F

        active_this_run: set[tuple[str, str]] = set()
        to_upsert: list[dict[str, str]] = []

        for f in findings:
            rule = (f.get("rule") or "").strip()
            if rule not in _ACCUMULATE_RULES:
                continue
            table = (f.get("table") or "").strip()
            if not table:
                continue
            rule_text = _rule_text_from_finding(f)
            if not rule_text:
                continue
            active_this_run.add((table, rule_text))
            to_upsert.append({"table_name": table, "rule_text": rule_text, "rule_type": rule})

        for r in to_upsert:
            obj, created = LearnedRule.objects.get_or_create(
                database_id=database_id,
                table_name=r["table_name"],
                rule_text=r["rule_text"],
                defaults={
                    "rule_type": r["rule_type"],
                    "consecutive_clean_runs": 0,
                    "last_violation_run_id": pipeline_run_id,
                },
            )
            if not created:
                obj.consecutive_clean_runs = 0
                obj.last_violation_run_id = pipeline_run_id
                obj.rule_type = r["rule_type"]
                obj.save(update_fields=[
                    "consecutive_clean_runs",
                    "last_violation_run_id",
                    "rule_type",
                    "updated_at",
                ])

        # Increment clean counter for rules that didn't fire this run
        stale_pks = [
            obj.pk
            for obj in LearnedRule.objects.filter(database_id=database_id)
            if (obj.table_name, obj.rule_text) not in active_this_run
        ]
        if stale_pks:
            LearnedRule.objects.filter(pk__in=stale_pks).update(
                consecutive_clean_runs=F("consecutive_clean_runs") + 1
            )

        logger.info(
            "learned_rules: upserted %d rule(s) for %s (run=%s), %d clean-ticked",
            len(to_upsert), database_id, pipeline_run_id, len(stale_pks),
        )
    except Exception as exc:
        logger.warning("learned_rules: could not save for %s: %s", database_id, exc)


def prune_learned_rules(database_id: str) -> int:
    """Delete rules with consecutive_clean_runs >= PRUNE_AFTER_CLEAN_RUNS. Returns deleted count."""
    if not database_id:
        return 0
    try:
        from api.models import LearnedRule
        deleted, _ = (
            LearnedRule.objects
            .filter(
                database_id=database_id,
                consecutive_clean_runs__gte=PRUNE_AFTER_CLEAN_RUNS,
            )
            .delete()
        )
        if deleted:
            logger.info("learned_rules: pruned %d expired rule(s) for %s", deleted, database_id)
        return deleted
    except Exception as exc:
        logger.warning("learned_rules: could not prune for %s: %s", database_id, exc)
        return 0


def format_for_prompt(learned_rules: list[dict[str, Any]]) -> str:
    """
    Format learned rules into a prompt-ready instruction block.
    Returns empty string when no rules exist — agent skips the empty template.
    """
    lines = [r["rule_text"] for r in learned_rules if r.get("rule_text")]
    if not lines:
        return ""
    bullet_list = "\n".join(f"  - {line}" for line in lines)
    return (
        "LEARNED RULES from previous runs — enforce each as a mandatory row-level check, "
        "identical to custom_rules entries. Run ONE COUNT(*) query per rule to count "
        "violating rows. Emit rule='custom_rule_violation' when violation_count >= 1 AND "
        "violation_count / total_rows >= max_violation_row_pct from domain_rules:\n"
        + bullet_list
    )
