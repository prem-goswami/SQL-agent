import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# Assemble DSNs from parts if provided (Railway), else fall back to full DSN vars (local)
_host = os.getenv("DB_HOST")
if _host:
    _port = os.getenv("DB_PORT", "5432")
    _name = os.getenv("DB_NAME", "railway")
    AGENT_RO_DSN = f"postgresql://{os.getenv('RO_USER')}:{os.getenv('RO_PASS')}@{_host}:{_port}/{_name}"
    AGENT_LOG_DSN = f"postgresql://{os.getenv('LOG_USER')}:{os.getenv('LOG_PASS')}@{_host}:{_port}/{_name}"
else:
    AGENT_RO_DSN = os.getenv("AGENT_RO_DSN")
    AGENT_LOG_DSN = os.getenv("AGENT_LOG_DSN")

LLM_MODEL = "gpt-4o-mini"
MAX_RETRIES = 2
STATEMENT_TIMEOUT = "10s"