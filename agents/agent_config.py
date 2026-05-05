"""
agent_config.py - Shared agent descriptions, prompts, and structured-output config.

This module contains only the agent-facing contract and prompt templates.
The actual Agent/Team wiring lives in team.py.
"""

from __future__ import annotations

import os

from agno.models.google import Gemini

from .schemas import FixPlan, ProfilerReport, ValidationReport

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

# Shared output-size guardrails injected into every validator agent.
# These prevent the model from generating a JSON payload so large it gets
# truncated before the closing brace, which causes total parsing failure.
_OUTPUT_GUARDRAILS = [
    "Report at most 15 findings total. Prioritise by severity: critical → high → medium → low. "
    "Omit low-confidence guesses (confidence < 0.45).",
    "Keep each 'evidence' dict to at most 5 keys. "
    "Values must be scalar (number, short string ≤ 120 chars). "
    "Do NOT embed raw SQL result sets or sample rows inside evidence.",
]


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
        "For every table in the schema snapshot, call summarize_table() to retrieve column-level statistics.",
        "For numeric columns, call detect_outliers() to flag potential outlier distributions.",
        "Collect per column: null_rate (fraction 0–1), cardinality (approximate distinct count), "
        "total_rows, min_value, max_value (as strings), top 5 most frequent values (as strings), outlier_flag.",
        "This is a data-collection step only. Do NOT report findings, issues, or recommendations.",
        "If a table cannot be profiled (permission error, zero rows), include it with row_count=0 and empty column_stats.",
        "Use run_query() for statistics not available via summarize_table() (e.g. exact cardinality via COUNT DISTINCT).",
        "Keep all collected values lightweight: no raw row samples, no large result sets.",
        "Budget 3–4 tool calls per table: one summarize_table() plus detect_outliers() for numeric columns.",
        "Return a ProfilerReport with one ProfilerTableStat entry per table.",
    ],
    "output_schema": ProfilerReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 20,
}


SCHEMA_AGENT_CONFIG = {
    "name": "Schema Integrity Validator",
    "description": (
        "Analyze PostgreSQL schema structure only. Identify missing primary keys, "
        "nullable foreign-key style columns, index gaps, naming issues, and other "
        "structural data-quality risks."
    ),
    "instructions": [
        "{common_rules}",
        "{policy_rules}",
        "Domain rules: {domain_rules}",
        "Use show_tables() to list real tables, describe_table(table) to confirm columns and nullability, and describe_constraints(table) to see all PRIMARY KEY, UNIQUE, and FOREIGN KEY constraints for each table.",
        "CRITICAL: Before reporting a table as missing a primary key, call describe_constraints(table) to verify. A table has a PK if describe_constraints() returns any row with constraint_type='PRIMARY KEY'. Only flag missing_pk if that query returns zero PRIMARY KEY rows.",
        "Do not inspect row values or cross-row behavior in this step.",
        "Cover all structural rule categories in one pass: missing PKs, nullable FK-style columns, naming convention, and index gaps. Do not spend all tool calls on a single rule type.",
        "Return validator='schema' and emit only validated structural findings.",
        *_OUTPUT_GUARDRAILS,
    ],
    "output_schema": ValidationReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 30,
}


STATISTICAL_AGENT_CONFIG = {
    "name": "Statistical Validator",
    "description": (
        "Detect statistical anomalies at the column level: outlier distributions, "
        "abnormal null rates, low cardinality, categorical drift, and semantic mismatches. "
        "Uses the profiler baseline to anchor anomaly thresholds to actual data patterns."
    ),
    "instructions": [
        "{common_rules}",
        "{policy_rules}",
        "Domain rules: {domain_rules}",
        "Schema snapshot: {schema}",
        "Profiler baseline: {profiler_report}",
        "CRITICAL: You MUST only report findings for tables and columns that are explicitly listed in the schema snapshot above. Never invent or assume table or column names. If you are unsure whether a table or column exists, call describe_table() to verify before reporting a finding.",
        "Stay within single-column statistical analysis only. Do not report schema-structure issues or cross-row relationship anomalies.",
        "Use the profiler baseline (profiler_report) as your reference: compare observed null_rate, cardinality, and outlier_flag against the profiler's measurements before raising a finding. Only flag deviations that meaningfully exceed the profiler baseline.",
        "Use summarize_table(), describe_table(), run_query(), and the statistical tools to validate concrete column-level signals.",
        "Only flag duplicate_values for columns whose names match unique_column_patterns in domain_rules (e.g. email, msisdn, reference, *_code). Never flag personal name fields (first_name, last_name, name) for duplicate values.",
        "Skip low_cardinality and categorical_drift findings for tables matching lookup_table_patterns in domain_rules, or for tables with fewer rows than min_rows_for_cardinality. Low cardinality is expected in lookup/reference tables.",
        "Do not report more than 2 findings per table. If a table has many columns with the same issue (e.g. multiple columns all with high_null_ratio), consolidate them into a single table-level finding listing the affected columns in the evidence dict — do not emit one finding per column.",
        "Return validator='statistical' and cite entity as table or table.column only.",
        *_OUTPUT_GUARDRAILS,
    ],
    "output_schema": ValidationReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 30,
}


