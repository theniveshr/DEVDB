"""Tests for prompt loader, prompt library (V1, V2, V3), and LLM client."""

import json
import pytest
from sqlalchemy import create_engine

from devdb.schema import get_schema
from devdb.prompt_loader import load_prompt
from devdb.llm import LLMClient


def test_prompts_loading_and_print():
    """Test and print final rendered V1, V2, and V3 prompts for sample DB."""
    engine = create_engine("sqlite:///dev.db")
    schema = get_schema(engine)

    # 1. Load V1
    p_v1 = load_prompt("v1", db_type="sqlite", schema=schema)
    assert "Convert this request to SQL" in p_v1
    assert "action" in p_v1

    # 2. Load V2
    p_v2 = load_prompt("v2", db_type="sqlite", schema=schema)
    assert "DEVELOPMENT environment" in p_v2
    assert "Database Dialect: sqlite" in p_v2
    assert "Table: customers" in p_v2
    assert "Table: orders" in p_v2
    assert "Table: products" in p_v2
    assert "Table: order_items" in p_v2

    # 3. Load V3
    p_v3 = load_prompt("v3", db_type="sqlite", schema=schema)
    assert "Think Step by Step" in p_v3
    assert "reasoning" in p_v3
    assert "Few-Shot Examples:" in p_v3
    assert "orders_new" in p_v3  # SQLite FK table-recreate pattern present in few-shot
    assert "Table: customers" in p_v3

    # Print prompts to console for visibility
    print("\n" + "=" * 80)
    print("FINAL RENDERED V1 PROMPT (BASELINE):")
    print("=" * 80)
    print(p_v1)

    print("\n" + "=" * 80)
    print("FINAL RENDERED V2 PROMPT (PERSONA + SCHEMA):")
    print("=" * 80)
    print(p_v2)

    print("\n" + "=" * 80)
    print("FINAL RENDERED V3 PROMPT (COT + FEW-SHOT + SCHEMA):")
    print("=" * 80)
    print(p_v3[:1200] + "\n... [truncated few-shot examples] ...\n" + p_v3[-300:])
    print("=" * 80)


def test_fewshot_files_content():
    """Verify sqlite.json and postgres.json have at least 8 example pairs each."""
    from pathlib import Path
    fewshot_dir = Path("prompts/fewshot")

    with open(fewshot_dir / "sqlite.json", "r", encoding="utf-8") as f:
        sqlite_examples = json.load(f)
    assert len(sqlite_examples) >= 8, f"Expected at least 8 examples, got {len(sqlite_examples)}"

    with open(fewshot_dir / "postgres.json", "r", encoding="utf-8") as f:
        postgres_examples = json.load(f)
    assert len(postgres_examples) >= 8, f"Expected at least 8 examples, got {len(postgres_examples)}"

    # Check required categories in sqlite
    actions = {ex["response"]["action"] for ex in sqlite_examples}
    assert "query" in actions
    assert "write" in actions
    assert "relationship" in actions
    assert "create_user" in actions
    assert "fake_data" in actions

    # Check sqlite specific notes
    user_ex = next(ex for ex in sqlite_examples if ex["response"]["action"] == "create_user")
    assert "not support" in user_ex["response"]["explanation"].lower()

    fk_ex = next(ex for ex in sqlite_examples if ex["response"]["action"] == "relationship")
    assert "recreate" in fk_ex["response"]["explanation"].lower() or "orders_new" in fk_ex["response"]["sql"]


def test_llm_client_mock_and_ask_json():
    """Test LLMClient mock mode, markdown stripping, and ask_json parsing."""
    client = LLMClient(provider="mock")

    # Test ask()
    response_text = client.ask("system prompt", "user query")
    assert "action" in response_text

    # Test ask_json()
    data = client.ask_json("system prompt", "user query")
    assert isinstance(data, dict)
    assert data.get("action") == "query"
    assert "sql" in data

    # Test markdown fence cleaning
    fenced_json = "```json\n{\"action\": \"write\", \"sql\": \"INSERT...\", \"explanation\": \"done\"}\n```"
    cleaned = client._clean_json_text(fenced_json)
    parsed = json.loads(cleaned)
    assert parsed["action"] == "write"


def test_llm_rate_limit_detection():
    """Test rate limit error classification."""
    client = LLMClient(provider="mock")
    assert client._is_rate_limit_error(Exception("429 Too Many Requests")) is True
    assert client._is_rate_limit_error(Exception("Resource_Exhausted quota exceeded")) is True
    assert client._is_rate_limit_error(Exception("SyntaxError: invalid syntax")) is False
