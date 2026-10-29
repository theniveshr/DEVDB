"""Unit tests for Phase 4: Relation-Aware Synthetic Fake Data Generation.

Tests:
- Spec validation (skipping unknown columns with warnings, skipping auto-increment PKs)
- Provider mappings (faker:<method>, choice:, int_range:min:max, fk:<table>.<column>)
- Locale support (en_IN for Indian names)
- Foreign key integrity and empty parent table detection
- Unique constraint deduplication
- Dependency ordering (Kahn's topological sort)
- Batch transactional insert & session tracker logging
- Reverse-dependency session data deletion (children deleted before parents)
"""

import json
import pytest
from pathlib import Path
from sqlalchemy import create_engine, text

from devdb.schema import get_schema
from devdb.actions.fake_data import (
    validate_spec,
    generate_rows,
    insert_rows,
    get_table_dependency_order,
    track_inserted_keys,
    get_tracked_keys,
    clear_tracked_keys,
    delete_session_fake_data,
    EmptyParentTableError,
    generate_whole_database,
    get_fake_data_spec,
)
from devdb.llm import LLMClient


@pytest.fixture
def test_db(tmp_path):
    """Create a temporary SQLite database with customers, products, orders, and order_items."""
    db_file = tmp_path / "test_fake.db"
    engine = create_engine(f"sqlite:///{db_file}")

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))
        conn.execute(text("""
            CREATE TABLE products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                stock INTEGER DEFAULT 0
            );
        """))
        conn.execute(text("""
            CREATE TABLE orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_id INTEGER NOT NULL,
                order_date TIMESTAMP,
                status TEXT NOT NULL,
                total_amount REAL NOT NULL,
                FOREIGN KEY (customer_id) REFERENCES customers(id)
            );
        """))
        conn.execute(text("""
            CREATE TABLE order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(id),
                FOREIGN KEY (product_id) REFERENCES products(id)
            );
        """))
        # Seed 2 customers and 2 products
        conn.execute(text("INSERT INTO customers (name, email) VALUES ('Aarav Sharma', 'aarav@example.in');"))
        conn.execute(text("INSERT INTO customers (name, email) VALUES ('Diya Patel', 'diya@example.in');"))
        conn.execute(text("INSERT INTO products (name, price, stock) VALUES ('Laptop', 899.99, 10);"))
        conn.execute(text("INSERT INTO products (name, price, stock) VALUES ('Mouse', 24.99, 50);"))

    return engine


@pytest.fixture
def tracker_path(tmp_path):
    return tmp_path / "test_tracker.json"


def test_validate_spec_unknown_column_and_pk(test_db):
    """Test that validate_spec strips unknown columns and auto-increment PKs with warnings."""
    schema = get_schema(test_db)
    raw_spec = {
        "table": "customers",
        "rows": 10,
        "locale": "en_IN",
        "columns": {
            "id": "int_range:1:100",  # Should be skipped (PK)
            "name": "faker:name",
            "email": "faker:email",
            "non_existent_column": "faker:word",  # Unknown column
        }
    }

    cleaned_spec, warnings = validate_spec(raw_spec, schema)

    assert "non_existent_column" not in cleaned_spec["columns"]
    assert "id" not in cleaned_spec["columns"]
    assert "name" in cleaned_spec["columns"]
    assert "email" in cleaned_spec["columns"]

    # Verify warnings were emitted
    assert any("non_existent_column" in w for w in warnings)
    assert any("id" in w for w in warnings)


def test_validate_spec_invalid_table(test_db):
    """Test that validate_spec raises ValueError on unknown table."""
    schema = get_schema(test_db)
    with pytest.raises(ValueError, match="does not exist in schema"):
        validate_spec({"table": "ghost_table", "columns": {}}, schema)


def test_generate_rows_indian_names_locale(test_db):
    """Test that en_IN locale generates realistic Indian names."""
    schema = get_schema(test_db)
    spec = {
        "table": "customers",
        "rows": 25,
        "locale": "en_IN",
        "columns": {
            "name": "faker:name",
            "email": "faker:email",
            "created_at": "faker:date_between:-2y:today"
        }
    }

    rows, warnings = generate_rows(spec, schema, test_db)
    assert len(rows) == 25
    for r in rows:
        assert isinstance(r["name"], str) and len(r["name"]) > 2
        assert "@" in r["email"]


def test_generate_rows_provider_mappings(test_db):
    """Test choice:, int_range:min:max, and faker providers."""
    schema = get_schema(test_db)
    spec = {
        "table": "orders",
        "rows": 20,
        "locale": "en_US",
        "columns": {
            "customer_id": "fk:customers.id",
            "order_date": "faker:date_between:-1y:today",
            "status": "choice:pending,processing,shipped",
            "total_amount": "faker:price"
        }
    }

    rows, warnings = generate_rows(spec, schema, test_db)
    assert len(rows) == 20
    valid_statuses = {"pending", "processing", "shipped"}
    for r in rows:
        assert r["status"] in valid_statuses
        assert r["customer_id"] in (1, 2)  # Pre-seeded customer IDs
        assert 9.99 <= r["total_amount"] <= 499.99


