"""Rule-to-remediation classification for fix cards.

The mapper intentionally covers the rule names emitted by the current
pipeline first, then aliases newer/canonical names onto the same taxonomy.
"""

from __future__ import annotations

from typing import Any


RemediationTuple = tuple[str, str, str]


RULE_TO_FAMILY: dict[str, RemediationTuple] = {
    # Schema design
    "missing_pk": ("schema_design", "execute_ddl", "deterministic"),
    "missing_primary_key": ("schema_design", "execute_ddl", "deterministic"),
    "add_pk": ("schema_design", "execute_ddl", "deterministic"),
    "missing_index_fk_column": ("schema_design", "execute_ddl", "deterministic"),
    "missing_fk_index": ("schema_design", "execute_ddl", "deterministic"),
    "add_index": ("schema_design", "execute_ddl", "deterministic"),
    "missing_constraint": ("schema_design", "execute_ddl", "deterministic"),
    # Referential
    "nullable_fk_column": ("referential", "human_decision", "deterministic"),
    "nullable_fk": ("referential", "human_decision", "deterministic"),
    "orphaned_fk": ("referential", "human_decision", "deterministic"),
    "orphan_fk": ("referential", "human_decision", "deterministic"),
    "broken_fk": ("referential", "human_decision", "deterministic"),
    "referential_integrity": ("referential", "human_decision", "deterministic"),
    # Nullability
    "null_ratio_high": ("nullability", "advisory", "deterministic"),
    "null_rate_high": ("nullability", "advisory", "deterministic"),
    "high_null_ratio": ("nullability", "advisory", "deterministic"),
    "fill_nulls": ("nullability", "execute_ddl", "deterministic"),
    "fill_nulls_blocked": ("nullability", "blocked", "deterministic"),
    # Duplicates
    "duplicate_rate_high": ("duplicates", "human_decision", "deterministic"),
    "duplicate_values": ("duplicates", "human_decision", "deterministic"),
    "duplicate_detected": ("duplicates", "human_decision", "deterministic"),
    "exact_duplicate": ("duplicates", "human_decision", "deterministic"),
    "unique_violation": ("duplicates", "human_decision", "deterministic"),
    # Statistical
    "low_cardinality": ("statistical", "advisory", "deterministic"),
    "categorical_drift": ("statistical", "advisory", "deterministic"),
    "distributional_outlier": ("statistical", "advisory", "hybrid"),
    "outlier": ("statistical", "advisory", "hybrid"),
    # Future families
    "invalid_order": ("temporal", "advisory", "deterministic"),
    "future_ts": ("temporal", "advisory", "deterministic"),
    "impossible_duration": ("temporal", "advisory", "deterministic"),
    "invalid_utf8": ("content", "advisory", "deterministic"),
    "whitespace": ("content", "advisory", "deterministic"),
    "placeholder_junk": ("content", "advisory", "deterministic"),
    "type_mismatch": ("type_integrity", "human_decision", "deterministic"),
    "unsafe_cast": ("type_integrity", "human_decision", "deterministic"),
    "precision_mismatch": ("type_integrity", "human_decision", "deterministic"),
    "impossible_status_transition": ("business_rules", "advisory", "hybrid"),
    "logical_contradiction": ("business_rules", "advisory", "hybrid"),
    # AI investigative
    "uncertain_pattern": ("ai_investigative", "investigate", "ai"),
    "semantic_anomaly": ("ai_investigative", "investigate", "ai"),
    "fraud_indicator": ("ai_investigative", "investigate", "ai"),
}


DETERMINISTIC_VALIDATORS = {"profiler", "schema"}
AI_VALIDATORS = {"structural", "statistical", "relational", "column", "row", "crossrow"}


def source_type_for_validator(validator: str | None, fallback: str = "ai") -> str:
    value = (validator or "").lower()
    if value in DETERMINISTIC_VALIDATORS:
        return "deterministic"
    if value in AI_VALIDATORS:
        return "ai"
    return fallback


def classify_fix(rule: str | None, validator: str | None = None) -> RemediationTuple:
    family, interaction_model, mapped_source = RULE_TO_FAMILY.get(
        (rule or "").strip(),
        ("unknown", "investigate", "ai"),
    )
    return family, interaction_model, source_type_for_validator(validator, mapped_source)


def build_evidence_payload(
    *,
    family: str,
    rule: str | None,
    target: str,
    evidence: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = dict(evidence or {})
    payload.setdefault("_type", family if family != "unknown" else "generic")
    if rule:
        payload.setdefault("rule", rule)
    if target:
        payload.setdefault("target", target)
    if "." in target:
        table, column = target.split(".", 1)
        payload.setdefault("table_name", table)
        payload.setdefault("column_name", column)
    else:
        payload.setdefault("table_name", target)
    return payload
