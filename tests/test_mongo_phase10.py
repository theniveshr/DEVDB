"""Unit and integration test suite for Phase 10 MongoDB Support."""

import pytest
import pymongo
from bson import ObjectId

from devdb.safety import is_dev_connection, check_mongo_op
from devdb.mongo_schema import infer_mongo_schema, mongo_schema_to_text
from devdb.mongo_ops import to_mongosh_syntax, execute_mongo_operation
from devdb.actions.mongo_relationships import (
    validate_relationships_mongo,
    build_mongo_ascii_map,
    render_mongo_rich_tree,
    safe_delete_check_mongo,
)
from devdb.actions.fake_data import clear_tracked_keys
from devdb.actions.mongo_fake_data import (
    generate_mongo_fake_data,
    delete_mongo_session_fake_data,
)
from devdb.actions.mongo_users import (
    create_mongo_user,
    list_mongo_users,
    drop_mongo_user,
    generate_mongo_password,
    validate_mongo_username,
)


@pytest.fixture(scope="module")
def real_mongo_db():
    """Connect to native local MongoDB running on localhost:27017."""
    try:
        client = pymongo.MongoClient("mongodb://localhost:27017/devdb", serverSelectionTimeoutMS=2000)
        client.server_info()
        return client["devdb"]
    except Exception as e:
        pytest.skip(f"Local MongoDB not available: {e}")


# 1. Connection Safety Tests
def test_mongo_connection_safety_allowed():
    assert is_dev_connection("mongodb://localhost:27017/devdb") is True
    assert is_dev_connection("mongodb://127.0.0.1:27017/devdb") is True
    assert is_dev_connection("mongodb://dev:dev@localhost:27017/devdb") is True
    assert is_dev_connection("mongodb://dev:dev@127.0.0.1:27017/devdb") is True


def test_mongo_connection_safety_blocked():
    assert is_dev_connection("mongodb+srv://cluster0.abc.mongodb.net/test") is False
    assert is_dev_connection("mongodb://production.server.com:27017/prod") is False
    assert is_dev_connection("mongodb://192.168.1.100:27017/devdb") is False


# 2. Structured Operation Safety Tests
def test_check_mongo_op_allowed():
    allowed, risk, reason = check_mongo_op({
        "operation": "find",
        "collection": "customers",
        "filter": {"city": "Chennai"}
    })
    assert allowed is True
    assert risk == "LOW"

    allowed, risk, reason = check_mongo_op({
        "operation": "update",
        "collection": "customers",
        "filter": {"email": "user@example.com"},
        "update": {"$set": {"status": "active"}}
    })
    assert allowed is True
    assert risk in ("MEDIUM", "HIGH")

    # Insert operation check
    allowed, risk, reason = check_mongo_op({
        "operation": "insert",
        "collection": "users",
        "columns": {"name": "paul", "email": "paul1@gmail.com", "role": "customer"}
    })
    assert allowed is True
    assert risk == "MEDIUM"


def test_check_mongo_op_blocked_drop_database():
    allowed, risk, reason = check_mongo_op({
        "operation": "dropDatabase",
        "explanation": "drop the database"
    })
    assert allowed is False
    assert "strictly forbidden" in reason


def test_check_mongo_op_blocked_empty_filter():
    allowed, risk, reason = check_mongo_op({
        "operation": "delete",
        "collection": "orders",
        "filter": {}
    })
    assert allowed is False
    assert "empty" in reason.lower()

    allowed, risk, reason = check_mongo_op({
        "operation": "update",
        "collection": "orders",
        "filter": {},
        "update": {"$set": {"status": "cancelled"}}
    })
    assert allowed is False
    assert "empty" in reason.lower()


def test_check_mongo_op_blocked_raw_javascript():
    # $where injection
    allowed, risk, reason = check_mongo_op({
        "operation": "find",
        "collection": "customers",
        "filter": {"$where": "function() { return true; }"}
    })
    assert allowed is False
    assert "javascript" in reason.lower()

    # $function injection
    allowed, risk, reason = check_mongo_op({
        "operation": "find",
        "collection": "customers",
        "filter": {"expr": {"$function": {"body": "return true;"}}}
    })
    assert allowed is False
    assert "javascript" in reason.lower()


