"""
schemas.py — Shared Pydantic models for the QualityLab validation pipeline.

These are the ONLY types that travel between agents, steps, and the reducer.
Every agent declares output_schema= one of these models. No DSL strings.
"""

from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

Severity  = Literal["low", "medium", "high", "critical"]

# Old values kept for backward compat; new pipeline uses structural/statistical/relational
Validator = Literal[
    "schema",
    "profiler",
    "structural",   # was: row
    "statistical",  # was: column
    "relational",   # was: crossrow
    # legacy — still accepted from old jobs
    "column",
    "row",
    "crossrow",
]

_SEVERITY_RANK: dict[str, int] = {
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}


# ---------------------------------------------------------------------------
# Core finding — emitted by every validator agent
# ---------------------------------------------------------------------------

class Finding(BaseModel):
    """A single validated data-quality issue."""

    validator: Validator = Field(..., description="Which validator produced this finding")
    entity: str = Field(
        ...,
        description="Fully-qualified entity: 'table_name' or 'table_name.column_name'. "
                    "Never multi-column like 'table.(col1,col2)'.",
    )
    rule: str = Field(
        ...,
        description="Short rule identifier, e.g. 'null_ratio_high', 'orphaned_fk', 'missing_pk'.",
    )
    severity: Severity
    confidence: float = Field(..., ge=0.0, le=1.0, description="Evidence strength [0, 1]")
    evidence: dict = Field(
        default_factory=dict,
        description="Concrete, quantifiable metrics — e.g. {'null_ratio': 0.34, 'sample_size': 1000}",
    )
    suggested_action: Optional[str] = Field(
        default=None,
        description="Plain-English recommendation for a data engineer.",
    )

    @property
    def severity_rank(self) -> int:
        return _SEVERITY_RANK.get(self.severity, 0)


# ---------------------------------------------------------------------------
# Validator output — one per agent run
# ---------------------------------------------------------------------------

class ValidationReport(BaseModel):
    """Returned by Schema, Structural, Statistical, and Relational validator agents."""

    validator: Validator
    findings: list[Finding] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Profiler output — produced by the Profiler agent before the parallel trio
# ---------------------------------------------------------------------------

class ProfilerColumnStat(BaseModel):
    """Statistical profile for a single column."""

    column_name: str
    null_rate: float = Field(..., ge=0.0, le=1.0, description="Fraction of NULL values")
    cardinality: int = Field(..., ge=0, description="Approximate distinct value count")
    total_rows: int = Field(..., ge=0)
    min_value: Optional[str] = None
    max_value: Optional[str] = None
    top_values: list[str] = Field(
        default_factory=list,
        description="Up to 5 most frequent values as strings",
    )
    outlier_flag: bool = Field(
        default=False,
        description="True if outliers were detected in numeric distribution",
    )


class ProfilerTableStat(BaseModel):
    """Statistical profile for a single table."""

    table_name: str
    row_count: int = Field(..., ge=0)
    column_stats: list[ProfilerColumnStat] = Field(default_factory=list)


class ProfilerReport(BaseModel):
    """Returned by the Profiler agent. Injected into session state for all parallel validators."""

    tables: list[ProfilerTableStat] = Field(default_factory=list)

    def get_table(self, table_name: str) -> Optional[ProfilerTableStat]:
        return next((t for t in self.tables if t.table_name == table_name), None)

    def table_names(self) -> list[str]:
        return [t.table_name for t in self.tables]


# ---------------------------------------------------------------------------
# Reducer output — produced by the pure-Python reduce_signals step
# ---------------------------------------------------------------------------

class ReducedFindings(BaseModel):
    """Deduped, ranked, capped list of findings passed to the Fix Planner."""

    findings: list[Finding] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Fix dependency graph — produced by reducer, consumed by Fix Planner + frontend
# ---------------------------------------------------------------------------

class FixDependencyEdge(BaseModel):
    """Directed dependency: fix `fix_id` must run after `depends_on` completes."""

    fix_id: str = Field(..., description="The fix that has a prerequisite")
    depends_on: str = Field(..., description="fix_id that must be accepted first")
    reason: str = Field(..., description="Plain-English explanation of why this ordering matters")


class FixDependencyGraph(BaseModel):
    """Reducer output: ranked findings plus an explicit fix-ordering graph."""

    findings: list[Finding] = Field(default_factory=list)
    dependency_edges: list[FixDependencyEdge] = Field(default_factory=list)

    def to_reduced_findings(self) -> ReducedFindings:
        """Backward-compat shim for code still consuming ReducedFindings."""
        return ReducedFindings(findings=self.findings)


# ---------------------------------------------------------------------------
# Fix Planner output
# ---------------------------------------------------------------------------

class FixCard(BaseModel):
    """One actionable fix — human-reviewable; must be Accept/Reject-safe."""

    fix_id: str = Field(
        default="",
        description="Stable slug or UUID identifying this fix, e.g. 'fill-null-emails'. Used for dependency references.",
    )
    title: str = Field(..., description="Short action title, e.g. 'Fill NULL emails with mode'")
    target: str = Field(..., description="'table_name' or 'table_name.column_name'")
    rationale: str = Field(..., description="Plain-English explanation for non-technical readers")
    sql: str = Field(
        default="",
        description=(
            "The mitigation SQL string — always required, never null. "
            "Use an empty string only if no SQL applies."
        ),
    )
    explain_summary: Optional[str] = Field(
        default=None,
        description="EXPLAIN (FORMAT JSON) output or summary if run",
    )
    preconditions: list[str] = Field(default_factory=list)
    verification_queries: list[str] = Field(default_factory=list)
    rollback_queries: list[str] = Field(default_factory=list)
    # Dry-run impact preview (populated by Fix Planner, shown in UI before user decides)
    rows_affected_estimate: Optional[int] = Field(
        default=None,
        description="Estimated number of rows this fix will touch",
    )
    tables_touched: list[str] = Field(
        default_factory=list,
        description="Tables modified or read by this fix's SQL",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="fix_id values that must be accepted before this fix can run",
    )

    @field_validator("sql", mode="before")
    @classmethod
    def coerce_sql_none(cls, v: Any) -> str:
        if not isinstance(v, str):
            return ""
        import re
        # Fix common LLM mistake: COUNT() → COUNT(*)
        return re.sub(r'\bCOUNT\(\s*\)', 'COUNT(*)', v)


class FixPlan(BaseModel):
    """Returned by the Fix Planner agent. One FixCard per actionable finding."""

    cards: list[FixCard] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Learning layer — persisted per user, fed back into the reducer
# ---------------------------------------------------------------------------

class LearnedPattern(BaseModel):
    """A user's historical accept/reject decision for a given rule+entity combination."""

    rule_family: str = Field(..., description="e.g. 'null_ratio_high', 'orphaned_fk'")
    entity: str = Field(..., description="'table_name' or 'table_name.column_name'")
    decision: Literal["accepted", "rejected"]
    frequency: int = Field(default=1, ge=1, description="How many times this pattern was seen")
    # datetime stored as ISO string to keep this model serialization-framework-agnostic
    last_seen: str = Field(default="", description="ISO-8601 datetime of most recent decision")
