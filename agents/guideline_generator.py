"""
guideline_generator.py — DatabaseGuidelineGeneratorAgent

Runs BEFORE each downstream validation agent to produce database-specific,
dynamically-generated guidelines based on live PostgreSQL introspection.

Public API
----------
    generator = DatabaseGuidelineGeneratorAgent(connection_string)
    guidelines_str = generator.generate(
        target_agent="schema_integrity_validator",
        prior_findings="",          # empty on first call
    )
    # ... run pipeline agents with guidelines_str injected ...
    generator.save_guidelines()    # persist versioned entry at end of run

The generator is stateless in its Agno agent instantiation (each call creates a
fresh Agent) but accumulates generated guidelines internally for persistence.
"""

import logging
import re
from typing import Optional

from agno.agent import Agent
from agno.models.google import Gemini

from .guidelines_store import GuidelinesStore, compute_schema_snapshot
from .tools import execute_pg_query

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known target agent identifiers — used for prompt routing and storage keys
# ---------------------------------------------------------------------------
TARGET_AGENT_KEYS = {
    "schema_integrity_validator",
    "row_level_validator",
    "cross_row_validator",
    "data_fix_planner",
}

# ---------------------------------------------------------------------------
# Introspection SQL queries (SELECT-only, safe for any PostgreSQL database)
# ---------------------------------------------------------------------------

_INTROSPECTION_QUERIES = """
-- 1. All public tables
SELECT table_name FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
ORDER BY table_name;

-- 2. All columns with types and nullability
SELECT table_name, column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public'
ORDER BY table_name, ordinal_position;

-- 3. Primary key constraints
SELECT tc.table_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema = kcu.table_schema
WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
ORDER BY tc.table_name;

-- 4. Foreign key relationships
SELECT
    tc.table_name         AS fk_table,
    kcu.column_name       AS fk_column,
    ccu.table_name        AS referenced_table,
    ccu.column_name       AS referenced_column,
    tc.constraint_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
    ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
ORDER BY fk_table;

-- 5. Unique constraints
SELECT tc.table_name, kcu.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
WHERE tc.constraint_type = 'UNIQUE' AND tc.table_schema = 'public'
ORDER BY table_name;

-- 6. Check constraints
SELECT tc.table_name, tc.constraint_name, cc.check_clause
FROM information_schema.table_constraints tc
JOIN information_schema.check_constraints cc
    ON tc.constraint_name = cc.constraint_name AND tc.table_schema = cc.constraint_schema
WHERE tc.constraint_type = 'CHECK' AND tc.table_schema = 'public'
ORDER BY tc.table_name;

-- 7. Indexes
SELECT indexname, tablename, indexdef
FROM pg_catalog.pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname;

-- 8. Tables missing a primary key
SELECT t.table_name
FROM information_schema.tables t
LEFT JOIN information_schema.table_constraints tc
    ON tc.table_name = t.table_name
    AND tc.table_schema = t.table_schema
    AND tc.constraint_type = 'PRIMARY KEY'
WHERE t.table_schema = 'public'
  AND t.table_type = 'BASE TABLE'
  AND tc.constraint_name IS NULL
ORDER BY t.table_name;

-- 9. Nullable columns that end with common FK suffixes (_id, _fk)
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'public'
  AND is_nullable = 'YES'
  AND (column_name LIKE '%_id' OR column_name LIKE '%_fk')
ORDER BY table_name, column_name;

-- 10. Row counts per table (up to 50 tables)
SELECT relname AS table_name, n_live_tup AS estimated_row_count
FROM pg_catalog.pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY n_live_tup DESC
LIMIT 50;
"""

# ---------------------------------------------------------------------------
# Role descriptions for each target agent (used in the generator prompt)
# ---------------------------------------------------------------------------
_ROLE_DESCRIPTIONS = {
    "schema_integrity_validator": (
        "checks the structural integrity of the database: missing primary keys, "
        "unlinked foreign key columns, suspicious nullability, naming convention "
        "violations, and missing indexes."
    ),
    "cross_row_validator": (
        "detects data quality issues that span multiple rows: referential integrity "
        "violations (orphaned foreign keys), aggregate math errors, duplicate values "
        "in columns that should be unique, and cross-table consistency problems."
    ),
    "row_level_validator": (
        "validates individual rows for logical consistency: impossible date ranges, "
        "negative or zero numeric values in fields that must be positive, blank required "
        "fields, format violations (phone numbers, emails, codes), and domain-specific "
        "business rule failures."
    ),
    "data_fix_planner": (
        "reads validation reports from the previous three agents and produces a structured "
        "list of human-reviewable SQL fix cards. It needs to understand which issues were "
        "found, which tables/columns are affected, and what safe corrective SQL patterns "
        "are appropriate."
    ),
}


