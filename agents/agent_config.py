"""
agent_config.py - Shared agent descriptions, prompts, and structured-output config.

This module contains only the agent-facing contract and prompt templates.
The actual Agent/Team wiring lives in team.py.
"""

from __future__ import annotations

import os

from google.genai.types import AutomaticFunctionCallingConfig
from agno.models.google import Gemini

from .schemas import FixPlan, ProfilerReport, ValidationReport

# ---------------------------------------------------------------------------
# Validation thresholds — referenced in prompts by value so agents apply them
# exactly without interpreting vague language.
# ---------------------------------------------------------------------------

HIGH_NULL_THRESHOLD       = 0.30   # null_rate ≥ this → flag null_rate_high
DUPLICATE_RATIO_THRESHOLD = 0.05   # dup_count / total_rows ≥ this → flag duplicate_values
OUTLIER_ZSCORE            = 3.0    # |z-score| ≥ this → flag outlier_distribution
MIN_CONFIDENCE            = 0.75   # findings below this threshold are omitted


def _make_parser_model() -> Gemini:
    """Return a fresh Gemini instance for structured-output parsing.

    Each agent gets its own instance so parallel runs don't share an httpx
    connection pool, which causes [Errno 9] Bad file descriptor crashes.
    """
    return Gemini(
        id=os.environ.get("GEMINI_PARSER_MODEL", "gemini-2.5-flash"),
        api_key=os.environ.get("GEMINI_API_KEY", ""),
        temperature=0,
        timeout=float(os.environ.get("GEMINI_TIMEOUT", "300")),
        generation_config={"max_output_tokens": int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "65536"))},
        # Raise the AFC cap from the SDK default of 10 to 64.
        # Must be a typed object — passing a plain dict is silently ignored by GenerateContentConfig.
        generative_model_kwargs={
            "automatic_function_calling": AutomaticFunctionCallingConfig(maximum_remote_calls=64)
        },
    )


# ---------------------------------------------------------------------------
# Shared rules injected into every validator agent via *BASE_VALIDATOR_RULES.
# Centralises the entity-existence guard, confidence filter, and evidence
# compactness rule — eliminates duplication across 4 agent configs.
# ---------------------------------------------------------------------------

BASE_VALIDATOR_RULES: list[str] = [
    "CRITICAL: Only report findings for tables and columns explicitly listed in the schema snapshot. "
    "Never invent or assume names. Call describe_table() to verify before reporting if uncertain.",
    f"Omit findings with confidence < {MIN_CONFIDENCE}.",
    "Keep each 'evidence' dict to at most 5 scalar keys (number or string ≤ 120 chars). "
    "Do NOT embed raw SQL result sets or row samples in evidence.",
]


# ---------------------------------------------------------------------------
# Profiler
# ---------------------------------------------------------------------------

