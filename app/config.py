import os
from dotenv import load_dotenv

load_dotenv()

# Environment Credentials & DSNs
AGENT_RO_DSN = os.getenv("AGENT_RO_DSN")
AGENT_LOG_DSN = os.getenv("AGENT_LOG_DSN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Agent & DB Constants
LLM_MODEL = "gpt-4o-mini"
MAX_RETRIES = 2
STATEMENT_TIMEOUT = "10s"