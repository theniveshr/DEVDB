"""Configuration manager for DevDB."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Search for .env in current working directory and project root
_ENV_PATH = Path(".env")
if _ENV_PATH.exists():
    load_dotenv(dotenv_path=_ENV_PATH)
else:
    load_dotenv()

LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "ollama")
LLM_MODEL: str = os.getenv("LLM_MODEL", "llama3.2")
OLLAMA_URL: str = os.getenv("OLLAMA_URL", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))
OLLAMA_BASE_URL: str = OLLAMA_URL
PROMPT_VERSION: str = os.getenv("PROMPT_VERSION", "v3")


def get_config() -> dict:
    """Return active configuration as a dictionary."""
    return {
        "LLM_PROVIDER": os.getenv("LLM_PROVIDER", "ollama"),
        "LLM_MODEL": os.getenv("LLM_MODEL", "llama3.2"),
        "OLLAMA_URL": os.getenv("OLLAMA_URL", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")),
        "OLLAMA_BASE_URL": os.getenv("OLLAMA_URL", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")),
        "PROMPT_VERSION": os.getenv("PROMPT_VERSION", "v3"),
    }