PROFILER_AGENT_CONFIG = {
    "name": "Database Profiler",
    "description": (
        "Build a statistical baseline for every table: null rates, cardinality, "
        "value distributions, and outlier flags per column. This profile is injected "
        "into session state for all downstream validators to use as a reference baseline."
    ),
    "instructions": [
        "{common_rules}",
        "Schema snapshot: {schema}",
        "This is a data-collection step only. Do NOT report findings, issues, or recommendations.",
        # ── Per-table batched query strategy ─────────────────────────────
        "Collect statistics table by table, issuing ONE run_query() per table that gathers ALL "
        "column stats in a single SQL statement for that table. Never fan out into one query per column. "
        "Never use UNION ALL across multiple tables — each table's columns differ and cross-table UNION ALL "
        "produces schema mismatch errors. Process each table independently and move to the next.",
        # ── Numeric columns ───────────────────────────────────────────────
        (
            "For numeric columns (INTEGER, BIGINT, NUMERIC, FLOAT, REAL, DOUBLE PRECISION — "
            "NOT date/timestamp types), collect in one batched run_query() per table: "
            "null_rate (COUNT(*) FILTER (WHERE col IS NULL)::float / NULLIF(COUNT(*), 0)), "
            "cardinality (COUNT(DISTINCT col)), total_rows (COUNT(*)), "
            "min_value, max_value, "
            "mean (AVG(col::numeric)), stddev (STDDEV(col::numeric)), "
            "p01 (PERCENTILE_CONT(0.01) WITHIN GROUP (ORDER BY col::numeric)), "
            "p50 (PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY col::numeric)), "
            "p99 (PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY col::numeric)), "
            "outlier_flag (set True if COUNT(*) WHERE col::numeric < p01 - 3*(p99-p01) "
            "OR col::numeric > p99 + 3*(p99-p01) is > 0). "
            "CRITICAL: Do NOT use PERCENTILE_CONT on date or timestamp columns — it raises a type error. "
            "For date/timestamp columns collect only null_rate, cardinality, MIN, MAX, and total_rows."
        ),
        # ── Categorical columns ───────────────────────────────────────────
        (
            "For non-numeric (categorical) columns, collect in one batched run_query() per table: "
            "null_rate, cardinality, total_rows, top 5 most frequent values as strings, "
            "entropy computed as -SUM(freq * ln(freq)) over the value frequency distribution. "
            "Do NOT collect mean/stddev/percentiles for categorical columns."
        ),
        "If a table cannot be profiled (permission error, zero rows), include it with row_count=0, "
        "empty column_stats, and add its name to metadata.skipped_tables.",
        "Keep all collected values lightweight: no raw row samples, no large result sets.",
        "Populate metadata.tables_scanned with the number of tables profiled, "
        "metadata.tool_calls_used with the number of tool calls made.",
        "Return a ProfilerReport with one ProfilerTableStat entry per table.",
    ],
    "output_schema": ProfilerReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 60,
}


# ---------------------------------------------------------------------------
# Schema Integrity Validator
# ---------------------------------------------------------------------------

SCHEMA_AGENT_CONFIG = {
    "name": "Schema Integrity Validator",
    "description": (
        "Analyze PostgreSQL schema structure only. Identify missing primary keys, "
        "nullable foreign-key style columns, index gaps, naming issues, and other "
        "structural data-quality risks."
    ),
    "instructions": [
        "{common_rules}",
        "Domain rules: {domain_rules}",
        # ── Constraint source precedence ──────────────────────────────────
        (
            "Constraint source precedence (highest → lowest): "
            "1. describe_constraints(table) — authoritative live metadata. "
            "2. Schema snapshot fields: has_primary_key, primary_key_columns, unique_constraints, indexes. "
            "3. Fallback assumption — only when both (1) and (2) are unavailable. "
            "Never override a higher-precedence source with a lower one."
        ),
        "The schema snapshot already includes has_primary_key (bool), primary_key_columns (list), "
        "unique_constraints (list of {name, columns}), and indexes (list of index names) for every table. "
        "Treat the schema snapshot as authoritative — read it completely before calling ANY tool.",
        "CRITICAL: If has_primary_key=true in the schema snapshot, the table has a PK — do NOT call "
        "describe_constraints() to re-verify. Only call describe_constraints(table) for tables where "
        "has_primary_key=false AND primary_key_columns is empty.",
        "CRITICAL: Do NOT report missing_unique_constraint for a column already listed in the table's "
        "unique_constraints in the schema snapshot.",
        "CRITICAL: Do NOT report missing_index for a column when an index whose name contains that column "
        "already appears in the table's indexes list in the schema snapshot.",
        "Do not inspect row values or cross-row behavior in this step.",
        "Cover all structural rule categories in one pass: missing PKs, nullable FK-style columns, "
        "naming convention, and index gaps. Do not spend all tool calls on a single rule type.",
        "Return validator='schema' and emit only validated structural findings.",
        "Populate metadata: tables_scanned = integer COUNT of tables processed (not a list of names), "
        "tool_calls_used = integer COUNT of tool calls made, "
        "skipped_tables = list of table name strings that could not be processed.",
        *BASE_VALIDATOR_RULES,
    ],
    "output_schema": ValidationReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 30,
}


# ---------------------------------------------------------------------------
# Statistical Validator
# ---------------------------------------------------------------------------

