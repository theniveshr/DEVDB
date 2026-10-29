"""Database schema inspection and serialization utilities for SQL and MongoDB."""

from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from sqlalchemy import inspect
from sqlalchemy.engine import Engine


def get_schema(target: Any) -> Dict[str, Any]:
    """
    Inspect the database schema using SQLAlchemy inspect() or MongoDB inspection.

    Supports:
      - SQLAlchemy Engine
      - MongoDB connection URL string (mongodb://...)
      - pymongo.MongoClient or pymongo.database.Database instance

    Returns a dict with:
      tables: {
        table_name: {
          columns: [{name, type, nullable, primary_key}, ...],
          foreign_keys: [{name, constrained_columns, referred_table, referred_columns}, ...],
          indexes: [{name, column_names, unique}, ...]
        }
      }
    """
    if isinstance(target, str):
        clean_target = target.strip()
        if clean_target.startswith("mongodb://") or clean_target.startswith("mongodb+srv://"):
            return get_mongo_schema(clean_target)

    # Check for pymongo client / database instances
    try:
        import pymongo
        if isinstance(target, (pymongo.MongoClient, pymongo.database.Database)):
            return get_mongo_schema(target)
    except ImportError:
        pass

    # SQLAlchemy Engine inspection
    insp = inspect(target)
    table_names = insp.get_table_names()
    tables: Dict[str, Any] = {}

    for table_name in table_names:
        # Columns
        raw_columns = insp.get_columns(table_name)
        columns: List[Dict[str, Any]] = []
        for col in raw_columns:
            columns.append({
                "name": col["name"],
                "type": str(col["type"]),
                "nullable": bool(col["nullable"]),
                "primary_key": bool(col.get("primary_key", False)),
            })

        # Foreign keys
        raw_fks = insp.get_foreign_keys(table_name)
        foreign_keys: List[Dict[str, Any]] = []
        for fk in raw_fks:
            foreign_keys.append({
                "name": fk.get("name"),
                "constrained_columns": fk.get("constrained_columns", []),
                "referred_table": fk.get("referred_table"),
                "referred_columns": fk.get("referred_columns", []),
            })

        # Indexes
        raw_indexes = insp.get_indexes(table_name)
        indexes: List[Dict[str, Any]] = []
        for idx in raw_indexes:
            indexes.append({
                "name": idx.get("name"),
                "column_names": idx.get("column_names", []),
                "unique": bool(idx.get("unique", False)),
            })

        # Unique constraints
        try:
            raw_ucs = insp.get_unique_constraints(table_name)
            for uc in raw_ucs:
                indexes.append({
                    "name": uc.get("name"),
                    "column_names": uc.get("column_names", []),
                    "unique": True,
                })
        except Exception:
            pass

        tables[table_name] = {
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
        }

    return {"tables": tables}


def get_mongo_schema(client_or_url: Any, db_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Inspect a MongoDB database and return a schema dict conforming to the DevDB schema format.
    Delegates to devdb.mongo_schema.infer_mongo_schema.
    """
    from devdb.mongo_schema import infer_mongo_schema
    return infer_mongo_schema(client_or_url, db_name=db_name)


def schema_to_text(schema: Dict[str, Any]) -> str:
    """
    Convert a schema dictionary into compact, formatted text for LLM prompts.
    Works identically for both SQL tables and MongoDB collections.
    """
    tables = schema.get("tables", schema)
    if not tables:
        return "No tables/collections found in schema."

    # Check if this is a MongoDB schema
    is_mongo = any(
        isinstance(meta, dict) and ("doc_count" in meta or any(c.get("is_embedded") for c in meta.get("columns", [])))
        for meta in tables.values()
    )
    if is_mongo:
        from devdb.mongo_schema import mongo_schema_to_text
        return mongo_schema_to_text(schema)

    lines: List[str] = []
    for table_name, table_info in tables.items():
        lines.append(f"Table: {table_name}")

        # Columns / Fields
        lines.append("  Columns:")
        columns = table_info.get("columns", [])
        if columns:
            for col in columns:
                modifiers: List[str] = []
                if col.get("primary_key"):
                    modifiers.append("PK")
                modifiers.append("NULL" if col.get("nullable") else "NOT NULL")
                mod_str = f" ({', '.join(modifiers)})"
                col_type = col.get("type", "TEXT")
                lines.append(f"    - {col['name']}: {col_type}{mod_str}")
        else:
            lines.append("    (none)")

        # Foreign keys / References
        lines.append("  References:")
        fks = table_info.get("foreign_keys", [])
        if fks:
            for fk in fks:
                cols = ", ".join(fk.get("constrained_columns", []))
                ref_table = fk.get("referred_table", "unknown")
                ref_cols = ", ".join(fk.get("referred_columns", []))
                lines.append(f"    - ({cols}) -> {ref_table}({ref_cols})")
        else:
            lines.append("    (none)")

        # Indexes
        lines.append("  Indexes:")
        indexes = table_info.get("indexes", [])
        if indexes:
            for idx in indexes:
                cols = ", ".join(idx.get("column_names", []))
                unique_str = " [UNIQUE]" if idx.get("unique") else ""
                lines.append(f"    - {idx.get('name')}: ({cols}){unique_str}")
        else:
            lines.append("    (none)")

        lines.append("")  # blank line separator

    return "\n".join(lines).strip()
