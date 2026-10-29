"""Tests for PostgreSQL user management and privilege control (devdb/actions/users.py)."""

import pytest
from sqlalchemy import create_engine, text

from devdb.actions.users import (
    validate_username,
    quote_identifier,
    quote_literal,
    mask_password_in_sql,
    generate_strong_password,
    parse_privileges,
    parse_tables,
    build_user_management_sql,
    parse_user_request_nl,
    list_pg_users,
    render_users_table,
    execute_user_statements,
    UserManagementError,
    SQLITE_UNSUPPORTED_MESSAGE,
    CONFIRMATION_SUPERUSER_KEYWORD,
    CONFIRMATION_GRANT_ALL_KEYWORD,
)
from devdb.cli import DevDBSession


MOCK_SCHEMA = {
    "tables": {
        "customers": {"columns": [{"name": "id", "primary_key": True}]},
        "orders": {"columns": [{"name": "id", "primary_key": True}]},
        "products": {"columns": [{"name": "id", "primary_key": True}]},
    }
}


def test_sqlite_unsupported_rejection():
    """Verify that SQLite connections are politely refused since SQLite has no users or permissions."""
    session = DevDBSession()
    session.connect("sqlite:///:memory:")

    # Attempt user management on SQLite
    success = session.handle_user_management("create a read-only user report_user for orders")
    assert success is False
    assert "sqlite" in SQLITE_UNSUPPORTED_MESSAGE.lower()
    assert "serverless" in SQLITE_UNSUPPORTED_MESSAGE.lower()


def test_username_validation():
    """Verify strict username validation (letters, digits, underscore, max 63 chars, no SQL injection)."""
    valid_names = ["report_user", "analyst1", "dev_user_2026", "A", "user_abc_123"]
    for name in valid_names:
        assert validate_username(name) == name

    invalid_names = [
        "report-user",          # Hyphen not allowed
        "user; DROP TABLE",     # SQL injection
        "user' OR '1'='1",      # Quote injection
        "admin user",           # Spaces not allowed
        "user@domain",          # Special characters
        "",                     # Empty string
        "a" * 64,               # Exceeds 63 characters
    ]
    for bad in invalid_names:
        with pytest.raises(UserManagementError):
            validate_username(bad)


def test_table_name_validation():
    """Verify that table names are strictly validated against the active schema."""
    # Valid tables in MOCK_SCHEMA
    assert parse_tables(["orders", "customers"], MOCK_SCHEMA) == ["orders", "customers"]
    assert parse_tables("orders", MOCK_SCHEMA) == ["orders"]

    # Invalid table not in schema
    with pytest.raises(UserManagementError, match="Unknown table 'passwords'"):
        parse_tables(["orders", "passwords"], MOCK_SCHEMA)


def test_safe_quoting():
    """Verify double-quoting of identifiers and single-quoting of string literals."""
    assert quote_identifier("report_user") == '"report_user"'
    assert quote_identifier('test"user') == '"test""user"'

    assert quote_literal("plain_pass") == "'plain_pass'"
    assert quote_literal("pass'word") == "'pass''word'"


def test_password_generation_and_masking():
    """Verify strong password generation and redaction in SQL and logs."""
    pwd = generate_strong_password(16)
    assert len(pwd) == 16
    assert any(c.isupper() for c in pwd)
    assert any(c.islower() for c in pwd)
    assert any(c.isdigit() for c in pwd)

    raw_sql = f"CREATE USER \"dev_test\" WITH PASSWORD '{pwd}';\nGRANT SELECT ON TABLE \"orders\" TO \"dev_test\";"
    masked = mask_password_in_sql(raw_sql)

    assert pwd not in masked
    assert "PASSWORD '[REDACTED]'" in masked
    assert "dev_test" in masked


def test_build_user_management_sql_create_user():
    """Verify SQL generation for create_user with safe identifier quoting."""
    spec = {
        "operation": "create_user",
        "username": "report_user",
        "privileges": ["SELECT"],
        "tables": ["orders"],
        "password": "SecretPassword123!",
    }

    statements, pwd, explanation, req_confirm = build_user_management_sql(spec, schema=MOCK_SCHEMA)
    assert pwd is None  # Since password was provided, generated password is None
    assert req_confirm is False
    assert len(statements) == 3

    # Statement 1: CREATE USER with safe identifier quoting
    assert statements[0] == 'CREATE USER "report_user" WITH PASSWORD \'SecretPassword123!\';'
    # Statement 2: Schema USAGE grant
    assert statements[1] == 'GRANT USAGE ON SCHEMA public TO "report_user";'
    # Statement 3: Table SELECT grant
    assert statements[2] == 'GRANT SELECT ON TABLE "orders" TO "report_user";'


def test_build_user_management_sql_auto_password():
    """Verify that when no password is given, a strong random password is generated and returned."""
    spec = {
        "operation": "create_user",
        "username": "report_user",
        "privileges": ["read-only"],
        "tables": ["orders"],
    }

    statements, generated_pwd, explanation, req_confirm = build_user_management_sql(spec, schema=MOCK_SCHEMA)
    assert generated_pwd is not None
    assert len(generated_pwd) == 16
    assert generated_pwd in statements[0]


def test_build_user_management_sql_read_write():
    """Verify read-write privilege mapping expands to SELECT, INSERT, UPDATE, DELETE."""
    spec = {
        "operation": "create_user",
        "username": "app_user",
        "privileges": ["read-write"],
        "tables": ["customers"],
        "password": "TestPassword99!",
    }

    statements, _, _, _ = build_user_management_sql(spec, schema=MOCK_SCHEMA)
    assert 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE "customers" TO "app_user";' in statements


