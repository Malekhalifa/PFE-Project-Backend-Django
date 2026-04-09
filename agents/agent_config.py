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
  GuidelineGenerator → Cross-Row Validator
        ↓
  GuidelineGenerator → Row-Level Validator
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
    "Once you have gathered enough data, stop calling tools and return your final markdown report.",
    "Write your final answer as a clean markdown report. Do not loop endlessly.",
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
    "provided in your prompt."
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
    "Report findings as a clean markdown document with sections: "
    "Table Overview, Missing PKs, Missing/Unlinked FKs, Suspicious Nullability, "
    "Naming Issues, and any additional sections suggested by the guidelines.",
    # TODO: Remove fallback once GuidelineGenerator is always guaranteed to run.
    # Fallback behaviour: if no guidelines are present, apply standard schema checks
    # (missing PKs on every table, nullable FK-suffix columns, tables without indexes).
] + COMMON_RULES

# ---------------------------------------------------------------------------
# Agent 2 — Cross-Row Validator
# ---------------------------------------------------------------------------
CROSS_ROW_DESCRIPTION = (
    "You detect data quality issues that are only visible across multiple rows. "
    "This includes referential integrity violations (orphaned foreign keys), "
    "aggregate math errors, duplicate values in fields that should be unique, "
    "and cross-table consistency problems. "
    "You do NOT evaluate single-row field formats — only aggregate and relational rules. "
    "Your specific checks for the current database are defined in the guidelines "
    "provided in your prompt."
)

CROSS_ROW_INSTRUCTIONS = [
    "Use the Schema Findings in your context to identify real table and column names "
    "before writing any queries.",
    # Guidelines-driven checks
    "Follow the tailored guidelines provided in your prompt to identify the specific "
    "cross-row violations relevant to this database.",
    "List all violations clearly — show the offending IDs and values. "
    "If no violations are found, state that explicitly.",
    # TODO: Remove fallback once GuidelineGenerator is always guaranteed to run.
    # Fallback behaviour: if no guidelines are present, check for:
    # - Duplicate values in any column named 'code', 'email', 'msisdn', or 'reference'.
    # - Rows where a '_id' column does not match any id in the referenced table.
    # - NULL values in columns that end with '_id' or '_fk'.
] + COMMON_RULES

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
    "Your specific checks for the current database are defined in the guidelines "
    "provided in your prompt."
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
    # TODO: Remove fallback once GuidelineGenerator is always guaranteed to run.
    # Fallback behaviour: if no guidelines are present, apply these generic checks
    # on every table:
    # - date_end/date_fin/end_date < date_start/date_debut/start_date
    # - created_at > updated_at
    # - Any numeric column named 'amount', 'price', 'cost', 'budget' WHERE value < 0
    # - Any text column named 'name', 'label', 'title', 'code' WHERE TRIM(col) = ''
] + COMMON_RULES

# ---------------------------------------------------------------------------
# Agent 4 — Fix Planner
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
  "fixes": [
    {
      "id": "fix_001",
      "table": "<source table name>",
      "target_table": "<source table name>_clean",
      "problem": "<concise plain-English description of the data quality issue>",
      "fix": "<one-line title of the proposed fix>",
      "queries": [
        "<SQL statement 1>",
        "<SQL statement 2>"
      ],
      "explanation": "<plain-English explanation of why these queries fix the problem>"
    }
  ]
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
]
