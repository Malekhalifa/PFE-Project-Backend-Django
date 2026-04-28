import os
import sys
import django
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    force=True
)

# Setup Django environment so we can use backend services/models
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from agents.validation import orchestrator_workflow
from services.postgres_connection import test_postgres_connection

def run_test():
    # Replace the connection string with the one you want to test
    test_conn_string = "postgresql://ebs-app:malekkh@localhost:5432/easybulk"
    
    print("Testing connection...")
    try:
        test_postgres_connection(test_conn_string, timeout_s=3)
        print("✅ Connection successful.")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return

    print("\n🚀 Starting orchestrator_workflow...")
    try:
        # job_id is left as None so it skips updating the UI/DB statuses
        results = orchestrator_workflow(
            connection_string=test_conn_string,
            domain_rules="",
            job_id=None,
            llm_model="gemini-2.5-flash"
        )
        print(f"Overall Quality Score: {results.get('data_quality_score')}")
        print(f"Confidence Score: {results.get('confidence_score')}")
        print(f"Report saved to: {results.get('report_file_path')}")
        
    except Exception as e:
        print(f"\n❌ Pipeline crashed:")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_test()