# 3. mongosh Syntax Generation Tests
def test_to_mongosh_syntax():
    cmd = to_mongosh_syntax({
        "operation": "find",
        "collection": "orders",
        "filter": {"status": "pending"},
        "limit": 10
    })
    assert 'db.orders.find({"status": "pending"}).limit(10)' in cmd

    cmd2 = to_mongosh_syntax({
        "operation": "count",
        "collection": "customers",
        "filter": {"city": "Delhi"}
    })
    assert 'db.customers.countDocuments({"city": "Delhi"})' in cmd2

    cmd3 = to_mongosh_syntax({
        "operation": "insert",
        "collection": "users",
        "columns": {"name": "paul", "email": "paul1@gmail.com"}
    })
    assert 'db.users.insertOne(' in cmd3
    assert 'paul1@gmail.com' in cmd3


def test_execute_mongo_insert(real_mongo_db):
    real_mongo_db.users.delete_many({"email": "paul1@gmail.com"})
    op = {
        "operation": "insert",
        "collection": "users",
        "columns": {
            "name": "paul",
            "email": "paul1@gmail.com",
            "role": "customer",
            "created_at": "currentDate"
        }
    }
    res = execute_mongo_operation(real_mongo_db, op)
    assert res["success"] is True
    assert res["inserted_count"] == 1
    # Cleanup inserted document
    real_mongo_db.users.delete_one({"email": "paul1@gmail.com"})


# 4. Schema Inference and Reference Detection Tests
def test_mongo_schema_inference(real_mongo_db):
    schema = infer_mongo_schema(real_mongo_db)
    tables = schema.get("tables", {})
    assert "customers" in tables
    assert "products" in tables
    assert "orders" in tables

    # Verify orders reference to customers._id
    orders_info = tables["orders"]
    fks = orders_info.get("foreign_keys", [])
    assert any(fk.get("referred_table") == "customers" for fk in fks)

    # Verify schema to text formatting
    text_summary = mongo_schema_to_text(schema)
    assert "Collection: customers" in text_summary
    assert "Collection: orders" in text_summary
    assert "customers._id" in text_summary


# 5. Relationship Health and Orphan Detection Tests
def test_validate_relationships_mongo_finds_orphans(real_mongo_db):
    report = validate_relationships_mongo(real_mongo_db)
    # seed_mongo created 2 intentional orphans in orders
    orphans = report.get("orphan_references", [])
    assert len(orphans) > 0
    order_orphans = [o for o in orphans if o["child_collection"] == "orders"]
    assert len(order_orphans) == 1
    assert order_orphans[0]["orphan_count"] >= 2


def test_build_mongo_ascii_map(real_mongo_db):
    schema = infer_mongo_schema(real_mongo_db)
    ascii_map = build_mongo_ascii_map(schema)
    assert "orders" in ascii_map
    assert "customers" in ascii_map


# 6. Safe Delete Check Tests
def test_safe_delete_check_mongo_blocks_parent_with_children(real_mongo_db):
    # Find a customer that has orders
    sample_order = real_mongo_db.orders.find_one({"customer_id": {"$exists": True}})
    if sample_order:
        cid = sample_order["customer_id"]
        res = safe_delete_check_mongo(real_mongo_db, "customers", {"_id": cid})
        assert res["allowed"] is False
        assert len(res["impacts"]) > 0


# 7. User Management Tests
def test_mongo_user_creation_and_listing(real_mongo_db):
    username = "test_phase10_user"
    pwd = generate_mongo_password()
    try:
        # Create
        res = create_mongo_user(real_mongo_db, username=username, roles="read", password=pwd)
        assert res["success"] is True
        assert res["username"] == username

        # List
        users = list_mongo_users(real_mongo_db)
        user_names = [u.get("user") for u in users]
        assert username in user_names
    finally:
        # Cleanup
        try:
            drop_mongo_user(real_mongo_db, username)
        except Exception:
            pass


def test_validate_mongo_username():
    assert validate_mongo_username("valid_user123") == "valid_user123"
    with pytest.raises(ValueError):
        validate_mongo_username("invalid user; drop")


