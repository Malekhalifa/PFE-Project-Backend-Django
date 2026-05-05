import json
import logging
from typing import Dict, Any, List
from agents.validation import orchestrator_workflow

logger = logging.getLogger(__name__)


def _fetch_learned_patterns(user_id: str | None) -> list[dict]:
    """Return serialized LearningPattern rows for the given user, or [] if unavailable."""
    if not user_id:
        return []
    try:
        from api.models import LearningPattern
        rows = (
            LearningPattern.objects
            .filter(user_id=user_id)
            .values('rule_family', 'entity', 'decision', 'frequency')
        )
        return list(rows)
    except Exception as exc:
        logger.warning("Could not fetch learning patterns for user %s: %s", user_id, exc)
        return []


def analyze_postgres_database(
    connection_string: str,
    custom_rules: List[Dict[str, Any]] = None,
    job_id: str = None,
    llm_model: str | None = None,
    historical_signals: str | None = None,
    user_id: str | None = None,
) -> Dict[str, Any]:
    """
    Service layer bridge between the Celery Tasks and the Multi-Agent orchestrator.
    Passes the current API/domain-rules payload through to the workflow adapter.

    Args:
        connection_string: The PostgreSQL dial URL
        custom_rules: Expected format: [{"description": "quota_free must be >= 0", "category": "BUDGET"}, ...]
        job_id: Optional job ID — passed to the orchestrator.
        llm_model: Optional Gemini model id selected for this job.
        user_id: Optional user ID — used to fetch learning patterns for this user.
    """
    learned_patterns = _fetch_learned_patterns(user_id)

    try:
        logger.info("Triggering Minimal Agentic Workflow...")

        agent_reports = orchestrator_workflow(
            connection_string,
            custom_rules or [],
            job_id=job_id,
            llm_model=llm_model,
            historical_signals=historical_signals,
            learned_patterns=learned_patterns,
        )
        
        # Ensure 'status' goes to the top level of the returned dictionary
        agent_reports["status"] = "success"
        return agent_reports
        
    except Exception as e:
        logger.error(f"Error executing Postgres Agentic Validation: {str(e)}")
        raise e
