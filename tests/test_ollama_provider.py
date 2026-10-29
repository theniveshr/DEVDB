"""Unit tests for OllamaProvider, small-model JSON safety, token warnings, and CLI model commands."""

import os
import json
import pytest
import warnings
from unittest.mock import patch, MagicMock
import requests

from devdb.llm import (
    OllamaProvider,
    LLMClient,
    strip_markdown_code_fences,
    extract_first_json_block,
)
from devdb.cli import DevDBSession


# =============================================================================
# 1. OllamaProvider HTTP & Parameter Tests
# =============================================================================

def test_ollama_provider_defaults():
    """Verify default parameters: model from LLM_MODEL, URL from OLLAMA_URL, num_ctx=4096, temp=0.1, timeout=120."""
    with patch.dict(os.environ, {"LLM_MODEL": "llama3.2", "OLLAMA_URL": "http://localhost:11434"}):
        provider = OllamaProvider()
        assert provider.model == "llama3.2"
        assert provider.url == "http://localhost:11434"
        assert provider.num_ctx == 4096
        assert provider.temperature == 0.1
        assert provider.timeout == 120


@patch("requests.post")
def test_ollama_provider_call_payload(mock_post):
    """Verify HTTP POST payload matches exact requirements: stream=False, 120s timeout, temp=0.1, num_ctx=4096."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "message": {"content": "SELECT * FROM customers;"}
    }
    mock_post.return_value = mock_resp

    provider = OllamaProvider(model="llama3.2", url="http://localhost:11434")
    res = provider.ask(system_prompt="You are a SQL bot.", user_prompt="Show all customers")

    assert res == "SELECT * FROM customers;"
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "http://localhost:11434/api/chat"
    assert kwargs["timeout"] == 120

    payload = kwargs["json"]
    assert payload["model"] == "llama3.2"
    assert payload["stream"] is False
    assert payload["options"]["temperature"] == 0.1
    assert payload["options"]["num_ctx"] == 4096
    assert len(payload["messages"]) == 2
    assert payload["messages"][0] == {"role": "system", "content": "You are a SQL bot."}
    assert payload["messages"][1] == {"role": "user", "content": "Show all customers"}
    assert "format" not in payload


@patch("requests.post")
def test_ollama_provider_json_format_flag(mock_post):
    """Verify format='json' is included in payload when json_mode=True."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "message": {"content": json.dumps({"action": "query", "sql": "SELECT 1;"})}
    }
    mock_post.return_value = mock_resp

    provider = OllamaProvider(model="llama3.2")
    raw = provider.ask_json_raw(system_prompt="System", user_prompt="User")
    assert "SELECT 1" in raw

    payload = mock_post.call_args[1]["json"]
    assert payload["format"] == "json"


# =============================================================================
# 2. Friendly Error Tests (Ollama down, model missing, timeout)
# =============================================================================

@patch("requests.post")
def test_friendly_error_ollama_not_running(mock_post):
    """When Ollama is not running (ConnectionError), message MUST contain 'start Ollama'."""
    mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused on 11434")

    provider = OllamaProvider(url="http://localhost:11434")
    with pytest.raises(ConnectionError) as exc_info:
        provider.call_api(system_prompt=None, user_prompt="hello")

    err_msg = str(exc_info.value)
    assert "start Ollama" in err_msg
    assert "http://localhost:11434" in err_msg


@patch("requests.post")
def test_friendly_error_model_missing_404(mock_post):
    """When model is missing (HTTP 404), message MUST tell user to run: 'ollama pull llama3.2'."""
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = '{"error": "model \'llama3.2\' not found"}'
    mock_post.return_value = mock_resp

    provider = OllamaProvider(model="llama3.2")
    with pytest.raises(RuntimeError) as exc_info:
        provider.call_api(system_prompt=None, user_prompt="hello")

    err_msg = str(exc_info.value)
    assert "run: ollama pull llama3.2" in err_msg


@patch("requests.post")
def test_friendly_error_timeout(mock_post):
    """When request times out (Timeout error), message MUST indicate 120s timeout."""
    mock_post.side_effect = requests.exceptions.Timeout("Read timed out after 120 seconds")

    provider = OllamaProvider(model="llama3.2", timeout=120)
    with pytest.raises(TimeoutError) as exc_info:
        provider.call_api(system_prompt=None, user_prompt="hello")

    err_msg = str(exc_info.value)
    assert "timed out after 120s" in err_msg
    assert "llama3.2" in err_msg


# =============================================================================
# 3. Small-Model JSON Safety Tests
# =============================================================================

def test_strip_markdown_code_fences():
    """Test removing markdown code fences."""
    raw = "```json\n{\"action\": \"query\", \"sql\": \"SELECT 1;\"}\n```"
    cleaned = strip_markdown_code_fences(raw)
    assert cleaned.startswith("{") and cleaned.endswith("}")

    raw2 = "```\n{\"table\": \"customers\"}\n```"
    assert strip_markdown_code_fences(raw2) == '{"table": "customers"}'


