"""
langfuse_tracer.py — Fail-safe Langfuse v4 observability for QualityLab.

Lazy-init, zero-coupling: if LANGFUSE_SECRET_KEY is unset every function is a no-op.
Langfuse errors are caught and logged — they never reach the pipeline caller.

Trace propagation uses ContextVar so it works across Agno's thread-pool parallelism:
spawned threads inherit the parent context at creation time.
"""
from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger("qualitylab.langfuse")

# Holds the active pipeline-level LangfuseSpan for the current pipeline run.
_current_span: ContextVar[Any] = ContextVar("_ql_langfuse_span", default=None)

_client: Any = None
_client_init_attempted = False


def _get_client() -> Any:
    global _client, _client_init_attempted
    if _client_init_attempted:
        return _client
    _client_init_attempted = True
    if not (os.environ.get("LANGFUSE_SECRET_KEY") and os.environ.get("LANGFUSE_PUBLIC_KEY")):
        return None
    try:
        from langfuse import Langfuse
        _client = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
        )
        logger.info("Langfuse client initialized (host=%s)", os.environ.get("LANGFUSE_HOST", "cloud"))
    except Exception as exc:
        logger.warning("Langfuse init failed — observability disabled: %s", exc)
    return _client


def start_pipeline_trace(
    *,
    pipeline_run_id: str,
    job_id: str | None,
    database_id: str,
    model_id: str,
) -> None:
    """Start a root Langfuse span for one pipeline run. Stores it in a ContextVar."""
    client = _get_client()
    if client is None:
        return
    try:
        # Langfuse v4 requires a 32-char lowercase hex trace ID (UUID without hyphens).
        lf_trace_id = pipeline_run_id.replace("-", "").lower()
        span = client.start_observation(
            trace_context={"trace_id": lf_trace_id},
            name="qualitylab-pipeline",
            as_type="span",
            input={"database_id": database_id, "model_id": model_id, "job_id": job_id or "unknown"},
            metadata={
                "job_id": job_id or "unknown",
                "database_id": database_id,
                "model_id": model_id,
            },
        )
        _current_span.set(span)
        logger.debug("Langfuse pipeline span started: trace_id=%s", pipeline_run_id)
    except Exception as exc:
        logger.warning("Langfuse start_pipeline_trace failed: %s", exc)


def create_agent_span(
    *,
    agent_name: str,
    elapsed_ms: float,
    tool_calls: int,
    findings_count: int,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    model_id: str | None = None,
) -> None:
    """Record one agent step as a child observation under the current pipeline span."""
    pipeline_span = _current_span.get(None)
    if pipeline_span is None:
        return
    try:
        metadata = {
            "tool_calls": tool_calls,
            "findings_count": findings_count,
            "elapsed_ms": round(elapsed_ms),
        }
        has_tokens = input_tokens is not None or output_tokens is not None

        if has_tokens:
            obs = pipeline_span.start_observation(
                name=agent_name,
                as_type="generation",
                model=model_id or os.environ.get("GEMINI_MODEL", "gemini-2.0-flash"),
                usage_details={
                    "input": input_tokens or 0,
                    "output": output_tokens or 0,
                    "total": (input_tokens or 0) + (output_tokens or 0),
                },
                metadata=metadata,
            )
        else:
            obs = pipeline_span.start_observation(
                name=agent_name,
                as_type="span",
                metadata=metadata,
            )

        obs.end()
    except Exception as exc:
        logger.warning("Langfuse create_agent_span failed (%s): %s", agent_name, exc)


def finish_pipeline_trace(
    *,
    data_quality_score: int | None = None,
    confidence_score: int | None = None,
    total_findings: int | None = None,
    total_fixes: int | None = None,
    total_duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    """Finalize the pipeline span with summary output and flush to Langfuse."""
    span = _current_span.get(None)
    if span is None:
        return
    try:
        output = {k: v for k, v in {
            "data_quality_score": data_quality_score,
            "confidence_score": confidence_score,
            "total_findings": total_findings,
            "total_fixes": total_fixes,
            "total_duration_ms": total_duration_ms,
        }.items() if v is not None}

        if error:
            span.update(output=output, level="ERROR", status_message=error[:500])
        else:
            span.update(output=output)

        span.set_trace_io(output=output)
        span.end()

        client = _get_client()
        if client:
            client.flush()
        logger.debug("Langfuse pipeline trace finalized")
    except Exception as exc:
        logger.warning("Langfuse finish_pipeline_trace failed: %s", exc)
    finally:
        _current_span.set(None)
