"""Tests for dev-only safety validation and query classification."""

import pytest
from src.config import validate_dev_database_url, SafetyViolationError
from src.sql_generator import SQLGenerator
from src.db import DatabaseManager


def test_allowed_local_database_urls():
    """Verify that only local dev databases are permitted."""
    valid_urls = [
        "sqlite:///devdb.sqlite",
        "sqlite://",
        "sqlite:///:memory:",
        "postgresql://user:pass@localhost:5432/testdb",
        "postgresql://user:pass@127.0.0.1:5432/testdb",
        "postgresql://user:pass@0.0.0.0:5432/testdb",
    ]
    for url in valid_urls:
        # Should not raise exception
        validate_dev_database_url(url)


def test_blocked_remote_database_urls():
    """Verify that remote/production databases are strictly blocked."""
    invalid_urls = [
        "postgresql://admin:secret@aws-rds-prod.us-east-1.amazonaws.com:5432/proddb",
        "postgresql://admin:secret@db.company.internal:5432/analytics",
        "postgresql://user:pass@192.168.1.50:5432/proddb",
        "postgresql://user:pass@10.0.0.5:5432/production",
    ]
    for url in invalid_urls:
        with pytest.raises(SafetyViolationError):
            validate_dev_database_url(url)


def test_sql_safety_classification():
    """Test SQL query classification into READ, WRITE, and DESTRUCTIVE."""
    db = DatabaseManager("sqlite://")
    gen = SQLGenerator(db)

    # READ statements
    assert gen.classify_sql("SELECT * FROM users")[0] == "READ"
    assert gen.classify_sql("EXPLAIN SELECT id FROM orders")[0] == "READ"
    assert gen.classify_sql("WITH cte AS (SELECT id FROM users) SELECT * FROM cte")[0] == "READ"

    # WRITE statements
    assert gen.classify_sql("INSERT INTO users (name) VALUES ('Bob')")[0] == "WRITE"
    assert gen.classify_sql("UPDATE orders SET status = 'shipped' WHERE id = 1")[0] == "WRITE"

    # DESTRUCTIVE statements
    assert gen.classify_sql("DELETE FROM users WHERE id = 5")[0] == "DESTRUCTIVE"
    assert gen.classify_sql("DROP TABLE orders")[0] == "DESTRUCTIVE"
    assert gen.classify_sql("TRUNCATE TABLE audit_logs")[0] == "DESTRUCTIVE"
    assert gen.classify_sql("ALTER TABLE users DROP COLUMN phone")[0] == "DESTRUCTIVE"


def test_ollama_provider_local_only():
    """Verify Ollama provider attempts local connection without requiring OpenAI/Gemini API keys."""
    from src.llm import LLMClient
    client = LLMClient(provider="ollama", model="llama3.2")
    # Should attempt to reach localhost:11434 and raise ConnectionError if server is offline
    # but NEVER complain about missing OPENAI_API_KEY or GEMINI_API_KEY!
    try:
        client.generate("test prompt")
    except ConnectionError as e:
        assert "localhost:11434" in str(e)
    except Exception as e:
        # If Ollama happens to be running, it shouldn't be an API key error
        assert "OPENAI_API_KEY" not in str(e)
        assert "GEMINI_API_KEY" not in str(e)
