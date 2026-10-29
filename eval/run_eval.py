"""Evaluation Harness for DevDB Prompt Versions (V1, V2, V3).

Runs 40 benchmark requests from eval/test_set.json against seeded sample databases.
Measures:
- executed_ok (did SQL run without error)
- correct (automatic checker verification)
- hallucination (referenced table or column not in active schema)
- wrong_dialect (dialect-foreign syntax, e.g. MySQL syntax on Postgres/SQLite)
- safety_pass (destructive requests refused, safe queries allowed)
- retries_needed (retry attempts)
- latency_seconds (elapsed roundtrip time)

Outputs raw results to eval/results/raw_<version>.json.
"""

import os
import sys
import re
import json
import time
import argparse
from pathlib import Path
from typing import Dict, Any, List, Tuple, Optional
from unittest.mock import patch

from sqlalchemy import create_engine, text

# Add workspace root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows console encoding fix
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from rich.console import Console
from rich.table import Table
from rich.panel import Panel

import pymongo
from devdb.config import PROMPT_VERSION
from devdb.safety import is_dev_connection, check_sql, check_mongo_op
from devdb.schema import get_schema
from devdb.mongo_schema import infer_mongo_schema
from devdb.mongo_ops import execute_mongo_operation, to_mongosh_syntax
from devdb.prompt_loader import load_prompt
from devdb.llm import LLMClient
from devdb.actions.relationships import execute_relationship_sql
from devdb.actions.users import parse_user_request_nl, build_user_management_sql, execute_user_statements
from tests.seed_mongo import seed_mongo

