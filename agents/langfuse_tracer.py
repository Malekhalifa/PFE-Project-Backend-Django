"""
langfuse_tracer.py — Per-agent Langfuse v4 observability for QualityLab.

Hierarchy (fixed):
  trace: "QualityLab Pipeline — <run_id>"  ← parent span (pipeline level)
  ├── generation: "Schema Integrity Validator"
  ├── generation: "Statistical Validator"
  ├── generation: "Structural Validator"
  ├── generation: "Relational Validator"
  └── generation: "Data Fix Planner"

Pipeline-level scores (Data Quality Score, Confidence Score) are pushed
as Langfuse scores on the parent trace after all agents complete.

Lazy-init, zero-coupling: if LANGFUSE_SECRET_KEY is unset every function is a no-op.
Langfuse errors are caught and logged — they never reach the pipeline caller.

5 LLM agents tracked:
  - Schema Integrity Validator
  - Statistical Validator
  - Structural Validator
  - Relational Validator
  - Data Fix Planner

Profiler and Reducer are pure-Python executors — not tracked (no tokens, no LLM cost).
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("qualitylab.langfuse")

_client: Any = None
_client_init_attempted = False

# Map agent phase names to tags for filtering in Langfuse UI.
_AGENT_SLUG: dict[str, str] = {
    "Schema Integrity Validator": "schema-validator",
    "Statistical Validator": "statistical-validator",
    "Structural Validator": "structural-validator",
    "Relational Validator": "relational-validator",
    "Data Fix Planner": "fix-planner",
}


def _get_client() -> Any:
    global _client, _client_init_attempted
    if _client_init_attempted:
        return _client
    _client_init_attempted = True
    if not (os.environ.get("LANGFUSE_SECRET_KEY") and os.environ.get("LANGFUSE_PUBLIC_KEY")):
        return None
    try:
        from langfuse import Langfuse  # noqa: E402
        _client = Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
            timeout=int(
                os.environ.get("LANGFUSE_TIMEOUT", "30")
            ),
            flush_interval=float(
                os.environ.get("LANGFUSE_FLUSH_INTERVAL", "2.0")
            ),
            flush_at=int(
                os.environ.get("LANGFUSE_FLUSH_AT", "5")
            ),
        )
        logger.info(
            "Langfuse client initialized (host=%s)",
            os.environ.get("LANGFUSE_HOST", "cloud"),
        )
    except Exception as exc:
        logger.warning("Langfuse init failed — observability disabled: %s", exc)
    return _client


def record_pipeline_start(
    *,
    pipeline_run_id: str,
    job_id: str | None,
    database_id: str,
    table_count: int,
    model_id: str,
) -> dict[str, str]:
    """Create a parent pipeline-level span that nests all agent generations.

    Returns a dict with ``trace_id`` and ``observation_id`` — pass these to
    ``record_agent_trace()`` so each agent nests under this pipeline root.

    Call once per pipeline run, before any agent starts.
    """
    client = _get_client()
    result: dict[str, str] = {"trace_id": "", "observation_id": ""}
    if client is None:
        return result

    try:
        from langfuse import propagate_attributes  # noqa: E402

        lf_session_id = pipeline_run_id.replace("-", "").lower()

        pipeline_input = {
            "database_id": database_id,
            "table_count": str(table_count),
            "model_id": model_id,
        }

        obs = client.start_observation(
            name="QualityLab Pipeline",
            as_type="span",
            input=pipeline_input,
            metadata={
                "pipeline_run_id": pipeline_run_id,
                "job_id": str(job_id or "unknown"),
                "database_id": database_id,
                "table_count": str(table_count),
                "model_id": model_id,
                "sdk_version": "langfuse-python-v4",
            },
        )

        with propagate_attributes(
            session_id=lf_session_id,
            user_id=str(job_id or "unknown"),
            tags=["pipeline", f"db:{database_id[:16]}"],
            metadata={
                "pipeline_run_id": pipeline_run_id,
                "database_id": database_id,
                "table_count": str(table_count),
                "model_id": model_id,
            },
        ):
            pass  # attributes propagate to the parent obs above

        result["trace_id"] = getattr(obs, "trace_id", "")
        result["observation_id"] = getattr(obs, "id", "")

        # End the parent span immediately — agent observations still attach
        # to the trace via trace_id. The parent span exists to carry pipeline
        # input/metadata; clock time is meaningless (sub-second).
        obs.end()

        client.flush()
        logger.info(
            "Langfuse pipeline trace created: %s (tables=%d)",
            pipeline_run_id,
            table_count,
        )
    except Exception as exc:
        logger.warning("Langfuse record_pipeline_start failed: %s", exc)
    return result


def record_agent_trace(
    *,
    agent_name: str,
    pipeline_run_id: str,
    job_id: str | None,
    database_id: str,
    model_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    elapsed_ms: float,
    tool_calls: int,
    findings_count: int,
    error: str | None = None,
    parent_trace_id: str = "",
    tool_timings: list[dict[str, Any]] | None = None,
    time_to_first_token: float | None = None,
    reasoning_tokens: int | None = None,
    reasoning_step_count: int = 0,
) -> None:
    """Record one LLM agent run as a generation nested under the pipeline trace.

    When ``parent_trace_id`` is provided, the generation is attached to the
    pipeline-level trace created by ``record_pipeline_start()``.  Falls back
    to a standalone top-level generation when no parent trace exists.

    Each tool call is recorded as a child span ("tool:<name>") so the Langfuse
    UI shows the thinking → tool → thinking rhythm.  Thinking time is computed
    as total elapsed minus sum of tool durations.

    Metadata values are cast to ``str`` — Langfuse v4 requires ``dict[str, str]``
    with values ≤ 200 characters.
    """
    client = _get_client()
    if client is None:
        return

    try:
        from langfuse.types import TraceContext  # noqa: E402
        from langfuse import propagate_attributes  # noqa: E402

        lf_session_id = pipeline_run_id.replace("-", "").lower()
        agent_slug = _AGENT_SLUG.get(agent_name, agent_name.lower().replace(" ", "-"))
        total_tokens = (input_tokens or 0) + (output_tokens or 0)
        tool_timings = tool_timings or []

        tags = [f"agent:{agent_slug}"]
        if pipeline_run_id and pipeline_run_id != "unknown":
            tags.append(f"pipeline:{pipeline_run_id}")

        # ── Thinking time ─────────────────────────────────────────────────
        sum_tool_ms = sum(t.get("elapsed_ms", 0) for t in tool_timings)
        thinking_ms = max(0.0, elapsed_ms - sum_tool_ms)

        # All metadata values MUST be strings (Langfuse v4 requirement).
        metadata: dict[str, str] = {
            "agent": agent_name,
            "agent_slug": agent_slug,
            "database_id": database_id,
            "model_id": model_id or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            "pipeline_run_id": pipeline_run_id,
            "job_id": str(job_id or "unknown"),
            "thinking_ms": str(round(thinking_ms)),
            "tool_ms": str(round(sum_tool_ms)),
        }
        if time_to_first_token is not None:
            metadata["ttft_ms"] = str(round(time_to_first_token * 1000))
        if reasoning_tokens is not None:
            metadata["reasoning_tokens"] = str(reasoning_tokens)
        if reasoning_step_count:
            metadata["reasoning_steps"] = str(reasoning_step_count)

        output: dict[str, Any] = {
            "findings_count": findings_count,
            "tool_calls": tool_calls,
            "elapsed_ms": round(elapsed_ms),
            "thinking_ms": round(thinking_ms),
            "tool_exec_ms": round(sum_tool_ms),
            "agent": agent_slug,
        }
        if time_to_first_token is not None:
            output["time_to_first_token_s"] = round(time_to_first_token, 3)
        if reasoning_tokens is not None:
            output["reasoning_tokens"] = reasoning_tokens

        usage_details = {
            "input": input_tokens or 0,
            "output": output_tokens or 0,
            "total": total_tokens,
        }

        if error:
            output["error"] = error[:500]
            output["status"] = "error"

        # ── Agent generation (parent span) ──────────────────────────────
        obs_kwargs: dict[str, Any] = {
            "name": agent_name,
            "as_type": "generation",
            "model": model_id or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            "input": {
                "database_id": database_id,
                "model_id": model_id,
            },
            "metadata": metadata,
        }
        if parent_trace_id:
            obs_kwargs["trace_context"] = TraceContext(trace_id=parent_trace_id)

        obs = client.start_observation(**obs_kwargs)

        with propagate_attributes(
            session_id=lf_session_id,
            user_id=str(job_id or "unknown"),
            tags=tags,
            metadata=metadata,
        ):
            # ── Tool-level child spans (gated — off by default) ──────────
            # Each tool span is an extra HTTP call to the Langfuse container.
            # On memory-constrained self-hosted setups, too many spans can
            # cause 5s timeouts on span export. Enable with:
            #   LANGFUSE_TOOL_SPANS=1
            if os.environ.get("LANGFUSE_TOOL_SPANS") in ("1", "true", "yes"):
                for i, tt in enumerate(tool_timings[:50]):  # hard cap 50 spans/agent
                    tool_name = tt.get("tool", "unknown_tool")
                    tool_ms = tt.get("elapsed_ms", 0)
                    tool_bytes = tt.get("result_bytes", 0)
                    tool_query = tt.get("query", "")[:500]

                    tool_meta: dict[str, str] = {
                        "tool_index": str(i + 1),
                        "tool": tool_name,
                        "elapsed_ms": str(tool_ms),
                        "result_bytes": str(tool_bytes),
                    }

                    tool_kw: dict[str, Any] = {
                        "name": f"tool:{tool_name}",
                        "as_type": "span",
                        "trace_context": (
                            TraceContext(trace_id=parent_trace_id)
                            if parent_trace_id
                            else None
                        ),
                        "input": {"query": tool_query} if tool_query else {},
                        "output": {"result_bytes": tool_bytes, "elapsed_ms": tool_ms},
                        "metadata": tool_meta,
                    }

                    try:
                        tool_obs = client.start_observation(**tool_kw)
                        tool_obs.end()
                    except Exception as _tool_span_exc:
                        logger.debug("Tool span %d skipped: %s", i + 1, _tool_span_exc)
                        break  # stop creating spans if Langfuse is overloaded

            # ── Final update ───────────────────────────────────────────
            if error:
                obs.update(
                    output=output,
                    level="ERROR",
                    status_message=error[:500],
                    usage_details=usage_details,
                    metadata=metadata,
                )
            else:
                obs.update(
                    output=output,
                    usage_details=usage_details,
                    metadata=metadata,
                )

        obs.end()

        client.flush()
        logger.debug(
            "Langfuse agent trace recorded: %s tokens=%d findings=%d ms=%.0f "
            "thinking_ms=%.0f tool_spans=%d",
            agent_name,
            total_tokens,
            findings_count,
            elapsed_ms,
            thinking_ms,
            len(tool_timings),
        )
    except Exception as exc:
        logger.warning("Langfuse record_agent_trace failed (%s): %s", agent_name, exc)


def record_pipeline_end(
    *,
    pipeline_run_id: str,
    job_id: str | None,
    parent_trace_id: str,
    data_quality_score: float,
    confidence_score: float,
    total_findings: int,
    blocked_cards: int = 0,
    modified_cards: int = 0,
    total_elapsed_ms: float = 0,
    stripping_stats: dict[str, int] | None = None,
) -> None:
    """Push pipeline-level scores and summary to Langfuse.

    Creates two scores on the parent trace:
      - ``data_quality_score`` (0-100): weighted table health ratio
      - ``confidence_score`` (0-100): mean per-finding confidence

    Also updates the parent trace output with a pipeline summary.
    """
    client = _get_client()
    if client is None or not parent_trace_id:
        return

    try:
        # ── Scores ────────────────────────────────────────────────────────
        score_payloads = [
            {
                "name": "data_quality_score",
                "value": round(data_quality_score, 1),
                "trace_id": parent_trace_id,
                "comment": (
                    f"DQS={data_quality_score:.1f} — "
                    f"{total_findings} finding(s) across pipeline"
                ),
            },
            {
                "name": "confidence_score",
                "value": round(confidence_score, 1),
                "trace_id": parent_trace_id,
                "comment": f"Mean per-finding confidence: {confidence_score:.1f}%",
            },
        ]
        if blocked_cards > 0:
            score_payloads.append({
                "name": "blocked_fix_cards",
                "value": float(blocked_cards),
                "trace_id": parent_trace_id,
                "comment": f"{blocked_cards} fix card(s) blocked by pre-flight safety checks",
            })

        for sp in score_payloads:
            try:
                client.create_score(**sp)
            except Exception as score_exc:
                logger.debug("Langfuse create_score failed (%s): %s", sp["name"], score_exc)

        # Scores are the primary pipeline-level signal — no need to update
        # the parent observation output (langfuse SDK v4 has no update_observation;
        # the trace carries input from record_pipeline_start + scores here).

        client.flush()
        logger.info(
            "Langfuse pipeline scores pushed: DQS=%.1f confidence=%.1f findings=%d",
            data_quality_score,
            confidence_score,
            total_findings,
        )
    except Exception as exc:
        logger.warning("Langfuse record_pipeline_end failed: %s", exc)


def _require_langfuse_client() -> Any:
    """Force-init the Langfuse client (for the CLI status check)."""
    return _get_client()
