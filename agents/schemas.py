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
Validator = Literal["schema", "column", "row", "crossrow"]

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
    """Returned by Schema, Column, Row, and CrossRow validator agents."""

    validator: Validator
    findings: list[Finding] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Reducer output — produced by the pure-Python reduce_signals step
# ---------------------------------------------------------------------------

class ReducedFindings(BaseModel):
    """Deduped, ranked, capped list of findings passed to the Fix Planner."""

    findings: list[Finding] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Fix Planner output
# ---------------------------------------------------------------------------

class FixCard(BaseModel):
    """One actionable fix — human-reviewable; must be Accept/Reject-safe."""

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
