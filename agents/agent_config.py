"""
agent_config.py - Shared agent descriptions, prompts, and structured-output config.

This module contains only the agent-facing contract and prompt templates.
The actual Agent/Team wiring lives in team.py.
"""

from __future__ import annotations

import os

from agno.models.google import Gemini

from .schemas import FixPlan, ValidationReport

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


COLUMN_AGENT_CONFIG = {
    "name": "Column Validator",
    "description": (
        "Analyze data quality at the single-column level. Focus on null ratios, "
        "cardinality, distributions, semantic mismatches, and categorical drift."
    ),
    "instructions": [
        "{common_rules}",
        "{policy_rules}",
        "Domain rules: {domain_rules}",
        "Schema snapshot: {schema}",
        "CRITICAL: You MUST only report findings for tables and columns that are explicitly listed in the schema snapshot above. Never invent or assume table or column names. If you are unsure whether a table or column exists, call describe_table() to verify before reporting a finding.",
        "Stay within single-column analysis only. Do not report schema-only or multi-row relationship issues.",
        "Use summarize_table(), describe_table(), run_query(), and the statistical tools to validate concrete column-level signals.",
        "Only flag duplicate_values for columns whose names match unique_column_patterns in domain_rules (e.g. email, msisdn, reference, *_code). Never flag personal name fields (first_name, last_name, name) for duplicate values.",
        "Skip low_cardinality and categorical_drift findings for tables matching lookup_table_patterns in domain_rules, or for tables with fewer rows than min_rows_for_cardinality. Low cardinality is expected in lookup/reference tables.",
        "Do not report more than 2 findings per table. If a table has many columns with the same issue (e.g. multiple columns all with high_null_ratio), consolidate them into a single table-level finding listing the affected columns in the evidence dict — do not emit one finding per column.",
        "Return validator='column' and cite entity as table or table.column only.",
        *_OUTPUT_GUARDRAILS,
    ],
    "output_schema": ValidationReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 30,
}


ROW_AGENT_CONFIG = {
    "name": "Row-Level Logic Validator",
    "description": (
        "Validate row-level logic with targeted predicates. Focus on impossible dates, "
        "blank required values, negative amounts, and domain-rule failures."
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
        "Stay within row-level logic. Do not report cross-row aggregates or schema-only issues.",
        "Return validator='row' and cite entity as table or table.column only.",
        *_OUTPUT_GUARDRAILS,
    ],
    "output_schema": ValidationReport,
    "parser_model": _make_parser_model(),
    "use_json_mode": True,
    "tool_call_limit": 30,
}


CROSSROW_AGENT_CONFIG = {
    "name": "Cross-Row Validator",
    "description": (
        "Detect anomalies that require comparing multiple rows or relationships across "
        "tables, including orphaned references, duplicates on supposedly unique fields, "
        "and relational inconsistencies."
    ),
    "instructions": [
        "{common_rules}",
        "{policy_rules}",
        "Domain rules: {domain_rules}",
        "Schema snapshot: {schema}",
        "CRITICAL: You MUST only report findings for tables and columns that are explicitly listed in the schema snapshot above. Never invent or assume table or column names.",
        "Stay within cross-row and cross-table analysis only.",
        "Use describe_table(), run_query(), and relational helper tools to verify every reported anomaly.",
        "Return validator='crossrow' and cite entity as table or table.column only.",
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
        "that can be executed inside a cloned sandbox database."
    ),
    "instructions": [
        "{common_rules}",
        "{policy_rules}",
        "Domain rules: {domain_rules}",
        "Schema snapshot: {schema}",
        "Use the ReducedFindings input as the source of truth. Do not invent new issues.",
        "CRITICAL: Only generate fix cards for entities listed in the schema snapshot. If a finding references a table or column that does not appear in the schema snapshot, skip it entirely — do not generate a placeholder or stub card.",
        "Before proposing a fix, validate the SQL predicate with inspect_query() or run_query().",
        "Target the original table names directly. Do not use *_clean table names in generated SQL.",
        "Every card MUST include: title, target, rationale, and sql. The 'sql' field must always be a non-empty SQL string — never null, never omitted.",
        "The 'sql' field must contain the actual remediation DDL or DML, not a diagnostic SELECT. Examples: 'ALTER TABLE t ADD PRIMARY KEY (id);' for missing PKs; 'DELETE FROM t WHERE ctid NOT IN (SELECT MIN(ctid) FROM t GROUP BY col); ALTER TABLE t ADD CONSTRAINT t_col_unique UNIQUE (col);' for duplicates.",
        "Only use a diagnostic SELECT as the sql when the finding is purely advisory and no safe DML can be written without human judgment. Examples of advisory findings: high_null_ratio (human must decide if column should be dropped or backfilled), nullable_fk_style_column (human must check application impact), low_cardinality (informational only).",
        "For orphaned_foreign_key findings: the sql field MUST be a DELETE statement that removes the orphaned rows, not a SELECT. Use the pattern: DELETE FROM <source_table> WHERE <source_col> NOT IN (SELECT <target_col> FROM <target_table>); Always include a rollback_query reminder (data deleted cannot be rolled back — note this in the rationale).",
        "Always use COUNT(*) in HAVING clauses — never COUNT().",
        "Include a verification_query that proves the fix worked (e.g. SELECT count(*) after a DELETE, or SELECT COUNT(*) FROM pg_constraint WHERE conname='...' after an ALTER).",
        "Include a rollback_query for any destructive fix (DELETE, UPDATE, DROP CONSTRAINT, ALTER TABLE DROP).",
        "If multiple findings target the same table.column entity, merge them into a single consolidated card that addresses all related issues together. Do not generate two separate cards for the same target.",
        "If multiple findings cover the same table with the same rule (e.g., several high_null_ratio columns in one table), generate ONE card that names all affected columns rather than a card per column.",
        "Return only a FixPlan object with cards.",
    ],
    "output_schema": FixPlan,
    "parser_model": _make_parser_model(),
    "tool_call_limit": 25,
}