def test_build_user_management_sql_grant_revoke():
    """Verify grant and revoke statements."""
    grant_spec = {
        "operation": "grant",
        "username": "analyst",
        "privileges": ["SELECT"],
        "tables": ["orders", "products"],
    }
    g_stmts, _, _, _ = build_user_management_sql(grant_spec, schema=MOCK_SCHEMA)
    assert 'GRANT SELECT ON TABLE "orders" TO "analyst";' in g_stmts
    assert 'GRANT SELECT ON TABLE "products" TO "analyst";' in g_stmts

    revoke_spec = {
        "operation": "revoke",
        "username": "analyst",
        "privileges": ["SELECT"],
        "tables": ["products"],
    }
    r_stmts, _, _, _ = build_user_management_sql(revoke_spec, schema=MOCK_SCHEMA)
    assert 'REVOKE SELECT ON TABLE "products" FROM "analyst";' in r_stmts


def test_build_user_management_sql_drop_user():
    """Verify drop_user safely reassigns and drops owned objects before dropping role."""
    spec = {
        "operation": "drop_user",
        "username": "temp_user",
    }
    stmts, _, _, _ = build_user_management_sql(spec, schema=MOCK_SCHEMA)
    assert len(stmts) == 3
    assert stmts[0] == 'REASSIGN OWNED BY "temp_user" TO CURRENT_USER;'
    assert stmts[1] == 'DROP OWNED BY "temp_user";'
    assert stmts[2] == 'DROP ROLE "temp_user";'


def test_superuser_confirmation_guardrail():
    """Verify that superuser and blanket database-wide ALL privilege require explicit confirmation."""
    # Superuser request
    su_spec = {
        "operation": "create_user",
        "username": "root_user",
        "is_superuser": True,
    }
    _, _, _, req_confirm = build_user_management_sql(su_spec, schema=MOCK_SCHEMA)
    assert req_confirm is True

    # Blanket ALL privileges request
    all_spec = {
        "operation": "grant",
        "username": "super_user",
        "privileges": ["ALL"],
        "tables": [],
    }
    _, _, _, req_confirm = build_user_management_sql(all_spec, schema=MOCK_SCHEMA)
    assert req_confirm is True


def test_parse_user_request_nl():
    """Verify natural language parsing for user management."""
    # 1. Create read-only user
    spec1 = parse_user_request_nl("create a read-only user report_user for orders", schema=MOCK_SCHEMA)
    assert spec1["operation"] == "create_user"
    assert spec1["username"] == "report_user"
    assert "SELECT" in spec1["privileges"]
    assert spec1["tables"] == ["orders"]

    # 2. Grant privileges
    spec2 = parse_user_request_nl("grant read-write on customers to app_user", schema=MOCK_SCHEMA)
    assert spec2["operation"] == "grant"
    assert spec2["username"] == "app_user"
    assert set(spec2["privileges"]) == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert spec2["tables"] == ["customers"]

    # 3. List users
    spec3 = parse_user_request_nl("list all users and privileges", schema=MOCK_SCHEMA)
    assert spec3["operation"] == "list"

    # 4. Drop user
    spec4 = parse_user_request_nl("drop user old_analyst", schema=MOCK_SCHEMA)
    assert spec4["operation"] == "drop_user"
    assert spec4["username"] == "old_analyst"


def is_postgres_available() -> bool:
    try:
        engine = create_engine("postgresql://dev:dev@localhost:5432/devdb")
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
        return True
    except Exception:
        return False


@pytest.mark.skipif(not is_postgres_available(), reason="Requires running PostgreSQL instance at localhost:5432/devdb")
def test_postgres_live_user_lifecycle():
    """Integration test: create user, verify privileges, test SELECT vs INSERT, and drop user."""
    pg_url = "postgresql://dev:dev@localhost:5432/devdb"
    engine = create_engine(pg_url)

    # Clean up test user if previously left behind
    try:
        with engine.connect() as conn:
            conn.execute(text("DROP ROLE IF EXISTS test_report_user;"))
            conn.commit()
    except Exception:
        pass

    # 1. Create user with read-only on orders
    spec = {
        "operation": "create_user",
        "username": "test_report_user",
        "privileges": ["SELECT"],
        "tables": ["orders"],
        "password": "TestReportPass2026!",
    }
    stmts, _, _, _ = build_user_management_sql(spec, schema=MOCK_SCHEMA)
    execute_user_statements(stmts, engine)

    # 2. Verify user listed in pg_roles
    users = list_pg_users(engine)
    user_names = [u["username"] for u in users]
    assert "test_report_user" in user_names

    # 3. Connect as test_report_user: SELECT must succeed, INSERT must fail
    user_engine = create_engine("postgresql://test_report_user:TestReportPass2026!@localhost:5432/devdb")
    with user_engine.connect() as conn:
        res = conn.execute(text("SELECT COUNT(*) FROM orders;")).scalar()
        assert res >= 0

        # Attempt INSERT -> must fail with permission denied
        with pytest.raises(Exception) as exc_info:
            conn.execute(text("INSERT INTO orders (customer_id, status, total_amount) VALUES (1, 'pending', 10.0);"))
            conn.commit()
        assert "permission denied" in str(exc_info.value).lower()

    # 4. Drop user
    drop_stmts, _, _, _ = build_user_management_sql({"operation": "drop_user", "username": "test_report_user"}, schema=MOCK_SCHEMA)
    execute_user_statements(drop_stmts, engine)

    # Verify user dropped
    users_after = list_pg_users(engine)
    assert "test_report_user" not in [u["username"] for u in users_after]
