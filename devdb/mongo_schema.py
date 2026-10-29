"""MongoDB dynamic schema inference module.

Samples up to 100 documents per collection to infer:
- Field names and most common BSON types
- Field presence percentage (%)
- Embedded sub-documents and arrays
- Probable cross-collection references (by naming and value matching)
- Collection document counts and indexes
- Compact prompt serialization via mongo_schema_to_text()
"""

from collections import Counter
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse
import pymongo
from bson import ObjectId


def infer_mongo_schema(
    client_or_db: Any,
    db_name: Optional[str] = None,
    sample_limit: int = 100,
) -> Dict[str, Any]:
    """
    Infer schema for all non-system collections in a MongoDB database.
    Samples up to `sample_limit` documents per collection.
    """
    if isinstance(client_or_db, str):
        # Connection string
        parsed = urlparse(client_or_db)
        path = (parsed.path or "").lstrip("/")
        db_name = path.split("?")[0] if path else "devdb"
        db_name = db_name or "devdb"
        client = pymongo.MongoClient(client_or_db, serverSelectionTimeoutMS=2500)
        db = client[db_name]
    elif isinstance(client_or_db, pymongo.MongoClient):
        db_name = db_name or "devdb"
        db = client_or_db[db_name]
    else:
        db = client_or_db

    all_colls = [
        c for c in db.list_collection_names()
        if not c.startswith("system.")
    ]

    collections_meta: Dict[str, Any] = {}

    # Pre-cache known _id samples per collection for value-based reference resolution
    id_cache: Dict[str, Set[str]] = {}
    for cname in all_colls:
        try:
            ids = db[cname].find({}, {"_id": 1}).limit(sample_limit)
            id_cache[cname] = {str(doc["_id"]) for doc in ids if "_id" in doc}
        except Exception:
            id_cache[cname] = set()

    for coll_name in all_colls:
        coll = db[coll_name]
        doc_count = coll.count_documents({})
        sample_docs = list(coll.find().limit(sample_limit))
        sample_size = len(sample_docs)

        fields_summary: Dict[str, Any] = {}
        type_counters: Dict[str, Counter] = {}
        field_counts: Counter = Counter()

        for doc in sample_docs:
            for k, v in doc.items():
                field_counts[k] += 1
                if k not in type_counters:
                    type_counters[k] = Counter()

                if v is None:
                    t_name = "null"
                elif isinstance(v, ObjectId):
                    t_name = "ObjectId"
                elif isinstance(v, list):
                    if v and isinstance(v[0], dict):
                        t_name = "list[dict]"
                    elif v:
                        t_name = f"list[{type(v[0]).__name__}]"
                    else:
                        t_name = "list"
                elif isinstance(v, dict):
                    t_name = "dict"
                else:
                    t_name = type(v).__name__

                type_counters[k][t_name] += 1

        columns: List[Dict[str, Any]] = []
        for field_name, count in field_counts.items():
            common_type = type_counters[field_name].most_common(1)[0][0]
            presence_pct = round((count / sample_size) * 100, 1) if sample_size else 0.0
            is_pk = (field_name == "_id")
            is_embedded = common_type.startswith("list[dict]") or common_type == "dict"

            col_info = {
                "name": field_name,
                "type": common_type,
                "nullable": not is_pk and presence_pct < 100.0,
                "primary_key": is_pk,
                "presence_pct": presence_pct,
                "is_embedded": is_embedded,
            }
            columns.append(col_info)

        # Detect cross-collection references
        foreign_keys: List[Dict[str, Any]] = []
        for col in columns:
            cname = col["name"]
            if cname == "_id":
                continue

            target_coll = None

            # 1. Name heuristic (e.g. customer_id, customerId -> customers)
            candidate_colls = []
            if cname.endswith("_id"):
                prefix = cname[:-3]
                candidate_colls = [prefix, f"{prefix}s", f"{prefix}es"]
            elif cname.endswith("Id"):
                prefix = cname[:-2]
                candidate_colls = [prefix, f"{prefix}s", f"{prefix}es"]

            for cand in candidate_colls:
                if cand in all_colls and cand != coll_name:
                    target_coll = cand
                    break

            # 2. Value matching heuristic
            if not target_coll:
                # Check sample values against id_cache
                sample_vals = [
                    str(d.get(cname)) for d in sample_docs
                    if d.get(cname) is not None
                ]
                if sample_vals:
                    for other_cname, other_ids in id_cache.items():
                        if other_cname == coll_name or not other_ids:
                            continue
                        matches = sum(1 for v in sample_vals if v in other_ids)
                        if matches > 0 and (matches / len(sample_vals)) >= 0.3:
                            target_coll = other_cname
                            break

            if target_coll:
                foreign_keys.append({
                    "name": f"ref_{cname}",
                    "constrained_columns": [cname],
                    "referred_table": target_coll,
                    "referred_columns": ["_id"],
                    "type": "reference",
                })

        # Indexes
        indexes: List[Dict[str, Any]] = []
        try:
            raw_indexes = coll.index_information()
            for idx_name, idx_info in raw_indexes.items():
                if idx_name == "_id_":
                    continue
                indexes.append({
                    "name": idx_name,
                    "column_names": [k[0] for k in idx_info.get("key", [])],
                    "unique": bool(idx_info.get("unique", False)),
                })
        except Exception:
            pass

        collections_meta[coll_name] = {
            "doc_count": doc_count,
            "sample_size": sample_size,
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
        }

    return {"tables": collections_meta}


def mongo_schema_to_text(schema: Dict[str, Any]) -> str:
    """
    Format inferred MongoDB schema into compact, rich representation for LLM prompts.
    """
    collections = schema.get("tables", schema)
    if not collections:
        return "No collections found in MongoDB database."

    lines: List[str] = []
    for coll_name, meta in collections.items():
        doc_count = meta.get("doc_count", 0)
        lines.append(f"Collection: {coll_name} ({doc_count} documents)")

        lines.append("  Fields:")
        columns = meta.get("columns", [])
        if columns:
            for col in columns:
                c_name = col["name"]
                c_type = col.get("type", "mixed")
                pct = col.get("presence_pct", 100.0)
                details = [f"{pct}% presence"]
                if col.get("primary_key"):
                    details.append("PK")
                if col.get("is_embedded"):
                    details.append("embedded")

                det_str = f" ({', '.join(details)})"
                lines.append(f"    - {c_name}: {c_type}{det_str}")
        else:
            lines.append("    (empty / no fields)")

        # References
        fks = meta.get("foreign_keys", [])
        if fks:
            lines.append("  References:")
            for fk in fks:
                col = ", ".join(fk.get("constrained_columns", []))
                ref_coll = fk.get("referred_table", "unknown")
                lines.append(f"    - ({col}) -> {ref_coll}._id (cross-collection reference)")

        # Indexes
        indexes = meta.get("indexes", [])
        if indexes:
            lines.append("  Indexes:")
            for idx in indexes:
                cols = ", ".join(idx.get("column_names", []))
                uniq = " [UNIQUE]" if idx.get("unique") else ""
                lines.append(f"    - {idx.get('name')}: ({cols}){uniq}")

        lines.append("")

    return "\n".join(lines).strip()
