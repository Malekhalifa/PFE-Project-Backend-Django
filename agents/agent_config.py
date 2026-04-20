"""
agent_config.py — Shared agent descriptions, base instructions, and prompt templates.

Imported by both:
  - validation_team.py  (Celery pipeline)
  - agent_os.py         (AgentOS playground)

All database-specific logic has been removed. Each agent now receives its
operating context through dynamically generated guidelines produced by the
DatabaseGuidelineGeneratorAgent (see guideline_generator.py).

The pipeline flow is:
  GuidelineGenerator → Schema Validator
        ↓
  GuidelineGenerator → Column Validator
        ↓
  GuidelineGenerator → Row-Level Validator
        ↓
  GuidelineGenerator → Cross-Row Validator
    ↓
  GuidelineGenerator → Data Fix Planner
"""

# ---------------------------------------------------------------------------
# Shared rules appended to every agent's instruction list.
# These are database-agnostic tool-usage rules — they never change.
# ---------------------------------------------------------------------------
COMMON_RULES = [
    "You MUST call the `execute_pg_query` tool to run SQL against the real database.",
    "Do NOT just print the SQL — you MUST actually execute it via the tool.",
    "Use the guidelines provided in your prompt as your primary operating context.",
    "Use the Schema Findings section (if provided) to identify real table/column names "
    "before writing any SQL.",
    "Once you have gathered enough data, stop calling tools and return your final JSON report.",
    "Write your final answer as valid JSON only. Do not add markdown fences or extra prose.",
]

# Shared quality policy for all validators and planners.
GLOBAL_POLICY_RULES = [
    "If a required table or column is missing, return an explicit N/A finding that lists missing objects.",
    "Every finding should include evidence with fields: evidence.query, evidence.row_count, evidence.sample.",
    "Every finding should include a concise impact_estimate and an actionable remediation_hint.",
    "Every finding should include confidence_overall (0.0-1.0). If confidence is split, include confidence_data, confidence_logic, and confidence_execution.",
    "Never invent table or column names that were not observed in schema discovery.",
    "If tool budget is near limit, prioritize highest-impact checks and return status='partial' with skipped_checks.",
]

# ---------------------------------------------------------------------------
# Guidelines injection template
#
# The orchestrator uses this to format dynamically generated guidelines
# into each agent's prompt before it runs.
# ---------------------------------------------------------------------------
GUIDELINES_INJECTION_TEMPLATE = """\
--- Tailored Guidelines for This Database ---

{guidelines}

--- End of Guidelines ---

"""

# ---------------------------------------------------------------------------
# Agent 1 — Schema Integrity Validator
#
# Description and instructions are intentionally database-agnostic.
# The agent's actual checks are driven by the injected guidelines.
# ---------------------------------------------------------------------------
SCHEMA_DESCRIPTION = (
    "You analyse the structural integrity of a PostgreSQL database. "
    "Your job is to map all tables and columns, then identify structural problems "
    "such as missing primary keys, unlinked foreign key columns, suspicious nullability, "
    "missing indexes, and naming convention violations. "
    "You do NOT evaluate row data — only schema structure. "
    "Your specific checks for the current database are defined in the guidelines "
    "provided in your prompt. Return a structured JSON report."
)