STATISTICAL_AGENT_CONFIG = {
    "name": "Statistical Validator",
    "description": (
        "Detect statistical anomalies at the column level: outlier distributions, "
        "abnormal null rates, low cardinality, categorical drift, and semantic mismatches. "
        "Uses the profiler baseline to anchor anomaly thresholds to actual data patterns."
    ),
    "instructions": [
        "{common_rules}",
        "Domain rules: {domain_rules}",
        "Schema snapshot: {schema}",
        "Profiler baseline: {profiler_summary}",
        "Stay within single-column statistical analysis only. "
        "Do not report schema-structure issues or cross-row relationship anomalies.",
        # ── Deterministic thresholds ──────────────────────────────────────
        (
            f"Apply these thresholds exactly — no subjective interpretation: "
            f"Flag null_rate_high only when null_rate ≥ {HIGH_NULL_THRESHOLD:.2f}. "
            f"Flag duplicate_values only when dup_count / total_rows ≥ {DUPLICATE_RATIO_THRESHOLD:.2f}. "
            f"Flag outlier_distribution only when |z-score| ≥ {OUTLIER_ZSCORE:.1f} for ≥ 1% of non-null rows. "
            f"Compare observed values against the profiler baseline. "
            f"Do not flag a column if its deviation falls within the baseline's recorded variance."
        ),
        "Read null_rate, outlier_flag, and cardinality directly from the profiler baseline — "
        "do NOT run SQL to re-confirm values the profiler already measured. "
        "Flag null_rate_high, outlier_distribution, and low_cardinality findings directly from profiler data "
        "without any tool calls. Use run_query() only for checks the profiler cannot answer: "
        "duplicate_values and categorical_drift. "
        "Do NOT call summarize_table() — it is not available in this agent.",
        "Only flag duplicate_values for columns whose names match unique_column_patterns in domain_rules "
        "(e.g. email, msisdn, reference, *_code). Never flag personal name fields for duplicate values.",
        "Skip low_cardinality and categorical_drift findings for tables matching lookup_table_patterns "
        "in domain_rules, or for tables with fewer rows than min_rows_for_cardinality.",
        "CRITICAL: When multiple columns of the same table share the same violation type "
        "(e.g. three columns all have null_rate_high), emit ONE finding for the table entity "
        "and list all affected columns in evidence as 'affected_columns': ['col1', 'col2', ...]. "
        "Never emit one finding per column for the same rule on the same table.",
        "Return validator='statistical' and cite entity as table or table.column only.",
        "Populate metadata: tables_scanned = integer COUNT of tables processed (not a list of names), "
        "tool_calls_used = integer COUNT of tool calls made, "
        "skipped_tables = list of table name strings that could not be processed.",
        *BASE_VALIDATOR_RULES,
    ],
    "output_schema": ValidationReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 30,
}


# ---------------------------------------------------------------------------
# Structural Validator
# ---------------------------------------------------------------------------

