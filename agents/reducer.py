"""
reducer.py — Pure-Python signal reduction step for the QualityLab pipeline.

Consumes typed ValidationReport objects from the three parallel validator steps
(statistical, structural, relational), deduplicates findings, ranks them by
(severity, confidence), caps at top-N, infers fix-ordering dependencies, and
emits a FixDependencyGraph for the Fix Planner.

Zero LLM tokens consumed here — this is plain Python.
"""

import json
import logging
import re

from agno.workflow.types import StepInput, StepOutput

from .schemas import (
    _SEVERITY_RANK,
    Finding,
    FixDependencyEdge,
    FixDependencyGraph,
    ValidationReport,
)

logger = logging.getLogger(__name__)


def _try_recover_partial(value: str, step_name: str) -> ValidationReport | None:
    """Two-strategy rescue for a truncated JSON ValidationReport string.

    Strategy 1 — append ']}' to close the structure and parse normally.
    Strategy 2 — walk the string finding every complete '{...}' object
                 inside the findings array and reconstruct a valid report.
    """
    # Strategy 1: cheap close
    for suffix in ("\n]}", "]}"):
        try:
            parsed = json.loads(value + suffix)
            report = ValidationReport.model_validate(parsed)
            logger.warning(
                "reducer: recovered %d findings from step '%s' via suffix close.",
                len(report.findings),
                step_name,
            )
            return report
        except Exception:
            pass

    # Strategy 2: detect validator name, extract complete finding objects
    validator_match = re.search(r'"validator"\s*:\s*"([^"]+)"', value)
    detected_validator = validator_match.group(1) if validator_match else step_name

    findings_start = value.find('"findings"')
    if findings_start == -1:
        return None
    bracket = value.find('[', findings_start)
    if bracket == -1:
        return None

    findings: list[dict] = []
    pos = bracket + 1
    while pos < len(value):
        open_pos = value.find('{', pos)
        if open_pos == -1:
            break
        depth = 0
        close_pos = -1
        in_string = False
        escape = False
        for i in range(open_pos, len(value)):
            char = value[i]
            if not in_string:
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        close_pos = i
                        break
                elif char == '"':
                    in_string = True
            else:
                if escape:
                    escape = False
                elif char == '\\':
                    escape = True
                elif char == '"':
                    in_string = False
        if close_pos == -1:
            break
        try:
            findings.append(json.loads(value[open_pos : close_pos + 1]))
        except Exception:
            pass
        pos = close_pos + 1

    if findings:
        try:
            report = ValidationReport.model_validate(
                {"validator": detected_validator, "findings": findings}
            )
            logger.warning(
                "reducer: recovered %d findings from step '%s' via object-walk.",
                len(report.findings),
                step_name,
            )
            return report
        except Exception:
            pass

    return None


def _finding_key(f: Finding) -> tuple:
    """Deduplication key — same entity + rule family = same logical issue.

    Excludes validator so that row and crossrow agents reporting the same
    entity violation (e.g. duplicate_unique_column vs duplicate_unique_field
    on the same column) are collapsed into one finding.
    The rule is normalised to its first word to absorb minor naming variations.
    """
    rule_family = f.rule.split("_")[0]
    return (f.entity, rule_family)


def _rank(f: Finding) -> tuple:
    """Sort key: highest severity first, then highest confidence."""
    return (_SEVERITY_RANK.get(f.severity, 0), f.confidence)