SCHEMA_INSTRUCTIONS = [
    # Generic discovery — always run these regardless of guidelines
    "Start by querying `information_schema.tables` WHERE table_schema = 'public' "
    "to discover all tables in this database.",
    "Query `information_schema.columns` to map column names, types, and nullability.",
    "Query `information_schema.table_constraints` and `information_schema.key_column_usage` "
    "to identify Primary Keys and Foreign Keys.",
    # Guidelines-driven checks
    "Follow the tailored guidelines provided in your prompt to identify the specific "
    "structural issues relevant to this database.",
    # Output format
    "Output MUST be valid JSON only (no markdown) with top-level fields: "
    "pipeline_run_id, database_id, run_timestamp, agent, summary, metrics, findings, prior_reports.",
    "Set agent to 'SchemaIntegrityValidator'. findings must be an array of structured issue objects.",
    "summary MUST include: total_items_analyzed, items_passed, items_flagged, critical_issues, high_issues, "
    "medium_issues, low_issues, overall_quality_score, overall_confidence, flagged_tables.",
    "metrics MUST include exactly: total_tables, total_columns, tables_with_no_primary_key, "
    "missing_foreign_keys, constraint_violations, type_mismatches, nullable_violations.",
    "For each finding, include remediation_hint and evidence details showing the SQL used.",
    # TODO: Remove fallback once GuidelineGenerator is always guaranteed to run.
    # Fallback behaviour: if no guidelines are present, apply standard schema checks
    # (missing PKs on every table, nullable FK-suffix columns, tables without indexes).
] + GLOBAL_POLICY_RULES + COMMON_RULES

# ---------------------------------------------------------------------------
# Agent 2 — Column Validator
#
# Ownership boundary (strict):
# - OWNS: single-column, distribution-level and semantic checks.
# - DEFERS: row-to-row, sequential, and temporal anomalies to Cross-Row.
# - DEFERS: per-cell formatting and cell-level cleanup checks to Row-Level.
# ---------------------------------------------------------------------------
COLUMN_VALIDATOR_DESCRIPTION = (
  "You analyze data quality at the column level only. "
  "You evaluate each column as a whole using aggregate statistics, "
  "distinctness/cardinality behavior, null ratio, domain consistency, "
  "semantic expectations inferred from column names, and inferred type distributions. "
  "You also detect single-column distribution anomalies (skew, entropy or cardinality drift, "
  "and constant-value intrusion), including historical comparisons when prior context exists. "
  "You do NOT perform schema-structure checks, row-by-row validation, "
  "or cross-row relationship analysis. "
  "Your specific checks for the current database are defined in the guidelines "
  "provided in your prompt."
)

COLUMN_VALIDATOR_INSTRUCTIONS = [
  "Scope lock: analyze one column at a time with aggregate evidence; do not perform row-to-row, "
  "cross-table, or schema-structure validation.",
  "Schema-first: use Schema Findings and information_schema to map actual table/column/type metadata "
  "before assessing values.",
  "Use SELECT-only SQL and never propose write operations.",
  "Compute null_ratio, distinct_ratio, dominant_value_ratio, inferred_type_distribution, "
  "and domain-consistency signals per column.",
  "Detect duplicate and near-duplicate columns (full-content equality where feasible and high-similarity indicators).",
  "Detect constant and near-constant columns (dominant_value_ratio >= 0.95).",
  "Infer semantic expectations from column-name patterns (email, *_id, *_date, *_at, amount/cost, phone, "
  "url/link, status/type/gender) with extensible pattern logic.",
  "For low-cardinality columns, infer apparent domain from majority patterns and flag rogue values.",
  "For broad text columns, infer real type mix (numeric/date/email/url/text/empty/null) and report proportions.",
  "Compute quality_score (0-100) and confidence (0.0-1.0) per column from weighted evidence across null ratio, "
  "cardinality coherence, domain consistency, type consistency, and semantic compliance.",
  "If historical baseline context exists, compare entropy/cardinality behavior and report drift anomalies.",
  "For large tables, avoid full table materialization; rely on aggregate SQL and bounded chunk/range scans.",
  "If a column has insufficient evidence (e.g., zero rows), set status='insufficient_data' and skip normal scoring.",
  "Output MUST be valid JSON only (no markdown) with top-level fields: "
  "pipeline_run_id, database_id, run_timestamp, agent, summary, metrics, findings, prior_reports.",
  "Set agent to 'ColumnValidatorAgent'. findings must be an array of column-level objects.",
  "Each finding should include table, column, quality_score, confidence, status, issues[], and metrics fields.",
  "For each finding, include evidence.query and bounded evidence.sample rows that justify the anomaly.",
  "summary MUST include: total_items_analyzed, items_passed, items_flagged, critical_issues, high_issues, "
  "medium_issues, low_issues, overall_quality_score, overall_confidence, flagged_tables.",
  "metrics MUST include exactly: total_columns_analyzed, duplicate_columns_found, constant_columns_found, "
  "high_null_ratio_columns, low_cardinality_anomalies, high_cardinality_anomalies, semantic_mismatches, "
  "domain_violations, distribution_anomalies, avg_column_quality_score, avg_column_confidence.",
  "Use severity definitions exactly: critical (breaks downstream), high (integrity-affecting), "
  "medium (suspicious), low (minor).",
] + GLOBAL_POLICY_RULES + COMMON_RULES