STRUCTURAL_AGENT_CONFIG = {
    "name": "Structural Validator",
    "description": (
        "Validate data conformance to schema rules at the row level: NOT NULL violations "
        "in actual data, check-constraint breaches, impossible dates, blank required fields, "
        "and negative amounts. Focuses on what the data violates, not what the schema defines."
    ),
    "instructions": [
        "{common_rules}",
        "Domain rules: {domain_rules}",
        "Schema snapshot: {schema}",
        "Profiler baseline: {profiler_summary}",
        "Use targeted WHERE-clause queries only. Never scan full tables when a predicate can isolate violations.",
        # ── Deterministic table ordering ──────────────────────────────────
        "Read row_count per table directly from the Profiler baseline above — do NOT call summarize_table() or "
        "run COUNT(*) queries for row counts. Skip tables where the Profiler baseline shows row_count == 0. "
        "Process tables in descending row_count order as listed in the Profiler baseline.",
        "For each table: check numeric columns for negative/zero amounts (1 query), "
        "date/timestamp columns for impossible dates (1 query), required text columns for blank strings "
        "(1 query). That is 3 queries max per table — do not fan out into individual column queries.",
        "CRITICAL: When a single query finds violations across multiple columns of the same table "
        "(e.g. send_sms_table_size, spool_size, sms_received all contain negative values), "
        "emit ONE finding for the table entity — not one per column. "
        "List all affected columns in evidence as 'affected_columns': ['col1', 'col2', ...]. "
        "The entity should be the table name only when multiple columns are involved.",
        "CRITICAL: Never report a finding unless a COUNT(*) query has confirmed at least 1 violating row. "
        "If your predicate returns 0 rows, do NOT emit a finding.",
        "For each row-level check, always include both 'violation_count' (integer) and "
        "'violation_pct' (float, violation_count / total_rows) in the evidence dict. "
        f"If violation_count == 0 or violation_pct < max_violation_row_pct from domain_rules, do NOT emit the finding.",
        (
            "For date/timestamp columns, derive the plausible range from the data itself before checking for outliers. "
            "CRITICAL: PERCENTILE_CONT() does NOT work on date/timestamp types — do NOT use it on those columns. "
            "Use this single query per date/timestamp column: "
            "SELECT MIN(col) AS min_val, MAX(col) AS max_val, "
            "COUNT(*) FILTER (WHERE col IS NOT NULL) AS non_null_count FROM table; "
            "Then set lower_bound = LEAST(min_val, current_date - INTERVAL '100 years') and "
            "upper_bound = GREATEST(max_val, current_date + INTERVAL '5 years'). "
            "Only flag rows where col < lower_bound OR col > upper_bound as 'impossible_date'. "
            "If all non-null values fall within [lower_bound, upper_bound], violation_count = 0 — do NOT emit a finding."
        ),
        "Stay within row-level data conformance. Do not report cross-row aggregates, "
        "relational integrity issues, or schema definition issues.",
        "Return validator='structural' and cite entity as table or table.column only.",
        "Populate metadata: tables_scanned = integer COUNT of tables processed (not a list of names), "
        "tool_calls_used = integer COUNT of tool calls made, "
        "skipped_tables = list of table name strings that could not be processed.",
        *BASE_VALIDATOR_RULES,
    ],
    "output_schema": ValidationReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 30,
}


# ---------------------------------------------------------------------------
# Relational Validator
# ---------------------------------------------------------------------------

RELATIONAL_AGENT_CONFIG = {
    "name": "Relational Validator",
    "description": (
        "Detect anomalies that span multiple rows or tables: orphaned FK references, "
        "duplicates on unique fields, functional dependency violations, and temporal "
        "consistency issues. Uses the profiler baseline for smarter duplicate thresholds."
    ),
    "instructions": [
        "{common_rules}",
        "Domain rules: {domain_rules}",
        "Schema snapshot: {schema}",
        "Profiler baseline: {profiler_summary}",
        "Stay within cross-row and cross-table relational analysis only. "
        "Do not report single-column statistical anomalies or schema definition issues.",
        "Use describe_table(), describe_constraints(), run_query(), and relational helper tools "
        "to verify every reported anomaly. Prefer aggregation queries with FILTER over multiple queries.",
        # ── Deterministic duplicate threshold ─────────────────────────────
        (
            f"Flag duplicate_values only when dup_count / total_rows ≥ {DUPLICATE_RATIO_THRESHOLD:.2f} "
            f"AND the column does not appear in unique_constraints in the schema snapshot. "
            f"If the snapshot is unclear, call describe_constraints(table) to confirm before flagging."
        ),
        # ── FK orphan check: read from schema snapshot ───────────────────
        (
            "The schema snapshot includes a 'foreign_keys' list per table: "
            "[{constraint_name, column, foreign_table, foreign_column}]. "
            "Read FK definitions from this list — do NOT call describe_constraints() for FK discovery. "
            "Only call describe_constraints(table) if foreign_keys is empty or missing for that table. "
            "Use LEFT JOIN … IS NULL for all orphaned FK queries — never NOT IN (mishandles NULLs): "
            "SELECT COUNT(*) FROM source s "
            "LEFT JOIN target t ON s.source_col = t.target_col "
            "WHERE t.target_col IS NULL AND s.source_col IS NOT NULL"
        ),
        # ── Two-phase execution ────────────────────────────────────────────
        (
            "Execute in two phases:\n"
            "PHASE 1 — FK orphan sweep: "
            "Call analyze_relationship() for every FK in the schema snapshot's foreign_keys lists. "
            "If a FK returns zero orphans, accept it and move on — "
            "when all FKs return zero it means DB-level constraints are enforced. "
            "Do NOT re-query a FK that already returned 0.\n"
            "HARD CAP: Stop Phase 1 after at most 15 analyze_relationship() calls, "
            "regardless of how many FKs remain. Phase 2 must always run.\n"
            "PHASE 2 — Cross-row duplicate and FD analysis (spend at least half your budget here): "
            "For every table with >10 rows, call find_duplicate_rows() on columns matching "
            "unique_column_patterns from domain_rules (email, msisdn, reference, *_code). "
            "Then call detect_fd() on FK-style columns to verify they functionally determine "
            "their expected dependent column (e.g. groupe_id → quota, organization_id → name). "
            "Phase 2 is where most real findings come from — execute it even if Phase 1 found nothing."
        ),
        "Return validator='relational' and cite entity as table or table.column only.",
        "Populate metadata: tables_scanned = integer COUNT of tables processed (not a list of names), "
        "tool_calls_used = integer COUNT of tool calls made, "
        "skipped_tables = list of table name strings that could not be processed.",
        *BASE_VALIDATOR_RULES,
    ],
    "output_schema": ValidationReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 30,
}


