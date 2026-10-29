"""Pytest test suite for devdb schema.py and safety.py (SQL and MongoDB)."""

import pytest
from sqlalchemy import create_engine, text

from devdb.safety import is_dev_connection, validate_dev_connection, SafetyViolationError
from devdb.schema import get_schema, get_mongo_schema, schema_to_text


def test_safety_allowed_dev_connections():
    """Verify that only SQLite, local MongoDB, and localhost/127.0.0.1 connections are accepted."""
    allowed_urls = [
        "sqlite:///dev.db",
        "sqlite:///:memory:",
        "sqlite://",
        "sqlite+pysqlite:///test.db",
        "mongodb://localhost:27017/devdb",
        "mongodb://127.0.0.1:27017/devdb",
        "mongodb://localhost:27017",
        "postgresql://dev:dev@localhost:5432/devdb",
        "postgresql://dev:dev@127.0.0.1:5432/devdb",
        "postgresql+psycopg2://dev:dev@localhost/devdb",
        "mysql://root@localhost:3306/testdb",
        "mysql://root@127.0.0.1/testdb",
    ]

    for url in allowed_urls:
        assert is_dev_connection(url) is True, f"Expected {url} to be allowed"
        validate_dev_connection(url)


def test_safety_remote_connections_refused():
    """Verify that remote database hosts and non-localhost IPs are strictly refused."""
    refused_urls = [
        "mongodb://admin:secret@cluster0.mongodb.net/devdb",
        "mongodb://admin:secret@192.168.1.100:27017/devdb",
        "postgresql://admin:secret@aws-rds-prod.us-east-1.amazonaws.com:5432/proddb",
        "postgresql://dev:dev@db.company.internal:5432/analytics",
        "postgresql://dev:dev@192.168.1.100:5432/devdb",
        "postgresql://dev:dev@10.0.0.5:5432/devdb",
        "postgresql://dev:dev@0.0.0.0:5432/devdb",
        "mysql://root@remote.server.org/proddb",
        "",
        "invalid://url",
    ]

    for url in refused_urls:
        assert is_dev_connection(url) is False, f"Expected {url} to be refused"
        with pytest.raises(SafetyViolationError):
            validate_dev_connection(url)


def test_sql_schema_inspection():
    """Test get_schema() properly extracts tables, columns, PKs, FKs, and indexes from SQL."""
    from sqlalchemy.pool import StaticPool
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool
    )

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                email TEXT
            );
        """))
        conn.execute(text("CREATE UNIQUE INDEX ix_users_email ON users (email);"))
        conn.execute(text("""
            CREATE TABLE posts (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );
        """))

    schema = get_schema(engine)
    assert "tables" in schema
    tables = schema["tables"]
    assert "users" in tables
    assert "posts" in tables

    # Verify users table inspection
    user_cols = {c["name"]: c for c in tables["users"]["columns"]}
    assert "id" in user_cols
    assert user_cols["id"]["primary_key"] is True
    assert "username" in user_cols
    assert user_cols["username"]["nullable"] is False

    user_indexes = tables["users"]["indexes"]
    assert any(idx["name"] == "ix_users_email" and idx["unique"] is True for idx in user_indexes)

    # Verify posts foreign keys
    post_fks = tables["posts"]["foreign_keys"]
    assert len(post_fks) == 1
    assert post_fks[0]["referred_table"] == "users"
    assert "user_id" in post_fks[0]["constrained_columns"]


def test_mongodb_schema_inspection():
    """Test MongoDB schema inspection on native local MongoDB instance."""
    try:
        import pymongo
        client = pymongo.MongoClient("mongodb://localhost:27017", serverSelectionTimeoutMS=1500)
        client.server_info()
    except Exception:
        pytest.skip("Local MongoDB server not available on localhost:27017")

    schema = get_mongo_schema("mongodb://localhost:27017/devdb")
    assert "tables" in schema
    collections = schema["tables"]
    # Check that devdb collections were found
    assert len(collections) > 0
    for coll_name, coll_info in collections.items():
        assert "columns" in coll_info
        assert "foreign_keys" in coll_info
        assert "indexes" in coll_info
        # _id field should be primary key
        col_map = {c["name"]: c for c in coll_info["columns"]}
        assert "_id" in col_map
        assert col_map["_id"]["primary_key"] is True


def test_schema_to_text():
    """Test schema_to_text() produces compact prompt-friendly formatting."""
    dummy_schema = {
        "tables": {
            "customers": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "nullable": False, "primary_key": True},
                    {"name": "name", "type": "TEXT", "nullable": False, "primary_key": False},
                ],
                "foreign_keys": [],
                "indexes": [
                    {"name": "ix_customers_name", "column_names": ["name"], "unique": False}
                ],
            }
        }
    }

    text_output = schema_to_text(dummy_schema)
    assert "Table: customers" in text_output
    assert "id: INTEGER (PK, NOT NULL)" in text_output
    assert "name: TEXT (NOT NULL)" in text_output
    assert "References:" in text_output
    assert "(none)" in text_output
    assert "ix_customers_name: (name)" in text_output
