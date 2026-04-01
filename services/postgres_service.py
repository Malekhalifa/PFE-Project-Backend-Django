import logging
from typing import Dict, Any, List
from agents.validation_team import orchestrator_workflow

logger = logging.getLogger(__name__)

def analyze_postgres_database(connection_string: str, custom_rules: List[Dict[str, Any]] = None, job_id: str = None) -> Dict[str, Any]:
    """
    Service layer bridge between the Celery Tasks and the Multi-Agent orchestrator.
    Takes a list of standard custom rule definitions and formats them as a readable text string for the LLM agents.
    
    Args:
        connection_string: The PostgreSQL dial URL
        custom_rules: Expected format: [{"description": "quota_free must be >= 0", "category": "BUDGET"}, ...]
        job_id: Optional job ID — passed to the orchestrator so it can emit per-agent progress statuses.
    """
    try:
        # 1. Build the domain rules string for the LLM context
        domain_rules_text = "No custom domain rules provided. Enforce physical schema constraints only."
        if custom_rules and len(custom_rules) > 0:
            domain_rules_text = "=== LOGICAL RULES TO ENFORCE ===\n"
            
            # Group by category if possible, to make it easier for the LLM to read
            categories = {}
            for rule in custom_rules:
                cat = rule.get("category", "GENERAL").upper()
                desc = rule.get("description", str(rule))
                if cat not in categories:
                    categories[cat] = []
                categories[cat].append(desc)
                
            for cat, rules_list in categories.items():
                domain_rules_text += f"\n--- {cat} ---\n"
                for r in rules_list:
                    domain_rules_text += f"- {r}\n"

        logger.info("Triggering Agentic Workflow with connection string...")
        
        # 2. Block and run the LLMs
        agent_reports = orchestrator_workflow(connection_string, domain_rules_text, job_id=job_id)
        
        # 3. Map the reports into the standardized dictionary the frontend expects
        return {
            "status": "success",
            "schema_analysis": {
                "report": agent_reports.get("schema_issues", "No report generated.")
            },
            "cross_row_analysis": {
                "report": agent_reports.get("cross_row_issues", "No report generated.")
            },
            "row_level_analysis": {
                "report": agent_reports.get("row_issues", "No report generated.")
            },
            "cleaning_plan": agent_reports.get("cleaning_plan", {}),
        }
        
    except Exception as e:
        logger.error(f"Error executing Postgres Agentic Validation: {str(e)}")
        raise e
