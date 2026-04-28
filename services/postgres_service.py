import logging
from typing import Dict, Any, List
from agents.validation import orchestrator_workflow

logger = logging.getLogger(__name__)

def analyze_postgres_database(
    connection_string: str,
    custom_rules: List[Dict[str, Any]] = None,
    job_id: str = None,
    llm_model: str | None = None,
    historical_signals: str | None = None,
) -> Dict[str, Any]:
    """
    Service layer bridge between the Celery Tasks and the Multi-Agent orchestrator.
    Passes the current API/domain-rules payload through to the workflow adapter.
    
    Args:
        connection_string: The PostgreSQL dial URL
        custom_rules: Expected format: [{"description": "quota_free must be >= 0", "category": "BUDGET"}, ...]
        job_id: Optional job ID — passed to the orchestrator.
        llm_model: Optional Gemini model id selected for this job.
    """
    try:
        logger.info("Triggering Minimal Agentic Workflow...")
        
        agent_reports = orchestrator_workflow(
            connection_string,
            custom_rules or [],
            job_id=job_id,
            llm_model=llm_model,
            historical_signals=historical_signals,
        )
        
        # Ensure 'status' goes to the top level of the returned dictionary
        agent_reports["status"] = "success"
        return agent_reports
        
    except Exception as e:
        logger.error(f"Error executing Postgres Agentic Validation: {str(e)}")
        raise e