def _build_dependency_edges(findings: list[Finding]) -> list[FixDependencyEdge]:
    """Infer fix-ordering dependencies from ranked findings.

    Uses entity strings as stable proxy IDs because actual FixCard fix_ids
    don't exist at reducer time. The Fix Planner maps these to real fix_id
    values when populating FixCard.depends_on.

    Four ordering rules:
      1. missing_pk   → orphaned_fk       (FK integrity requires a PK target)
      2. duplicate    → unique_constr     (UNIQUE constraint needs clean data first)
      3. null_high    → orphaned_fk       (NULLs always appear as orphaned refs)
      4. null_high    → not_null_constr   (same column: fill NULLs before adding NOT NULL)
    """
    if not findings:
        return []

    by_table: dict[str, list[Finding]] = {}
    for f in findings:
        table = f.entity.split(".")[0]
        by_table.setdefault(table, []).append(f)

    def _matches(f: Finding, families: set[str], exact: set[str]) -> bool:
        return f.rule in exact or f.rule.split("_")[0] in families

    edges: list[FixDependencyEdge] = []

    for _table, group in by_table.items():
        pk_findings   = [f for f in group if _matches(f, {"missing"}, {"missing_pk", "missing_primary_key"})]
        fk_findings   = [f for f in group if _matches(f, {"orphaned"}, {"orphaned_fk", "orphaned_foreign_key", "referential_integrity"})]
        dupe_findings = [f for f in group if _matches(f, {"duplicate"}, {"duplicate_values", "unique_violation"})]
        uniq_findings = [f for f in group if _matches(f, {"unique"}, {"missing_unique", "unique_constraint"})]
        null_findings = [f for f in group if _matches(f, {"null"}, {"null_ratio_high", "high_null_ratio"})]

        for pk_f in pk_findings:
            for fk_f in fk_findings:
                edges.append(FixDependencyEdge(
                    fix_id=fk_f.entity,
                    depends_on=pk_f.entity,
                    reason=(
                        f"Orphaned FK on '{fk_f.entity}' must be resolved after the missing PK "
                        f"on '{pk_f.entity}' is added — FK references cannot be validated without a PK target."
                    ),
                ))

        for dupe_f in dupe_findings:
            for uniq_f in uniq_findings:
                if dupe_f.entity.split(".")[0] == uniq_f.entity.split(".")[0]:
                    edges.append(FixDependencyEdge(
                        fix_id=uniq_f.entity,
                        depends_on=dupe_f.entity,
                        reason=(
                            f"UNIQUE constraint on '{uniq_f.entity}' cannot be added until "
                            f"duplicate values in '{dupe_f.entity}' are removed first."
                        ),
                    ))

        for null_f in null_findings:
            for fk_f in fk_findings:
                if null_f.entity == fk_f.entity:
                    edges.append(FixDependencyEdge(
                        fix_id=fk_f.entity,
                        depends_on=null_f.entity,
                        reason=(
                            f"FK integrity fix on '{fk_f.entity}' should run after null remediation "
                            f"— NULL values always appear as orphaned references."
                        ),
                    ))

        # Rule 4: null fill → NOT NULL constraint (same table.column)
        # Adding NOT NULL to a column that still contains NULLs raises a constraint
        # violation at ALTER TABLE time. The fill card must run first.
        notnull_findings = [
            f for f in group
            if _matches(f, {"nullable"}, {"nullable_fk_style_column", "missing_not_null_constraint"})
        ]
        for null_f in null_findings:
            null_col = null_f.entity.split(".", 1)[1] if "." in null_f.entity else ""
            for nn_f in notnull_findings:
                nn_col = nn_f.entity.split(".", 1)[1] if "." in nn_f.entity else ""
                if null_col and nn_col and null_col == nn_col:
                    edges.append(FixDependencyEdge(
                        fix_id=nn_f.entity,
                        depends_on=null_f.entity,
                        reason=(
                            f"NOT NULL constraint on '{nn_f.entity}' cannot be added until "
                            f"NULL values in '{null_f.entity}' are filled first."
                        ),
                    ))

        # Rule 5: fk_type_mismatch → nullable_fk / not_null_constr (same table.column)
        # ALTER COLUMN TYPE (with USING clause) must execute before any NOT NULL or
        # nullable_fk constraint change on the same column — casting NULL values
        # changes what values exist and must precede constraint enforcement.
        type_mismatch_findings = [
            f for f in group if f.rule in ("fk_type_mismatch", "type_mismatch", "column_type_mismatch")
        ]
        nullable_fk_findings = [
            f for f in group
            if _matches(f, {"nullable"}, {
                "nullable_foreign_key", "nullable_fk",
                "nullable_fk_style_column", "missing_not_null_constraint",
            })
        ]
        for tm_f in type_mismatch_findings:
            tm_col = tm_f.entity.split(".", 1)[1] if "." in tm_f.entity else ""
            for nfk_f in nullable_fk_findings:
                nfk_col = nfk_f.entity.split(".", 1)[1] if "." in nfk_f.entity else ""
                if tm_col and nfk_col and tm_col == nfk_col:
                    edges.append(FixDependencyEdge(
                        fix_id=nfk_f.entity,
                        depends_on=tm_f.entity,
                        reason=(
                            f"Constraint change on '{nfk_f.entity}' must run after the type "
                            f"mismatch on '{tm_f.entity}' is resolved — ALTER COLUMN TYPE "
                            "with USING clause must precede NOT NULL or FK constraint changes."
                        ),
                    ))

    # Deduplicate edges by (fix_id, depends_on) pair
    seen: set[tuple[str, str]] = set()
    deduped: list[FixDependencyEdge] = []
    for e in edges:
        key = (e.fix_id, e.depends_on)
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    if deduped:
        logger.info("reducer: inferred %d dependency edge(s)", len(deduped))
    return deduped


