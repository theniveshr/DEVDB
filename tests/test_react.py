"""Unit and integration tests for Phase 7 ReAct Self-Correction, History, and Undo."""

import os
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine, text

from devdb.actions.react import (
    is_retryable_error,
    format_beginner_friendly_error,
    build_react_correction_prompt,
    render_observation_panel,
    render_react_step,
)
from devdb.logger import log_session_event, get_session_history, render_history_table, SESSION_LOG
from devdb.actions.fake_data import TRACKER_FILE
from devdb.cli import DevDBSession
from devdb.schema import get_schema
from devdb.safety import SafetyViolationError


def test_is_retryable_error_classification():
    """Verify is_retryable_error identifies retryable vs blocked errors."""
    # Retryable cases
    can_retry, cat = is_retryable_error(Exception("no such column: user_name"))
    assert can_retry is True
    assert cat == "unknown_column"

    can_retry, cat = is_retryable_error(Exception("column 'non_existent' does not exist"))
    assert can_retry is True
    assert cat == "unknown_column"

    can_retry, cat = is_retryable_error(Exception("no such table: client_records"))
    assert can_retry is True
    assert cat == "unknown_table"

    can_retry, cat = is_retryable_error(Exception('relation "client" does not exist'))
    assert can_retry is True
    assert cat == "unknown_table"

    can_retry, cat = is_retryable_error(Exception('near "FORM": syntax error'))
    assert can_retry is True
    assert cat == "syntax_error"

    can_retry, cat = is_retryable_error(Exception('datatype mismatch in column amount'))
    assert can_retry is True
    assert cat == "type_mismatch"

    # Blocked / Non-retryable cases
    can_retry, cat = is_retryable_error(SafetyViolationError("DROP DATABASE is blocked"))
    assert can_retry is False
    assert cat == "safety_block"

    can_retry, cat = is_retryable_error(Exception("TRUNCATE is strictly prohibited"))
    assert can_retry is False
    assert cat == "safety_block"

    can_retry, cat = is_retryable_error(Exception("permission denied for table customers"))
    assert can_retry is False
    assert cat == "permission_denied"

    can_retry, cat = is_retryable_error(Exception("connection timed out"))
    assert can_retry is False
    assert cat == "connection_error"


def test_format_beginner_friendly_error():
    """Verify friendly explanations, icons, and actionable tips for beginners."""
    friendly_col = format_beginner_friendly_error(Exception("no such column: user_name"), db_type="sqlite")
    assert "Unknown Column" in friendly_col
    assert "user_name" in friendly_col
    assert "💡" in friendly_col

    friendly_tbl = format_beginner_friendly_error(Exception("no such table: clients"), db_type="sqlite")
    assert "Unknown Table" in friendly_tbl
    assert "clients" in friendly_tbl
    assert "💡" in friendly_tbl

    friendly_syntax = format_beginner_friendly_error(Exception('near "WHER": syntax error'), db_type="sqlite")
    assert "SQL Syntax Error" in friendly_syntax
    assert "💡" in friendly_syntax

    friendly_safety = format_beginner_friendly_error(SafetyViolationError("DROP DATABASE blocked"), db_type="sqlite")
    assert "Safety Guardrail" in friendly_safety


def test_build_react_correction_prompt():
    """Verify ReAct prompt generation with failed SQL, error, schema, and attempt counter."""
    schema = {
        "tables": {
            "customers": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True},
                    {"name": "name", "type": "TEXT"},
                    {"name": "email", "type": "TEXT"}
                ],
                "foreign_keys": []
            }
        }
    }
    sys_prompt, user_prompt = build_react_correction_prompt(
        original_request="find all customers named Alice",
        failed_sql="SELECT user_name FROM customers WHERE user_name = 'Alice';",
        db_error="no such column: user_name",
        schema=schema,
        db_type="sqlite",
        attempt=1,
        max_attempts=2
    )

    assert "ReAct (Reason + Act)" in sys_prompt
    assert "Step 1 (Thought)" in sys_prompt
    assert "Step 2 (Action)" in sys_prompt
    assert "find all customers named Alice" in user_prompt
    assert "SELECT user_name FROM customers" in user_prompt
    assert "no such column: user_name" in user_prompt
    assert "customers" in user_prompt
    assert "Correction Attempt: 1 of 2" in user_prompt