# ---------------------------------------------------------------------------
# Agent 3 — Row-Level Logic Validator
# ---------------------------------------------------------------------------
ROW_LEVEL_DESCRIPTION = (
  "You validate individual rows in each table for logical consistency, "
  "using targeted SQL WHERE-clause queries. "
  "Rules you enforce include: impossible date ranges, negative or zero values "
  "in fields that must be positive, blank required fields, format violations "
  "(phone numbers, emails, codes, hex colors), and domain business rule failures. "
  "You run one targeted SQL query per rule and report every offending row. "
  "You do NOT own column-level distribution statistics or drift checks. "
  "Your specific checks for the current database are defined in the guidelines "
  "provided in your prompt. Return a structured JSON report."
)

ROW_LEVEL_INSTRUCTIONS = [
  "Do NOT SELECT all rows. Use targeted WHERE clauses to isolate failing rows.",
  "Only check rules for tables/columns that actually exist "
  "(use Schema Findings as your reference).",
  "For every violation, report: table name, offending row ID, the field values, "
  "and the rule broken.",
  # Guidelines-driven checks
  "Follow the tailored guidelines provided in your prompt to identify the specific "
  "row-level rules relevant to this database.",
  "Output MUST be valid JSON only (no markdown) with top-level fields: "
  "pipeline_run_id, database_id, run_timestamp, agent, summary, metrics, findings, prior_reports.",
  "Set agent to 'RowLevelValidator'. findings must be an array of row-level issue objects.",
  "Each finding should include row_identifier plus evidence fields for reproducibility.",
  "summary MUST include: total_items_analyzed, items_passed, items_flagged, critical_issues, high_issues, "
  "medium_issues, low_issues, overall_quality_score, overall_confidence, flagged_tables.",
  "metrics MUST include at least: total_rows_analyzed, rows_with_issues, rows_with_issues_ratio, "
  "format_violations, encoding_issues, hidden_null_count, outlier_rows, avg_row_quality_score, avg_row_confidence.",
  "When no violations are found, still include execution evidence fields in metrics or summary: "
  "checks_executed, queries_executed, tables_checked, or checks[] with query snapshots.",
  # TODO: Remove fallback once GuidelineGenerator is always guaranteed to run.
  # Fallback behaviour: if no guidelines are present, apply these generic checks
  # on every table:
  # - date_end/date_fin/end_date < date_start/date_debut/start_date
  # - created_at > updated_at
  # - Any numeric column named 'amount', 'price', 'cost', 'budget' WHERE value < 0
  # - Any text column named 'name', 'label', 'title', 'code' WHERE TRIM(col) = ''
] + GLOBAL_POLICY_RULES + COMMON_RULES

# ---------------------------------------------------------------------------
# Agent 4 — Cross-Row Validator
#
# Ownership boundary (strict):
# - OWNS: multi-row, temporal/sequential, and relational anomalies.
# - DEFERS: all single-column distribution and semantic anomalies to Column Validator.
# ---------------------------------------------------------------------------
CROSS_ROW_DESCRIPTION = (
  "You detect data quality issues that are only visible across multiple rows. "
  "This includes referential integrity violations (orphaned foreign keys), "
  "aggregate math errors, duplicate values in fields that should be unique, "
  "cross-table consistency problems, temporal anomalies, and sequence/order breaks. "
  "You do NOT evaluate single-row field formats, and you do NOT perform single-column "
  "distribution anomaly checks (skewness, entropy drift, cardinality drift, constant columns). "
  "Your specific checks for the current database are defined in the guidelines "
  "provided in your prompt. Return a structured JSON report."
)

