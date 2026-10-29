import os
import sqlite3
import pytest
from pathlib import Path
from sqlalchemy import create_engine
from devdb.schema import get_schema
from devdb.actions.relationships import (
    generate_add_relationship_sql,
    execute_relationship_sql,
    build_ascii_relationship_map,
    generate_mermaid_er,
    validate_relationships,
    safe_delete_check,
)


@pytest.fixture
def test_db_engine():
    """In-memory SQLite database seeded with customers, products, orders, order_items."""
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, email TEXT UNIQUE);")
        conn.exec_driver_sql("CREATE TABLE products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, price REAL NOT NULL);")
        conn.exec_driver_sql("CREATE TABLE orders (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL, status TEXT NOT NULL, total_amount REAL NOT NULL);")
        conn.exec_driver_sql("CREATE TABLE order_items (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity INTEGER NOT NULL);")

        conn.exec_driver_sql("INSERT INTO customers (id, name, email) VALUES (1, 'Alice', 'alice@test.com'), (2, 'Bob', 'bob@test.com');")
        conn.exec_driver_sql("INSERT INTO products (id, name, price) VALUES (101, 'Laptop', 999.0), (102, 'Mouse', 25.0);")
        conn.exec_driver_sql("INSERT INTO orders (id, customer_id, status, total_amount) VALUES (1, 1, 'completed', 1024.0), (2, 2, 'pending', 25.0);")
        conn.exec_driver_sql("INSERT INTO order_items (id, order_id, product_id, quantity) VALUES (1, 1, 101, 1), (2, 1, 102, 1);")
    return engine


def test_generate_add_relationship_sql_sqlite(test_db_engine):
    """Test safe SQLite table-recreate migration pattern generation."""
    schema = get_schema(test_db_engine)
    sql, explanation = generate_add_relationship_sql(
        child_table="orders",
        child_col="customer_id",
        parent_table="customers",
        parent_col="id",
        schema=schema,
        rel_type="one_to_many",
        on_delete="RESTRICT",
        db_type="sqlite"
    )

    assert "PRAGMA foreign_keys = OFF;" in sql
    assert "BEGIN TRANSACTION;" in sql
    assert "CREATE TABLE orders_new" in sql
    assert "FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT" in sql
    assert "INSERT INTO orders_new" in sql
    assert "DROP TABLE orders;" in sql
    assert "ALTER TABLE orders_new RENAME TO orders;" in sql
    assert "COMMIT;" in sql
    assert "PRAGMA foreign_keys = ON;" in sql
    assert "Safe SQLite table recreation" in explanation


def test_generate_add_relationship_sql_postgres():
    """Test PostgreSQL ALTER TABLE ADD CONSTRAINT foreign key generation."""
    schema = {"tables": {"orders": {"columns": [{"name": "customer_id"}]}}}
    sql, explanation = generate_add_relationship_sql(
        child_table="orders",
        child_col="customer_id",
        parent_table="customers",
        parent_col="id",
        schema=schema,
        rel_type="one_to_many",
        on_delete="CASCADE",
        db_type="postgresql"
    )

    assert "ALTER TABLE orders ADD CONSTRAINT fk_orders_customer_id_customers" in sql
    assert "FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE;" in sql


def test_generate_add_relationship_one_to_one(test_db_engine):
    """Test one-to-one relationship enforces UNIQUE constraint on child column."""
    schema = get_schema(test_db_engine)
    sql, explanation = generate_add_relationship_sql(
        child_table="orders",
        child_col="customer_id",
        parent_table="customers",
        parent_col="id",
        schema=schema,
        rel_type="one_to_one",
        db_type="sqlite"
    )
    assert "customer_id INTEGER NOT NULL UNIQUE" in sql or "UNIQUE" in sql


def test_generate_add_relationship_many_to_many():
    """Test many-to-many relationship creates junction table with composite PK."""
    schema = {}
    sql, explanation = generate_add_relationship_sql(
        child_table="products",
        child_col="id",
        parent_table="categories",
        parent_col="id",
        schema=schema,
        rel_type="many_to_many",
        join_table="product_categories",
        db_type="sqlite"
    )
    assert "CREATE TABLE product_categories" in sql
    assert "PRIMARY KEY (product_id, category_id)" in sql
    assert "FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE" in sql
    assert "FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE" in sql


def test_execute_relationship_sql_sqlite(test_db_engine):
    """Test executing generated table recreation script on SQLite and checking resulting schema."""
    schema = get_schema(test_db_engine)
    sql, _ = generate_add_relationship_sql(
        child_table="orders",
        child_col="customer_id",
        parent_table="customers",
        parent_col="id",
        schema=schema,
        rel_type="one_to_many",
        db_type="sqlite"
    )

    success = execute_relationship_sql(sql, test_db_engine)
    assert success is True

    # Re-inspect schema
    updated_schema = get_schema(test_db_engine)
    orders_fks = updated_schema["tables"]["orders"]["foreign_keys"]
    assert len(orders_fks) == 1
    assert orders_fks[0]["constrained_columns"] == ["customer_id"]
    assert orders_fks[0]["referred_table"] == "customers"
    assert orders_fks[0]["referred_columns"] == ["id"]

    # Verify original rows are intact
    with test_db_engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT COUNT(*) FROM orders;").scalar()
        assert rows == 2


