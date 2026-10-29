"""Core integration tests for DB reflection, data generator, and relationship manager."""

import pytest
from src.db import DatabaseManager
from src.data_generator import FakeDataGenerator
from src.relations import RelationshipManager
from src.sql_generator import SQLGenerator


@pytest.fixture
def sample_db():
    """Create an in-memory sample database with seed data."""
    db = DatabaseManager("sqlite://")
    db.init_sample_database()
    return db


def test_schema_reflection(sample_db):
    """Verify that all sample tables and foreign keys are correctly reflected."""
    tables = sample_db.get_tables()
    assert "users" in tables
    assert "products" in tables
    assert "orders" in tables
    assert "order_items" in tables
    assert "user_profiles" in tables

    # Check foreign keys for orders
    fks = sample_db.get_foreign_keys("orders")
    assert len(fks) > 0
    assert fks[0]["referred_table"] == "users"

    # Check summary string
    summary = sample_db.get_schema_summary()
    assert "Table: users" in summary
    assert "PRIMARY KEY" in summary


def test_query_execution(sample_db):
    """Test read and write query execution."""
    cols, rows = sample_db.execute_query("SELECT COUNT(*) FROM users")
    assert len(rows) == 1
    assert rows[0][0] == 4

    # Insert a new record
    _, res = sample_db.execute_query("INSERT INTO users (name, email) VALUES ('Tester', 'test@test.com')")
    assert res[0][0] == "Success"

    # Verify count increased
    _, rows = sample_db.execute_query("SELECT COUNT(*) FROM users")
    assert rows[0][0] == 5


def test_relationship_manager(sample_db):
    """Test relationship tree, orphan detection, and safe-delete impact."""
    rel_mgr = RelationshipManager(sample_db)
    rels = rel_mgr.get_all_relationships()
    assert len(rels) >= 4

    # Tree should render without errors
    tree = rel_mgr.build_relationship_tree()
    assert tree is not None

    # Initially, sample DB has no orphans
    orphans = rel_mgr.find_orphan_rows()
    assert len(orphans) == 0

    # Test safe-delete impact for user 1 (has orders and profile)
    impact = rel_mgr.analyze_safe_delete_impact("users", condition="id = 1")
    assert impact["risk_level"] == "HIGH"
    assert len(impact["dependent_children"]) > 0


def test_fake_data_generation(sample_db):
    """Test relation-aware synthetic data generation using Faker."""
    gen = FakeDataGenerator(sample_db)
    rows, spec = gen.generate_rows("orders", count=3, provider="mock")
    assert len(rows) == 3

    # Verify relation-awareness: user_id must point to an existing user id (1, 2, 3, or 4)
    existing_user_ids = {1, 2, 3, 4}
    for row in rows:
        assert row["user_id"] in existing_user_ids

    # Insert the rows
    inserted = gen.insert_rows("orders", rows)
    assert inserted == 3


def test_sql_generator_mock(sample_db):
    """Test SQL generator parsing and prompt loading in mock mode."""
    gen = SQLGenerator(sample_db)

    # Test V1
    res_v1 = gen.generate("Who are the top customers?", version="v1", provider="mock")
    assert "SELECT" in res_v1.sql.upper()
    assert res_v1.operation_type == "READ"

    # Test V3 CoT
    res_v3 = gen.generate("Who are the top customers?", version="v3", provider="mock")
    assert "SELECT" in res_v3.sql.upper()
    assert res_v3.thinking is not None
    assert "Schema Grounding" in res_v3.thinking