class ReduceSignals:
    """
    Configurable reducer step.

    Args:
        min_confidence: Findings below this threshold are dropped before ranking.
        top_n: Maximum number of findings passed to the Fix Planner.
    """

    def __init__(self, min_confidence: float = 0.75, top_n: int = 50):
        self.min_confidence = min_confidence
        self.top_n = top_n

    def __call__(self, step_input: StepInput) -> StepOutput:
        # Push running_reducer status immediately so the UI advances past the
        # parallel validators as soon as they all complete.
        try:
            ws = step_input.workflow_session
            job_id = (ws.session_data or {}).get("job_id") if ws is not None else None
            if job_id:
                from api.db import update_job_status
                update_job_status(job_id, "running_reducer")
        except Exception:
            pass

        # Resolve schema, profiler baseline, and learned patterns from session state.
        schema: dict = {}
        profiler_tables: set[str] = set()
        # Set of (rule_family, entity) pairs the user has previously rejected.
        rejected_patterns: set[tuple[str, str]] = set()
        try:
            ws = step_input.workflow_session
            if ws is not None:
                sd = ws.session_data or {}
                schema = sd.get("schema") or {}
                # Fast path: profiler_tables is pre-computed by py_profiler / store_profiler_output.
                # Backward compat: fall back to deriving from profiler_report when the key is absent.
                tables_list = sd.get("profiler_tables")
                if tables_list is not None:
                    profiler_tables = set(tables_list)
                else:
                    profiler_data = sd.get("profiler_report") or {}
                    if isinstance(profiler_data, dict):
                        for tbl in profiler_data.get("tables", []):
                            if isinstance(tbl, dict) and tbl.get("table_name"):
                                profiler_tables.add(tbl["table_name"])
                for pattern in sd.get("learned_patterns") or []:
                    if isinstance(pattern, dict) and pattern.get("decision") == "reject":
                        rf = str(pattern.get("rule_family") or "").strip()
                        ent = str(pattern.get("entity") or "").strip()
                        if rf and ent:
                            rejected_patterns.add((rf, ent))
        except Exception:
            pass

        # ── 1. Pull typed reports from all validator steps ────────────────
        # "schema" runs before the parallel phase; statistical/structural/relational run in parallel.
        # "profiler" is skipped here — it returns ProfilerReport, not ValidationReport.
        # All steps are accessible via step_input.get_step_content().
        reports: list[ValidationReport] = []
        for step_name in ("schema", "statistical", "structural", "relational"):
            content = step_input.get_step_content(step_name)
            if isinstance(content, ValidationReport):
                reports.append(content)
            elif isinstance(content, dict):
                reports.append(ValidationReport.model_validate(content))
            elif isinstance(content, str):
                try:
                    parsed = json.loads(content)
                    reports.append(ValidationReport.model_validate(parsed))
                except Exception:
                    # Primary parse failed — attempt partial recovery before giving up.
                    recovered = _try_recover_partial(content, step_name)
                    if recovered is not None:
                        reports.append(recovered)
                    else:
                        logger.warning(
                            "reducer: step '%s' returned unparseable string — could not recover. skipping.",
                            step_name,
                        )
            elif content is not None:
                logger.warning(
                    "reducer: step '%s' returned unexpected type %s — skipping.",
                    step_name,
                    type(content).__name__,
                )

        # ── 1b. Merge deterministic auto-findings from py_profiler ───────────
        # profiler_auto_findings (statistical) and schema_auto_findings (schema) are
        # computed deterministically in py_profiler and stored in session_state.
        # They must be merged HERE (inside the reduce step) so they participate in
        # ranking and fix-card generation — not in validation.py's post-processing
        # which runs after fix_plan and only affects the final report display.
        try:
            _sd = (ws.session_data or {}) if ws is not None else {}
            _profiler_auto: list[dict] = _sd.get("profiler_auto_findings") or []
            _schema_auto:   list[dict] = _sd.get("schema_auto_findings") or []

            if _profiler_auto or _schema_auto:
                _stat_report   = next((r for r in reports if r.validator == "statistical"), None)
                _schema_report = next((r for r in reports if r.validator == "schema"), None)

                def _merge_auto(report: ValidationReport | None, auto_raws: list[dict], label: str) -> None:
                    if report is None or not auto_raws:
                        return
                    existing = {(f.entity, f.rule) for f in report.findings}
                    added = 0
                    for raw in auto_raws:
                        try:
                            f = Finding.model_validate(raw)
                            if (f.entity, f.rule) not in existing:
                                report.findings.append(f)
                                existing.add((f.entity, f.rule))
                                added += 1
                        except Exception:
                            pass
                    if added:
                        logger.info(
                            "reducer: merged %d deterministic %s finding(s) from py_profiler",
                            added, label,
                        )

                _merge_auto(_stat_report,   _profiler_auto, "statistical")
                _merge_auto(_schema_report, _schema_auto,   "schema")
        except Exception as _merge_exc:
            logger.warning("reducer: auto-findings merge failed: %s", _merge_exc)

        # ── 2. Flatten ────────────────────────────────────────────────────
        all_findings: list[Finding] = [f for r in reports for f in r.findings]
        logger.info("reducer: %d raw findings from %d reports", len(all_findings), len(reports))

        # ── 3. Confidence filter ──────────────────────────────────────────
        filtered = [f for f in all_findings if f.confidence >= self.min_confidence]

        # ── 3-learning. Downweight findings matching previously rejected patterns ──
        # A rejected pattern means the user decided this (rule_family, entity) was a
        # false positive. We halve confidence so these findings sort below genuine
        # issues but are not silently dropped — the user can still act on them.
        if rejected_patterns:
            downweighted = 0
            adjusted: list[Finding] = []
            for f in filtered:
                table = f.entity.split(".")[0] if "." in f.entity else f.entity
                rule_family = f.rule.split("_")[0]
                # Match on exact entity OR table-level entity (e.g. "users.email" matches "users")
                if (
                    (rule_family, f.entity) in rejected_patterns
                    or (rule_family, table) in rejected_patterns
                ):
                    # model_copy is Pydantic v2; use model_copy with update
                    adjusted.append(f.model_copy(update={"confidence": f.confidence * 0.5}))
                    downweighted += 1
                else:
                    adjusted.append(f)
            if downweighted:
                logger.info(
                    "reducer: downweighted %d finding(s) matching rejected learning patterns",
                    downweighted,
                )
            filtered = adjusted

        # ── 3a. Row-level violation percentage filter ─────────────────────
        # domain_rules() reads domain_rules.yaml from disk on every call (no lru_cache).
        # Load once here and reuse across all filters that need it.
        try:
            from .rules import domain_rules as load_domain_rules
            _dr = load_domain_rules()
        except Exception:
            _dr = {}
        try:
            _max_vio_pct = _dr.get("max_violation_row_pct")
            if _max_vio_pct is not None:
                _max_vio_pct = float(_max_vio_pct)
        except Exception:
            _max_vio_pct = None

        if _max_vio_pct is not None:
            before_vio = len(filtered)
            filtered = [
                f for f in filtered
                if f.rule not in ("row_level", "impossible_date", "negative_amount", "blank_required",
                                  "domain_rule_failure", "row_violation")
                or float((f.evidence or {}).get("violation_pct", 1.0)) >= _max_vio_pct
            ]
            dropped_vio = before_vio - len(filtered)
            if dropped_vio:
                logger.info(
                    "reducer: suppressed %d row-level finding(s) below violation_pct threshold %.4f",
                    dropped_vio,
                    _max_vio_pct,
                )

        # ── 3a-2. Zero-violation backstop ────────────────────────────────────
        # Drop row-level findings where evidence explicitly shows 0 violations.
        # Catches LLM false positives where the COUNT(*) returned 0 rows but the
        # agent still emitted a finding (e.g. created_at < '1900-01-01' on 2026 data).
        # Checks both violation_count == 0 and violation_pct == 0.0 since agents
        # are instructed to include violation_pct rather than violation_count.
        def _is_zero_violation(f: Finding) -> bool:
            # "structural" is the new name; "row"/"row_level" kept for backward compat with old jobs
            if f.validator not in ("row", "row_level", "structural"):
                return False
            ev = f.evidence or {}
            viol_count = ev.get("violation_count")
            if viol_count is not None:
                try:
                    return int(viol_count) == 0
                except (ValueError, TypeError):
                    pass
            viol_pct = ev.get("violation_pct")
            if viol_pct is not None:
                try:
                    return float(viol_pct) == 0.0
                except (ValueError, TypeError):
                    pass
            return False

        before_zero = len(filtered)
        filtered = [f for f in filtered if not _is_zero_violation(f)]
        dropped_zero = before_zero - len(filtered)
        if dropped_zero:
            logger.info(
                "reducer: suppressed %d row-level finding(s) with zero violations in evidence",
                dropped_zero,
            )

        # ── 3b. Domain-rule low-cardinality suppressions ──────────────────
        try:
            min_rows_cardinality: int = int(_dr.get("min_rows_for_cardinality", 100))
            _excl_lc_cols: list[str] = list(_dr.get("excluded_low_cardinality_columns", []))
        except Exception:
            min_rows_cardinality = 100
            _excl_lc_cols = []

        def _suppress_low_cardinality(f: Finding) -> bool:
            """Return True if this low_cardinality finding should be kept."""
            if f.rule not in ("low_cardinality", "categorical_drift"):
                return True
            ev = f.evidence or {}
            # Drop if the table has fewer rows than the threshold
            total_rows = ev.get("total_rows")
            if total_rows is not None and int(total_rows) < min_rows_cardinality:
                return False
            # Drop if the column is a FK-style column (*_id) — cardinality is
            # constrained by the parent table size and is always expected to be low.
            col = f.entity.split(".", 1)[1] if "." in f.entity else ""
            if col.endswith("_id"):
                return False
            # Drop if the column name is in the domain-level exclusion list
            import fnmatch as _fnmatch
            if any(_fnmatch.fnmatch(col, pat) for pat in _excl_lc_cols):
                return False
            return True

        before_lc = len(filtered)
        filtered = [f for f in filtered if _suppress_low_cardinality(f)]
        dropped_lc = before_lc - len(filtered)
        if dropped_lc:
            logger.info(
                "reducer: suppressed %d low_cardinality finding(s) (small table or FK column)",
                dropped_lc,
            )

        # ── 3b-2. nullable_fk_by_design suppression ─────────────────────────
        # Drop nullable_foreign_key findings for columns declared as intentionally
        # nullable in domain_rules.yaml (nullable_fk_by_design). This is the
        # config-layer complement to the learn-from-rejection mechanism — users
        # can preemptively exclude known-nullable FKs without running a full cycle.
        try:
            _nullable_by_design: dict[str, list[str]] = dict(
                _dr.get("nullable_fk_by_design") or {}
            )
        except Exception:
            _nullable_by_design = {}

        if _nullable_by_design:
            _nbd_rules = frozenset({
                "nullable_foreign_key", "nullable_fk",
                "nullable_fk_style_column", "missing_not_null_constraint",
            })

            def _is_nullable_by_design(f: Finding) -> bool:
                if f.rule not in _nbd_rules:
                    return True
                table = f.entity.split(".")[0]
                col   = f.entity.split(".", 1)[1] if "." in f.entity else ""
                return col not in _nullable_by_design.get(table, [])

            before_nbd = len(filtered)
            filtered = [f for f in filtered if _is_nullable_by_design(f)]
            dropped_nbd = before_nbd - len(filtered)
            if dropped_nbd:
                logger.info(
                    "reducer: suppressed %d nullable_fk finding(s) declared nullable_by_design",
                    dropped_nbd,
                )

        # ── 3c-0. Schema-constraint backstop — drop findings that contradict the
        # enriched schema snapshot (PK/UNIQUE/index facts collected at priming time).
        # This catches LLM false positives that slipped past agent-level guards.
        if schema:
            def _contradicts_known_constraints(f: Finding) -> bool:
                table = f.entity.split(".")[0]
                col   = f.entity.split(".", 1)[1] if "." in f.entity else ""
                info  = schema.get(table, {})
                rule  = f.rule.lower()

                # missing_pk / missing_primary_key — table already has a PK
                if ("missing" in rule or "primary" in rule) and "pk" in rule or "primary_key" in rule:
                    if info.get("has_primary_key"):
                        return True

                # missing_unique_constraint — column already covered by a UNIQUE constraint
                if "unique" in rule and col:
                    for uc in info.get("unique_constraints", []):
                        if col in uc.get("columns", []):
                            return True

                # nullable_fk_style_column / missing_not_null_constraint — column is already NOT NULL
                if col and ("nullable" in rule or "not_null" in rule):
                    for detail in info.get("column_details", []):
                        if detail.get("column_name") == col and detail.get("is_nullable") == "NO":
                            return True

                # missing_index — column already covered by an index.
                # Primary check: indexed_columns list built from pg_index (column-accurate).
                # Fallback: substring match on index names for older schema snapshots.
                if ("index" in rule or "idx" in rule) and col:
                    if col in info.get("indexed_columns", []):
                        return True
                    for idx_name in info.get("indexes", []):
                        if col in idx_name:
                            return True

                return False

            before_cs = len(filtered)
            filtered = [f for f in filtered if not _contradicts_known_constraints(f)]
            dropped_cs = before_cs - len(filtered)
            if dropped_cs:
                logger.warning(
                    "reducer: dropped %d finding(s) contradicted by schema-snapshot constraints",
                    dropped_cs,
                )

        # ── 3c. Schema + profiler entity filter — drop hallucinated table/column refs ─
        if schema or profiler_tables:
            valid_tables = set(schema.keys()) | profiler_tables | {"general", "database"}
            valid_pairs = {
                f"{t}.{c}"
                for t, info in schema.items()
                for c in info.get("columns", [])
            }
            before = len(filtered)
            filtered = [
                f for f in filtered
                if f.entity in valid_tables or f.entity in valid_pairs
            ]
            dropped = before - len(filtered)
            if dropped:
                logger.warning(
                    "reducer: dropped %d finding(s) referencing non-existent schema entities",
                    dropped,
                )

        # ── 3c-4. Orphaned-FK hallucination / type-safety guard ─────────────────
        # Drop orphaned FK findings in two cases:
        #   (a) source column has NO FK in schema → hallucinated relationship
        #   (b) FK exists but source/target types are incompatible (e.g. bigint→varchar)
        #       → fix planner cannot generate a safe JOIN-based DELETE query
        if schema:
            _orphaned_fk_rules = frozenset({
                "orphaned_fk", "orphaned_foreign_key", "referential_integrity",
                "orphaned_fk_references",
            })

            _int_families   = frozenset({"smallint", "integer", "int", "bigint", "int2", "int4", "int8"})
            _text_families  = frozenset({"character varying", "varchar", "text", "char", "character", "bpchar"})
            _float_families = frozenset({"numeric", "decimal", "real", "double precision", "float4", "float8"})

            def _col_type_family(data_type: str) -> str:
                t = data_type.strip().lower()
                if t in _int_families:
                    return "integer"
                if any(t.startswith(p) for p in ("character varying", "varchar", "char", "bpchar", "text")):
                    return "text"
                if t in _float_families or t.startswith(("numeric", "decimal")):
                    return "numeric"
                return t

            def _is_unsound_fk(f: Finding) -> bool:
                if f.rule not in _orphaned_fk_rules:
                    return False
                if "." not in f.entity:
                    return False
                table, col = f.entity.split(".", 1)
                table_info = schema.get(table, {})
                fk_list    = table_info.get("foreign_keys", [])
                fk_columns = {fk.get("column") for fk in fk_list}

                # (a) No FK declared → hallucinated
                if col not in fk_columns:
                    return True

                # (b) FK exists — check type compatibility
                fk_entry = next((fk for fk in fk_list if fk.get("column") == col), None)
                if fk_entry is None:
                    return False
                foreign_table = fk_entry.get("foreign_table")
                foreign_col   = fk_entry.get("foreign_column")
                if not foreign_table or not foreign_col:
                    return False

                src_type = next(
                    (d.get("data_type", "") for d in table_info.get("column_details", [])
                     if d.get("column_name") == col),
                    None,
                )
                tgt_type = next(
                    (d.get("data_type", "") for d in schema.get(foreign_table, {}).get("column_details", [])
                     if d.get("column_name") == foreign_col),
                    None,
                )
                if src_type is None or tgt_type is None:
                    return False  # can't determine — keep

                if _col_type_family(src_type) != _col_type_family(tgt_type):
                    logger.debug(
                        "reducer: orphaned_fk '%s.%s' type mismatch (%s → %s.%s %s) — dropping",
                        table, col, src_type, foreign_table, foreign_col, tgt_type,
                    )
                    return True

                return False

            before_hfk = len(filtered)
            filtered = [f for f in filtered if not _is_unsound_fk(f)]
            dropped_hfk = before_hfk - len(filtered)
            if dropped_hfk:
                logger.warning(
                    "reducer: dropped %d orphaned_fk finding(s) — no FK in schema or incompatible FK types",
                    dropped_hfk,
                )

        # ── 3d. Statistical findings require profiler coverage ────────────────────
        # When the profiler ran for at least some tables, statistical findings for
        # tables NOT in the profiler baseline are unsupported — the validator had
        # no measured anchor and any claim is potentially hallucinated. Only
        # activate this guard when profiler_tables is non-empty so that a total
        # profiler failure does not incorrectly suppress standalone statistical
        # findings produced via live queries.
        if profiler_tables:
            before_stat = len(filtered)
            filtered = [
                f for f in filtered
                if f.validator != "statistical"
                or f.entity.split(".")[0] in profiler_tables | {"general", "database"}
            ]
            dropped_stat = before_stat - len(filtered)
            if dropped_stat:
                logger.warning(
                    "reducer: dropped %d statistical finding(s) for tables outside profiler coverage",
                    dropped_stat,
                )

        # ── 4. Dedupe — keep the highest-ranked copy per (validator, entity, rule) ──
        best: dict[tuple, Finding] = {}
        for f in filtered:
            key = _finding_key(f)
            existing = best.get(key)
            if existing is None or _rank(f) > _rank(existing):
                best[key] = f

        # ── 4b. Collapse same-rule same-table findings (max 2 per table) ──
        # When the column validator emits multiple same-rule findings for the
        # same table (e.g. high_null_ratio for expediteur_id and groupe_id both
        # in smsc_routing_rule), merge them into one table-level finding to
        # avoid the fix planner generating one card per column.
        from collections import defaultdict

        table_rule_groups: dict[tuple, list[Finding]] = defaultdict(list)
        for f in best.values():
            rule_family = f.rule.split("_")[0]
            table = f.entity.split(".")[0]
            table_rule_groups[(table, rule_family)].append(f)

        merged: list[Finding] = []
        for (table, _), group in table_rule_groups.items():
            if len(group) <= 1:
                merged.extend(group)
                continue
            # Keep the highest-ranked one as the representative; fold the column
            # names from the rest into its evidence dict.
            representative = sorted(group, key=_rank, reverse=True)[0]
            extra_cols = [
                f.entity.split(".", 1)[1] if "." in f.entity else f.entity
                for f in group
                if f is not representative
            ]
            if extra_cols and representative.evidence is not None:
                lead_col = representative.entity.split(".", 1)[1] if "." in representative.entity else ""
                all_cols = [lead_col] + [c for c in extra_cols if c != lead_col]
                representative.evidence["columns_with_high_nulls"] = all_cols
                representative.entity = table
            merged.append(representative)
            logger.info(
                "reducer: merged %d same-rule findings for table '%s' into one",
                len(group),
                table,
            )

        # ── 5. Sort descending by (severity rank, confidence) ─────────────
        ranked = sorted(merged, key=_rank, reverse=True)

        # ── 6. Cap ────────────────────────────────────────────────────────
        top = ranked[: self.top_n]
        logger.info(
            "reducer: %d unique findings → top %d after dedup/rank (min_confidence=%.2f)",
            len(best),
            len(top),
            self.min_confidence,
        )

        # ── 7. Build dependency graph ─────────────────────────────────────
        dependency_edges = _build_dependency_edges(top)
        result = FixDependencyGraph(findings=top, dependency_edges=dependency_edges)

        # ── 8. Persist for Playground continuity ─────────────────────────
        try:
            ws = step_input.workflow_session
            if ws is not None:
                ss = ws.session_data or {}
                ss["last_reduced_findings"] = result.to_reduced_findings().model_dump()
                ws.session_data = ss
        except Exception:
            pass  # session_state may not exist in all execution contexts

        # ── 9. Bridge UI gap: parallel phase → fix planner ───────────────
        # emit_pipeline_status only fires on agent pre-hooks; the reducer has
        # no agent, so the status stays frozen at running_relational until the
        # fix planner starts.  Push running_fix_planner here so the loading
        # screen advances immediately when the parallel phase finishes.
        try:
            ws = step_input.workflow_session
            job_id = (ws.session_data or {}).get("job_id") if ws is not None else None
            if job_id:
                from api.db import update_job_status
                update_job_status(job_id, "running_fix_planner")
                logger.debug("reducer: pushed running_fix_planner status for job %s", job_id)
        except Exception:
            pass  # non-critical — fix planner pre-hook will push the status itself

        return StepOutput(content=result)


# Default singleton — used by the Workflow step wiring.
reduce_signals = ReduceSignals(min_confidence=0.75, top_n=50)
