import logging
from typing import Dict, Any, List
from agents.validation_team import orchestrator_workflow

logger = logging.getLogger(__name__)
_NO_REPORT_GENERATED = "No report generated."

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
        
        # 3. Return the standardised dict — keys now come directly from the new agent output
        return {
            "status": "success",
            "pipeline_run_id": agent_reports.get("pipeline_run_id"),
            "database_id": agent_reports.get("database_id"),
            "schema_analysis":    agent_reports.get("schema_analysis",    {"report": _NO_REPORT_GENERATED}),
            "column_validator_analysis": agent_reports.get("column_validator_analysis", {
                "agent": "ColumnValidatorAgent",
                "database_id": "unknown",
                "run_timestamp": "unknown",
                "report": _NO_REPORT_GENERATED,
                "findings": [],
                "summary": {
                    "total_items_analyzed": 0,
                    "items_passed": 0,
                    "items_flagged": 0,
                    "critical_issues": 0,
                    "high_issues": 0,
                    "medium_issues": 0,
                    "low_issues": 0,
                    "overall_quality_score": 100,
                    "overall_confidence": 0.0,
                    "flagged_tables": [],
                },
                "metrics": {
                    "total_columns_analyzed": 0,
                    "duplicate_columns_found": 0,
                    "constant_columns_found": 0,
                    "high_null_ratio_columns": 0,
                    "low_cardinality_anomalies": 0,
                    "high_cardinality_anomalies": 0,
                    "semantic_mismatches": 0,
                    "domain_violations": 0,
                    "distribution_anomalies": 0,
                    "avg_column_quality_score": 100,
                    "avg_column_confidence": 0.0,
                },
                "prior_reports": [],
            }),
            "row_level_analysis": agent_reports.get("row_level_analysis", {"report": _NO_REPORT_GENERATED}),
            "cross_row_analysis": agent_reports.get("cross_row_analysis", {"report": _NO_REPORT_GENERATED}),
            "fixes": agent_reports.get("fixes", {"fixes": []}),
            "agent_reports": agent_reports.get("agent_reports", {}),
            "final_report": agent_reports.get("final_report", {}),
            "confidence_score": agent_reports.get("confidence_score"),
            "data_quality_score": agent_reports.get("data_quality_score"),
            "report_file_path": agent_reports.get("report_file_path"),
            "report_file_name": agent_reports.get("report_file_name"),
            "report_timestamp": agent_reports.get("report_timestamp"),
            "report_database_id": agent_reports.get("report_database_id"),
        }
        
    except Exception as e:
        logger.error(f"Error executing Postgres Agentic Validation: {str(e)}")
        raise e
