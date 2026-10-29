"""Configuration and dev-only safety validation module."""

import os
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv

# Base paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"

# Load environment variables from .env
load_dotenv(PROJECT_ROOT / ".env")

# Allowed local hostnames for safety validation
ALLOWED_LOCAL_HOSTS = {
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
}


class SafetyViolationError(Exception):
    """Raised when an operation violates dev-only safety constraints."""
    pass


def validate_dev_database_url(database_url: str) -> None:
    """Validate that the target database is strictly a local/dev instance.
    
    Permitted:
      - SQLite local files: sqlite:///path/to/db.sqlite or sqlite://
      - Local network hosts: localhost, 127.0.0.1, ::1
      
    Blocked:
      - Any remote host, public IP, cloud RDS, production domain.
    """
    if not database_url:
        raise SafetyViolationError("Database URL cannot be empty.")

    clean_url = database_url.strip()

    # SQLite is inherently local/file-based
    if clean_url.startswith("sqlite"):
        return

    # Parse connection URI
    try:
        parsed = urlparse(clean_url)
    except Exception as exc:
        raise SafetyViolationError(f"Malformed database URL: {exc}")

    hostname = (parsed.hostname or "").lower()

    if not hostname:
        # Some URLs might have no explicit hostname (e.g. sqlite or local sockets)
        if parsed.scheme in ("postgresql", "postgres"):
            # Default postgres socket or local connection
            return
        raise SafetyViolationError(
            f"Could not determine host in '{clean_url}'. "
            "For safety, only explicit localhost connections are permitted."
        )

    if hostname not in ALLOWED_LOCAL_HOSTS:
        raise SafetyViolationError(
            f"SAFETY VIOLATION: Remote database host detected: '{hostname}'.\n"
            "DevDB Assistant strictly blocks connections to non-localhost hosts to prevent "
            "accidental execution on staging or production databases."
        )


def get_database_url() -> str:
    """Retrieve the database URL from environment or default to local MongoDB."""
    url = os.getenv("DATABASE_URL", "mongodb://localhost:27017/devdb")
    validate_dev_database_url(url)
    return url


def get_llm_provider() -> str:
    """Get the active LLM provider name (default: 'ollama')."""
    return os.getenv("LLM_PROVIDER", "ollama").lower()


def get_llm_model() -> str:
    """Get preferred model name, or sensible default based on provider."""
    custom_model = os.getenv("LLM_MODEL")
    if custom_model:
        return custom_model

    provider = get_llm_provider()
    if provider == "ollama":
        return "llama3.2"
    if provider == "openai":
        return "gpt-4o-mini"
    if provider == "gemini":
        return "gemini-1.5-flash"
    return "mock-model"
