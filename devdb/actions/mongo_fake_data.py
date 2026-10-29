"""Synthetic fake data generation action for MongoDB collections.

Supports:
- Declarative JSON specs with Faker rules
- Nested sub-documents (e.g. address: {city: ..., pincode: ...})
- Dynamic array generation (e.g. tags: list:choice:vip,new:1:3)
- Valid parent references (e.g. ref:customers._id sampling real ObjectIds)
- Batch insertion via insert_many
- Session tracking in logs/fake_data_tracker.json for rollback / undo
"""

import os
import json
import random
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from faker import Faker
import pymongo
from bson import ObjectId

from devdb.actions.fake_data import _get_faker_instance, track_inserted_keys, get_tracked_keys, clear_tracked_keys

TRACKER_FILE = Path("logs/fake_data_tracker.json")


def _resolve_mongo_rule_value(
    rule: Any,
    fake: Faker,
    db: pymongo.database.Database,
    parent_cache: Dict[str, List[Any]],
) -> Any:
    """Recursively resolve a fake data rule into concrete Python/BSON data."""
    if isinstance(rule, dict):
        return {
            k: _resolve_mongo_rule_value(v, fake, db, parent_cache)
            for k, v in rule.items()
        }

    rule_str = str(rule).strip()

    # 1. Parent Reference: ref:collection.field (e.g. ref:customers._id)
    if rule_str.startswith("ref:"):
        target = rule_str[4:].strip()
        parts = target.split(".", 1)
        parent_coll = parts[0]
        parent_field = parts[1] if len(parts) > 1 else "_id"

        if parent_coll not in parent_cache:
            try:
                cursor = db[parent_coll].find({}, {parent_field: 1}).limit(200)
                vals = [d.get(parent_field) for d in cursor if d.get(parent_field) is not None]
                parent_cache[parent_coll] = vals
            except Exception:
                parent_cache[parent_coll] = []

        existing_vals = parent_cache[parent_coll]
        if existing_vals:
            chosen = random.choice(existing_vals)
            return str(chosen) if parent_field == "_id" and isinstance(chosen, ObjectId) else chosen
        return str(ObjectId())

    # 2. List of choices: list:choice:a,b,c:min:max
    if rule_str.startswith("list:choice:"):
        payload = rule_str[12:].strip()
        parts = payload.split(":")
        choices = [c.strip() for c in parts[0].split(",")]
        min_items = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
        max_items = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 3
        count = random.randint(min_items, max(min_items, max_items))
        return random.sample(choices, min(count, len(choices))) if len(choices) >= count else random.choices(choices, k=count)

    # 3. Single Choice: choice:a,b,c
    if rule_str.startswith("choice:"):
        choices = [c.strip() for c in rule_str[7:].split(",")]
        return random.choice(choices)

    # 4. Integer Range: int_range:min:max
    if rule_str.startswith("int_range:"):
        parts = rule_str[10:].split(":")
        low = int(parts[0]) if len(parts) > 0 and parts[0].lstrip("-").isdigit() else 1
        high = int(parts[1]) if len(parts) > 1 and parts[1].lstrip("-").isdigit() else 100
        return random.randint(low, high)

    # 5. Price / Float: price:min:max or faker:price
    if rule_str.startswith("price:") or rule_str == "faker:price":
        parts = rule_str[6:].split(":") if rule_str.startswith("price:") else []
        low = float(parts[0]) if len(parts) > 0 else 100.0
        high = float(parts[1]) if len(parts) > 1 else 50000.0
        return round(random.uniform(low, high), 2)

    # 6. Standard Faker methods
    if rule_str.startswith("faker:"):
        meth = rule_str[6:].strip()
        if hasattr(fake, meth):
            try:
                return getattr(fake, meth)()
            except Exception:
                pass
        if meth in ("name", "full_name"):
            return fake.name()
        elif meth == "email":
            return fake.email()
        elif meth in ("phone", "phone_number"):
            return fake.phone_number()
        elif meth == "city":
            return fake.city()
        elif meth in ("postcode", "pincode", "zipcode"):
            return fake.postcode()
        elif meth in ("address", "street_address"):
            return fake.address()
        elif meth in ("company", "company_name"):
            return fake.company()
        elif meth in ("word", "text"):
            return fake.word()

    # Fallback to direct string or word
    return rule_str