STRUCTURAL_AGENT_CONFIG = {
    "name": "Structural Validator",
    "description": (
        "Validate data conformance to schema rules at the row level: NOT NULL violations "
        "in actual data, check-constraint breaches, impossible dates, blank required fields, "
        "and negative amounts. Focuses on what the data violates, not what the schema defines."
    ),
    "instructions": [
        "{common_rules}",
        "{policy_rules}",
        "Domain rules: {domain_rules}",
        "Schema snapshot: {schema}",
        "CRITICAL: You MUST only report findings for tables and columns that are explicitly listed in the schema snapshot above. Never invent or assume table or column names.",
        "Call summarize_table() ONCE for ALL tables together — or use a single run_query() with COUNT(*) per table — to get row counts, then skip tables with 0 rows. Do NOT call summarize_table() once per table.",
        "Prioritise tables with the most rows. Once you have 10 confirmed findings, stop checking additional tables and proceed to output.",
        "Use targeted WHERE-clause queries only. Never scan full tables when a predicate can isolate violations.",
        "For each prioritised table: check numeric columns for negative/zero amounts (1 query), date/timestamp columns for impossible dates (1 query), required text columns for blank strings (1 query). That is 3 queries max per table — do not fan out into individual column queries.",
        "CRITICAL: Never report a finding unless a COUNT(*) query has confirmed at least 1 violating row. If your predicate returns 0 rows, do NOT emit a finding — skip it silently. Only verified violations belong in the output.",
        "For each row-level check, always include both 'violation_count' (integer, the COUNT(*) result) and 'violation_pct' (float, violation_count / total_rows) in the evidence dict — even if the count is 0. This allows the pipeline to verify the finding. If violation_count == 0 or violation_pct < max_violation_row_pct from domain_rules, do NOT emit the finding.",
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
            "Never use hardcoded thresholds like '1900-01-01' independently of the column's observed pattern. "
            "If all non-null values fall within [lower_bound, upper_bound], violation_count = 0 — do NOT emit a finding."
        ),
        "Stay within row-level data conformance. Do not report cross-row aggregates, relational integrity issues, or schema definition issues.",
        "Return validator='structural' and cite entity as table or table.column only.",
        *_OUTPUT_GUARDRAILS,
    ],
    "output_schema": ValidationReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 30,
}


RELATIONAL_AGENT_CONFIG = {
    "name": "Relational Validator",
    "description": (
        "Detect anomalies that span multiple rows or tables: orphaned FK references, "
        "duplicates on unique fields, functional dependency violations, and temporal "
        "consistency issues. Uses the profiler baseline for smarter duplicate thresholds."
    ),
    "instructions": [
        "{common_rules}",
        "{policy_rules}",
        "Domain rules: {domain_rules}",
        "Schema snapshot: {schema}",
        "Profiler baseline: {profiler_report}",
        "CRITICAL: You MUST only report findings for tables and columns that are explicitly listed in the schema snapshot above. Never invent or assume table or column names.",
        "Stay within cross-row and cross-table relational analysis only. Do not report single-column statistical anomalies or schema definition issues.",
        "Use describe_table(), describe_constraints(), run_query(), and relational helper tools to verify every reported anomaly.",
        "Use the profiler baseline (profiler_report) to inform duplicate checks: if the profiler reports a column's cardinality as already low relative to row_count, calibrate your duplicate threshold accordingly before flagging uniqueness violations.",
        (
            "BEFORE flagging duplicate values in any column as a uniqueness violation: call describe_constraints(table) "
            "and check whether a composite UNIQUE constraint already covers that column together with another column. "
            "If a composite UNIQUE constraint exists (e.g. UNIQUE (groupe_id, campaign_type)), the per-column "
            "duplicates are intentional by design — do NOT report a finding."
        ),
        (
            "BEFORE checking orphaned foreign key references: call describe_constraints(source_table) to read the "
            "actual FK definition and identify the correct target table and column. Never infer the target table "
            "from the column name alone — e.g. a column named 'route_smsc_id' may reference 'smsc_organization', "
            "not 'smsc'. Always use the FK metadata returned by describe_constraints()."
        ),
        "Return validator='relational' and cite entity as table or table.column only.",
        *_OUTPUT_GUARDRAILS,
    ],
    "output_schema": ValidationReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 30,
}