# 8. Fake Data Generation and Rollback Tests
def test_generate_and_delete_mongo_fake_data(real_mongo_db):
    clear_tracked_keys()
    op = {
        "operation": "insert_fake",
        "collection": "customers",
        "count": 5,
        "locale": "en_IN",
        "columns": {
            "name": "faker:name",
            "city": "choice:Mumbai,Delhi,Chennai,Bangalore",
            "tier": "choice:silver,gold",
        }
    }
    initial_count = real_mongo_db.customers.count_documents({})
    gen_res = generate_mongo_fake_data(real_mongo_db, op)
    assert gen_res["success"] is True
    assert gen_res["inserted_count"] == 5
    assert real_mongo_db.customers.count_documents({}) == initial_count + 5

    # Delete session fake data
    del_res = delete_mongo_session_fake_data(real_mongo_db)
    assert del_res.get("customers", 0) >= 5
    assert real_mongo_db.customers.count_documents({}) == initial_count


def test_mongo_user_list_operation_allowed():
    """Verify 'list' operation passes safety check and maps to db.getUsers()."""
    allowed, risk, reason = check_mongo_op({"operation": "list"})
    assert allowed is True
    assert risk == "LOW"
    assert to_mongosh_syntax({"operation": "list"}) == "db.getUsers()"


def test_mongo_grant_revoke_operations_allowed():
    """Verify 'grant' and 'revoke' operations pass safety check."""
    from devdb.safety import SessionMode
    # In ADMIN mode, grant is permitted
    allowed, risk, reason = check_mongo_op({
        "operation": "grant",
        "username": "test_user",
        "roles": "read"
    }, current_mode=SessionMode.ADMIN)
    assert allowed is True
    assert risk == "HIGH"

    # In READ_WRITE mode, grant requires admin mode
    allowed, risk, reason = check_mongo_op({
        "operation": "grant",
        "username": "test_user",
        "roles": "read"
    }, current_mode=SessionMode.READ_WRITE)
    assert allowed is False
    assert "requires admin mode" in reason.lower()


def test_mongo_handle_request_list_users_no_string_error(real_mongo_db):
    """Verify handle_mongo_request renders users table without 'str object has no attribute get'."""
    from devdb.cli import DevDBSession
    session = DevDBSession()
    session.mongo_db = real_mongo_db
    session.db_type = "MONGODB"
    # Should execute and format cleanly without raising AttributeError
    success = session.handle_mongo_request("show users", {"operation": "list_users"})
    assert success is True

    # Also works with 'list' operation alias
    success_alias = session.handle_mongo_request("list users", {"operation": "list"})
    assert success_alias is True


def test_router_users_count_intent():
    """Verify 'users count' and 'count users' are routed to count exploration."""
    from devdb.router import classify_deterministic
    r1 = classify_deterministic("users count")
    assert r1 is not None
    assert r1.intent == "explore"
    assert r1.params.get("action") == "count"
    assert r1.params.get("entity") == "users"

    r2 = classify_deterministic("count users")
    assert r2 is not None
    assert r2.intent == "explore"
    assert r2.params.get("action") == "count"
    assert r2.params.get("entity") == "users"

    r3 = classify_deterministic("how many orders")
    assert r3 is not None
    assert r3.intent == "explore"
    assert r3.params.get("action") == "count"
    assert r3.params.get("entity") == "orders"


def test_show_all_users_disambiguation(real_mongo_db):
    """Verify 'show all users' queries the 'users' collection when it exists, while 'list_users' queries auth users."""
    from devdb.router import classify_deterministic
    existing_tables = ["customers", "orders", "products", "users"]

    # When 'users' collection exists, 'show all users' is a collection query
    res = classify_deterministic("show all users", existing_entities=existing_tables)
    assert res is not None
    assert res.intent == "query"
    assert res.target_entity == "users"

    # Explicit database users request routes to auth user management
    res_db = classify_deterministic("show database users", existing_entities=existing_tables)
    assert res_db is not None
    assert res_db.intent == "users"

    res_db2 = classify_deterministic("list_users", existing_entities=existing_tables)
    assert res_db2 is not None
    assert res_db2.intent == "users"

    # When no 'users' collection exists, generic 'list users' falls back to user management
    res_fallback = classify_deterministic("show all users", existing_entities=["orders", "products"])
    assert res_fallback is not None
    assert res_fallback.intent == "users"