def test_react_self_correction_success_query():
    """Test full ReAct loop correcting unknown column 'user_name' -> 'name'."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, email TEXT);"))
        conn.execute(text("INSERT INTO customers (name, email) VALUES ('Aarav Sharma', 'aarav@example.com');"))

    session = DevDBSession()
    session.engine = engine
    session.db_type = "SQLITE"
    session.schema = get_schema(engine)
    session.prompt_version = "v4"

    # Simulate query failure with 'user_name'
    failed_sql = "SELECT user_name FROM customers;"
    db_err = Exception("no such column: user_name")

    with patch.dict(os.environ, {"LLM_PROVIDER": "mock"}):
        success = session.handle_react_self_correction(
            err=db_err,
            failed_sql=failed_sql,
            action="query",
            user_request="list all customers",
            max_attempts=2
        )

    assert success is True


def test_react_self_correction_safety_guardrail_interrupt():
    """Test that safety violations emitted during correction are halted immediately."""
    engine = create_engine("sqlite:///:memory:")
    session = DevDBSession()
    session.engine = engine
    session.db_type = "SQLITE"
    session.schema = {"tables": {}}

    failed_sql = "SELECT * FROM non_existent;"
    db_err = Exception("no such table: non_existent")

    # Mock client to return unsafe SQL
    with patch("devdb.cli.LLMClient") as mock_client_cls:
        mock_instance = MagicMock()
        mock_instance.ask_json.return_value = {
            "thought": "Let's reset database instead.",
            "action": "write",
            "sql": "DROP DATABASE devdb;",
            "explanation": "Resetting database"
        }
        mock_client_cls.return_value = mock_instance

        success = session.handle_react_self_correction(
            err=db_err,
            failed_sql=failed_sql,
            action="query",
            user_request="fix table",
            max_attempts=2
        )

        assert success is False


def test_history_logging_and_display(tmp_path):
    """Test session logging, retrieval, and table rendering."""
    log_file = tmp_path / "session_test.jsonl"
    with patch("devdb.logger.SESSION_LOG", log_file):
        log_session_event(
            request="find user Aarav",
            sql="SELECT * FROM customers WHERE name LIKE '%Aarav%';",
            ran=True,
            success=True,
            action="query",
            risk="LOW",
            prompt_version="v4",
            db_type="sqlite",
            dry_run=False
        )

        history = get_session_history(limit=5)
        assert len(history) == 1
        assert history[0]["request"] == "find user Aarav"
        assert history[0]["success"] is True

        table = render_history_table(history)
        assert table is not None
        assert table.row_count == 1


def test_undo_last_fake_data(tmp_path):
    """Test undoing fake data generation via tracker."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);"))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (1, 'Alice');"))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (2, 'Bob');"))

    tracker = tmp_path / "tracker_test.json"
    tracker.write_text(json.dumps({"customers": [1, 2]}), encoding="utf-8")

    session = DevDBSession()
    session.engine = engine
    session.db_type = "SQLITE"
    session.schema = get_schema(engine)
    session.undo_stack.append({"action": "fake_data", "table": "customers", "rows": 2})

    with patch("devdb.cli.TRACKER_FILE", tracker), \
         patch("devdb.actions.fake_data.TRACKER_FILE", tracker), \
         patch("devdb.cli.console.input", return_value="y"):

        undone = session.handle_undo_last()
        assert undone is True

        with engine.connect() as conn:
            remaining = conn.execute(text("SELECT count(*) FROM customers;")).scalar()
            assert remaining == 0


def test_undo_last_single_insert():
    """Test undoing a single row INSERT statement."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT);"))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (1, 'Original');"))
        conn.execute(text("INSERT INTO customers (id, name) VALUES (2, 'Latest Insert');"))

    session = DevDBSession()
    session.engine = engine
    session.db_type = "SQLITE"
    session.schema = get_schema(engine)
    session.undo_stack.append({"action": "write", "sql": "INSERT INTO customers (id, name) VALUES (2, 'Latest Insert');"})

    with patch("devdb.cli.console.input", return_value="y"):
        undone = session.handle_undo_last()
        assert undone is True

        with engine.connect() as conn:
            remaining = conn.execute(text("SELECT count(*) FROM customers;")).scalar()
            assert remaining == 1
            last_name = conn.execute(text("SELECT name FROM customers WHERE id = 1;")).scalar()
            assert last_name == "Original"