FIX_PLANNER_AGENT_CONFIG = {
    "name": "Data Fix Planner",
    "description": (
        "Turn validated findings into human-reviewable PostgreSQL remediation cards "
        "with dry-run impact previews and explicit execution-order dependencies."
    ),
    "instructions": [
        "{common_rules}",
        "{policy_rules}",
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
        # ── Dry-run impact preview ────────────────────────────────────────
        (
            "For every fix card, populate rows_affected_estimate and tables_touched: "
            "(1) Run a COUNT(*) query that mirrors the fix predicate (e.g. for a DELETE fix, count the rows "
            "that match the WHERE clause; for an ALTER ADD COLUMN, count the table rows). "
            "Set rows_affected_estimate to that integer result. "
            "(2) Set tables_touched to the list of table names the fix SQL directly modifies or reads "
            "(INSERT/UPDATE/DELETE/ALTER/CREATE targets). Schema-only DDL that touches no rows is still "
            "listed — use the table name from the ALTER TABLE statement."
        ),
        # ── Dependency mapping ────────────────────────────────────────────
        (
            "The input dependency_edges list carries ordering constraints from the reducer. "
            "Each edge has fix_id (entity of the dependent fix) and depends_on (entity that must run first). "
            "After assigning all card fix_ids, resolve these edges: for each edge, find the card whose target "
            "matches the edge's fix_id entity, and the card whose target matches the edge's depends_on entity. "
            "Add the second card's fix_id slug to the first card's depends_on list. "
            "If no card covers the depends_on entity (e.g. it was filtered out), leave depends_on empty. "
            "Never add a card's own fix_id to its own depends_on list."
        ),
        # ── Card ordering ─────────────────────────────────────────────────
        "Order cards in the output list so that prerequisite cards appear before the cards that depend on them. "
        "Within the same dependency level, order by severity descending.",
        # ── SQL generation ────────────────────────────────────────────────
        "Before proposing a fix, validate the SQL predicate with inspect_query() or run_query().",
        "Target the original table names directly. Do not use *_clean table names in generated SQL.",
        "Every card MUST include: title, target, rationale, and sql. The 'sql' field must always be a non-empty SQL string — never null, never omitted.",
        "The 'sql' field must contain the actual remediation DDL or DML, not a diagnostic SELECT. "
        "Examples: 'ALTER TABLE t ADD PRIMARY KEY (id);' for missing PKs; "
        "'DELETE FROM t WHERE ctid NOT IN (SELECT MIN(ctid) FROM t GROUP BY col); "
        "ALTER TABLE t ADD CONSTRAINT t_col_unique UNIQUE (col);' for duplicates.",
        "Only use a diagnostic SELECT as the sql when the finding is purely advisory and no safe DML can be "
        "written without human judgment — e.g. high_null_ratio (human decides drop vs backfill), "
        "nullable_fk_style_column (application impact unknown), low_cardinality (informational).",
        (
            "For orphaned_foreign_key findings: FIRST call describe_constraints(source_table) to confirm the exact "
            "FK target table and column — never derive them from the column name alone. "
            "Then write: DELETE FROM <source_table> WHERE <source_col> NOT IN (SELECT <target_col> FROM <actual_target_table>). "
            "Always include a rollback_query reminder (data deleted cannot be rolled back — note this in the rationale)."
        ),
        "Always use COUNT(*) in HAVING clauses — never COUNT().",
        "Include a verification_query that proves the fix worked (e.g. SELECT COUNT(*) after a DELETE, "
        "or SELECT COUNT(*) FROM pg_constraint WHERE conname='...' after an ALTER).",
        "Include a rollback_query for any destructive fix (DELETE, UPDATE, DROP CONSTRAINT, ALTER TABLE DROP).",
        "If multiple findings target the same table.column entity, merge them into a single consolidated card. "
        "Do not generate two separate cards for the same target.",
        "If multiple findings cover the same table with the same rule (e.g. high_null_ratio across several columns), "
        "generate ONE card naming all affected columns rather than one card per column.",
        "Return only a FixPlan object with cards.",
    ],
    "output_schema": FixPlan,
    "parser_model": _make_parser_model(),
    # Bumped from 25: rows_affected_estimate requires one COUNT(*) query per card.
    "tool_call_limit": 35,
}
