"""Integration tests for native local MongoDB support."""

import pytest
from src.db import DatabaseManager
from src.relations import RelationshipManager
from src.data_generator import FakeDataGenerator
from src.sql_generator import SQLGenerator


@pytest.fixture
def mongo_db():
    """Connect to local MongoDB devdb and seed sample collections."""
    db = DatabaseManager("mongodb://localhost:27017/devdb_test")
    db.init_sample_database()
    return db


def test_mongodb_connection_and_schema(mongo_db):
    """Verify MongoDB reflection, collections, and fields."""
    assert mongo_db.dialect_name == "mongodb"
    colls = mongo_db.get_tables()
    assert "users" in colls
    assert "orders" in colls
    assert "products" in colls

    # Verify foreign keys
    fks = mongo_db.get_foreign_keys("orders")
    assert len(fks) > 0
    assert fks[0]["referred_table"] == "users"

    # Schema summary
    summary = mongo_db.get_schema_summary()
    assert "Collection: users" in summary


def test_mongodb_query_execution(mongo_db):
    """Test find and aggregation queries on MongoDB."""
    cols, rows = mongo_db.execute_query('db.users.find({"role": "admin"})')
    assert len(rows) >= 1
    assert "Alice Johnson" in [r[cols.index("name")] for r in rows]

    # Test count query
    cols, rows = mongo_db.execute_query("db.products.count()")
    assert rows[0][1] == 5


def test_mongodb_relations_and_orphans(mongo_db):
    """Test relationship map and orphan detection in MongoDB."""
    rel_mgr = RelationshipManager(mongo_db)
    rels = rel_mgr.get_all_relationships()
    assert len(rels) >= 4

    # Tree
    tree = rel_mgr.build_relationship_tree()
    assert tree is not None

    # No initial orphans
    orphans = rel_mgr.find_orphan_rows()
    assert len(orphans) == 0

    # Safe delete impact
    impact = rel_mgr.analyze_safe_delete_impact("users", condition="id = 1")
    assert impact["risk_level"] == "HIGH"
    assert len(impact["dependent_children"]) > 0


def test_mongodb_fake_data_generation(mongo_db):
    """Test synthetic data generation with MongoDB."""
    gen = FakeDataGenerator(mongo_db)
    rows, spec = gen.generate_rows("orders", count=2, provider="mock")
    assert len(rows) == 2

    # Check relation-awareness
    valid_user_ids = {1, 2, 3, 4}
    for row in rows:
        assert row["user_id"] in valid_user_ids

    # Insert into Mongo
    inserted = gen.insert_rows("orders", rows)
    assert inserted == 2
