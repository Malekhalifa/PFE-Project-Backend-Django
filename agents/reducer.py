"""
reducer.py — Pure-Python signal reduction step for the QualityLab pipeline.

Replaces:
  - reduce_signals()       in validation.py  (DSL string parser)
  - final_reduce_signals() in validation.py  (DSL string parser)

This step consumes typed ValidationReport objects from the three parallel
validator steps (column, row, crossrow), deduplicates findings, ranks them
by (severity, confidence), caps at top-N, and emits a ReducedFindings model
for the Fix Planner.

Zero LLM tokens consumed here — this is plain Python.
"""

import json
import logging
import re

from agno.workflow.types import StepInput, StepOutput

from .schemas import _SEVERITY_RANK, Finding, ValidationReport, ReducedFindings

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


class ReduceSignals:
    """
    Configurable reducer step.

    Args:
        min_confidence: Findings below this threshold are dropped before ranking.
        top_n: Maximum number of findings passed to the Fix Planner.
    """

    def __init__(self, min_confidence: float = 0.4, top_n: int = 50):
        self.min_confidence = min_confidence
        self.top_n = top_n

    def __call__(self, step_input: StepInput) -> StepOutput:
        # Resolve schema from session state for entity validation.
        schema: dict = {}
        try:
            ws = step_input.workflow_session
            if ws is not None:
                schema = (ws.session_data or {}).get("schema") or {}
        except Exception:
            pass

        # ── 1. Pull typed reports from all validator steps ────────────────
        # "schema" runs before the parallel phase; column/row/crossrow run in parallel.
        # All four are accessible via step_input.get_step_content().
        reports: list[ValidationReport] = []
        for step_name in ("schema", "column", "row", "crossrow"):
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

        # ── 2. Flatten ────────────────────────────────────────────────────
        all_findings: list[Finding] = [f for r in reports for f in r.findings]
        logger.info("reducer: %d raw findings from %d reports", len(all_findings), len(reports))

        # ── 3. Confidence filter ──────────────────────────────────────────
        filtered = [f for f in all_findings if f.confidence >= self.min_confidence]

        # ── 3b. Domain-rule low-cardinality suppressions ──────────────────
        # These are code-enforced to close the gap between the domain_rules
        # config and what the model actually honours.
        try:
            from .rules import domain_rules as load_domain_rules
            _dr = load_domain_rules()
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

        # ── 3c. Schema entity filter — drop hallucinated table/column refs ─
        if schema:
            valid_tables = set(schema.keys()) | {"general", "database"}
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

        reduced = ReducedFindings(findings=top)

        # ── 7. Persist for Playground continuity ─────────────────────────
        try:
            ws = step_input.workflow_session
            if ws is not None:
                ss = ws.session_data or {}
                ss["last_reduced_findings"] = reduced.model_dump()
                ws.session_data = ss
        except Exception:
            pass  # session_state may not exist in all execution contexts

        return StepOutput(content=reduced)


# Default singleton — used by the Workflow step wiring.
reduce_signals = ReduceSignals(min_confidence=0.4, top_n=50)
