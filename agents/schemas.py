"""
schemas.py — Shared Pydantic models for the QualityLab validation pipeline.

These are the ONLY types that travel between agents, steps, and the reducer.
Every agent declares output_schema= one of these models. No DSL strings.
"""

import logging
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Observability metadata — emitted by every agent
# ---------------------------------------------------------------------------

class AgentMetadata(BaseModel):
    tables_scanned: int = 0
    tool_calls_used: int = 0
    skipped_tables: list[str] = Field(default_factory=list)

    @field_validator("tables_scanned", "tool_calls_used", mode="before")
    @classmethod
    def coerce_to_int(cls, v: Any) -> int:
        if isinstance(v, list):
            return len(v)
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0


# ---------------------------------------------------------------------------
# Controlled vocabularies
# ---------------------------------------------------------------------------

Severity  = Literal["low", "medium", "high", "critical"]
RemediationFamily = Literal[
    "schema_design",
    "referential",
    "nullability",
    "duplicates",
    "statistical",
    "temporal",
    "content",
    "type_integrity",
    "business_rules",
    "ai_investigative",
    "unknown",
]
InteractionModel = Literal[
    "execute_ddl",
    "human_decision",
    "advisory",
    "blocked",
    "investigate",
]
SourceType = Literal["deterministic", "ai", "hybrid"]

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

# Deterministic rule → card_family mapping used as fallback when LLM omits card_family.
_RULE_TO_FAMILY: dict[str, str] = {
    "missing_primary_key":    "schema_design",
    "missing_pk":             "schema_design",
    "missing_fk_index":       "schema_design",
    "missing_index":          "schema_design",
    "unindexed_fk":           "schema_design",
    "nullable_fk":            "nullability",
    "null_rate_high":         "nullability",
    "null_ratio_high":        "nullability",
    "nullable_foreign_key":   "nullability",
    "duplicate_values":       "duplicates",
    "duplicate_rows":         "duplicates",
    "orphaned_fk":            "referential",
    "orphaned_row":           "referential",
    "referential_integrity":  "referential",
    "type_mismatch":          "type_integrity",
    "categorical_drift":      "statistical",
    "outlier_detected":       "statistical",
    "distribution_anomaly":   "statistical",
    "temporal_inconsistency": "temporal",
    "stale_timestamp":        "temporal",
    "naming_convention":      "schema_design",
    "missing_not_null":       "nullability",
}


# ---------------------------------------------------------------------------
# Core finding — emitted by every validator agent
# ---------------------------------------------------------------------------