def test_build_ascii_relationship_map():
    """Test ASCII map chain rendering."""
    schema = {
        "tables": {
            "customers": {"columns": [{"name": "id", "primary_key": True}], "foreign_keys": []},
            "orders": {
                "columns": [{"name": "id", "primary_key": True}, {"name": "customer_id"}],
                "foreign_keys": [{"referred_table": "customers", "constrained_columns": ["customer_id"], "referred_columns": ["id"]}]
            },
            "products": {"columns": [{"name": "id", "primary_key": True}], "foreign_keys": []},
            "order_items": {
                "columns": [{"name": "id", "primary_key": True}, {"name": "order_id"}, {"name": "product_id"}],
                "foreign_keys": [
                    {"referred_table": "orders", "constrained_columns": ["order_id"], "referred_columns": ["id"]},
                    {"referred_table": "products", "constrained_columns": ["product_id"], "referred_columns": ["id"]}
                ]
            }
        }
    }

    ascii_map = build_ascii_relationship_map(schema)
    expected_chain = "customers (1) ──< orders (N) ──< order_items (N) >── (1) products"
    assert expected_chain in ascii_map


def test_generate_mermaid_er(tmp_path):
    """Test Mermaid ER diagram generation and export."""
    schema = {
        "tables": {
            "customers": {"columns": [{"name": "id", "primary_key": True, "type": "INTEGER"}, {"name": "name", "type": "TEXT"}], "foreign_keys": []},
            "orders": {
                "columns": [{"name": "id", "primary_key": True, "type": "INTEGER"}, {"name": "customer_id", "type": "INTEGER"}],
                "foreign_keys": [{"referred_table": "customers", "constrained_columns": ["customer_id"], "referred_columns": ["id"]}]
            }
        }
    }
    out_file = tmp_path / "relationships.md"
    mermaid = generate_mermaid_er(schema, output_path=out_file)

    assert "erDiagram" in mermaid
    assert "customers ||--o{ orders : \"customer_id\"" in mermaid
    assert out_file.exists()
    assert "erDiagram" in out_file.read_text(encoding="utf-8")


def test_validate_relationships_orphan_detection(test_db_engine):
    """Test orphan row detection, missing index check, and unconstrained foreign key discovery."""
    # First link orders -> customers
    schema = get_schema(test_db_engine)
    sql, _ = generate_add_relationship_sql("orders", "customer_id", "customers", "id", schema, db_type="sqlite")
    execute_relationship_sql(sql, test_db_engine)
    schema = get_schema(test_db_engine)

    # Initial check: no orphans
    report = validate_relationships(test_db_engine, schema)
    assert report["total_orphans"] == 0
    assert report["orphan_checks"][0]["status"] == "HEALTHY"

    # Insert orphan row with PRAGMA foreign_keys = OFF
    with test_db_engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF;")
        conn.exec_driver_sql("INSERT INTO orders (id, customer_id, status, total_amount) VALUES (999, 88888, 'pending', 50.0);")
        conn.exec_driver_sql("PRAGMA foreign_keys = ON;")

    # Re-validate: orphan row detected!
    orphan_report = validate_relationships(test_db_engine, schema)
    assert orphan_report["health_status"] == "CRITICAL"
    assert orphan_report["total_orphans"] == 1
    assert 88888 in orphan_report["orphan_checks"][0]["sample_orphans"]

    # Also verify likely missing FK on order_items (product_id points to products, order_id points to orders)
    likely_missing = [lm["child_col"] for lm in orphan_report["likely_missing_fks"]]
    assert "product_id" in likely_missing or "order_id" in likely_missing


def test_safe_delete_check_restrict(test_db_engine):
    """Test safe delete check blocks parent deletion when child rows exist with RESTRICT."""
    schema = get_schema(test_db_engine)
    sql, _ = generate_add_relationship_sql("orders", "customer_id", "customers", "id", schema, on_delete="RESTRICT", db_type="sqlite")
    execute_relationship_sql(sql, test_db_engine)
    schema = get_schema(test_db_engine)

    # Customer 1 has order 1
    check = safe_delete_check("customers", 1, test_db_engine, schema)
    assert check["allowed"] is False
    assert check["has_restrict"] is True
    assert "Cannot delete customer 1" in check["warning"]


def test_safe_delete_check_cascade():
    """Test safe delete check calculates affected rows across cascade tree."""
    schema = {
        "tables": {
            "customers": {
                "columns": [{"name": "id", "primary_key": True}],
                "foreign_keys": []
            },
            "orders": {
                "columns": [{"name": "id", "primary_key": True}, {"name": "customer_id"}],
                "foreign_keys": [{"referred_table": "customers", "constrained_columns": ["customer_id"], "referred_columns": ["id"], "options": {"ondelete": "CASCADE"}}]
            },
            "order_items": {
                "columns": [{"name": "id", "primary_key": True}, {"name": "order_id"}],
                "foreign_keys": [{"referred_table": "orders", "constrained_columns": ["order_id"], "referred_columns": ["id"], "options": {"ondelete": "CASCADE"}}]
            }
        }
    }

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE customers (id INTEGER PRIMARY KEY);")
        conn.exec_driver_sql("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER);")
        conn.exec_driver_sql("CREATE TABLE order_items (id INTEGER PRIMARY KEY, order_id INTEGER);")
        conn.exec_driver_sql("INSERT INTO customers VALUES (5);")
        for i in range(1, 13):
            conn.exec_driver_sql(f"INSERT INTO orders VALUES ({i}, 5);")
        for i in range(1, 41):
            conn.exec_driver_sql(f"INSERT INTO order_items VALUES ({i}, 1);")

    res = safe_delete_check("customers", 5, engine, schema)
    assert res["allowed"] is True
    assert res["warning"] == "Deleting customer 5 will delete 12 orders and 40 order_items."