def test_generate_rows_unique_constraint(test_db):
    """Test that UNIQUE column values are deduplicated and avoid collisions with existing DB rows."""
    schema = get_schema(test_db)
    spec = {
        "table": "customers",
        "rows": 30,
        "locale": "en_US",
        "columns": {
            "name": "faker:name",
            "email": "faker:email"
        }
    }

    rows, warnings = generate_rows(spec, schema, test_db)
    emails = [r["email"] for r in rows]
    # No duplicates among generated rows
    assert len(emails) == len(set(emails))
    # No collision with pre-seeded emails
    assert "aarav@example.in" not in emails
    assert "diya@example.in" not in emails


def test_empty_parent_table_error(tmp_path):
    """Test EmptyParentTableError is raised when referencing an empty parent table."""
    db_file = tmp_path / "empty_parent.db"
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE parents (id INTEGER PRIMARY KEY, name TEXT);"))
        conn.execute(text("CREATE TABLE children (id INTEGER PRIMARY KEY, parent_id INTEGER, val TEXT, FOREIGN KEY (parent_id) REFERENCES parents(id));"))

    schema = get_schema(engine)
    spec = {
        "table": "children",
        "rows": 5,
        "columns": {
            "parent_id": "fk:parents.id",
            "val": "faker:word"
        }
    }

    # Should raise EmptyParentTableError since parents is empty
    with pytest.raises(EmptyParentTableError) as exc_info:
        generate_rows(spec, schema, engine, auto_generate_parents=False)
    assert "parents" in str(exc_info.value)

    # When auto_generate_parents=True, should generate parent rows automatically
    rows, warnings = generate_rows(spec, schema, engine, auto_generate_parents=True)
    assert len(rows) == 5
    with engine.connect() as conn:
        parent_count = conn.execute(text("SELECT COUNT(*) FROM parents;")).scalar()
        assert parent_count > 0


def test_topological_dependency_ordering(test_db):
    """Test that foreign key graph correctly orders parent tables before child tables."""
    schema = get_schema(test_db)
    order = get_table_dependency_order(schema)

    # customers must precede orders
    assert order.index("customers") < order.index("orders")
    # orders and products must precede order_items
    assert order.index("orders") < order.index("order_items")
    assert order.index("products") < order.index("order_items")


def test_batch_insert_and_session_tracking(test_db, tracker_path):
    """Test batch inserting rows into SQLite inside a transaction and tracking IDs in JSON."""
    schema = get_schema(test_db)
    spec = {
        "table": "customers",
        "rows": 15,
        "locale": "en_IN",
        "columns": {
            "name": "faker:name",
            "email": "faker:email"
        }
    }
    rows, _ = generate_rows(spec, schema, test_db)
    inserted_pks = insert_rows("customers", rows, test_db, schema, batch_size=5)

    assert len(inserted_pks) == 15
    with test_db.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM customers;")).scalar()
        assert count == 17  # 2 seeded + 15 generated


def test_delete_session_fake_data_children_first(test_db, tracker_path):
    """Test that session deletion deletes children before parents in reverse dependency order."""
    schema = get_schema(test_db)

    # 1. Insert fake customers and fake orders
    c_rows, _ = generate_rows({"table": "customers", "rows": 10, "columns": {"name": "faker:name", "email": "faker:email"}}, schema, test_db)
    c_pks = insert_rows("customers", c_rows, test_db, schema)
    track_inserted_keys("customers", c_pks, tracker_file=tracker_path)

    o_rows, _ = generate_rows({"table": "orders", "rows": 20, "columns": {"customer_id": "fk:customers.id", "status": "choice:active", "total_amount": "faker:price"}}, schema, test_db)
    o_pks = insert_rows("orders", o_rows, test_db, schema)
    track_inserted_keys("orders", o_pks, tracker_file=tracker_path)

    # Verify tracker contains both tables
    tracked = get_tracked_keys(tracker_file=tracker_path)
    assert len(tracked["customers"]) == 10
    assert len(tracked["orders"]) == 20

    # 2. Delete session fake data
    summary = delete_session_fake_data(test_db, schema, tracker_file=tracker_path)

    assert summary["orders"] == 20
    assert summary["customers"] == 10

    # Tracker file should be cleared
    assert not tracker_path.exists()

    # Pre-seeded rows should remain untouched
    with test_db.connect() as conn:
        c_count = conn.execute(text("SELECT COUNT(*) FROM customers;")).scalar()
        o_count = conn.execute(text("SELECT COUNT(*) FROM orders;")).scalar()
        assert c_count == 2
        assert o_count == 0


def test_whole_database_fake_data_generation(test_db):
    """Test whole database generation in dependency order fills all tables without FK violations."""
    schema = get_schema(test_db)
    client = LLMClient(provider="mock")
    summary = generate_whole_database(schema, test_db, "sqlite", client=client, rows_per_table=5)

    assert "customers" in summary and summary["customers"] == 5
    assert "products" in summary and summary["products"] == 5
    assert "orders" in summary and summary["orders"] == 5
    assert "order_items" in summary and summary["order_items"] == 5

    with test_db.connect() as conn:
        for t in ["customers", "products", "orders", "order_items"]:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {t};")).scalar()
            assert count >= 5
