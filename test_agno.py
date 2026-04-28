import os
from pydantic import BaseModel
from agno.agent import Agent
from agno.models.google import Gemini

class TestOutput(BaseModel):
    name: str

# Let's use Gemini api key for GOOGLE_API_KEY
os.environ["GOOGLE_API_KEY"] = os.environ.get("GEMINI_API_KEY", "")

agent = Agent(model=Gemini(id="gemini-2.5-flash"), output_schema=TestOutput)
response = agent.run("Return {'name': 'test'}")
print("content type:", type(response.content))
if type(response.content) is not str:
    print("content name:", response.content.name)
