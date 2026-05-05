"""
agent_config.py - Shared agent descriptions, prompts, and structured-output config.

This module contains only the agent-facing contract and prompt templates.
The actual Agent/Team wiring lives in team.py.
"""

from __future__ import annotations

import os

from agno.models.google import Gemini

from .schemas import FixPlan, ProfilerReport, ValidationReport

# ---------------------------------------------------------------------------
# Validation thresholds — referenced in prompts by value so agents apply them
# exactly without interpreting vague language.
# ---------------------------------------------------------------------------

HIGH_NULL_THRESHOLD       = 0.30   # null_rate ≥ this → flag null_rate_high
DUPLICATE_RATIO_THRESHOLD = 0.05   # dup_count / total_rows ≥ this → flag duplicate_values
OUTLIER_ZSCORE            = 3.0    # |z-score| ≥ this → flag outlier_distribution
MIN_CONFIDENCE            = 0.45   # findings below this threshold are omitted


def _make_parser_model() -> Gemini:
    """Return a fresh Gemini instance for structured-output parsing.

    Each agent gets its own instance so parallel runs don't share an httpx
    connection pool, which causes [Errno 9] Bad file descriptor crashes.
    """
    return Gemini(
        id=os.environ.get("GEMINI_PARSER_MODEL", "gemini-2.5-flash"),
        api_key=os.environ.get("GEMINI_API_KEY", ""),
        temperature=0,
        generation_config={"max_output_tokens": int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "65536"))},
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
        # ── Batched query strategy ────────────────────────────────────────
        "Collect all table statistics in the minimum number of queries. "
        "Use a single UNION ALL query across all tables per statistic type rather than one query per table. "
        "Never issue one summarize_table() call per table — that does not scale.",
        # ── Numeric columns ───────────────────────────────────────────────
        (
            "For numeric columns, collect in one batched run_query() per table: "
            "null_rate (COUNT(*) FILTER (WHERE col IS NULL)::float / COUNT(*)), "
            "cardinality (COUNT(DISTINCT col)), total_rows (COUNT(*)), "
            "min_value, max_value, "
            "mean (AVG(col::numeric)), stddev (STDDEV(col::numeric)), "
            "p01 (PERCENTILE_CONT(0.01) WITHIN GROUP (ORDER BY col::numeric)), "
            "p50 (PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY col::numeric)), "
            "p99 (PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY col::numeric)), "
            "outlier_flag (set True if COUNT(*) WHERE col::numeric < p01 - 3*(p99-p01) "
            "OR col::numeric > p99 + 3*(p99-p01) is > 0)."
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
        "unique_constraints (list of {name, columns}), and indexes (list of index names) for every table — "
        "read these fields FIRST before calling any tool.",
        "CRITICAL: A table has a PK if has_primary_key=true in the schema snapshot OR if describe_constraints(table) "
        "returns a PRIMARY KEY row. Only emit missing_pk when BOTH confirm there is no PK.",
        "CRITICAL: Do NOT report missing_unique_constraint for a column already listed in the table's "
        "unique_constraints in the schema snapshot.",
        "CRITICAL: Do NOT report missing_index for a column when an index whose name contains that column "
        "already appears in the table's indexes list in the schema snapshot.",
        "Do not inspect row values or cross-row behavior in this step.",
        "Cover all structural rule categories in one pass: missing PKs, nullable FK-style columns, "
        "naming convention, and index gaps. Do not spend all tool calls on a single rule type.",
        "Return validator='schema' and emit only validated structural findings.",
        "Populate metadata.tables_scanned, metadata.tool_calls_used, and metadata.skipped_tables.",
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
        "Profiler baseline: {profiler_report}",
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
        "Use summarize_table(), describe_table(), run_query(), and statistical tools to validate "
        "concrete column-level signals. Prefer aggregation queries with FILTER over multiple queries.",
        "Only flag duplicate_values for columns whose names match unique_column_patterns in domain_rules "
        "(e.g. email, msisdn, reference, *_code). Never flag personal name fields for duplicate values.",
        "Skip low_cardinality and categorical_drift findings for tables matching lookup_table_patterns "
        "in domain_rules, or for tables with fewer rows than min_rows_for_cardinality.",
        "Return validator='statistical' and cite entity as table or table.column only.",
        "Populate metadata.tables_scanned, metadata.tool_calls_used, and metadata.skipped_tables.",
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
        "Use targeted WHERE-clause queries only. Never scan full tables when a predicate can isolate violations.",
        # ── Deterministic table ordering ──────────────────────────────────
        "Process tables in descending order of row_count from the profiler baseline. "
        "Stop processing additional tables once 10 confirmed findings (violation_count ≥ 1) are collected.",
        "Call summarize_table() ONCE for ALL tables together — or use a single run_query() with COUNT(*) "
        "per table — to get row counts, then skip tables with 0 rows. "
        "Do NOT call summarize_table() once per table.",
        "For each table: check numeric columns for negative/zero amounts (1 query), "
        "date/timestamp columns for impossible dates (1 query), required text columns for blank strings "
        "(1 query). That is 3 queries max per table — do not fan out into individual column queries.",
        "CRITICAL: Never report a finding unless a COUNT(*) query has confirmed at least 1 violating row. "
        "If your predicate returns 0 rows, do NOT emit a finding.",
        "For each row-level check, always include both 'violation_count' (integer) and "
        "'violation_pct' (float, violation_count / total_rows) in the evidence dict. "
        f"If violation_count == 0 or violation_pct < max_violation_row_pct from domain_rules, do NOT emit the finding.",
        (
            "For date/timestamp columns, derive the plausible range from the data itself before checking for outliers. "
            "Use this single query per column: "
            "SELECT MIN(col) AS min_val, MAX(col) AS max_val, "
            "PERCENTILE_CONT(0.01) WITHIN GROUP (ORDER BY col) AS p01, "
            "PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY col) AS p99, "
            "COUNT(*) FILTER (WHERE col IS NOT NULL) AS non_null_count FROM table; "
            "Then set lower_bound = MIN(p01, current_date - INTERVAL '100 years') and "
            "upper_bound = MAX(p99, current_date + INTERVAL '5 years'). "
            "Only flag rows where col < lower_bound OR col > upper_bound as 'impossible_date'. "
            "If all non-null values fall within [lower_bound, upper_bound], violation_count = 0 — do NOT emit a finding."
        ),
        "Stay within row-level data conformance. Do not report cross-row aggregates, "
        "relational integrity issues, or schema definition issues.",
        "Return validator='structural' and cite entity as table or table.column only.",
        "Populate metadata.tables_scanned, metadata.tool_calls_used, and metadata.skipped_tables.",
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
        "Profiler baseline: {profiler_report}",
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
        # ── FK orphan check: LEFT JOIN IS NULL only ───────────────────────
        (
            "BEFORE checking orphaned foreign key references: call describe_constraints(source_table) to read the "
            "actual FK definition and identify the correct target table and column. Never infer the target table "
            "from the column name alone. Always use the FK metadata returned by describe_constraints(). "
            "Use LEFT JOIN … IS NULL for all orphaned FK queries — never NOT IN (mishandles NULLs): "
            "SELECT COUNT(*) FROM source s "
            "LEFT JOIN target t ON s.source_col = t.target_col "
            "WHERE t.target_col IS NULL AND s.source_col IS NOT NULL"
        ),
        "Return validator='relational' and cite entity as table or table.column only.",
        "Populate metadata.tables_scanned, metadata.tool_calls_used, and metadata.skipped_tables.",
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
            "For every fix card, populate rows_affected_estimate and tables_touched: "
            "(1) Run a COUNT(*) query that mirrors the fix predicate. "
            "Set rows_affected_estimate to that integer result. "
            "(2) Set tables_touched to the list of table names the fix SQL directly modifies or reads."
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
        "Before proposing a fix, validate the SQL predicate with inspect_query() or run_query().",
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
        "Populate metadata.tables_scanned, metadata.tool_calls_used, and metadata.skipped_tables.",
        "Return only a FixPlan object with cards.",
    ],
    "output_schema": FixPlan,
    "parser_model": _make_parser_model(),
    "tool_call_limit": 35,
}
