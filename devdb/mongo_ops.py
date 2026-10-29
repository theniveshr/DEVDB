"""MongoDB structured JSON operation dispatcher and safe PyMongo execution engine.

Converts declarative JSON operations into parameterized PyMongo calls.
Never uses eval(), exec(), or raw JavaScript execution.
"""

import json
from typing import Any, Dict, List, Optional, Tuple, Union
import pymongo
from bson import ObjectId

from devdb.safety import check_mongo_op, SafetyViolationError


def format_doc_for_display(doc: Any) -> Any:
    """Recursively convert ObjectId and datetime instances to strings for terminal formatting."""
    if isinstance(doc, ObjectId):
        return str(doc)
    if hasattr(doc, "isoformat"):
        return doc.isoformat()
    if isinstance(doc, dict):
        return {k: format_doc_for_display(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [format_doc_for_display(item) for item in doc]
    return doc


def to_mongosh_syntax(op: Dict[str, Any]) -> str:
    """
    Generate equivalent mongosh command string for terminal display and learning.
    This string is NEVER executed; it is for display only.
    """
    operation = str(op.get("operation") or op.get("action") or "").lower()
    coll = op.get("collection", "collection")
    filter_spec = op.get("filter", {})
    filt_str = json.dumps(filter_spec) if filter_spec else "{}"

    if operation == "find":
        proj = op.get("projection")
        proj_str = f", {json.dumps(proj)}" if proj else ""
        lim = op.get("limit", 50)
        return f"db.{coll}.find({filt_str}{proj_str}).limit({lim})"
    elif operation == "count":
        return f"db.{coll}.countDocuments({filt_str})"
    elif operation == "aggregate":
        pipe = op.get("pipeline", [])
        return f"db.{coll}.aggregate({json.dumps(pipe, indent=2)})"
    elif operation == "update":
        upd = op.get("update", {})
        return f"db.{coll}.updateMany({filt_str}, {json.dumps(upd)})"
    elif operation == "delete":
        return f"db.{coll}.deleteMany({filt_str})"
    elif operation == "create_index":
        keys = op.get("keys", {})
        opts = f", {{unique: true}}" if op.get("unique") else ""
        return f"db.{coll}.createIndex({json.dumps(keys)}{opts})"
    elif operation in ("insert", "insert_one", "insert_many"):
        docs = op.get("documents")
        if docs:
            return f"db.{coll}.insertMany({json.dumps(docs, default=str)})"
        doc = op.get("document") or op.get("columns") or op.get("data")
        if doc:
            return f"db.{coll}.insertOne({json.dumps(doc, default=str)})"
        return f"db.{coll}.insertOne({{}})"
    elif operation == "insert_fake":
        cnt = op.get("count", 10)
        return f"// DevDB Synthetic Batch: generate and insert {cnt} documents into '{coll}'"
    elif operation == "create_user":
        u = op.get("username", "user")
        roles = op.get("roles", [{"role": "read", "db": "devdb"}])
        return f"db.createUser({{user: '{u}', pwd: '***', roles: {json.dumps(roles)}}})"
    elif operation in ("list_users", "list"):
        return "db.getUsers()"
    elif operation in ("grant_role", "grant"):
        u = op.get("username", "user")
        roles = op.get("roles", [{"role": "read", "db": "devdb"}])
        return f"db.grantRolesToUser('{u}', {json.dumps(roles)})"
    elif operation in ("revoke_role", "revoke"):
        u = op.get("username", "user")
        roles = op.get("roles", [{"role": "read", "db": "devdb"}])
        return f"db.revokeRolesFromUser('{u}', {json.dumps(roles)})"
    elif operation == "drop_user":
        u = op.get("username", "user")
        return f"db.dropUser('{u}')"
    elif operation in ("show_relationships", "validate_relationships", "add_reference"):
        return f"// DevDB Relationship Engine: {operation} on '{coll}'"

    return f"db.{coll}.{operation}({filt_str})"


def execute_mongo_operation(db: pymongo.database.Database, op: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a validated structured JSON operation against a live PyMongo database.
    """
    allowed, risk, reason = check_mongo_op(op)
    if not allowed:
        raise SafetyViolationError(f"Safety Violation: {reason}")

    operation = str(op.get("operation") or op.get("action") or "").lower()
    coll_name = op.get("collection")
    coll = db[coll_name] if coll_name else None

    # 1. READ: find
    if operation == "find":
        if coll is None:
            raise ValueError("Operation 'find' requires a target collection.")
        filter_spec = op.get("filter", {})
        projection = op.get("projection")
        limit = min(int(op.get("limit", 50)), 100)

        cursor = coll.find(filter_spec, projection).limit(limit)
        docs = [format_doc_for_display(doc) for doc in cursor]
        return {
            "success": True,
            "operation": "find",
            "collection": coll_name,
            "count": len(docs),
            "documents": docs,
            "mongosh": to_mongosh_syntax(op),
        }

    # 2. READ: count
    elif operation == "count":
        if coll is None:
            raise ValueError("Operation 'count' requires a target collection.")
        filter_spec = op.get("filter", {})
        cnt = coll.count_documents(filter_spec)
        return {
            "success": True,
            "operation": "count",
            "collection": coll_name,
            "count": cnt,
            "mongosh": to_mongosh_syntax(op),
        }

    # 3. READ: aggregate
    elif operation == "aggregate":
        if coll is None:
            raise ValueError("Operation 'aggregate' requires a target collection.")
        pipeline = op.get("pipeline", [])
        if not isinstance(pipeline, list):
            raise ValueError("Operation 'aggregate' requires a list of pipeline stages.")

        cursor = coll.aggregate(pipeline)
        docs = [format_doc_for_display(doc) for doc in cursor]
        return {
            "success": True,
            "operation": "aggregate",
            "collection": coll_name,
            "count": len(docs),
            "documents": docs,
            "mongosh": to_mongosh_syntax(op),
        }

    # 4. WRITE: insert / insert_one / insert_many
    elif operation in ("insert", "insert_one", "insert_many"):
        if coll is None:
            raise ValueError(f"Operation '{operation}' requires a target collection.")

        docs_to_insert: List[Dict[str, Any]] = []
        if "documents" in op and isinstance(op["documents"], list):
            docs_to_insert = [dict(d) for d in op["documents"]]
        elif "document" in op and isinstance(op["document"], dict):
            docs_to_insert = [dict(op["document"])]
        elif "columns" in op and isinstance(op["columns"], dict):
            docs_to_insert = [dict(op["columns"])]
        elif "data" in op:
            d = op["data"]
            if isinstance(d, list):
                docs_to_insert = [dict(x) for x in d]
            elif isinstance(d, dict):
                docs_to_insert = [dict(d)]

        if not docs_to_insert:
            raise ValueError(f"Operation '{operation}' requires 'document', 'documents', or 'columns'.")

        from datetime import datetime, timezone
        for doc in docs_to_insert:
            for k, v in list(doc.items()):
                if isinstance(v, str) and v.lower() in ("currentdate", "now", "current_timestamp"):
                    doc[k] = datetime.now(timezone.utc)

        res = coll.insert_many(docs_to_insert)
        inserted_ids = [str(i) for i in res.inserted_ids]

        # Record in fake data tracker so session rollback / undo works cleanly
        try:
            from devdb.actions.fake_data import track_inserted_keys
            track_inserted_keys(coll_name, inserted_ids)
        except Exception:
            pass

        return {
            "success": True,
            "operation": operation,
            "collection": coll_name,
            "inserted_count": len(inserted_ids),
            "inserted_ids": inserted_ids,
            "mongosh": to_mongosh_syntax(op),
        }

    # 5. WRITE: update
    elif operation == "update":
        if coll is None:
            raise ValueError("Operation 'update' requires a target collection.")
        filter_spec = op.get("filter", {})
        update_spec = op.get("update", {})
        if not any(k.startswith("$") for k in update_spec.keys()):
            update_spec = {"$set": update_spec}

        res = coll.update_many(filter_spec, update_spec)
        return {
            "success": True,
            "operation": "update",
            "collection": coll_name,
            "matched_count": res.matched_count,
            "modified_count": res.modified_count,
            "mongosh": to_mongosh_syntax(op),
        }

    # 5. WRITE: delete
    elif operation == "delete":
        if coll is None:
            raise ValueError("Operation 'delete' requires a target collection.")
        filter_spec = op.get("filter", {})
        res = coll.delete_many(filter_spec)
        return {
            "success": True,
            "operation": "delete",
            "collection": coll_name,
            "deleted_count": res.deleted_count,
            "mongosh": to_mongosh_syntax(op),
        }

    # 6. DDL: create_index
    elif operation == "create_index":
        if coll is None:
            raise ValueError("Operation 'create_index' requires a target collection.")
        keys = op.get("keys", {})
        if isinstance(keys, dict):
            key_tuples = [(k, int(v)) for k, v in keys.items()]
        elif isinstance(keys, list):
            key_tuples = [(k, 1) for k in keys]
        elif isinstance(keys, str):
            key_tuples = [(keys, 1)]
        else:
            raise ValueError("Invalid index key specification.")

        unique = bool(op.get("unique", False))
        idx_name = coll.create_index(key_tuples, unique=unique)
        return {
            "success": True,
            "operation": "create_index",
            "collection": coll_name,
            "index_name": idx_name,
            "keys": key_tuples,
            "unique": unique,
            "mongosh": to_mongosh_syntax(op),
        }

    # 7. Synthetic Fake Data
    elif operation == "insert_fake":
        from devdb.actions.mongo_fake_data import generate_mongo_fake_data
        return generate_mongo_fake_data(db, op)

    # 8. Relationship Actions
    elif operation in ("add_reference", "show_relationships", "validate_relationships"):
        from devdb.actions.mongo_relationships import handle_mongo_relationship_op
        return handle_mongo_relationship_op(db, op)

    # 9. User & Role Management Actions
    elif operation in ("create_user", "grant_role", "revoke_role", "list_users", "drop_user", "list", "grant", "revoke"):
        from devdb.actions.mongo_users import handle_mongo_user_op
        return handle_mongo_user_op(db, op)

    raise ValueError(f"Unknown MongoDB operation: '{operation}'")