class Finding(BaseModel):
    """A single validated data-quality issue."""

    @model_validator(mode="before")
    @classmethod
    def normalize_field_names(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        # LLM sometimes emits finding_type instead of rule
        if "rule" not in values and "finding_type" in values:
            values["rule"] = values.pop("finding_type")
        # LLM sometimes emits table/table_name instead of entity
        if "entity" not in values:
            table = values.get("table") or values.get("table_name") or ""
            col = values.get("column") or values.get("column_name") or ""
            if table:
                values["entity"] = f"{table}.{col}" if col else table
        return values

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
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Evidence strength [0, 1]. Required float — never null or omitted.",
    )

    @field_validator("confidence", mode="before")
    @classmethod
    def coerce_confidence(cls, v: Any, info: Any) -> float:
        if v is not None:
            try:
                return max(0.0, min(1.0, float(v)))
            except (TypeError, ValueError):
                pass
        # null/unparseable: use severity as proxy so high-signal findings survive
        # while low-severity uncertain ones drop below min_confidence and get filtered.
        severity = (info.data or {}).get("severity", "medium") if info else "medium"
        _fallback: dict[str, float] = {
            "critical": 0.90,
            "high":     0.85,
            "medium":   0.75,
            "low":      0.70,  # above MIN_CONFIDENCE=0.65 threshold
        }
        return _fallback.get(str(severity), 0.75)

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

    @model_validator(mode="before")
    @classmethod
    def infer_validator(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values
        if not values.get("validator"):
            findings = values.get("findings") or []
            if findings and isinstance(findings[0], dict):
                values["validator"] = findings[0].get("validator", "schema")
        return values

    validator: Validator
    findings: list[Finding] = Field(default_factory=list)
    metadata: Optional[AgentMetadata] = None


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
    # Extended numeric stats (optional — populated for numeric columns only)
    mean: Optional[float] = None
    stddev: Optional[float] = None
    p01: Optional[float] = None
    p50: Optional[float] = None
    p99: Optional[float] = None
    # Categorical entropy: -SUM(freq * ln(freq)) — populated for non-numeric columns
    entropy: Optional[float] = None


class ProfilerTableStat(BaseModel):
    """Statistical profile for a single table."""

    table_name: str
    row_count: int = Field(..., ge=0)
    column_stats: list[ProfilerColumnStat] = Field(default_factory=list)


class ProfilerReport(BaseModel):
    """Returned by the Profiler agent. Injected into session state for all parallel validators."""

    tables: list[ProfilerTableStat] = Field(default_factory=list)
    metadata: Optional[AgentMetadata] = None

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
    findings_capped: bool = Field(
        default=False,
        description="True when the pipeline found more issues than the top-N cap. "
                    "Score reflects only the top-N highest-priority findings.",
    )


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
    findings_capped: bool = Field(default=False)

    def to_reduced_findings(self) -> ReducedFindings:
        """Backward-compat shim for code still consuming ReducedFindings."""
        return ReducedFindings(findings=self.findings, findings_capped=self.findings_capped)


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
    risk_level: Optional[Literal["low", "medium", "high"]] = Field(
        default=None,
        description="Operational risk: high=DELETE/DROP/UPDATE, medium=ADD CONSTRAINT/INDEX, low=advisory/ADD COLUMN",
    )
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
    verification_results: Optional[dict] = Field(
        default=None,
        description="Populated by FixVerifier post-processor after the LLM agent completes. Never written by the LLM.",
    )
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
    card_family: RemediationFamily = Field(
        default="unknown",
        description="Semantic remediation family used by frontend evidence renderers.",
    )
    interaction_model: InteractionModel = Field(
        default="investigate",
        description=(
            "User interaction model — controls the UI action gate. "
            "execute_ddl: safe reversible DDL (ADD PK, CREATE INDEX, ADD CONSTRAINT) — shows Execute button. "
            "human_decision: irreversible DML (DELETE, UPDATE, DROP) — requires explicit human approval. "
            "advisory: diagnostic SELECT only, no DML. "
            "investigate: manual inspection needed. "
            "blocked: fix depends on unresolved prerequisite."
        ),
    )
    evidence: dict = Field(default_factory=dict)
    evidence_payload: Optional[dict] = Field(
        default=None,
        description="Family-specific evidence payload with a '_type' discriminator.",
    )
    source_rule: str = Field(default="", description="Finding.rule that produced this card.")
    source_type: SourceType = Field(default="ai", description="deterministic, ai, or hybrid provenance.")
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    severity: Severity = "medium"

    @model_validator(mode="before")
    @classmethod
    def derive_title(cls, values: Any) -> Any:
        if isinstance(values, dict) and not values.get("title") and values.get("fix_id"):
            values["title"] = values["fix_id"].replace("-", " ").title()
        return values

    @field_validator("sql", mode="before")
    @classmethod
    def coerce_sql_none(cls, v: Any) -> str:
        if not isinstance(v, str):
            return ""
        import re
        return re.sub(r'\bCOUNT\(\s*\)', 'COUNT(*)', v)

    @model_validator(mode="after")
    def infer_risk_level(self) -> "FixCard":
        # Auto-derive card_family from source_rule when LLM leaves it as "unknown".
        if self.card_family == "unknown" and self.source_rule:
            derived = _RULE_TO_FAMILY.get(self.source_rule.lower())
            if derived:
                self.card_family = derived  # type: ignore[assignment]

        if self.interaction_model == "blocked" and self.sql:
            self.sql = ""
        if self.interaction_model == "blocked":
            self.risk_level = self.risk_level or "medium"
            return self
        # Semantic contract enforcement — warn, never hard-fail (LLM is the source)
        if self.interaction_model == "execute_ddl" and not self.rollback_queries:
            logger.warning(
                "FixCard contract: execute_ddl '%s' has no rollback_queries",
                self.fix_id or self.title,
            )
        if self.interaction_model == "advisory":
            sql_upper = (self.sql or "").upper().strip()
            if sql_upper and not sql_upper.startswith("SELECT"):
                logger.warning(
                    "FixCard contract: advisory '%s' has non-SELECT sql — clearing",
                    self.fix_id or self.title,
                )
                self.sql = ""
        if self.interaction_model == "human_decision" and not self.evidence_payload:
            logger.warning(
                "FixCard contract: human_decision '%s' missing evidence_payload",
                self.fix_id or self.title,
            )
        if self.risk_level is not None:
            return self
        sql = (self.sql or "").upper()
        if not sql:
            return self
        if any(kw in sql for kw in ("DELETE ", "DROP ", "TRUNCATE ")):
            self.risk_level = "high"
        elif any(kw in sql for kw in ("UPDATE ", "ADD CONSTRAINT", "CREATE INDEX", "CREATE UNIQUE")):
            self.risk_level = "medium"
        elif any(kw in sql for kw in ("ADD COLUMN", "ALTER COLUMN", "ALTER TABLE")):
            self.risk_level = "medium"
        else:
            self.risk_level = "low"
        return self


class FixPlan(BaseModel):
    """Returned by the Fix Planner agent. One FixCard per actionable finding."""

    cards: list[FixCard] = Field(default_factory=list)
    metadata: Optional[AgentMetadata] = None


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
