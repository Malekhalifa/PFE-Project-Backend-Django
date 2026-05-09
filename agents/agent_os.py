import os
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

# Load backend/.env so GEMINI_API_KEY etc. are populated. Django/Celery do this
# automatically via config.settings; this standalone entrypoint must not.
try:
    from dotenv import load_dotenv
    load_dotenv(_BACKEND_DIR / ".env")
except ImportError:
    pass

os.environ.setdefault("QL_MODE", "playground")

from agno.os import AgentOS

from agents.team import qualitylab_team

app = AgentOS(teams=[qualitylab_team]).get_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="localhost", port=7777, reload=False)