# ---------------------------------------------------------------------------
# DatabaseGuidelineGeneratorAgent
# ---------------------------------------------------------------------------

class DatabaseGuidelineGeneratorAgent:
    """
    Wraps an Agno agent that introspects a PostgreSQL database and generates
    tailored guidelines for each downstream validation agent.

    Lifecycle (one per pipeline run)::

        gen = DatabaseGuidelineGeneratorAgent(connection_string, model_id, api_key)

        # Called once before each downstream agent:
        guidelines = gen.generate(target_agent="schema_integrity_validator",
                                  prior_findings="")

        # ... run Schema Validator, collect schema_report ...

        guidelines = gen.generate(target_agent="cross_row_validator",
                                  prior_findings=schema_report)

        # ... and so on for row_level_validator and data_fix_planner ...

        gen.save_guidelines()   # persist new version to disk
    """

    def __init__(
        self,
        connection_string: str,
        model_id: str,
        api_key: str,
    ):
        self._connection_string = connection_string
        self._model_id = model_id
        self._api_key = api_key

        # Accumulates all guidelines generated this run — persisted at the end
        self._generated_guidelines: dict[str, str] = {}

        # Schema snapshot string built after introspection (used for versioning)
        self._schema_snapshot_source: str = ""

        # Load prior guidelines from the persistence layer
        self._store = GuidelinesStore(connection_string)
        self._prior_guidelines = self._store.load_latest()
        if self._prior_guidelines:
            logger.info(
                "GuidelineGenerator: loaded prior guidelines v%s for database '%s'.",
                self._prior_guidelines.get("_version"),
                self._store.database_id,
            )
        else:
            logger.info(
                "GuidelineGenerator: no prior guidelines found for database '%s' — "
                "will generate from scratch.",
                self._store.database_id,
            )

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def generate(self, target_agent: str, prior_findings: str = "") -> str:
        """
        Generate guidelines for `target_agent` by introspecting the live database.

        Args:
            target_agent: One of 'schema_integrity_validator', 'cross_row_validator',
                          'row_level_validator', 'data_fix_planner'.
            prior_findings: Accumulated markdown output from agents that have already
                            run this pipeline pass. Empty string on the first call.

        Returns:
            A markdown-formatted guidelines string ready to be injected into the
            downstream agent's prompt.
        """
        if target_agent not in TARGET_AGENT_KEYS:
            logger.warning(
                "GuidelineGenerator: unknown target_agent '%s' — generating generic guidelines.",
                target_agent,
            )

        logger.info(
            "GuidelineGenerator: generating guidelines for '%s'...", target_agent
        )

        prior_guideline_context = self._build_prior_guidelines_context(target_agent)
        prior_findings_section = (
            f"\n\n---\n**Prior Agent Findings (use to focus your guidelines):**\n\n{prior_findings}"
            if prior_findings.strip()
            else ""
        )

        prompt = self._build_prompt(
            target_agent=target_agent,
            prior_guideline_context=prior_guideline_context,
            prior_findings_section=prior_findings_section,
        )

        agent = Agent(
            name="Database Guideline Generator",
            model=Gemini(id=self._model_id, api_key=self._api_key or None),
            description=(
                "You are a database analyst that introspects PostgreSQL databases and "
                "produces precise, actionable validation guidelines for specialist AI agents. "
                "You run SELECT-only queries to understand the database structure, then output "
                "a structured set of guidelines tailored to the target agent's role."
            ),
            instructions=[
                "Run the introspection queries provided in the prompt to discover the real "
                "database structure before writing any guidelines.",
                "Use ONLY SELECT queries — never modify the database in any way.",
                "Base your guidelines entirely on what you discover from the live database. "
                "Do NOT assume any specific table or column names in advance.",
                "Use prior agent findings (if provided) to sharpen your guidelines "
                "— for example, if the schema validator found a likely orphaned FK column, "
                "tell the cross-row validator to specifically probe that relationship.",
                "Output ONLY the guidelines text — no preamble, no meta-commentary.",
                "Format the guidelines as a numbered markdown list for clarity.",
            ],
            tools=[execute_pg_query],
            markdown=True,
        )

        try:
            response = agent.run(prompt)
            guidelines_text = (
                response.content
                if hasattr(response, "content") and response.content
                else str(response)
            )
        except Exception as exc:
            logger.error("GuidelineGenerator agent failed for '%s': %s", target_agent, exc)
            # Fallback: return minimal agnostic guidance so the pipeline doesn't stall
            guidelines_text = (
                f"# Fallback Guidelines for {target_agent}\n\n"
                "1. Use `information_schema.tables` and `information_schema.columns` to "
                "discover all tables and columns in the public schema.\n"
                "2. Apply standard data quality checks appropriate to your role based on "
                "what you find in the database structure.\n"
                "3. Report all findings with table name, column name, and a clear explanation.\n"
                f"\n_Note: Guideline generation failed ({exc}). These are generic fallbacks._"
            )

        self._generated_guidelines[target_agent] = guidelines_text
        self._update_schema_snapshot_source(guidelines_text)

        logger.info("GuidelineGenerator: guidelines ready for '%s'.", target_agent)
        return guidelines_text

    def save_guidelines(self) -> Optional[int]:
        """
        Persist the guidelines generated during this pipeline run as a new version.

        Should be called AFTER all four downstream agents have completed.
        Returns the new version number, or None on failure.
        """
        if not self._generated_guidelines:
            logger.warning("GuidelineGenerator: no guidelines to save — skipping persistence.")
            return None

        snapshot = compute_schema_snapshot(self._schema_snapshot_source or "unknown")

        try:
            version = self._store.save_version(
                guidelines=self._generated_guidelines,
                schema_snapshot=snapshot,
            )
            logger.info(
                "GuidelineGenerator: persisted version %s for database '%s'.",
                version,
                self._store.database_id,
            )
            return version
        except Exception as exc:
            logger.error(
                "GuidelineGenerator: failed to persist guidelines for '%s': %s",
                self._store.database_id,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_prior_guidelines_context(self, target_agent: str) -> str:
        """Build the prior-guidelines section for the generator prompt."""
        if not self._prior_guidelines:
            return ""

        prior_text = self._prior_guidelines.get(target_agent, "")
        if not prior_text:
            return ""

        version = self._prior_guidelines.get("_version", "?")
        created = self._prior_guidelines.get("_created_at", "?")

        return (
            f"\n\n---\n**Prior Guidelines for this agent (from v{version}, {created}):**\n\n"
            f"{prior_text}\n\n"
            "Use these as a warm reference — re-evaluate them against the current live "
            "database state and update or discard anything that no longer applies."
        )

    def _build_prompt(
        self,
        target_agent: str,
        prior_guideline_context: str,
        prior_findings_section: str,
    ) -> str:
        role_desc = _ROLE_DESCRIPTIONS.get(
            target_agent,
            "performs data quality validation on the PostgreSQL database.",
        )
        readable_name = target_agent.replace("_", " ").title()

        return f"""You are generating guidelines for the **{readable_name}** agent.

This agent {role_desc}

---

## Step 1 — Introspect the Database

Run the following SQL queries using the `execute_pg_query` tool to discover the real \
database structure. Run them ONE AT A TIME:

```sql
{_INTROSPECTION_QUERIES.strip()}
```

---

## Step 2 — Generate Tailored Guidelines

Based on what you discover, produce a comprehensive, numbered list of guidelines \
for the **{readable_name}** agent. The guidelines must:

1. Reference REAL table and column names found in this database (not placeholders).
2. Flag specific structural or data risks you detected during introspection.
3. Include concrete SQL patterns or checks the agent should run for this specific database.
4. Be actionable — each guideline should map to a specific check the agent can perform.
{prior_guideline_context}{prior_findings_section}

---

## Output

Write ONLY the guidelines — a numbered markdown list. \
No introduction, no conclusion, no meta-commentary.
"""

    def _update_schema_snapshot_source(self, text: str) -> None:
        """
        Accumulate text snippets to build the schema snapshot hash.
        We append table/column patterns found in the guidelines text.
        """
        # Extract apparent table.column patterns from the generated text
        matches = re.findall(r"\b([a-z_]+\.[a-z_]+)\b", text)
        self._schema_snapshot_source += "\n".join(sorted(set(matches)))