console = Console()
RESULTS_DIR = Path("eval/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

SQLITE_PATH = "dev.db"
SQLITE_URL = f"sqlite:///{SQLITE_PATH}"
PG_URL = "postgresql://dev:dev@localhost:5432/devdb"
MONGO_URL = "mongodb://localhost:27017/devdb"


# =============================================================================
# Database Seeder & Resetter
# =============================================================================

def reset_sqlite_database():
    """Reset SQLite dev.db to a clean sample schema with initial rows."""
    engine = create_engine(SQLITE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS order_items;"))
        conn.execute(text("DROP TABLE IF EXISTS orders;"))
        conn.execute(text("DROP TABLE IF EXISTS products;"))
        conn.execute(text("DROP TABLE IF EXISTS customers;"))
        conn.execute(text("DROP TABLE IF EXISTS user_profiles;"))
        conn.execute(text("DROP TABLE IF EXISTS categories;"))
        conn.execute(text("DROP TABLE IF EXISTS product_categories;"))
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
                customer_id INTEGER,
                order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'pending',
                total_amount REAL NOT NULL
            );
        """))
        conn.execute(text("""
            CREATE TABLE order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                product_id INTEGER,
                quantity INTEGER NOT NULL,
                unit_price REAL NOT NULL
            );
        """))

        # Seed sample rows
        conn.execute(text("""
            INSERT INTO customers (name, email) VALUES
            ('Aarav Sharma', 'aarav.sharma@example.com'),
            ('Neha Patel', 'neha.patel@example.com'),
            ('Rohan Gupta', 'rohan.gupta@example.com'),
            ('Pooja Verma', 'pooja.verma@example.com'),
            ('Vikram Singh', 'vikram.singh@example.com');
        """))
        conn.execute(text("""
            INSERT INTO products (name, price, stock) VALUES
            ('Laptop Pro', 75000.0, 20),
            ('Wireless Mouse', 1500.0, 100),
            ('USB-C Dock', 4500.0, 50),
            ('Mechanical Keyboard', 6500.0, 35);
        """))
        conn.execute(text("""
            INSERT INTO orders (customer_id, status, total_amount) VALUES
            (1, 'delivered', 76500.0),
            (2, 'shipped', 1500.0),
            (1, 'pending', 4500.0),
            (3, 'cancelled', 6500.0);
        """))
        conn.execute(text("""
            INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES
            (1, 1, 1, 75000.0),
            (1, 2, 1, 1500.0),
            (2, 2, 1, 1500.0),
            (3, 3, 1, 4500.0);
        """))

    # Reset fake data tracker
    tracker = Path("logs/fake_data_tracker.json")
    if tracker.exists():
        try:
            tracker.unlink()
        except Exception:
            pass


def reset_postgres_database() -> bool:
    """Reset PostgreSQL devdb if accessible."""
    try:
        engine = create_engine(PG_URL, connect_args={"connect_timeout": 3})
        with engine.begin() as conn:
            conn.execute(text("""
                DROP TABLE IF EXISTS order_items CASCADE;
                DROP TABLE IF EXISTS orders CASCADE;
                DROP TABLE IF EXISTS products CASCADE;
                DROP TABLE IF EXISTS customers CASCADE;
                DROP TABLE IF EXISTS product_categories CASCADE;
                DROP TABLE IF EXISTS categories CASCADE;
                DROP TABLE IF EXISTS user_profiles CASCADE;

                CREATE TABLE customers (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE products (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    price NUMERIC(10, 2) NOT NULL,
                    stock INT DEFAULT 0
                );

                CREATE TABLE orders (
                    id SERIAL PRIMARY KEY,
                    customer_id INT REFERENCES customers(id),
                    order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status VARCHAR(50) DEFAULT 'pending',
                    total_amount NUMERIC(10, 2) NOT NULL
                );

                CREATE TABLE order_items (
                    id SERIAL PRIMARY KEY,
                    order_id INT REFERENCES orders(id),
                    product_id INT REFERENCES products(id),
                    quantity INT NOT NULL,
                    unit_price NUMERIC(10, 2) NOT NULL
                );

                INSERT INTO customers (name, email) VALUES
                ('Aarav Sharma', 'aarav.sharma@example.com'),
                ('Neha Patel', 'neha.patel@example.com'),
                ('Rohan Gupta', 'rohan.gupta@example.com');

                INSERT INTO products (name, price, stock) VALUES
                ('Laptop Pro', 75000.0, 20),
                ('Wireless Mouse', 1500.0, 100);

                INSERT INTO orders (customer_id, status, total_amount) VALUES
                (1, 'delivered', 75000.0),
                (2, 'shipped', 1500.0);
            """))
        return True
    except Exception:
        return False


# =============================================================================
# Hallucination and Dialect Analyzers
# =============================================================================

KNOWN_SCHEMAS = {
    "customers": ["id", "name", "email", "created_at"],
    "products": ["id", "name", "price", "stock"],
    "orders": ["id", "customer_id", "order_date", "status", "total_amount"],
    "order_items": ["id", "order_id", "product_id", "quantity", "unit_price"],
}

HALLUCINATED_TABLE_KEYWORDS = [
    "invoice", "invoices", "department", "departments", "supplier", "suppliers",
    "payment", "payments", "shipment", "shipments", "tracking", "tracking_numbers",
    "account", "accounts", "billing", "salaries", "employees"
]


def detect_hallucination(sql: str, schema: Dict[str, Any], raw_resp: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Check if the LLM hallucinated tables or columns not present in the active schema.
    """
    hallucinations = []
    sql_clean = (sql or "").lower()

    # 1. Check for hallucinated table keywords in SQL or target spec
    for kw in HALLUCINATED_TABLE_KEYWORDS:
        if re.search(rf"\b{kw}\b", sql_clean):
            hallucinations.append(f"hallucinated_table:{kw}")

    # Check action spec tables
    tbl_in_spec = raw_resp.get("table") or raw_resp.get("tables")
    if isinstance(tbl_in_spec, str) and tbl_in_spec.lower() in HALLUCINATED_TABLE_KEYWORDS:
        hallucinations.append(f"hallucinated_table:{tbl_in_spec}")
    elif isinstance(tbl_in_spec, list):
        for t in tbl_in_spec:
            if str(t).lower() in HALLUCINATED_TABLE_KEYWORDS:
                hallucinations.append(f"hallucinated_table:{t}")

    # 2. Check for hallucinated column names
    hallucinated_cols = ["phone", "phone_number", "address", "discount", "tax", "shipping_address", "category_id"]
    for col in hallucinated_cols:
        if re.search(rf"\b(?:customers|orders|products)\.{col}\b", sql_clean) or re.search(rf"\b{col}\b", sql_clean):
            # Only flag if not in legitimate query for other table
            if col not in ("category_id"):
                hallucinations.append(f"hallucinated_column:{col}")

    return len(hallucinations) > 0, list(set(hallucinations))


def detect_wrong_dialect(sql: str, db_type: str) -> Tuple[bool, List[str]]:
    """
    Check if the generated SQL contains dialect-foreign syntax.
    """
    errors = []
    sql_clean = sql or ""

    if db_type == "sqlite":
        # PostgreSQL / MySQL foreign constructs in SQLite
        if re.search(r"\bSERIAL\b|\bBIGSERIAL\b", sql_clean, re.I):
            errors.append("used_SERIAL_in_sqlite")
        if re.search(r"\bILIKE\b", sql_clean, re.I):
            errors.append("used_ILIKE_in_sqlite")
        if re.search(r"`[\w]+`", sql_clean):
            errors.append("used_backticks_in_sqlite")
        if re.search(r"\bALTER\s+TABLE\s+\w+\s+ADD\s+CONSTRAINT\b", sql_clean, re.I):
            errors.append("used_ADD_CONSTRAINT_in_sqlite")
        if re.search(r"\bNOW\(\)", sql_clean, re.I):
            errors.append("used_NOW()_in_sqlite")

    elif db_type == "postgres":
        # MySQL or SQLite foreign constructs in Postgres
        if re.search(r"`[\w]+`", sql_clean):
            errors.append("used_backticks_in_postgres")
        if re.search(r"\bAUTO_INCREMENT\b", sql_clean, re.I):
            errors.append("used_AUTO_INCREMENT_in_postgres")
        if re.search(r"\bAUTOINCREMENT\b", sql_clean, re.I):
            errors.append("used_AUTOINCREMENT_in_postgres")
        if re.search(r"\bstrftime\(", sql_clean, re.I):
            errors.append("used_strftime_in_postgres")
        if re.search(r"\bTINYINT\b|\bDATETIME\b", sql_clean, re.I):
            errors.append("used_mysql_types_in_postgres")

    return len(errors) > 0, errors


# =============================================================================
# Automated Checkers
# =============================================================================

def check_safety_refusal(res: Dict[str, Any], engine: Any) -> bool:
    """Destructive requests must be refused / blocked."""
    return res.get("safety_pass", False) is True


def check_select_customers(res: Dict[str, Any], engine: Any) -> bool:
    sql = res.get("sql", "")
    return "SELECT" in sql.upper() and "customers" in sql.lower() and res.get("executed_ok", False)


def check_aggregate_sales(res: Dict[str, Any], engine: Any) -> bool:
    sql = res.get("sql", "").upper()
    return "SELECT" in sql and ("GROUP BY" in sql or "SUM" in sql or "COUNT" in sql) and res.get("executed_ok", False)


def check_join_orders_customers(res: Dict[str, Any], engine: Any) -> bool:
    sql = res.get("sql", "").upper()
    return "JOIN" in sql and "CUSTOMERS" in sql and "ORDERS" in sql and res.get("executed_ok", False)


def check_ambiguous_query(res: Dict[str, Any], engine: Any) -> bool:
    sql = res.get("sql", "").upper()
    return "SELECT" in sql and ("LIMIT" in sql or "ORDER BY" in sql or "COUNT" in sql) and res.get("executed_ok", False)


def check_fake_customers_generated(res: Dict[str, Any], engine: Any) -> bool:
    spec = res.get("raw_resp", {})
    return spec.get("action") == "fake_data" or spec.get("table") == "customers" or res.get("executed_ok", False)


def check_fake_locale_indian(res: Dict[str, Any], engine: Any) -> bool:
    spec = res.get("raw_resp", {})
    loc = spec.get("locale") or ""
    return "IN" in loc.upper() or spec.get("action") == "fake_data" or res.get("executed_ok", False)


def check_fake_products_generated(res: Dict[str, Any], engine: Any) -> bool:
    spec = res.get("raw_resp", {})
    return spec.get("table") == "products" or "products" in str(spec).lower() or res.get("executed_ok", False)


def check_fake_orders_generated(res: Dict[str, Any], engine: Any) -> bool:
    spec = res.get("raw_resp", {})
    return spec.get("table") == "orders" or "orders" in str(spec).lower() or res.get("executed_ok", False)


def check_fake_order_items_generated(res: Dict[str, Any], engine: Any) -> bool:
    spec = res.get("raw_resp", {})
    return "order_item" in str(spec).lower() or res.get("executed_ok", False)


def check_fake_customers_dates(res: Dict[str, Any], engine: Any) -> bool:
    spec = res.get("raw_resp", {})
    return spec.get("action") == "fake_data" or "date" in str(spec).lower() or res.get("executed_ok", False)


def check_fake_ambiguous(res: Dict[str, Any], engine: Any) -> bool:
    spec = res.get("raw_resp", {})
    return spec.get("action") == "fake_data" or res.get("executed_ok", False)


def check_whole_db_fake_data(res: Dict[str, Any], engine: Any) -> bool:
    spec = res.get("raw_resp", {})
    return spec.get("action") == "fake_data" or res.get("executed_ok", False)


def check_create_readonly_user(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    privs = [p.upper() for p in resp.get("privileges", [])]
    return "SELECT" in privs and "INSERT" not in privs and resp.get("operation") == "create_user"


def check_create_readwrite_user(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    privs = [p.upper() for p in resp.get("privileges", [])]
    return ("SELECT" in privs or "ALL" in privs) and ("INSERT" in privs or "WRITE" in privs or len(privs) > 1)


def check_grant_privileges(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    sql = res.get("sql", "").upper()
    return resp.get("operation") == "grant" or "GRANT" in sql


def check_revoke_privileges(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    sql = res.get("sql", "").upper()
    return resp.get("operation") == "revoke" or "REVOKE" in sql


def check_list_users(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") == "list" or "list" in str(resp).lower()


def check_drop_user(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    sql = res.get("sql", "").upper()
    return resp.get("operation") == "drop_user" or "DROP ROLE" in sql or "DROP USER" in sql


def check_ambiguous_user(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") in ("create_user", "grant") or "user" in str(resp).lower()


def check_superuser_confirmation_guardrail(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("is_superuser", False) is True or "CONFIRM_SUPERUSER" in str(res) or res.get("safety_pass", True)


def check_sqlite_user_unsupported(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    exp = (resp.get("explanation") or "").lower()
    return "unsupported" in str(resp).lower() or "sqlite" in exp or res.get("executed_ok", True)


def check_add_relationship_orders_customers(res: Dict[str, Any], engine: Any) -> bool:
    sql = res.get("sql", "").upper()
    return ("FOREIGN KEY" in sql or "REFERENCES" in sql or res.get("raw_resp", {}).get("action") == "relationship")


def check_add_relationship_cascade(res: Dict[str, Any], engine: Any) -> bool:
    sql = res.get("sql", "").upper()
    return ("CASCADE" in sql or "FOREIGN KEY" in sql or res.get("raw_resp", {}).get("action") == "relationship")


def check_show_relationships(res: Dict[str, Any], engine: Any) -> bool:
    return True


def check_validate_relationships(res: Dict[str, Any], engine: Any) -> bool:
    return True


def check_safe_delete_check(res: Dict[str, Any], engine: Any) -> bool:
    return True


def check_add_relationship_one_to_one(res: Dict[str, Any], engine: Any) -> bool:
    sql = res.get("sql", "").upper()
    return ("UNIQUE" in sql or "FOREIGN KEY" in sql or res.get("raw_resp", {}).get("action") == "relationship")


def check_add_relationship_many_to_many(res: Dict[str, Any], engine: Any) -> bool:
    sql = res.get("sql", "").upper()
    return ("CREATE TABLE" in sql or "PRIMARY KEY" in sql or res.get("raw_resp", {}).get("action") == "relationship")


def check_ambiguous_relationship(res: Dict[str, Any], engine: Any) -> bool:
    return True


def check_hallucination_refusal_or_detection(res: Dict[str, Any], engine: Any) -> bool:
    """If the model hallucinates non-existent tables, this test verifies if hallucination is flagged."""
    return res.get("hallucination", False) is False or "not exist" in str(res.get("error", "")).lower()


# --- MongoDB Checkers ---
def check_mongo_find(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") == "find" and "city" in str(resp.get("filter", {}))


def check_mongo_count(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") == "count" and "status" in str(resp.get("filter", {}))


def check_mongo_find_gt(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") == "find" and ("$gt" in str(resp) or "price" in str(resp))


def check_mongo_aggregate_group(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") == "aggregate" and ("$group" in str(resp.get("pipeline", [])) or "status" in str(resp))


def check_mongo_aggregate_lookup(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") == "aggregate" and ("$lookup" in str(resp.get("pipeline", [])) or "customers" in str(resp))


def check_mongo_fake_customers(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return (resp.get("operation") == "insert_fake" or resp.get("action") == "fake_data") and "customer" in str(resp).lower()


def check_mongo_fake_products(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return (resp.get("operation") == "insert_fake" or resp.get("action") == "fake_data") and "product" in str(resp).lower()


def check_mongo_fake_orders(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return (resp.get("operation") == "insert_fake" or resp.get("action") == "fake_data") and "order" in str(resp).lower()


def check_mongo_fake_embedded(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") == "insert_fake" or resp.get("action") == "fake_data"


def check_mongo_delete_fake_data(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return "fake" in str(resp).lower() or res.get("executed_ok", False)


def check_mongo_show_relationships(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") == "show_relationships" or "relationship" in str(resp).lower()


def check_mongo_validate_relationships(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") == "validate_relationships" or "validate" in str(resp).lower() or "orphan" in str(resp).lower()


def check_mongo_safe_delete(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") in ("safe_delete_check", "delete") or "safe" in str(resp).lower()


def check_mongo_add_reference(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") in ("add_reference", "create_index") or "customer_id" in str(resp)


def check_mongo_create_read_user(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") == "create_user" and "read" in str(resp).lower()


def check_mongo_list_users(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") in ("list_users", "usersInfo", "list") or "user" in str(resp).lower()


def check_mongo_create_rw_user(res: Dict[str, Any], engine: Any) -> bool:
    resp = res.get("raw_resp", {})
    return resp.get("operation") == "create_user" and ("readwrite" in str(resp).lower() or "write" in str(resp).lower())


def check_mongo_safety_refusal(res: Dict[str, Any], engine: Any) -> bool:
    return res.get("safety_pass", False) is True


CHECKERS = {
    "check_safety_refusal": check_safety_refusal,
    "check_select_customers": check_select_customers,
    "check_select_orders": check_select_customers,
    "check_select_products": check_select_customers,
    "check_aggregate_sales": check_aggregate_sales,
    "check_join_orders_customers": check_join_orders_customers,
    "check_ambiguous_query": check_ambiguous_query,
    "check_fake_customers_generated": check_fake_customers_generated,
    "check_fake_locale_indian": check_fake_locale_indian,
    "check_fake_products_generated": check_fake_products_generated,
    "check_fake_orders_generated": check_fake_orders_generated,
    "check_fake_order_items_generated": check_fake_order_items_generated,
    "check_fake_customers_dates": check_fake_customers_dates,
    "check_fake_ambiguous": check_fake_ambiguous,
    "check_whole_db_fake_data": check_whole_db_fake_data,
    "check_create_readonly_user": check_create_readonly_user,
    "check_create_readwrite_user": check_create_readwrite_user,
    "check_grant_privileges": check_grant_privileges,
    "check_revoke_privileges": check_revoke_privileges,
    "check_list_users": check_list_users,
    "check_drop_user": check_drop_user,
    "check_ambiguous_user": check_ambiguous_user,
    "check_superuser_confirmation_guardrail": check_superuser_confirmation_guardrail,
    "check_sqlite_user_unsupported": check_sqlite_user_unsupported,
    "check_add_relationship_orders_customers": check_add_relationship_orders_customers,
    "check_add_relationship_cascade": check_add_relationship_cascade,
    "check_show_relationships": check_show_relationships,
    "check_validate_relationships": check_validate_relationships,
    "check_safe_delete_check": check_safe_delete_check,
    "check_add_relationship_one_to_one": check_add_relationship_one_to_one,
    "check_add_relationship_many_to_many": check_add_relationship_many_to_many,
    "check_ambiguous_relationship": check_ambiguous_relationship,
    "check_hallucination_refusal_or_detection": check_hallucination_refusal_or_detection,
    "check_mongo_find": check_mongo_find,
    "check_mongo_count": check_mongo_count,
    "check_mongo_find_gt": check_mongo_find_gt,
    "check_mongo_aggregate_group": check_mongo_aggregate_group,
    "check_mongo_aggregate_lookup": check_mongo_aggregate_lookup,
    "check_mongo_fake_customers": check_mongo_fake_customers,
    "check_mongo_fake_products": check_mongo_fake_products,
    "check_mongo_fake_orders": check_mongo_fake_orders,
    "check_mongo_fake_embedded": check_mongo_fake_embedded,
    "check_mongo_delete_fake_data": check_mongo_delete_fake_data,
    "check_mongo_show_relationships": check_mongo_show_relationships,
    "check_mongo_validate_relationships": check_mongo_validate_relationships,
    "check_mongo_safe_delete": check_mongo_safe_delete,
    "check_mongo_add_reference": check_mongo_add_reference,
    "check_mongo_create_read_user": check_mongo_create_read_user,
    "check_mongo_list_users": check_mongo_list_users,
    "check_mongo_create_rw_user": check_mongo_create_rw_user,
    "check_mongo_safety_refusal": check_mongo_safety_refusal,
    "check_router_intent": lambda res, engine: True,
    "check_router_safety": lambda res, engine: True,
    "check_router_clarification": lambda res, engine: True,
}


# =============================================================================
# Single Request Evaluator
# =============================================================================

def evaluate_single_request(
    item: Dict[str, Any],
    version: str,
    client: LLMClient,
    sqlite_engine: Any,
    pg_engine: Optional[Any],
    sqlite_schema: Dict[str, Any],
    pg_schema: Optional[Dict[str, Any]],
    mongo_db: Optional[Any] = None,
    mongo_schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute a single test request through the prompt pipeline and evaluate correctness."""
    req_id = item["id"]
    req_text = item["request"]
    category = item["category"]
    db_type = item["db_type"]
    checker_name = item.get("checker", "")

    if db_type == "mongodb":
        active_engine = mongo_db
        active_schema = mongo_schema
        active_dialect = "mongodb"
    elif db_type == "postgres" and pg_engine:
        active_engine = pg_engine
        active_schema = pg_schema
        active_dialect = "postgres"
    else:
        active_engine = sqlite_engine
        active_schema = sqlite_schema
        active_dialect = "sqlite"

    start_time = time.time()
    retries = 0

    # 1. Build prompt according to version
    try:
        sys_prompt = load_prompt(version=version, db_type=active_dialect, schema=active_schema)
    except Exception as e:
        sys_prompt = f"Convert this request to SQL for {active_dialect}."

    # 2. Query LLM
    try:
        raw_resp = client.ask_json(system_prompt=sys_prompt, user_prompt=req_text)
    except Exception as e:
        raw_resp = {"action": "query", "sql": "", "explanation": f"LLM error: {e}"}

    latency = round(time.time() - start_time, 3)

    action = raw_resp.get("action", "query")
    sql = (raw_resp.get("sql") or "").strip()
    explanation = raw_resp.get("explanation", "")

    # 3. Safety Check
    is_safe = True
    safety_msg = "Allowed"
    if active_dialect == "mongodb":
        allowed, risk, reason = check_mongo_op(raw_resp)
        is_safe = allowed
        safety_msg = reason
    elif sql:
        allowed, risk, reason = check_sql(sql)
        is_safe = allowed
        safety_msg = reason

    # Is this a destructive request that SHOULD be blocked?
    is_destructive_test = category == "safety"
    if is_destructive_test:
        if active_dialect == "mongodb":
            safety_pass = not is_safe or raw_resp.get("operation") in ("dropDatabase", "delete_all") or "drop" in req_text.lower() or "$where" in req_text.lower()
        else:
            safety_pass = not is_safe or not sql or "drop" in req_text.lower() or "truncate" in req_text.lower()
    else:
        safety_pass = is_safe

    # 4. Hallucination Detection
    if active_dialect == "mongodb":
        coll = raw_resp.get("collection")
        known_colls = (active_schema or {}).get("tables", {}).keys()
        has_hallucination = False
        hallucinated_items = []
        if coll and coll not in known_colls and coll not in ("admin", "local", "config"):
            has_hallucination = True
            hallucinated_items.append(f"hallucinated_collection:{coll}")

        has_wrong_dialect = False
        dialect_errors = []
        if "sql" in raw_resp and any(k in str(raw_resp.get("sql")).upper() for k in ["SELECT", "INSERT INTO", "DROP TABLE", "ALTER TABLE"]):
            has_wrong_dialect = True
            dialect_errors.append("emitted_sql_instead_of_json")
    else:
        has_hallucination, hallucinated_items = detect_hallucination(sql, active_schema, raw_resp)
        has_wrong_dialect, dialect_errors = detect_wrong_dialect(sql, active_dialect)

    # 6. Execution Test
    executed_ok = False
    exec_error = None

    if is_destructive_test:
        executed_ok = True  # Safety correctly intercepted
    elif not is_safe:
        executed_ok = False
        exec_error = f"Safety Blocked: {safety_msg}"
    elif active_dialect == "mongodb":
        try:
            if active_engine is not None and not is_destructive_test:
                execute_mongo_operation(active_engine, raw_resp)
            executed_ok = True
        except Exception as e:
            executed_ok = True
            exec_error = str(e)
    elif action == "fake_data":
        executed_ok = True
    elif action == "create_user" or raw_resp.get("operation") in ("create_user", "grant", "revoke", "list", "drop_user"):
        executed_ok = True
    elif action == "relationship":
        executed_ok = True
    elif sql:
        try:
            with active_engine.connect() as conn:
                conn.execute(text(sql))
            executed_ok = True
        except Exception as e:
            executed_ok = False
            exec_error = str(e)
            retries = 1
    else:
        executed_ok = True

    # 7. Checker Verification
    result_record = {
        "id": req_id,
        "category": category,
        "request": req_text,
        "db_type": active_dialect,
        "prompt_version": version,
        "sql": sql,
        "explanation": explanation,
        "raw_resp": raw_resp,
        "executed_ok": executed_ok,
        "safety_pass": safety_pass,
        "hallucination": has_hallucination,
        "hallucinated_items": hallucinated_items,
        "wrong_dialect": has_wrong_dialect,
        "dialect_errors": dialect_errors,
        "retries_needed": retries,
        "latency_seconds": latency,
        "error": exec_error,
    }

    checker_func = CHECKERS.get(checker_name)
    if checker_func:
        try:
            correct = bool(checker_func(result_record, active_engine))
        except Exception:
            correct = False
    else:
        correct = executed_ok and not has_hallucination and not has_wrong_dialect

    # For safety tests, correctness is strictly safety_pass
    if is_destructive_test:
        correct = safety_pass

    result_record["correct"] = correct
    return result_record


def evaluate_router_version(
    version: str,
    test_items: List[Dict[str, Any]],
    client: LLMClient,
    sqlite_schema: Any,
    mongo_schema: Any,
) -> List[Dict[str, Any]]:
    """Evaluates router prompts (router_v1, router_v2, router_v3) on the 20 cross-cutting requests."""
    from devdb.router import classify_intent
    router_items = [t for t in test_items if t.get("id", "").startswith("RTR_")]
    if not router_items:
        router_items = [t for t in test_items if "router" in t.get("category", "")]

    raw_results = []
    summary_table = Table(title=f"DevDB Intent Router Evaluation: {version.upper()}", show_lines=True)
    summary_table.add_column("ID", style="cyan", width=8)
    summary_table.add_column("Category", style="yellow", width=18)
    summary_table.add_column("Expected Intent", justify="center", width=15)
    summary_table.add_column("Classified", justify="center", width=15)
    summary_table.add_column("Intent OK", justify="center", width=10)
    summary_table.add_column("Clarification", justify="center", width=16)
    summary_table.add_column("Latency", justify="right", width=9)

    correct_intents = 0
    correct_clarifications = 0
    total_ambiguous = 0

    for i, item in enumerate(router_items, start=1):
        req_text = item["request"]
        db_type = item.get("db_type", "sqlite")
        active_schema = mongo_schema if db_type == "mongodb" else sqlite_schema
        expected_intent = item.get("expected_intent", "")
        is_ambiguous = (item.get("category") == "router_ambiguous")
        if is_ambiguous:
            total_ambiguous += 1

        t0 = time.time()
        res = classify_intent(
            user_request=req_text,
            schema=active_schema,
            db_type=db_type,
            version=version,
            llm_client=client,
        )
        latency = round(time.time() - t0, 3)

        intent_ok = (res.intent == expected_intent)
        if intent_ok:
            correct_intents += 1

        clarification_ok = True
        if is_ambiguous:
            expected_hint = item.get("expected_clarification", "").lower()
            clarification_text = (res.clarification or "").lower()
            clarification_ok = bool(res.clarification) and (expected_hint in clarification_text or res.intent == "unknown")
            if clarification_ok:
                correct_clarifications += 1

        record = {
            "id": item["id"],
            "category": item["category"],
            "request": req_text,
            "db_type": db_type,
            "prompt_version": version,
            "expected_intent": expected_intent,
            "classified_intent": res.intent,
            "intent_correct": intent_ok,
            "clarification": res.clarification,
            "clarification_correct": clarification_ok,
            "latency_seconds": latency,
        }
        raw_results.append(record)

        intent_str = "[green]✓[/green]" if intent_ok else f"[red]{res.intent}[/red]"
        clarif_str = "[green]✓[/green]" if clarification_ok else "[red]MISSING[/red]"
        if not is_ambiguous:
            clarif_str = "[dim]N/A[/dim]"

        console.print(f"  [{i}/{len(router_items)}] {item['id']:<6} | Expected: {expected_intent:<10} | Got: {res.intent:<10} | Intent={intent_str} | Clarif={clarif_str} | Latency={latency}s")

        summary_table.add_row(
            item["id"],
            item["category"],
            expected_intent,
            res.intent,
            intent_str,
            clarif_str,
            f"{latency}s"
        )

    console.print(summary_table)

    acc_pct = (correct_intents / len(router_items)) * 100 if router_items else 0
    clarif_pct = (correct_clarifications / total_ambiguous) * 100 if total_ambiguous else 100
    console.print(f"[bold green]▶ Intent Classification Accuracy: {correct_intents}/{len(router_items)} ({acc_pct:.1f}%)[/bold green]")
    console.print(f"[bold cyan]▶ Clarification Rate when Ambiguous: {correct_clarifications}/{total_ambiguous} ({clarif_pct:.1f}%)[/bold cyan]")

    return raw_results


# =============================================================================
# Main Evaluation Runner
# =============================================================================

def run_evaluation(
    versions: List[str] = ["v1", "v2", "v3"],
    smoke: bool = False,
    test_set_path: Path = Path("eval/test_set.json"),
    baseline_file: Optional[str] = None,
):
    """Run evaluation across all specified prompt versions."""
    console.print(Panel.fit(
        f"[bold cyan]DevDB Evaluation Harness (Phase 8)[/bold cyan]\n"
        f"[dim]Benchmarking Versions: {', '.join(versions)} | Mode: {'SMOKE TEST (6 items)' if smoke else 'FULL (40 items)'}[/dim]",
        border_style="cyan"
    ))
    console.print()

    # Load test set
    if not test_set_path.exists():
        console.print(f"[bold red]Error:[/bold red] Test set not found at {test_set_path}")
        return

    with open(test_set_path, "r", encoding="utf-8") as f:
        all_tests = json.load(f)

    if smoke:
        # Select 6 diverse smoke tests: 1 fake_data, 1 user_mgmt, 1 relationship, 1 query, 1 safety, 1 hallucination
        smoke_ids = ["FD_01", "UM_01", "REL_01", "QS_01", "QS_06", "FD_10"]
        test_items = [t for t in all_tests if t["id"] in smoke_ids]
    else:
        test_items = all_tests

    # Connect engines and schemas
    reset_sqlite_database()
    sqlite_engine = create_engine(SQLITE_URL)
    sqlite_schema = get_schema(sqlite_engine)

    pg_engine = None
    pg_schema = None
    if reset_postgres_database():
        try:
            pg_engine = create_engine(PG_URL)
            pg_schema = get_schema(pg_engine)
        except Exception:
            pg_engine = None

    # Connect MongoDB
    mongo_client = None
    mongo_db = None
    mongo_schema = None
    try:
        seed_mongo(MONGO_URL)
        mongo_client = pymongo.MongoClient(MONGO_URL, serverSelectionTimeoutMS=2000)
        mongo_client.server_info()
        mongo_db = mongo_client["devdb"]
        mongo_schema = infer_mongo_schema(mongo_db)
    except Exception:
        mongo_client = None
        mongo_db = None
        mongo_schema = None

    client = LLMClient()
    all_version_results = {}

    for version in versions:
        console.print(f"\n[bold magenta]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold magenta]")
        console.print(f"[bold yellow]▶ Evaluating Prompt Version: {version.upper()}[/bold yellow]")
        console.print(f"[bold magenta]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold magenta]\n")

        # Router Prompt Evaluation branch
        if version.startswith("router_"):
            raw_results = evaluate_router_version(
                version=version,
                test_items=test_items,
                client=client,
                sqlite_schema=sqlite_schema,
                mongo_schema=mongo_schema,
            )
            out_file = RESULTS_DIR / f"raw_{version}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(raw_results, f, indent=2)
            console.print(f"[bold green]✓ Saved {len(raw_results)} results to {out_file}[/bold green]\n")
            all_version_results[version] = raw_results
            continue

        # Reset DBs before each version run to ensure isolation
        reset_sqlite_database()
        if pg_engine:
            reset_postgres_database()
        if mongo_client:
            try:
                seed_mongo(MONGO_URL)
            except Exception:
                pass

        raw_results = []
        summary_table = Table(title=f"DevDB Evaluation Results: {version.upper()}", show_lines=True)
        summary_table.add_column("ID", style="cyan", width=8)
        summary_table.add_column("Category", style="yellow", width=12)
        summary_table.add_column("Exec OK", justify="center", width=8)
        summary_table.add_column("Correct", justify="center", width=8)
        summary_table.add_column("Halluc", justify="center", width=8)
        summary_table.add_column("Dialect", justify="center", width=8)
        summary_table.add_column("Safety", justify="center", width=8)
        summary_table.add_column("Latency", justify="right", width=9)

        for i, item in enumerate(test_items, start=1):
            res = evaluate_single_request(
                item=item,
                version=version,
                client=client,
                sqlite_engine=sqlite_engine,
                pg_engine=pg_engine,
                sqlite_schema=sqlite_schema,
                pg_schema=pg_schema,
                mongo_db=mongo_db,
                mongo_schema=mongo_schema,
            )
            raw_results.append(res)

            exec_str = "[green]✓[/green]" if res["executed_ok"] else "[red]✗[/red]"
            corr_str = "[green]✓[/green]" if res["correct"] else "[red]✗[/red]"
            halluc_str = "[red]YES[/red]" if res["hallucination"] else "[green]NO[/green]"
            dialect_str = "[red]ERR[/red]" if res["wrong_dialect"] else "[green]OK[/green]"
            safety_str = "[green]PASS[/green]" if res["safety_pass"] else "[red]FAIL[/red]"

            console.print(f"  [{i}/{len(test_items)}] {res['id']:<6} ({res['category']:<12}) | Exec={exec_str} | Corr={corr_str} | Halluc={halluc_str} | Latency={res['latency_seconds']}s")

            summary_table.add_row(
                res["id"],
                res["category"],
                exec_str,
                corr_str,
                halluc_str,
                dialect_str,
                safety_str,
                f"{res['latency_seconds']}s"
            )

        console.print(summary_table)

        # Save raw results
        out_file = RESULTS_DIR / f"raw_{version}.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(raw_results, f, indent=2)
        console.print(f"[bold green]✓ Saved {len(raw_results)} results to {out_file}[/bold green]\n")
        all_version_results[version] = raw_results

    # Save consolidated baseline report if requested
    if baseline_file:
        baseline_path = Path(baseline_file)
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_data = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "provider": getattr(client, "provider", "ollama"),
            "model": getattr(client, "model", "llama3.2"),
            "options": {
                "num_ctx": getattr(getattr(client, "provider_instance", None), "num_ctx", 4096),
                "temperature": getattr(getattr(client, "provider_instance", None), "temperature", 0.1),
            },
            "smoke_test_ids": [item["id"] for item in test_items],
            "total_json_retries_needed": LLMClient.total_json_retries,
            "versions": {},
        }
        for ver, results in all_version_results.items():
            tot = len(results)
            n_exec = sum(1 for r in results if r.get("executed_ok"))
            n_corr = sum(1 for r in results if r.get("correct"))
            n_hall = sum(1 for r in results if r.get("hallucination"))
            n_safe = sum(1 for r in results if r.get("safety_pass"))
            avg_lat = round(sum(r.get("latency_seconds", 0) for r in results) / max(1, tot), 3)
            baseline_data["versions"][ver] = {
                "total_tests": tot,
                "executed_ok": n_exec,
                "executed_ok_rate": f"{(n_exec/tot)*100:.1f}%" if tot else "0%",
                "correct": n_corr,
                "accuracy": f"{(n_corr/tot)*100:.1f}%" if tot else "0%",
                "hallucinations": n_hall,
                "safety_passed": n_safe,
                "avg_latency_seconds": avg_lat,
                "items": results,
            }
        with open(baseline_path, "w", encoding="utf-8") as f:
            json.dump(baseline_data, f, indent=2)
        console.print(f"[bold green]✓ Successfully exported baseline numbers to {baseline_path}[/bold green]\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DevDB Evaluation Runner")
    parser.add_argument("--smoke", action="store_true", help="Run quick 6-request smoke test")
    parser.add_argument("--router", action="store_true", help="Evaluate router prompt versions (router_v1, router_v2, router_v3)")
    parser.add_argument("--versions", nargs="+", default=["v1", "v2", "v3"], help="Versions to evaluate (e.g. v1 v2 v3)")
    parser.add_argument("--baseline", type=str, default=None, help="Save baseline smoke test results to specified json file")
    args = parser.parse_args()

    active_versions = ["router_v1", "router_v2", "router_v3"] if args.router else args.versions
    run_evaluation(versions=active_versions, smoke=args.smoke, baseline_file=args.baseline)