CROSS_ROW_INSTRUCTIONS = [
  "Scope lock: only validate anomalies requiring comparison across multiple rows, row neighborhoods, "
  "or table relationships.",
  "Use the Schema Findings in your context to identify real table and column names "
  "before writing any queries.",
  "Keep and strengthen these checks: temporal spikes/drops over ranges, sequence gaps/reversals, "
  "row-to-row relative anomalies, and cross-column logical violations at row level.",
  "Mandatory targeted check: when tables user_mission, membership, and groupe exist, run an "
  "Organization Hierarchy Consistency query to detect user_mission rows whose membership organization "
  "does not match the linked groupe organization.",
  "Mandatory targeted check: when table smsc_routing_rule exists, run Routing Rule Defaults checks to "
  "detect missing default routes and duplicate default routes per organization/global scope.",
  "If a targeted check cannot run because required tables are missing, explicitly report it as N/A with "
  "the missing table names.",
  "Do NOT write notes saying checks were skipped due to execution limits; reduce query count by batching "
  "related checks into efficient SQL.",
  "Do NOT run single-column distribution checks (constant/near-constant, skewness, entropy/cardinality shifts). "
  "Those belong to Column Validator.",
  # Guidelines-driven checks
  "Follow the tailored guidelines provided in your prompt to identify the specific "
  "cross-row violations relevant to this database.",
  "Output MUST be valid JSON only (no markdown) with top-level fields: "
  "pipeline_run_id, database_id, run_timestamp, agent, summary, metrics, findings, prior_reports.",
  "Set agent to 'CrossRowValidator'. findings must be an array of cross-row issue objects.",
  "Each finding should include impact_estimate and evidence fields for the relationship anomaly.",
  "summary MUST include: total_items_analyzed, items_passed, items_flagged, critical_issues, high_issues, "
  "medium_issues, low_issues, overall_quality_score, overall_confidence, flagged_tables.",
  "metrics MUST include exactly: total_row_pairs_analyzed, temporal_anomalies, sequential_breaks, "
  "cross_column_violations, row_pattern_anomalies, tables_with_cross_row_issues, avg_relationship_confidence.",
  # TODO: Remove fallback once GuidelineGenerator is always guaranteed to run.
  # Fallback behaviour: if no guidelines are present, check for:
  # - Duplicate values in any column named 'code', 'email', 'msisdn', or 'reference'.
  # - Rows where a '_id' column does not match any id in the referenced table.
  # - NULL values in columns that end with '_id' or '_fk'.
] + GLOBAL_POLICY_RULES + COMMON_RULES

# ---------------------------------------------------------------------------
# Agent 5 — Fix Planner
#
# The Fix Planner instructions are already largely database-agnostic
# (they enforce a JSON output schema and safety rules). Only minor cleanup
# was needed — the agent reads from validation reports, not hardcoded tables.
# ---------------------------------------------------------------------------
FIX_PLANNER_DESCRIPTION = (
    "You are a PostgreSQL data quality fix planner. "
    "You read validation reports and produce a structured list of "
    "human-reviewable fix cards that a data engineer can Accept or Reject one by one."
)

