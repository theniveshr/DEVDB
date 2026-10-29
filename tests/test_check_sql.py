"""Comprehensive pytest test suite for devdb safety check_sql().

Validates at least 12 distinct scenarios:
- Safe operations (SELECT, INSERT, UPDATE with WHERE, DELETE with WHERE, ALTER)
- Blocked operations (DROP DATABASE, TRUNCATE, DROP SCHEMA, DELETE/UPDATE without WHERE)
- Confirmation requirements (DROP TABLE table-name confirmation)
- Edge cases (lowercase keywords, embedded comments, stacked statements, semicolons in string literals, empty SQL)
"""

import pytest
from devdb.safety import check_sql


def test_safe_select_query():
    """Verify standard SELECT is allowed with LOW risk."""
    allowed, risk, msg = check_sql("SELECT id, name FROM customers WHERE id = 1;")
    assert allowed is True
    assert risk == "LOW"


def test_safe_select_with_join():
    """Verify multi-table JOIN SELECT is allowed with LOW risk."""
    sql = "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id;"
    allowed, risk, msg = check_sql(sql)
    assert allowed is True
    assert risk == "LOW"


def test_safe_insert():
    """Verify INSERT is allowed with MEDIUM risk."""
    allowed, risk, msg = check_sql("INSERT INTO customers (name, email) VALUES ('Alice', 'alice@test.com');")
    assert allowed is True
    assert risk == "MEDIUM"


def test_safe_update_with_where():
    """Verify UPDATE containing WHERE clause is allowed with MEDIUM risk."""
    allowed, risk, msg = check_sql("UPDATE customers SET name = 'Bob' WHERE id = 10;")
    assert allowed is True
    assert risk == "MEDIUM"


def test_safe_delete_with_where():
    """Verify DELETE containing WHERE clause is allowed with HIGH risk."""
    allowed, risk, msg = check_sql("DELETE FROM orders WHERE id = 5;")
    assert allowed is True
    assert risk == "HIGH"


def test_safe_alter_table():
    """Verify ALTER TABLE is allowed with HIGH risk."""
    allowed, risk, msg = check_sql("ALTER TABLE customers ADD COLUMN phone TEXT;")
    assert allowed is True
    assert risk == "HIGH"


def test_blocked_delete_without_where():
    """Verify blanket DELETE without WHERE clause is strictly blocked."""
    allowed, risk, msg = check_sql("DELETE FROM customers;")
    assert allowed is False
    assert "WHERE" in msg


def test_blocked_update_without_where():
    """Verify blanket UPDATE without WHERE clause is strictly blocked."""
    allowed, risk, msg = check_sql("UPDATE customers SET name = 'Hacked';")
    assert allowed is False
    assert "WHERE" in msg


def test_blocked_drop_database():
    """Verify DROP DATABASE is strictly blocked."""
    allowed, risk, msg = check_sql("DROP DATABASE devdb;")
    assert allowed is False
    assert "DROP DATABASE" in msg


def test_blocked_drop_database_lowercase():
    """Verify lowercase drop database is strictly blocked."""
    allowed, risk, msg = check_sql("drop database production;")
    assert allowed is False
    assert "DROP DATABASE" in msg


def test_blocked_truncate():
    """Verify TRUNCATE is strictly blocked."""
    allowed, risk, msg = check_sql("TRUNCATE TABLE customers;")
    assert allowed is False
    assert "TRUNCATE" in msg


def test_blocked_drop_schema():
    """Verify DROP SCHEMA is strictly blocked."""
    allowed, risk, msg = check_sql("DROP SCHEMA public CASCADE;")
    assert allowed is False
    assert "DROP SCHEMA" in msg


def test_blocked_multiple_stacked_statements():
    """Verify multiple stacked SQL statements separated by semicolon are blocked."""
    allowed, risk, msg = check_sql("SELECT 1; DROP TABLE customers;")
    assert allowed is False
    assert "Multiple stacked statements" in msg


def test_blocked_delete_without_where_with_comments():
    """Verify DELETE without WHERE is blocked even when hidden with comments."""
    sql = "/* administrative action */ DELETE FROM customers; -- danger"
    allowed, risk, msg = check_sql(sql)
    assert allowed is False
    assert "WHERE" in msg


def test_drop_table_requires_table_name_confirmation():
    """Verify DROP TABLE is flagged to require typing table name to confirm."""
    allowed, risk, msg = check_sql("DROP TABLE customers;")
    assert allowed is True
    assert risk == "HIGH"
    assert "customers" in msg


def test_drop_table_if_exists_with_quotes():
    """Verify DROP TABLE IF EXISTS with quotes extracts target table name."""
    allowed, risk, msg = check_sql('DROP TABLE IF EXISTS "order_items";')
    assert allowed is True
    assert risk == "HIGH"
    assert "order_items" in msg


def test_empty_sql_handled():
    """Verify empty SQL or pure comments return not allowed."""
    allowed, risk, msg = check_sql("")
    assert allowed is False
    allowed2, risk2, msg2 = check_sql("   -- pure comment only   ")
    assert allowed2 is False


def test_semicolon_inside_string_literal_allowed():
    """Verify that a semicolon inside a quoted string literal is NOT treated as multiple statements."""
    sql = "SELECT * FROM customers WHERE email = 'semi;colon@example.com';"
    allowed, risk, msg = check_sql(sql)
    assert allowed is True
    assert risk == "LOW"
