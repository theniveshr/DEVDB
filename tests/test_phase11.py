"""Phase 11 comprehensive automated tests for DevDB Assistant.

Covers:
1. Connection profiles (profiles.json saving, password-less storage, password resolution)
2. Atlas Safety Guardrail (production name refusal, non-dev profile refusal, READ-ONLY write blocking)
3. Router intent classification on 20 diverse sentences (with mocked LLM where necessary)
4. Capability matrix verification across all 4 database types
5. Data I/O (export to CSV/JSON, preview import with row counts and types)
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from devdb.connections import (
    ConnectionManager,
    ConnectionProfile,
    sanitize_url_for_storage,
)
from devdb.safety import (
    SessionMode,
    check_sql,
    check_mongo_op,
    is_production_named,
    validate_remote_atlas_connection,
)
from devdb.capabilities import (
    CAPABILITY_MATRIX,
    is_capability_supported,
    render_capability_matrix,
)
from devdb.router import classify_intent
from devdb.actions.data_io import (
    export_data,
    preview_import_data,
    import_data,
)


# ==============================================================================
# 1. Connection Profiles & Password-less Storage Tests
# ==============================================================================

def test_connection_profile_never_stores_passwords(tmp_path):
    """Verify that passwords are stripped and NEVER written to profiles.json on disk."""
    profiles_file = tmp_path / "profiles.json"
    manager = ConnectionManager(profiles_file=profiles_file)

    # URL with sensitive password
    secret_url = "postgresql://dev_admin:SuperSecretP@ss123!@localhost:5432/devdb"
    prof = manager.save_profile(
        name="local_pg",
        url=secret_url,
        conn_type="postgres",
        environment="dev",
        default_mode="READ-WRITE"
    )

    # Check the in-memory object
    assert "SuperSecretP@ss123!" not in prof.url
    assert prof.url == "postgresql://dev_admin@localhost:5432/devdb"

    # Read the raw file directly from disk
    raw_disk_content = profiles_file.read_text(encoding="utf-8")
    assert "SuperSecretP@ss123!" not in raw_disk_content
    assert "dev_admin@" in raw_disk_content

    # Reload fresh manager from disk
    fresh_mgr = ConnectionManager(profiles_file=profiles_file)
    loaded = fresh_mgr.get_profile("local_pg")
    assert loaded is not None
    assert "SuperSecretP@ss123!" not in loaded.url


def test_sanitize_url_for_storage_handles_various_schemes():
    """Verify password stripping across SQLite, Postgres, Mongo local, and Mongo Atlas."""
    clean_sqlite, _ = sanitize_url_for_storage("sqlite:///dev.db")
    assert clean_sqlite == "sqlite:///dev.db"

    pg_url = "postgresql://bob:mypassword@localhost:5432/testdb"
    clean_pg, pwd_pg = sanitize_url_for_storage(pg_url)
    assert clean_pg == "postgresql://bob@localhost:5432/testdb"
    assert pwd_pg == "mypassword"

    mongo_local = "mongodb://admin:secret@localhost:27017/devdb"
    clean_m, pwd_m = sanitize_url_for_storage(mongo_local)
    assert clean_m == "mongodb://admin@localhost:27017/devdb"
    assert pwd_m == "secret"

    atlas_url = "mongodb+srv://user1:cloudSecret99@cluster0.abcde.mongodb.net/sandbox_db?retryWrites=true"
    sanitized_atlas, pwd_atlas = sanitize_url_for_storage(atlas_url)
    assert "cloudSecret99" not in sanitized_atlas
    assert pwd_atlas == "cloudSecret99"
    assert "user1@" in sanitized_atlas
    assert "cluster0.abcde.mongodb.net" in sanitized_atlas


# ==============================================================================
# 2. Atlas Safety Guardrail Tests
# ==============================================================================

def test_atlas_guard_refuses_production_names():
    """Atlas connection must be refused if cluster or db contains prod, production, live."""
    # Production cluster names
    assert is_production_named("cluster0-prod.mongodb.net") is True
    assert is_production_named("cluster0.production.mongodb.net") is True
    assert is_production_named("live-db-cluster.mongodb.net") is True
    assert is_production_named("prod_replica") is True

    # Safe dev/sandbox names
    assert is_production_named("cluster0.sandbox.mongodb.net") is False
    assert is_production_named("dev-cluster.mongodb.net") is False
    assert is_production_named("devdb") is False

    # Check validate_remote_atlas_connection refusal
    prod_url = "mongodb+srv://admin@cluster0-prod.mongodb.net/analytics"
    ok, reason, _ = validate_remote_atlas_connection(prod_url, environment="dev")
    assert ok is False
    assert "production keyword" in reason.lower()

    # Prod database name refusal
    prod_db_url = "mongodb+srv://admin@cluster0.abcde.mongodb.net/live_orders"
    ok2, reason2, _ = validate_remote_atlas_connection(prod_db_url, environment="dev")
    assert ok2 is False
    assert "production keyword" in reason2.lower()


def test_atlas_guard_refuses_non_dev_profile():
    """Atlas connection must be refused if environment is not dev or sandbox."""
    atlas_url = "mongodb+srv://user@cluster0.abcde.mongodb.net/devdb"
    ok, reason, _ = validate_remote_atlas_connection(atlas_url, environment="production")
    assert ok is False
    assert "dev" in reason.lower() and "sandbox" in reason.lower()

    ok2, reason2, _ = validate_remote_atlas_connection(atlas_url, environment="staging")
    assert ok2 is False

    # Allowed with dev or sandbox
    ok3, _, cluster = validate_remote_atlas_connection(atlas_url, environment="dev")
    assert ok3 is True
    assert "cluster0.abcde.mongodb.net" in cluster


def test_read_only_mode_blocks_writes_at_code_level():
    """READ-ONLY mode must block all SQL and Mongo write operations."""
    # 1. SQL Write in READ-ONLY mode
    sql_insert = "INSERT INTO customers (name, email) VALUES ('Ravi', 'ravi@test.com');"
    allowed, risk, msg = check_sql(sql_insert, current_mode=SessionMode.READ_ONLY)
    assert allowed is False
    assert "READ-ONLY" in msg

    sql_update = "UPDATE customers SET name = 'Bob' WHERE id = 1;"
    allowed_u, _, msg_u = check_sql(sql_update, current_mode=SessionMode.READ_ONLY)
    assert allowed_u is False
    assert "READ-ONLY" in msg_u

    sql_delete = "DELETE FROM customers WHERE id = 1;"
    allowed_d, _, msg_d = check_sql(sql_delete, current_mode=SessionMode.READ_ONLY)
    assert allowed_d is False
    assert "READ-ONLY" in msg_d

    # SQL SELECT in READ-ONLY mode must pass
    sql_select = "SELECT * FROM customers;"
    allowed_s, risk_s, _ = check_sql(sql_select, current_mode=SessionMode.READ_ONLY)
    assert allowed_s is True
    assert risk_s == "LOW"

    # 2. Mongo Write in READ-ONLY mode
    mongo_insert = {"operation": "insert", "collection": "customers", "columns": {"name": "Ravi"}}
    m_allowed, _, m_msg = check_mongo_op(mongo_insert, current_mode=SessionMode.READ_ONLY)
    assert m_allowed is False
    assert "READ-ONLY" in m_msg

    mongo_update = {"operation": "update", "collection": "customers", "filter": {"id": 1}, "update": {"$set": {"name": "Bob"}}}
    m_allowed_u, _, m_msg_u = check_mongo_op(mongo_update, current_mode=SessionMode.READ_ONLY)
    assert m_allowed_u is False
    assert "READ-ONLY" in m_msg_u

    # Mongo Query in READ-ONLY mode must pass
    mongo_find = {"operation": "find", "collection": "customers", "filter": {}}
    m_allowed_f, m_risk_f, _ = check_mongo_op(mongo_find, current_mode=SessionMode.READ_ONLY)
    assert m_allowed_f is True
    assert m_risk_f == "LOW"


def test_admin_mode_guardrail():
    """Verify that DDL / user management in READ-WRITE mode requires switching to ADMIN mode."""
    sql_alter = "ALTER TABLE customers ADD COLUMN age INT;"
    allowed, risk, msg = check_sql(sql_alter, current_mode=SessionMode.READ_WRITE)
    assert allowed is False
    assert "ADMIN mode" in msg

    # In ADMIN mode, ALTER TABLE is allowed
    allowed_admin, risk_admin, _ = check_sql(sql_alter, current_mode=SessionMode.ADMIN)
    assert allowed_admin is True
    assert risk_admin == "HIGH"


# ==============================================================================
# 3. Router Intent Classification on 20 Sample Sentences
# ==============================================================================

ROUTER_20_SAMPLE_REQUESTS = [
    # 1. explore
    ("list all collections", "explore"),
    ("show schema for customers", "explore"),
    ("count documents in users", "explore"),
    # 2. query
    ("find customers where city is Chennai", "query"),
    ("get top 10 orders sorted by amount", "query"),
    # 3. insert
    ("add a customer named Ravi, Chennai", "insert"),
    # 4. fake_data
    ("generate 50 fake orders", "fake_data"),
    # 5. update
    ("update customer status to active where id=10", "update"),
    # 6. delete
    ("delete from orders where id = 5", "delete"),
    # 7. schema
    ("create index on email in customers", "schema"),
    # 8. relationships
    ("show relationships between orders and customers", "relationships"),
    ("validate relationships and check for orphan rows", "relationships"),
    # 9. users
    ("create read-only user analyst for table orders", "users"),
    # 10. data_io
    ("export orders to orders.csv", "data_io"),
    ("import customers.json into customers", "data_io"),
    ("backup database", "data_io"),
    # 11. copy
    ("copy customers from local_pg to atlas_dev", "copy"),
    # 12. connection / mode
    ("switch to read-write mode", "connection"),
    # 13. help / capabilities
    ("what can you do on this database?", "help"),
    # 14. unknown / ambiguous entity asking for clarification
    ("show data from nonexistent_unknown_tbl", "unknown"),
]


def test_router_classification_on_20_sample_sentences():
    """Verify router classifies all 20 distinct intents properly with mock LLM fallback."""
    mock_schema = {
        "tables": {
            "customers": {"columns": [{"name": "id"}, {"name": "name"}, {"name": "city"}, {"name": "email"}]},
            "orders": {"columns": [{"name": "id"}, {"name": "customer_id"}, {"name": "amount"}]},
            "users": {"columns": [{"name": "id"}, {"name": "username"}, {"name": "email"}]},
        }
    }

    mock_llm = MagicMock()
    # Mock LLM returns appropriate structured JSON when asked
    def mock_ask_json(system_prompt, user_prompt):
        u_low = user_prompt.lower()
        if "chennai" in u_low and "find" in u_low:
            return {"intent": "query", "params": {"target": "customers"}}
        if "top 10" in u_low:
            return {"intent": "query", "params": {"target": "orders"}}
        if "ravi" in u_low:
            return {"intent": "insert", "params": {"target": "customers"}}
        if "update" in u_low:
            return {"intent": "update", "params": {"target": "customers"}}
        if "delete" in u_low:
            return {"intent": "delete", "params": {"target": "orders"}}
        if "index" in u_low:
            return {"intent": "schema", "params": {"target": "customers"}}
        if "count" in u_low:
            return {"intent": "explore", "params": {"target": "users"}}
        if "schema for customers" in u_low:
            return {"intent": "explore", "params": {"target": "customers"}}
        if "analyst" in u_low:
            return {"intent": "users", "params": {"username": "analyst"}}
        return {"intent": "unknown", "params": {}, "clarification": "Could you clarify?"}

    mock_llm.ask_json.side_effect = mock_ask_json

    classified_intents = []
    for sentence, expected_intent in ROUTER_20_SAMPLE_REQUESTS:
        result = classify_intent(
            user_request=sentence,
            schema=mock_schema,
            db_type="sqlite",
            prompt_version="v3",
            llm_client=mock_llm
        )
        classified_intents.append((sentence, result.intent, expected_intent))
        assert result.intent == expected_intent, f"Failed on '{sentence}': got '{result.intent}', expected '{expected_intent}'"

    # Verify 20/20 classified accurately
    assert len(classified_intents) == 20


def test_router_disambiguation_suggests_closest_name():
    """Verify that ambiguous or misspelled table names produce a clarifying suggestion."""
    mock_schema = {
        "tables": {
            "customers": {},
            "orders": {},
            "products": {},
        }
    }
    mock_llm = MagicMock()
    mock_llm.ask_json.return_value = {"intent": "query", "target_entity": "custmers"}
    result = classify_intent(
        user_request="find all from custmers",  # Misspelled
        schema=mock_schema,
        db_type="sqlite",
        llm_client=mock_llm,
    )
    assert result.suggested_clarification is not None
    assert "customers" in result.suggested_clarification


# ==============================================================================
# 4. Capability Matrix Tests
# ==============================================================================

def test_capability_matrix_coverage():
    """Verify capability matrix accurately represents feature coverage across all 4 targets."""
    # SQLite
    ok, note = is_capability_supported("users", "SQLITE")
    assert ok is False
    assert "does not support user" in note.lower()

    ok_exp, _ = is_capability_supported("explore", "SQLITE")
    assert ok_exp is True

    # PostgreSQL
    ok_pg_users, _ = is_capability_supported("users", "POSTGRESQL")
    assert ok_pg_users is True

    # MongoDB Local
    ok_m_users, _ = is_capability_supported("users", "MONGO_LOCAL")
    assert ok_m_users is True

    # MongoDB Atlas
    ok_atlas_users, note_atlas = is_capability_supported("users", "MONGO_ATLAS")
    assert ok_atlas_users is False
    assert "Atlas Admin API" in note_atlas or "dashboard" in note_atlas

    # Verify Table rendering works
    tbl = render_capability_matrix("SQLITE")
    assert tbl is not None
    assert tbl.title is not None
    assert "Capability" in str(tbl.columns[0].header)


# ==============================================================================
# 5. Data I/O & Preview Tests
# ==============================================================================

def test_data_io_csv_export_and_preview(tmp_path):
    """Test exporting fake records and previewing import data."""
    csv_file = tmp_path / "test_export.csv"

    # Mock adapter
    mock_adapter = MagicMock()
    mock_adapter.db_type = "SQLITE"
    mock_conn = MagicMock()
    # Mock SQL execution returning rows
    mock_conn.execute.return_value.fetchall.return_value = [
        (1, "Alice", 100.5),
        (2, "Bob", 250.0),
        (3, "Charlie", 75.25),
    ]
    mock_conn.execute.return_value.keys.return_value = ["id", "name", "total"]
    mock_adapter.engine.connect.return_value.__enter__.return_value = mock_conn

    # 1. Export
    ok, count, msg = export_data(mock_adapter, "customers", str(csv_file), format_type="csv")
    assert ok is True
    assert count == 3
    assert csv_file.exists()

    # 2. Preview Import
    ok_prev, total_rows, preview, types = preview_import_data(str(csv_file))
    assert ok_prev is True
    assert total_rows == 3
    assert len(preview) == 3
    assert preview[0]["name"] == "Alice"
    assert "total" in types
