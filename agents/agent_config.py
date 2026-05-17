"""
agent_config.py - Shared agent descriptions, prompts, and structured-output config.

This module contains only the agent-facing contract and prompt templates.
The actual Agent/Team wiring lives in team.py.

Prompt-engineering conventions used across all agents:
  • ROLE & GOAL: first instruction states identity and objective in one sentence.
  • WORKFLOW: numbered steps so the LLM has a deterministic execution order.
  • EVIDENCE REQUIREMENTS: explicit gates that must hold before emitting output.
  • NEVER blocks: negative constraints clustered together (anti-hallucination anchor).
  • OUTPUT: schema and metadata rules near the end (recency saliency).
  • Concrete examples preferred over abstract rules; CRITICAL used sparingly.
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
MIN_CONFIDENCE            = 0.65   # findings below this threshold are omitted (matches QL_MIN_CONFIDENCE env var)


def _make_parser_model() -> Gemini:
    """Return a fresh Gemini instance for structured-output parsing.

    Each agent gets its own instance so parallel runs don't share an httpx
    connection pool, which causes [Errno 9] Bad file descriptor crashes.
    """
    return Gemini(
        id=os.environ.get("GEMINI_PARSER_MODEL", "gemini-2.5-flash"),
        api_key=os.environ.get("GEMINI_API_KEY", ""),
        temperature=0,
        seed=7,
        timeout=float(os.environ.get("GEMINI_TIMEOUT", "300")),
        generation_config={"max_output_tokens": int(os.environ.get("GEMINI_MAX_OUTPUT_TOKENS", "65536"))},
        # Raise the AFC cap from the SDK default of 10 to 64.
        # Must be a typed object — passing a plain dict is silently ignored by GenerateContentConfig.
        generative_model_kwargs={
            "automatic_function_calling": AutomaticFunctionCallingConfig(maximum_remote_calls=64)
        },
    )


# ---------------------------------------------------------------------------
# Shared rules injected at the END of every validator's instructions via
# *BASE_VALIDATOR_RULES. End-position gives them recency saliency: they are
# the freshest constraints in the LLM's working memory when it emits findings.
# ---------------------------------------------------------------------------

BASE_VALIDATOR_RULES: list[str] = [
    # Hallucination guard — highest-priority constraint.
    (
        "HALLUCINATION GUARD: every finding's 'entity' field MUST be a table name or 'table.column' "
        "pair present in the schema snapshot. Never invent, abbreviate, or guess names. "
        "When uncertain, call describe_table() before emitting the finding. "
        "Findings referencing non-existent entities are silently dropped — they reduce your "
        "effective output count without ever appearing in the results."
    ),
    f"Skip any finding with confidence < {MIN_CONFIDENCE}.",
    (
        "Every finding must include a 'confidence' float between 0.0 and 1.0 — never null, never omitted. "
        "If evidence strength is uncertain, use 0.75."
    ),
    (
        "Keep each 'evidence' dict to at most 5 scalar keys (numbers or short strings ≤ 120 chars). "
        "Never embed raw SQL result sets or row samples."
    ),
]


# ---------------------------------------------------------------------------
# Database Profiler
# ---------------------------------------------------------------------------

PROFILER_AGENT_CONFIG = {
    "name": "Database Profiler",
    "description": (
        "Build a statistical baseline for every table: null rates, cardinality, "
        "value distributions, and outlier flags per column. This profile is injected "
        "into session state for all downstream validators to use as a reference baseline."
    ),
    "instructions": [
        # ROLE & GOAL
        (
            "ROLE: PostgreSQL Database Profiler. "
            "GOAL: Build a per-table statistical baseline (null rates, cardinality, value distributions, "
            "outlier flags) that downstream validators use as a measurement anchor. "
            "SCOPE: Data collection only — never report issues, never recommend actions, never emit findings."
        ),
        "{common_rules}",
        # profiler_schema is a compact view (column name + data_type only) built by
        # ensure_schema_primed. Using it instead of the full {schema} reduces the
        # prompt context by ~60% and prevents MALFORMED_FUNCTION_CALL on Gemini Flash.
        "Schema snapshot: {profiler_schema}",
        # WORKFLOW
        (
            "WORKFLOW: process tables one at a time. For each table issue ONE batched run_query() "
            "that collects ALL column stats for that table. "
            "Never split into per-column queries. "
            "Never use UNION ALL across multiple tables — different tables have different columns and "
            "cross-table UNION ALL produces schema-mismatch errors."
        ),
        # NUMERIC COLUMNS
        (
            "Numeric columns (INTEGER, BIGINT, NUMERIC, FLOAT, REAL, DOUBLE PRECISION — NOT date/timestamp): "
            "collect in one batched query per table: "
            "null_rate = COUNT(*) FILTER (WHERE col IS NULL)::float / NULLIF(COUNT(*), 0), "
            "cardinality = COUNT(DISTINCT col), total_rows = COUNT(*), "
            "min_value, max_value, "
            "mean = AVG(col::numeric), stddev = STDDEV(col::numeric), "
            "p01 = PERCENTILE_CONT(0.01) WITHIN GROUP (ORDER BY col::numeric), "
            "p50 = PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY col::numeric), "
            "p99 = PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY col::numeric), "
            "outlier_flag = True when COUNT(*) WHERE col::numeric < p01 − 3*(p99−p01) "
            "OR col::numeric > p99 + 3*(p99−p01) is > 0."
        ),
        # DATE/TIMESTAMP COLUMNS
        (
            "Date/timestamp columns: collect ONLY null_rate, cardinality, MIN, MAX, total_rows. "
            "NEVER use PERCENTILE_CONT() on date/timestamp types — it raises a type error."
        ),
        # CATEGORICAL COLUMNS
        (
            "Categorical (non-numeric, non-date) columns: collect null_rate, cardinality, total_rows, "
            "top 5 most frequent values as strings, and "
            "entropy = -SUM(freq * ln(freq)) over the value frequency distribution. "
            "Never collect mean/stddev/percentiles for categorical columns."
        ),
        # FAILURE HANDLING
        (
            "If a table cannot be profiled (permission error, zero rows): include it with row_count = 0, "
            "empty column_stats, and add its name to metadata.skipped_tables."
        ),
        # OUTPUT
        (
            "Keep collected values lightweight — never embed raw row samples or large result sets. "
            "Populate metadata: tables_scanned = number of tables profiled, "
            "tool_calls_used = number of tool calls made. "
            "Return a ProfilerReport with one ProfilerTableStat entry per table."
        ),
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
        # ROLE & GOAL
        (
            "ROLE: PostgreSQL Schema Integrity Validator. "
            "GOAL: Identify schema-structure data-quality risks — missing primary keys, nullable "
            "foreign-key columns, missing FK indexes, naming convention issues. "
            "SCOPE: Schema definition only. Never inspect row values, never run COUNT(*) on data, "
            "never test cross-row behavior."
        ),
        "{common_rules}",
        "Domain rules: {schema_rules}",
        # AUTHORITATIVE SOURCE
        # {schema} is pre-primed by ensure_schema_primed before this agent runs. It contains
        # has_primary_key, primary_key_columns, unique_constraints, indexes, indexed_columns,
        # foreign_keys, and full column_details for every table.
        (
            "Schema snapshot (authoritative — read fully before calling any tool): {schema}. "
            "It includes has_primary_key, primary_key_columns, unique_constraints, indexes, "
            "indexed_columns, foreign_keys, and column_details for every table."
        ),
        (
            "Source precedence (highest → lowest): "
            "(1) describe_constraints(table) — authoritative live metadata. "
            "(2) Schema snapshot fields above. "
            "(3) Fallback assumption — only when both (1) and (2) are unavailable. "
            "Never override a higher-precedence source with a lower one."
        ),
        # WORKFLOW
        (
            "WORKFLOW: cover all structural rule categories in one pass — missing PKs, nullable FK-style "
            "columns, naming convention, index gaps. Do not spend all tool calls on a single rule type."
        ),
        # TOOL DISCIPLINE
        (
            "Tool discipline: "
            "If has_primary_key = true in the snapshot, NEVER call describe_constraints() to re-verify the PK. "
            "Call describe_constraints(table) only when has_primary_key = false AND primary_key_columns is empty."
        ),
        # NEVER blocks (clustered negative constraints)
        (
            "NEVER report: "
            "(a) missing_unique_constraint for a column already in the table's unique_constraints. "
            "(b) missing_index for a column when an index name in the table's indexes list contains that column. "
            "(c) foreign_key_validation or index_gap for a column NOT in the table's foreign_keys list — "
            "a column named 'contact_id' or 'user_id' is NOT a FK unless foreign_keys says so. "
            "If foreign_keys is empty for a table, that table has zero FK columns to check. "
            "(d) row-level or cross-row anomalies — those belong to other validators."
        ),
        # OUTPUT
        (
            "Return validator = 'schema'. Order findings by severity descending (critical → high → medium → low). "
            "Populate metadata: tables_scanned = integer COUNT (not a list), "
            "tool_calls_used = integer COUNT, "
            "skipped_tables = list of table name strings."
        ),
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
        # SKIP CHECK — first instruction so the early-exit case is unmissable.
        # Set by py_profiler when no unique-pattern or low-cardinality columns exist.
        (
            "SKIP CHECK (first action): if {statistical_skip_reason} is non-empty, return an empty "
            "ValidationReport immediately — validator = 'statistical', findings = [], "
            "metadata.tables_scanned = 0, metadata.tool_calls_used = 0 — without calling any tool. "
            "Skip reason: {statistical_skip_reason}"
        ),
        # ROLE & GOAL
        (
            "ROLE: Single-column Statistical Validator. "
            "GOAL: Detect statistical anomalies at the column level — abnormal null rates, "
            "duplicate values, outlier distributions, low cardinality, categorical drift. "
            "SCOPE: One column at a time. Never report schema-structure issues or cross-row relationship anomalies."
        ),
        "{common_rules}",
        "Domain rules: {statistical_rules}",
        # validator_schema is a compact view (column_details + unique_constraints only) — the
        # statistical agent does not need FK definitions, index names, or PK metadata.
        "Schema snapshot: {validator_schema}",
        "Profiler baseline: {profiler_summary}",
        # SCOPE GUARD — anti-hallucination anchor.
        (
            "SCOPE GUARD: when {profiler_summary} is non-empty, ONLY emit findings for tables that "
            "appear as keys in {profiler_summary}. Tables absent from the baseline cannot be assessed "
            "for deviations — produce zero findings for them."
        ),
        # DETERMINISTIC THRESHOLDS — exact numbers prevent subjective interpretation.
        (
            f"THRESHOLDS — apply exactly, no subjective interpretation: "
            f"null_rate_high when null_rate ≥ {HIGH_NULL_THRESHOLD:.2f}. "
            f"duplicate_values when dup_count / total_rows ≥ {DUPLICATE_RATIO_THRESHOLD:.2f}. "
            f"outlier_distribution when |z-score| ≥ {OUTLIER_ZSCORE:.1f} for ≥ 1% of non-null rows. "
            f"Compare observed values against the profiler baseline — never flag a column whose "
            f"deviation falls within the baseline's recorded variance."
        ),
        # TOOL DISCIPLINE
        (
            "Tool discipline — read directly from {profiler_summary} where possible: "
            "null_rate_high, outlier_distribution, low_cardinality findings come from profiler data "
            "with ZERO tool calls. Never re-query values the profiler already measured. "
            "Use run_query() ONLY for: "
            "(1) duplicate_values — on tables that have at least one column matching "
            "unique_column_patterns from domain_rules (e.g. email, msisdn, reference, *_code). "
            "Skip tables without matching columns entirely. "
            "(2) categorical_drift via check_categorical_drift — only when profiler cardinality is "
            "between 2 and 50 AND total_rows > min_rows_for_cardinality from domain_rules. "
            "summarize_table() is NOT available — never call it."
        ),
        # NEVER blocks
        (
            "NEVER: "
            "(a) flag duplicate_values for columns that do not match unique_column_patterns "
            "(never flag personal name fields). "
            "(b) flag low_cardinality or categorical_drift for tables matching lookup_table_patterns. "
            "(c) flag any rule for a table with fewer rows than min_rows_for_cardinality. "
            "(d) emit one finding per column when multiple columns of the same table share the same "
            "rule — instead emit ONE finding for the table entity with "
            "evidence.affected_columns = ['col1', 'col2', ...]."
        ),
        # OUTPUT
        (
            "Return validator = 'statistical'. Cite entity as table or table.column only. "
            "Order findings by severity descending (critical → high → medium → low). "
            "Populate metadata: tables_scanned = integer COUNT (not a list), "
            "tool_calls_used = integer COUNT, "
            "skipped_tables = list of table name strings."
        ),
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
        # ROLE & GOAL
        (
            "ROLE: Row-level Structural Validator. "
            "GOAL: Detect data conformance violations at the row level — NOT NULL violations, "
            "check-constraint breaches, impossible dates, blank required fields, negative amounts, "
            "custom domain-rule violations. "
            "SCOPE: Row content only. Never report schema definition issues, cross-row aggregates, "
            "or relational integrity issues."
        ),
        "{common_rules}",
        "Domain rules: {structural_rules}",
        # validator_schema is a compact view (column_details + unique_constraints only).
        # The structural agent needs column names and types for row checks but not FK/index metadata.
        "Schema snapshot: {validator_schema}",
        # structural_profiler_summary is {table_name: row_count} only — sufficient for
        # descending-by-row-count ordering without sending full profiler stats.
        "Profiler baseline (row counts): {structural_profiler_summary}",
        "{learned_rules}",
        # WORKFLOW
        (
            "WORKFLOW: "
            "(1) Read row_count per table directly from the profiler baseline. "
            "Skip any table where row_count = 0. "
            "(2) Process tables in descending row_count order. "
            "(3) For each table run AT MOST 3 batched queries: one for numeric columns "
            "(negative/zero amounts), one for date/timestamp columns (impossible dates), "
            "one for required text columns (blank strings). "
            "Never fan out into per-column queries. "
            "Never call summarize_table() or run COUNT(*) for row count — that is already in the baseline. "
            "Always use targeted WHERE-clause queries — never scan full tables when a predicate can "
            "isolate violations."
        ),
        # EVIDENCE REQUIREMENT — strong gate against hallucinated row-level findings.
        (
            "EVIDENCE REQUIREMENT — emit a finding only when ALL of: "
            "(a) a COUNT(*) query has confirmed violation_count ≥ 1. "
            "(b) violation_count / total_rows ≥ max_violation_row_pct from domain_rules. "
            "(c) the evidence dict includes both 'violation_count' (integer) and 'violation_pct' (float). "
            "If any of these fail, do NOT emit the finding."
        ),
        # DATE/TIMESTAMP TEMPLATE
        (
            "Date/timestamp template — use static plausibility bounds, never derive bounds from the data. "
            "NEVER use PERCENTILE_CONT() on date/timestamp types — it raises a type error. "
            "Run ONE query per date/timestamp column: "
            "SELECT "
            "COUNT(*) FILTER (WHERE col IS NOT NULL) AS non_null_count, "
            "COUNT(*) FILTER (WHERE col < (CURRENT_DATE - INTERVAL '100 years') "
            "  OR col > (CURRENT_DATE + INTERVAL '5 years')) AS violation_count, "
            "COUNT(*) AS total_rows FROM table; "
            "If non_null_count = 0, skip. "
            "If violation_count > 0 AND violation_count::float / total_rows ≥ max_violation_row_pct, "
            "flag as 'impossible_date'. The bounds are always CURRENT_DATE ± fixed interval — never set from MIN/MAX of the data."
        ),
        # CUSTOM DOMAIN RULES
        (
            "Custom domain rules: for each entry in domain_rules.custom_rules that targets a specific "
            "table, run ONE COUNT(*) query using the rule as a WHERE predicate. "
            "Emit rule = 'custom_rule_violation' only when the evidence requirement above holds. "
            "Use this to enforce cross-column logical constraints such as "
            "'status = shipped implies shipped_at IS NOT NULL'."
        ),
        # CONSOLIDATION
        (
            "When a single query reveals violations across multiple columns of the same table "
            "(e.g. send_sms_table_size, spool_size, sms_received all contain negative values), "
            "emit ONE finding for the table entity with affected_columns = ['col1', 'col2', ...]. "
            "Never emit one finding per column."
        ),
        # OUTPUT
        (
            "Return validator = 'structural'. Cite entity as table or table.column. "
            "Order findings by severity descending (critical → high → medium → low). "
            "Populate metadata: tables_scanned = integer COUNT (not a list), "
            "tool_calls_used = integer COUNT, "
            "skipped_tables = list of table name strings."
        ),
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
        # ROLE & GOAL
        (
            "ROLE: Cross-row Relational Validator. "
            "GOAL: Detect anomalies that span multiple rows or tables — orphaned FK references, "
            "duplicate values on unique-style fields, functional dependency violations, "
            "temporal inconsistencies, referential constraint violations. "
            "SCOPE: Cross-row only. Never report null rates, outliers, cardinality, drift, or "
            "schema-definition issues — those belong to other validators."
        ),
        "{common_rules}",
        "Domain rules: {relational_rules}",
        # relational_schema is a compact view: column_details, foreign_keys, unique_constraints,
        # has_primary_key, primary_key_columns. Strips indexes/indexed_columns (saves ~20% tokens).
        "Schema snapshot: {relational_schema}",
        "Profiler baseline: {profiler_summary}",
        # VALID RULES — positive scope statement.
        (
            "VALID RULES for this validator (any other rule is out of scope and MUST NOT appear in output): "
            "duplicate_values, orphaned_fk_reference, functional_dependency_violation, "
            "temporal_inconsistency, referential_constraint_violation."
        ),
        # TWO-PHASE WORKFLOW — central to this agent.
        (
            "WORKFLOW — 25-tool-call budget split into two mandatory phases. "
            "Use describe_table(), describe_constraints(), run_query(), and relational helper tools "
            "(analyze_relationship, find_duplicate_rows, detect_fd). "
            "Prefer aggregation queries with FILTER over multiple queries. "
            "PHASE 1 — FK orphan sweep (≤ 10 calls): "
            "call analyze_relationship() for each FK in the schema snapshot's foreign_keys lists. "
            "If a FK returns 0 orphans, accept it and move on. Never re-query a FK that already returned 0. "
            "HARD STOP after 10 calls regardless of remaining FKs — Phase 2 must always run. "
            "PHASE 2 — Cross-row duplicate and FD analysis (≥ 15 calls): "
            "for every table with > 10 rows, call find_duplicate_rows() on columns matching "
            "unique_column_patterns from domain_rules (email, msisdn, reference, *_code). "
            "Then call detect_fd() on columns with single-column unique constraints or known "
            "determinants (e.g. groupe_id → quota, organization_id → name). "
            "Skip detect_fd() for any column covered by a composite unique constraint. "
            "Phase 2 MUST execute — most real findings come from it, even when Phase 1 found nothing."
        ),
        # DETERMINISTIC THRESHOLD
        (
            f"Duplicate threshold: flag duplicate_values only when dup_count / total_rows ≥ "
            f"{DUPLICATE_RATIO_THRESHOLD:.2f} AND the column is not the sole column in any "
            f"single-column unique constraint."
        ),
        # COMPOSITE UNIQUE GUARD — single consolidated rule covering both dup and FD checks.
        (
            "COMPOSITE UNIQUE GUARD — applies to both duplicate_values and functional_dependency_violation: "
            "if a column appears ONLY in composite unique constraints (e.g. UNIQUE(code, organization_id)), "
            "duplicates in that column alone are BY DESIGN — the same value is valid when paired with "
            "different partner columns. NEVER flag such columns. "
            "Flag duplicate_values only when: "
            "(a) the column has a single-column UNIQUE constraint and duplicates still exist, OR "
            "(b) the column has no unique constraint but matches unique_column_patterns. "
            "Flag functional_dependency_violation (X → Y) only when X has NO unique constraint, "
            "OR X is the sole column in a single-column unique constraint. "
            "Junction tables (many-to-many relations such as user_contact_tags, campagne_contacts) "
            "NEVER have FDs between their two FK columns — never run FD checks on them."
        ),
        # FK ORPHAN DISCIPLINE
        (
            "FK orphan discipline: the schema snapshot's foreign_keys list per table is the AUTHORITATIVE "
            "FK source — entries look like {constraint_name, column, foreign_table, foreign_column}. "
            "Read FK definitions from this list — never call describe_constraints() for FK discovery. "
            "Only run orphan checks on FK relationships explicitly listed in foreign_keys. "
            "NEVER infer FK relationships from column naming patterns (a column named 'contact_id' is "
            "NOT a FK to 'contacts' unless foreign_keys says so). "
            "A column absent from foreign_keys has no FK relationship to check. "
            "Use LEFT JOIN … IS NULL for all orphan queries — never NOT IN (mishandles NULLs): "
            "SELECT COUNT(*) FROM source s "
            "LEFT JOIN target t ON s.source_col = t.target_col "
            "WHERE t.target_col IS NULL AND s.source_col IS NOT NULL"
        ),
        # NEVER blocks
        (
            "NEVER: "
            "(a) report null_rate_high, null_ratio_high, outlier_distribution, low_cardinality, or "
            "categorical_drift — those belong to the statistical validator. Even when profiler_summary "
            "shows a high null rate for a column, do NOT create a finding for it here. "
            "(b) infer FK relationships from column naming. "
            "(c) flag composite-unique-protected duplicates or FDs."
        ),
        # OUTPUT
        (
            "Return validator = 'relational' — exact string, not 'column' or 'relational_validator'. "
            "Cite entity as table (table-level findings) or table.column (column-level findings). "
            "Order findings by severity descending (critical → high → medium → low) — this ensures "
            "the most important findings survive if the response is truncated. "
            "Populate metadata: tables_scanned = integer COUNT (not a list), "
            "tool_calls_used = integer COUNT, "
            "skipped_tables = list of table name strings."
        ),
        *BASE_VALIDATOR_RULES,
    ],
    "output_schema": ValidationReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 25,
}


# ---------------------------------------------------------------------------
# Data Fix Planner
# ---------------------------------------------------------------------------

FIX_PLANNER_AGENT_CONFIG = {
    "name": "Data Fix Planner",
    "description": (
        "Turn validated findings into executable PostgreSQL remediation cards with explicit "
        "execution-order dependencies, risk levels, rollback queries, and verification queries."
    ),
    "instructions": [
        # ROLE & GOAL
        (
            "ROLE: PostgreSQL Data Fix Planner. "
            "GOAL: for every finding in the input, produce one FixCard with concrete SQL, "
            "correct interaction_model and risk_level, and a full audit trail (source_rule, evidence_payload). "
            "SCOPE: input findings only — never invent new issues, never re-detect data quality problems."
        ),
        "{common_rules}",
        "Domain rules: {fix_planner_rules}",
        # fix_planner_schema is a compact view: column_details, has_primary_key,
        # primary_key_columns, unique_constraints. Strips FK list (planner calls
        # describe_constraints() for FK discovery) and index metadata (~25% token reduction).
        "Schema snapshot: {fix_planner_schema}",
        # INPUT CONTRACT
        (
            "INPUT: a FixDependencyGraph with two fields — 'findings' (the ranked issues) and "
            "'dependency_edges' (ordering constraints inferred by the reducer). "
            "Use findings as the sole source of truth for what to fix. "
            "Skip any finding whose entity (table or table.column) is absent from the schema snapshot."
        ),
        # WORKFLOW
        (
            "WORKFLOW: "
            "(1) Draft every card completely (title, target, rationale, sql, interaction_model, "
            "risk_level, card_family, severity, source_type, confidence, depends_on, source_rule, "
            "tables_touched, evidence_payload) using only the input — no tool calls yet. "
            "(2) Run EXACTLY ONE batched run_query() combining COUNT(*) predicates for DML cards via UNION ALL: "
            "SELECT 'fix_id_1' AS fix_id, COUNT(*) AS n FROM t1 WHERE <pred1> "
            "UNION ALL SELECT 'fix_id_2', COUNT(*) FROM t2 WHERE <pred2> ... "
            "Assign each result to its card's rows_affected_estimate. "
            "(3) For schema-only DDL with no WHERE predicate (ALTER TABLE ADD PRIMARY KEY, CREATE INDEX), "
            "set rows_affected_estimate from the profiler baseline — no query needed. "
            "(4) Set tables_touched from the SQL text directly — no additional query required. "
            "(5) Resolve dependency_edges into depends_on slugs (see Dependencies below). "
            "(6) Order the output list so prerequisites appear before dependents; within the same level, sort by severity descending."
        ),
        # FIX_ID
        (
            "fix_id: short, lowercase, hyphen-separated slug, unique within the plan. "
            "Derive from action + target — e.g. 'add-pk-users', 'fill-null-orders-email', "
            "'remove-dup-sessions-token'. "
            "Assign fix_ids BEFORE resolving dependencies. No two cards may share a fix_id."
        ),
        # INTERACTION_MODEL — the most error-prone field, so explicit mapping + negative anchor.
        (
            "interaction_model — controls the UI action gate. Apply exactly: "
            "'execute_ddl'    → safe, reversible DDL: ALTER TABLE ADD PRIMARY KEY, ADD CONSTRAINT, "
            "                    CREATE INDEX, ADD COLUMN. MUST include rollback_queries. "
            "'human_decision' → irreversible or risky DML: DELETE, UPDATE, ALTER TABLE DROP COLUMN/CONSTRAINT. "
            "                    Requires explicit human approval before execution. "
            "'advisory'       → informational finding; sql is a diagnostic SELECT only; no DML generated. "
            "'investigate'    → manual inspection required before any action can be taken. "
            "NEVER use 'human_decision' for CREATE INDEX or ALTER TABLE ADD PRIMARY KEY — those are safe DDL."
        ),
        # RISK_LEVEL
        (
            "risk_level — apply exactly: "
            "'high'   → DELETE, DROP, UPDATE, ALTER TABLE DROP COLUMN/CONSTRAINT. "
            "'medium' → ALTER TABLE ADD PRIMARY KEY, ADD CONSTRAINT, CREATE INDEX, ALTER COLUMN. "
            "'low'    → ALTER TABLE ADD COLUMN (non-nullable), advisory SELECT with no DML, investigate cards."
        ),
        # SEVERITY / SOURCE_TYPE / CONFIDENCE — copy from source finding, never default.
        (
            "severity: copy verbatim from the triggering finding's severity field "
            "(critical / high / medium / low). NEVER default to 'medium' — always read the finding. "
            "source_type: copy from the finding — 'deterministic' if the finding has "
            "source_type='deterministic' or confidence=1.0; 'ai' otherwise. "
            "confidence: copy the finding's confidence value as a float [0, 1]."
        ),
        # SOURCE_RULE — straightforward; emphasise "no tool calls" so the LLM doesn't over-verify.
        (
            "source_rule: copy the 'rule' field of the triggering finding verbatim "
            "(e.g. 'missing_pk', 'nullable_fk', 'null_ratio_high'). "
            "Use the finding input directly — no tool calls needed to resolve this."
        ),
        # CARD_FAMILY — deterministic mapping; must be set on every card.
        (
            "card_family — set this on every card using the exact mapping below: "
            "missing_primary_key / missing_pk / missing_fk_index / missing_index → 'schema_design'. "
            "nullable_fk / null_rate_high / nullable_foreign_key / missing_not_null → 'nullability'. "
            "duplicate_values / duplicate_rows → 'duplicates'. "
            "orphaned_fk / orphaned_row / referential_integrity → 'referential'. "
            "type_mismatch → 'type_integrity'. "
            "categorical_drift / outlier_detected / distribution_anomaly → 'statistical'. "
            "temporal_inconsistency / stale_timestamp → 'temporal'. "
            "Any rule not in the list above → 'unknown'."
        ),
        # GROUPED FINDINGS — one finding with count > 1 = ONE card.
        (
            "Grouped findings: a finding with count > 1 or affected_columns already lists multiple "
            "columns on the same table+rule. "
            "NEVER expand a grouped finding into N individual cards — one finding = one card. "
            "List all affected columns in the card's rationale and in evidence_payload. "
            "The card's target is the table name (not table.column) when multiple columns are involved. "
            "Set evidence_payload.column_name to the primary column only; list the rest under "
            "evidence_payload.affected_columns."
        ),
        # SQL FIELD
        (
            "sql field: a non-empty SQL string containing the actual remediation DDL or DML, "
            "NOT a diagnostic SELECT. Examples: "
            "'ALTER TABLE t ADD PRIMARY KEY (id);' for missing PK. "
            "'DELETE FROM t WHERE ctid NOT IN (SELECT MIN(ctid) FROM t GROUP BY col); "
            "ALTER TABLE t ADD CONSTRAINT t_col_unique UNIQUE (col);' for duplicates. "
            "Use a diagnostic SELECT as the sql ONLY for purely advisory findings where no safe DML can "
            "be written without human judgement (high_null_ratio, nullable_fk_style_column, low_cardinality)."
        ),
        # SQL STYLE
        (
            "SQL style: plain SQL only — never DO $$ PL/pgSQL blocks. "
            "ADD COLUMN: ALTER TABLE t ADD COLUMN IF NOT EXISTS col type; "
            "ADD CONSTRAINT: ALTER TABLE t ADD CONSTRAINT name UNIQUE (col); "
            "No existence guard needed — the pre-flight system verifies the constraint does not exist "
            "before this card is applied. "
            "Target the original table names directly — never *_clean. "
            "Always use COUNT(*) in HAVING clauses, never COUNT()."
        ),
        # ORPHANED FK SPECIAL CASE
        (
            "Orphaned FK fixes — strict procedure: "
            "(1) call describe_constraints(source_table) to confirm the exact FK target table and column "
            "— never derive them from the column name alone. "
            "(2) If describe_constraints shows no FK between these tables, skip the card entirely. "
            "(3) Verify both join columns have the same data type from the schema snapshot. "
            "If types differ (e.g. bigint vs character varying), add an explicit cast: "
            "s.source_col::text = t.target_col OR s.source_col = t.target_col::bigint. "
            "Never join mismatched types without a cast or PostgreSQL raises a type error. "
            "(4) Use LEFT JOIN … IS NULL — never NOT IN (mishandles NULLs): "
            "DELETE FROM source WHERE id IN ( "
            "  SELECT s.id FROM source s "
            "  LEFT JOIN target t ON s.source_col = t.target_col "
            "  WHERE t.target_col IS NULL AND s.source_col IS NOT NULL "
            "); "
            "(5) Note in the rationale that deleted rows cannot be rolled back."
        ),
        # VERIFICATION
        (
            "verification_queries: SQL SELECT strings declaring what to check after the fix runs. "
            "For DELETE fixes: re-run the LEFT JOIN IS NULL count — must return 0. "
            "For UNIQUE constraint fixes: SELECT COUNT(*) FROM pg_constraint WHERE conname = '...' — must return 1. "
            "rationale: describe WHY the fix is needed and WHAT the SQL does. "
            "NEVER claim what a verification query returned in the rationale unless you ran it as a "
            "run_query() tool call in this session."
        ),
        # ROLLBACK
        (
            "rollback_queries: required for every execute_ddl card and for every destructive DML "
            "(DELETE, UPDATE, DROP CONSTRAINT, ALTER TABLE DROP)."
        ),
        # DEPENDENCIES
        (
            "Dependencies: dependency_edges carries ordering constraints from the reducer. "
            "Each edge has fix_id (entity of the dependent fix) and depends_on (entity that must run first). "
            "After assigning all card fix_ids, resolve each edge: find the card whose target matches "
            "the edge's fix_id entity, and the card whose target matches the edge's depends_on entity. "
            "Add the second card's fix_id slug to the first card's depends_on list. "
            "If no card covers the depends_on entity, leave depends_on empty. "
            "Never add a card's own fix_id to its own depends_on list."
        ),
        # CONSOLIDATION
        (
            "Consolidation: "
            "If multiple findings target the same table.column entity, merge them into ONE card. "
            "If a finding already has count > 1 or affected_columns, treat it as pre-grouped — "
            "emit ONE card for the whole group (see Grouped findings above)."
        ),
        # TOOL USAGE
        (
            "Tool usage: "
            "summarize_table() is NOT available — never call it; use run_query() for all row counts. "
            "Call inspect_query() only for risk_level = 'high' cards containing DELETE, UPDATE, or DROP — "
            "skip it for DDL-only cards (ALTER TABLE ADD, CREATE INDEX) and advisory SELECTs."
        ),
        # OUTPUT
        (
            "Every card MUST include title, target, rationale, and sql. "
            "Populate metadata: tables_scanned = integer COUNT (not a list), "
            "tool_calls_used = integer COUNT, "
            "skipped_tables = list of table name strings. "
            "Return a FixPlan object containing the ordered cards list — no other top-level keys."
        ),
    ],
    "output_schema": FixPlan,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 20,
}