def test_extract_first_json_block_conversational():
    """Small models frequently include conversational filler around JSON."""
    messy = (
        "Sure, here is the database query JSON you asked for:\n"
        "{\n"
        '  "action": "query",\n'
        '  "sql": "SELECT * FROM orders WHERE total > 100;",\n'
        '  "explanation": "Find orders over $100"\n'
        "}\n"
        "Let me know if you need any adjustments!"
    )
    block = extract_first_json_block(messy)
    parsed = json.loads(block)
    assert parsed["action"] == "query"
    assert "orders" in parsed["sql"]


def test_extract_first_json_block_nested_braces_in_strings():
    """Verify braces inside SQL string literals do not break balanced-brace parsing."""
    raw = '{"action": "query", "sql": "SELECT \'{nested: 1}\' AS val", "status": "ok"}'
    block = extract_first_json_block(raw)
    parsed = json.loads(block)
    assert parsed["sql"] == "SELECT '{nested: 1}' AS val"


@patch("requests.post")
def test_ask_json_retry_on_malformed_json_and_counter(mock_post):
    """
    Test retry logic in ask_json():
    1. First response has malformed JSON.
    2. Retries once with 'Return ONLY valid JSON, no extra text.'
    3. Second response succeeds.
    4. Retry counter increments.
    """
    LLMClient.reset_total_retries()

    # First response is broken JSON, second is valid
    resp1 = MagicMock()
    resp1.ok = True
    resp1.status_code = 200
    resp1.json.return_value = {
        "message": {"content": "Here is the query: {action: query, sql: broken... NOT JSON"}
    }

    resp2 = MagicMock()
    resp2.ok = True
    resp2.status_code = 200
    resp2.json.return_value = {
        "message": {"content": '{"action": "query", "sql": "SELECT 1;", "explanation": "Valid after retry"}'}
    }

    mock_post.side_effect = [resp1, resp2]

    client = LLMClient(provider="ollama", model="llama3.2")
    assert client.json_retry_count == 0

    result = client.ask_json(system_prompt="sys", user_prompt="Show table")
    assert result["action"] == "query"
    assert result["sql"] == "SELECT 1;"

    # Verify retry was counted
    assert client.json_retry_count == 1
    assert LLMClient.total_json_retries == 1

    # Verify retry prompt included the required instruction
    assert mock_post.call_count == 2
    retry_call_payload = mock_post.call_args_list[1][1]["json"]
    user_msg = retry_call_payload["messages"][-1]["content"]
    assert "Return ONLY valid JSON, no extra text." in user_msg


# =============================================================================
# 4. Context Length (num_ctx 4096) and Token Warning Tests
# =============================================================================

@patch("requests.post")
def test_token_warning_triggered_over_3000(mock_post):
    """Warn if prompt + schema is longer than about 3,000 tokens (> ~12,000 characters)."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"message": {"content": "ok"}}
    mock_post.return_value = mock_resp

    provider = OllamaProvider()
    long_schema = "CREATE TABLE large (col TEXT);\n" * 500  # ~15,000 chars => ~3,750 tokens

    with pytest.warns(UserWarning, match="Prompt \\+ schema is longer than about 3,000 tokens"):
        provider.call_api(system_prompt=long_schema, user_prompt="query")


@patch("requests.post")
def test_token_warning_not_triggered_under_3000(mock_post):
    """No warning when prompt is well within limits."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"message": {"content": "ok"}}
    mock_post.return_value = mock_resp

    provider = OllamaProvider()
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        provider.call_api(system_prompt="Short schema", user_prompt="query")
        token_warnings = [w for w in recorded if "3,000 tokens" in str(w.message)]
        assert len(token_warnings) == 0


# =============================================================================
# 5. CLI Overrides & English Model Command Tests
# =============================================================================

def test_which_model_am_i_using_english_command():
    """Test 'which model am I using?' English command in DevDBSession."""
    session = DevDBSession()
    session.llm_provider = "ollama"
    session.llm_model = "llama3.2"

    with patch.object(session, "handle_model_info") as mock_info:
        session.handle_natural_language_request("which model am I using?")
        mock_info.assert_called_once()


def test_which_model_command_variations():
    """Verify various conversational phrasings trigger handle_model_info."""
    session = DevDBSession()
    variations = [
        "what model am I using?",
        "which model is this",
        "which llm am i using",
        "model info",
        "show model",
    ]
    for phrasing in variations:
        with patch.object(session, "handle_model_info") as mock_info:
            session.handle_natural_language_request(phrasing)
            mock_info.assert_called_once(), f"Failed for phrasing: {phrasing}"


def test_cli_provider_and_model_overrides():
    """Verify DevDBSession uses overridden provider and model."""
    session = DevDBSession()
    session.llm_provider = "gemini"
    session.llm_model = "gemini-1.5-flash"

    client = session.get_llm_client()
    assert client.provider == "gemini"
    assert client.model == "gemini-1.5-flash"