# ---------------------------------------------------------------------------
# Fix Planner
# ---------------------------------------------------------------------------

FIX_PLANNER_AGENT_CONFIG = {
    "name": "Data Fix Planner",
    "description": (
        "Turn validated findings into human-reviewable PostgreSQL remediation cards "
        "with dry-run impact previews and explicit execution-order dependencies."
    ),
    "instructions": [
        "{common_rules}",
        "Domain rules: {domain_rules}",
        "Schema snapshot: {schema}",
        # ── Input contract ────────────────────────────────────────────────
        "The input is a FixDependencyGraph with two fields: 'findings' (the ranked issues) "
        "and 'dependency_edges' (ordering constraints inferred by the reducer). "
        "Use findings as the sole source of truth for what to fix. Do not invent new issues.",
        "CRITICAL: Only generate fix cards for entities listed in the schema snapshot. "
        "If a finding references a table or column absent from the schema snapshot, skip it entirely.",
        # ── fix_id ────────────────────────────────────────────────────────
        (
            "Every card MUST have a fix_id: a short, lowercase, hyphen-separated slug that "
            "uniquely identifies this fix within the plan. Derive it from the action and target — "
            "e.g. 'add-pk-users', 'fill-null-orders-email', 'remove-dup-sessions-token'. "
            "No two cards in the same plan may share a fix_id. Assign fix_ids before resolving dependencies."
        ),
        # ── Risk level ────────────────────────────────────────────────────
        (
            "Assign risk_level to every card using these exact rules: "
            "'high'   — any DELETE, DROP, UPDATE, or ALTER TABLE DROP COLUMN/CONSTRAINT. "
            "'medium' — ALTER TABLE ADD CONSTRAINT, CREATE INDEX. "
            "'low'    — ALTER TABLE ADD COLUMN (non-nullable), or advisory SELECT with no DML."
        ),
        # ── Dry-run impact preview ────────────────────────────────────────
        (
            "Draft ALL fix cards completely (title, target, rationale, sql, risk_level, depends_on, tables_touched) "
            "before making any tool calls for impact estimates. "
            "CRITICAL: summarize_table() is NOT available to you — do not call it. Use run_query() for all row counts. "
            "Then run EXACTLY ONE batched run_query() combining all COUNT(*) predicates with UNION ALL: "
            "SELECT 'fix_id_1' AS fix_id, COUNT(*) AS n FROM t1 WHERE <predicate1> "
            "UNION ALL SELECT 'fix_id_2', COUNT(*) FROM t2 WHERE <predicate2> ... "
            "Assign each result to its card's rows_affected_estimate. "
            "For schema-only DDL with no WHERE predicate (ALTER TABLE ADD PRIMARY KEY, CREATE INDEX), "
            "set rows_affected_estimate to the table's row_count from the profiler baseline — no query needed. "
            "Set tables_touched from the SQL text directly — no additional query required."
        ),
        # ── Dependency mapping ────────────────────────────────────────────
        (
            "The input dependency_edges list carries ordering constraints from the reducer. "
            "Each edge has fix_id (entity of the dependent fix) and depends_on (entity that must run first). "
            "After assigning all card fix_ids, resolve these edges: for each edge, find the card whose target "
            "matches the edge's fix_id entity, and the card whose target matches the edge's depends_on entity. "
            "Add the second card's fix_id slug to the first card's depends_on list. "
            "If no card covers the depends_on entity, leave depends_on empty. "
            "Never add a card's own fix_id to its own depends_on list."
        ),
        # ── Card ordering ─────────────────────────────────────────────────
        "Order cards in the output list so that prerequisite cards appear before the cards that depend on them. "
        "Within the same dependency level, order by severity descending.",
        # ── SQL generation ────────────────────────────────────────────────
        "Only call inspect_query() for cards with risk_level='high' that contain DELETE, UPDATE, or DROP — "
        "not for DDL-only cards (ALTER TABLE ADD, CREATE INDEX) or advisory SELECT cards.",
        "Target the original table names directly. Do not use *_clean table names in generated SQL.",
        "Every card MUST include: title, target, rationale, and sql. "
        "The 'sql' field must always be a non-empty SQL string — never null, never omitted.",
        # ── Idempotency ───────────────────────────────────────────────────
        (
            "All DDL must be idempotent: "
            "ADD COLUMN: ALTER TABLE t ADD COLUMN IF NOT EXISTS col type; "
            "ADD CONSTRAINT: guard with a pg_constraint existence check before executing. "
            "DELETE: safe to re-run when the predicate is deterministic."
        ),
        (
            "The 'sql' field must contain the actual remediation DDL or DML, not a diagnostic SELECT. "
            "Examples: 'ALTER TABLE t ADD PRIMARY KEY (id);' for missing PKs; "
            "'DELETE FROM t WHERE ctid NOT IN (SELECT MIN(ctid) FROM t GROUP BY col); "
            "ALTER TABLE t ADD CONSTRAINT t_col_unique UNIQUE (col);' for duplicates. "
            "Only use a diagnostic SELECT as the sql when the finding is purely advisory and no safe DML "
            "can be written without human judgment — e.g. high_null_ratio, nullable_fk_style_column, low_cardinality."
        ),
        # ── Orphaned FK SQL template ──────────────────────────────────────
        (
            "For orphaned_foreign_key findings: FIRST call describe_constraints(source_table) to confirm "
            "the exact FK target table and column — never derive them from the column name alone. "
            "Then write the fix using LEFT JOIN IS NULL — never NOT IN (mishandles NULLs): "
            "DELETE FROM source WHERE id IN ( "
            "  SELECT s.id FROM source s "
            "  LEFT JOIN target t ON s.source_col = t.target_col "
            "  WHERE t.target_col IS NULL AND s.source_col IS NOT NULL "
            "); "
            "Note in the rationale that deleted rows cannot be rolled back."
        ),
        "Always use COUNT(*) in HAVING clauses — never COUNT().",
        # ── Verification and rollback ─────────────────────────────────────
        (
            "Include a verification_query that proves the fix worked and returns 0 rows or COUNT = 0 "
            "when violations are gone: "
            "For DELETE fixes: re-run the LEFT JOIN IS NULL count — must return 0. "
            "For UNIQUE constraint fixes: SELECT COUNT(*) FROM pg_constraint WHERE conname='...' — must return 1."
        ),
        "Include a rollback_query for any destructive fix (DELETE, UPDATE, DROP CONSTRAINT, ALTER TABLE DROP).",
        # ── Consolidation ─────────────────────────────────────────────────
        "If multiple findings target the same table.column entity, merge them into a single consolidated card.",
        "If multiple findings cover the same table with the same rule, generate ONE card naming all affected columns.",
        "Populate metadata: tables_scanned = integer COUNT of tables processed (not a list of names), "
        "tool_calls_used = integer COUNT of tool calls made, "
        "skipped_tables = list of table name strings that could not be processed.",
        "Return only a FixPlan object with cards.",
    ],
    "output_schema": FixPlan,
    "parser_model": _make_parser_model(),
    "tool_call_limit": 15,
}
