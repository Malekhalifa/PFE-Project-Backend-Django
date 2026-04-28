import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QL_MODE", "playground")

from agno.os import AgentOS

from agents.team import qualitylab_team

app = AgentOS(teams=[qualitylab_team]).get_app()