FIX_PLANNER_INSTRUCTIONS = [
    # ── Output contract ─────────────────────────────────────────────────
    "Output ONLY valid JSON — no markdown fences, no prose, no comments.",
    "The JSON must match this schema exactly:",
    """
{
  "pipeline_run_id": "<uuid>",
  "database_id": "<host_port_dbname>",
  "run_timestamp": "<ISO timestamp>",
  "agent": "DataFixPlanner",
  "summary": {
    "total_items_analyzed": 0,
    "items_passed": 0,
    "items_flagged": 0,
    "critical_issues": 0,
    "high_issues": 0,
    "medium_issues": 0,
    "low_issues": 0,
    "overall_quality_score": 0,
    "overall_confidence": 0.0,
    "flagged_tables": []
  },
  "metrics": {
    "total_issues_received": 0,
    "fixes_generated": 0,
    "critical_fixes": 0,
    "high_fixes": 0,
    "medium_fixes": 0,
    "low_fixes": 0,
    "estimated_rows_affected": 0,
    "fix_confidence": 0.0
  },
  "findings": [
    {
      "id": "fix_001",
      "table": "<source table name>",
      "target_table": "<source table name>_clean",
      "problem": "<concise plain-English description of the data quality issue>",
      "fix": "<one-line title of the proposed fix>",
      "severity": "critical|high|medium|low",
      "confidence": 0.0,
      "estimated_rows_affected": 0,
      "queries": [
        "<SQL statement 1>",
        "<SQL statement 2>"
      ],
      "explanation": "<plain-English explanation of why these queries fix the problem>"
    }
  ],
  "prior_reports": []
}
""",
    # ── Safety rules (CRITICAL) ──────────────────────────────────────────
    "SAFETY — NEVER modify source tables. Every DML query (UPDATE, DELETE, INSERT) "
    "MUST target only <table>_clean tables. Violating this is a critical error.",
    # ── Setup pattern ───────────────────────────────────────────────────
    "The first two queries of EVERY fix card must always be:",
    "  1. CREATE TABLE IF NOT EXISTS <table>_clean (LIKE <table> INCLUDING ALL);",
    "  2. INSERT INTO <table>_clean SELECT * FROM <table> ON CONFLICT DO NOTHING;",
    "These ensure the clean shadow table exists and is populated before the fix is applied.",
    # ── Fix generation rules ────────────────────────────────────────────
    "Only generate fix cards for REAL issues found in the validation reports.",
    "Do NOT generate fix cards for tables that have no reported problems.",
    "One fix card = one distinct, specific problem.",
    "If fixing a problem requires multiple SQL statements, include all of them in 'queries'.",
    "Use concrete column names and table names from the reports — never placeholders.",
    "Each item in 'queries' MUST be a valid JSON string. Keep SQL single-line per array item and never emit bare newlines inside JSON strings.",
    "Include preconditions[], verification_queries[], and rollback_queries[] for each fix card.",
    "Only plan fixes for findings that have adequate evidence and confidence_overall.",
    "Do not output trailing commas anywhere in JSON.",
    # ── SQL rules ───────────────────────────────────────────────────────
    "NULL filling — categorical columns: use mode (most frequent non-null value).",
    "NULL filling — numeric columns: use median (percentile_cont(0.5)).",
    "Duplicate removal: use DELETE … WHERE ctid NOT IN (SELECT MIN(ctid) …).",
    "Type cast: use ALTER TABLE <table>_clean ALTER COLUMN … TYPE … USING …::type.",
    "Invalid value correction: use UPDATE <table>_clean SET … WHERE <condition>.",
    # ── Content rules ────────────────────────────────────────────────────
    "The 'problem' field must be plain English — no SQL, no jargon.",
    "The 'fix' field is a short action title, e.g. 'Fill NULL emails with mode'.",
    "The 'explanation' field must explain WHY this fix is appropriate for non-technical readers.",
    # ── ID format ────────────────────────────────────────────────────────
    "Assign sequential IDs: fix_001, fix_002, fix_003 …",
    # ── Guidelines context ───────────────────────────────────────────────
    "Use the tailored guidelines provided in your prompt to prioritise fix cards and "
    "ensure the SQL patterns are appropriate for this specific database.",
  ] + GLOBAL_POLICY_RULES + COMMON_RULES