def generate_mongo_fake_data(
    db: pymongo.database.Database,
    op: Dict[str, Any],
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Generate synthetic documents conforming to the spec, preview, and batch-insert.
    """
    coll_name = op.get("collection", "customers")
    count = max(1, min(int(op.get("count", 10)), 500))
    locale = op.get("locale") or "en_US"
    if "indian" in str(op).lower() or "chennai" in str(op).lower() or "delhi" in str(op).lower():
        locale = "en_IN"

    fake = _get_faker_instance(locale)
    columns_spec = op.get("columns") or op.get("fields") or {}

    # Default schemas if columns are omitted by LLM
    if not columns_spec:
        if coll_name in ("customers", "users"):
            columns_spec = {
                "name": "faker:name",
                "email": "faker:email",
                "city": "faker:city",
                "phone": "faker:phone_number",
                "tier": "choice:bronze,silver,gold,platinum",
                "created_at": "faker:date_time_this_year",
            }
        elif coll_name == "products":
            columns_spec = {
                "name": "faker:word",
                "category": "choice:Electronics,Furniture,Accessories,Audio",
                "price": "price:500:50000",
                "stock": "int_range:10:200",
            }
        elif coll_name == "orders":
            columns_spec = {
                "order_number": "faker:uuid4",
                "customer_id": "ref:customers._id",
                "status": "choice:pending,completed,shipped,cancelled",
                "total_amount": "price:500:25000",
                "items": [
                    {"item": "faker:word", "qty": "int_range:1:4", "price": "price:200:5000"}
                ],
            }
        else:
            columns_spec = {
                "name": "faker:name",
                "email": "faker:email",
                "created_at": "faker:date_time_this_year",
            }

    parent_cache: Dict[str, List[Any]] = {}
    documents: List[Dict[str, Any]] = []

    for _ in range(count):
        doc: Dict[str, Any] = {}
        for field, rule in columns_spec.items():
            if isinstance(rule, list):
                # Array of sub-documents
                doc[field] = [
                    _resolve_mongo_rule_value(item, fake, db, parent_cache)
                    for item in rule
                ]
            else:
                doc[field] = _resolve_mongo_rule_value(rule, fake, db, parent_cache)

        if "created_at" in doc and hasattr(doc["created_at"], "isoformat"):
            # Ensure native datetime
            pass
        elif "created_at" in doc and isinstance(doc["created_at"], str):
            try:
                doc["created_at"] = datetime.now(timezone.utc)
            except Exception:
                pass
        documents.append(doc)

    inserted_ids: List[str] = []
    if not dry_run:
        result = db[coll_name].insert_many(documents)
        inserted_ids = [str(i) for i in result.inserted_ids]

        # Record in fake data tracker
        track_inserted_keys(coll_name, inserted_ids, tracker_file=TRACKER_FILE)

    preview_docs = documents[:3]
    for d in preview_docs:
        if "_id" in d:
            d["_id"] = str(d["_id"])

    return {
        "success": True,
        "operation": "insert_fake",
        "collection": coll_name,
        "count": len(documents),
        "inserted_count": len(inserted_ids) if not dry_run else 0,
        "locale": locale,
        "preview": preview_docs,
        "documents": documents,
        "inserted_ids": inserted_ids[:10],
    }


def delete_mongo_session_fake_data(db: pymongo.database.Database) -> Dict[str, int]:
    """
    Remove all documents inserted during this session using the tracker log.
    Deletes child collections first to maintain referential integrity.
    Returns: Dict[collection_name, count_deleted]
    """
    tracked = get_tracked_keys(tracker_file=TRACKER_FILE)
    if not tracked:
        return {}

    counts: Dict[str, int] = {}

    for coll_name, key_list in reversed(list(tracked.items())):
        if not key_list:
            continue
        obj_ids = []
        str_ids = []
        for k in key_list:
            try:
                obj_ids.append(ObjectId(str(k)))
            except Exception:
                str_ids.append(k)

        query = {"$or": []}
        if obj_ids:
            query["$or"].append({"_id": {"$in": obj_ids}})
        if str_ids:
            query["$or"].append({"_id": {"$in": str_ids}})

        if query["$or"]:
            res = db[coll_name].delete_many(query)
            counts[coll_name] = res.deleted_count

    clear_tracked_keys(tracker_file=TRACKER_FILE)
    return counts
