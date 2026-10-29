"""Synthetic fake data generation action using Faker and JSON specs.

The LLM produces a declarative JSON spec (not raw rows), and this module
validates the spec, resolves dependencies, respects schema constraints,
shows a preview, and batch-inserts rows inside a database transaction.
"""

import os
import re
import json
import random
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from faker import Faker
from sqlalchemy import text
from sqlalchemy.engine import Engine

TRACKER_FILE = Path("logs/fake_data_tracker.json")


class EmptyParentTableError(ValueError):
    """Raised when a parent table referenced by a foreign key has no existing rows."""

    def __init__(self, parent_table: str, parent_col: str, child_table: str = ""):
        self.parent_table = parent_table
        self.parent_col = parent_col
        self.child_table = child_table
        msg = f"Parent table '{parent_table}' has no existing rows for foreign key '{parent_col}'."
        if child_table:
            msg += f" Child table '{child_table}' cannot be populated."
        super().__init__(msg)


def validate_spec(spec: Dict[str, Any], schema: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """
    Validate the LLM-generated fake data specification against the actual schema.
    - Removes unknown column names and emits a warning
    - Skips auto-increment primary keys if included
    - Normalizes row count and locale
    """
    warnings: List[str] = []
    cleaned_spec = dict(spec)

    tables = schema.get("tables", {})
    table_name = spec.get("table", "")

    if not table_name:
        raise ValueError("Fake data specification missing 'table' property.")

    if table_name not in tables:
        # Case-insensitive match check
        matched = next((t for t in tables if t.lower() == table_name.lower()), None)
        if matched:
            table_name = matched
            cleaned_spec["table"] = matched
        else:
            raise ValueError(
                f"Target table '{table_name}' does not exist in schema. "
                f"Available tables: {', '.join(tables.keys())}"
            )

    table_info = tables[table_name]
    valid_cols = {c["name"]: c for c in table_info.get("columns", [])}

    raw_columns = spec.get("columns", {})
    cleaned_columns: Dict[str, str] = {}

    for col_name, rule in raw_columns.items():
        if col_name not in valid_cols:
            warnings.append(
                f"Warning: Column '{col_name}' does not exist in table '{table_name}'. Skipped."
            )
            continue

        col_meta = valid_cols[col_name]
        # Skip primary key columns to allow database auto-generation
        if col_meta.get("primary_key"):
            warnings.append(
                f"Warning: Primary key column '{col_name}' skipped to allow database auto-generation."
            )
            continue

        cleaned_columns[col_name] = str(rule)

    cleaned_spec["columns"] = cleaned_columns
    cleaned_spec["rows"] = max(1, int(spec.get("rows", 10)))
    cleaned_spec["locale"] = spec.get("locale", "en_US")

    return cleaned_spec, warnings


def _get_faker_instance(locale: str) -> Faker:
    """Instantiate Faker with requested locale, falling back to en_US."""
    try:
        return Faker(locale)
    except Exception:
        return Faker("en_US")


def _resolve_rule_value(
    rule: str,
    fake: Faker,
    engine_or_mongo: Any,
    parent_cache: Dict[str, List[Any]],
    child_table: str = "",
) -> Any:
    """Map a spec entry to Faker providers, choices, ranges, or foreign keys."""
    rule_clean = rule.strip()

    # 1. Foreign key reference: fk:<table>.<column>
    if rule_clean.startswith("fk:"):
        fk_target = rule_clean[3:].strip()
        parts = fk_target.split(".", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid fk rule format: '{rule}'. Expected fk:<table>.<column>")

        p_table, p_col = parts[0].strip(), parts[1].strip()
        cache_key = f"{p_table}.{p_col}"

        if cache_key not in parent_cache:
            parent_cache[cache_key] = _fetch_existing_parent_keys(p_table, p_col, engine_or_mongo)

        candidates = parent_cache[cache_key]
        if not candidates:
            raise EmptyParentTableError(parent_table=p_table, parent_col=p_col, child_table=child_table)

        return random.choice(candidates)

    # 2. Choice: choice:val1,val2,...
    if rule_clean.startswith("choice:"):
        options = [opt.strip() for opt in rule_clean[7:].split(",") if opt.strip()]
        if not options:
            return "active"
        return random.choice(options)

    # 3. Integer range: int_range:min:max
    if rule_clean.startswith("int_range:"):
        parts = rule_clean.split(":")
        min_val = int(parts[1]) if len(parts) > 1 else 1
        max_val = int(parts[2]) if len(parts) > 2 else 100
        return random.randint(min_val, max_val)

    # 4. Standard Faker: faker:<method>[:arg1[:arg2]]
    if rule_clean.startswith("faker:"):
        parts = rule_clean.split(":")
        method_name = parts[1].strip()
        args = parts[2:]

        if method_name == "date_between":
            start_date = args[0] if len(args) > 0 else "-2y"
            end_date = args[1] if len(args) > 1 else "today"
            d = fake.date_between(start_date=start_date, end_date=end_date)
            return d.strftime("%Y-%m-%d %H:%M:%S")

        elif method_name in ("pyfloat", "price"):
            return round(random.uniform(9.99, 499.99), 2)

        elif method_name in ("pyint", "random_int"):
            min_v = int(args[0]) if len(args) > 0 else 1
            max_v = int(args[1]) if len(args) > 1 else 100
            return random.randint(min_v, max_v)

        elif method_name == "address":
            return fake.address().replace("\n", ", ")

        elif hasattr(fake, method_name):
            val = getattr(fake, method_name)()
            if isinstance(val, (datetime, date)):
                return val.strftime("%Y-%m-%d %H:%M:%S")
            return val

        # Fallback to word
        return fake.word()

    # Raw string default fallback
    return rule_clean


def _fetch_existing_parent_keys(table: str, col: str, engine_or_mongo: Any) -> List[Any]:
    """Retrieve existing values of a parent column to preserve foreign key integrity."""
    if hasattr(engine_or_mongo, "connect"):
        # SQLAlchemy Engine
        with engine_or_mongo.connect() as conn:
            stmt = text(f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL LIMIT 2000;")
            result = conn.execute(stmt)
            return [row[0] for row in result.fetchall()]
    else:
        # MongoDB database or client
        db = getattr(engine_or_mongo, "db", engine_or_mongo)
        coll = db[table]
        vals = coll.distinct(col)
        return [v for v in vals if v is not None]


def generate_rows(
    spec: Dict[str, Any],
    schema: Dict[str, Any],
    engine_or_mongo: Any,
    auto_generate_parents: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Generate synthetic rows conforming to the validated spec.
    Respects NOT NULL and UNIQUE constraints with deduplication retries.
    Uses requested locale (e.g. en_IN) and falls back to en_US.
    Picks values from existing parent rows for fk: columns.
    If a parent table is empty, offers/auto-generates parent rows first.
    """
    clean_spec, warnings = validate_spec(spec, schema)
    table_name = clean_spec["table"]
    num_rows = clean_spec["rows"]
    locale = clean_spec.get("locale", "en_US")
    columns = clean_spec.get("columns", {})

    fake = _get_faker_instance(locale)
    table_info = schema.get("tables", {}).get(table_name, {})

    # Ensure NOT NULL columns that aren't primary keys have a rule assigned
    for col in table_info.get("columns", []):
        c_name = col["name"]
        if not col.get("nullable") and not col.get("primary_key") and c_name not in columns:
            c_type = str(col.get("type", "")).upper()
            if "INT" in c_type:
                columns[c_name] = "int_range:1:100"
            elif "FLOAT" in c_type or "REAL" in c_type or "NUMERIC" in c_type:
                columns[c_name] = "faker:price"
            elif "DATE" in c_type or "TIME" in c_type:
                columns[c_name] = "faker:date_between:-1y:today"
            else:
                columns[c_name] = "faker:word"
            warnings.append(f"Notice: Auto-assigned generator for NOT NULL column '{c_name}'.")

    # Pre-check all foreign key references for empty parent tables
    parent_cache: Dict[str, List[Any]] = {}
    for col_name, rule in list(columns.items()):
        rule_str = str(rule).strip()
        if rule_str.startswith("fk:"):
            fk_target = rule_str[3:].strip()
            parts = fk_target.split(".", 1)
            if len(parts) == 2:
                p_table, p_col = parts[0].strip(), parts[1].strip()
                cache_key = f"{p_table}.{p_col}"
                existing = _fetch_existing_parent_keys(p_table, p_col, engine_or_mongo)
                if not existing:
                    if auto_generate_parents:
                        # Auto-generate 10 parent rows with default columns
                        parent_cols = {}
                        p_info = schema.get("tables", {}).get(p_table, {})
                        for col in p_info.get("columns", []):
                            cn = col["name"]
                            if not col.get("primary_key"):
                                if "name" in cn:
                                    parent_cols[cn] = "faker:name"
                                elif "email" in cn:
                                    parent_cols[cn] = "faker:email"
                                elif "price" in cn or "amount" in cn:
                                    parent_cols[cn] = "faker:price"
                                elif "int" in str(col.get("type", "")).lower():
                                    parent_cols[cn] = "int_range:1:100"
                                else:
                                    parent_cols[cn] = "faker:word"
                        parent_spec = {
                            "table": p_table,
                            "rows": 10,
                            "locale": locale,
                            "columns": parent_cols
                        }
                        p_rows, _ = generate_rows(parent_spec, schema, engine_or_mongo, auto_generate_parents=True)
                        insert_rows(p_table, p_rows, engine_or_mongo, schema)
                        existing = _fetch_existing_parent_keys(p_table, p_col, engine_or_mongo)
                        warnings.append(f"Auto-generated 10 parent rows for '{p_table}'.")
                    else:
                        raise EmptyParentTableError(parent_table=p_table, parent_col=p_col, child_table=table_name)
                parent_cache[cache_key] = existing

    # Identify UNIQUE columns from column definitions and indexes
    unique_columns: Set[str] = set()
    for col in table_info.get("columns", []):
        if col.get("unique"):
            unique_columns.add(col["name"])

    for idx in table_info.get("indexes", []):
        if idx.get("unique"):
            unique_columns.update(idx.get("column_names", []))

    # Pre-populate seen values with existing values from database to prevent collision
    seen_unique: Dict[str, Set[Any]] = {c: set() for c in unique_columns}
    for u_col in unique_columns:
        if u_col in columns:
            try:
                existing = _fetch_existing_parent_keys(table_name, u_col, engine_or_mongo)
                seen_unique[u_col].update(existing)
            except Exception:
                pass

    generated_rows: List[Dict[str, Any]] = []

    for _ in range(num_rows):
        row: Dict[str, Any] = {}
        for col_name, rule in columns.items():
            is_unique = col_name in unique_columns
            val = None
            max_attempts = 50

            for _ in range(max_attempts):
                candidate = _resolve_rule_value(rule, fake, engine_or_mongo, parent_cache, child_table=table_name)
                if not is_unique or candidate not in seen_unique[col_name]:
                    val = candidate
                    if is_unique:
                        seen_unique[col_name].add(candidate)
                    break

            if val is None:
                # If unique retry exhausted, append suffix to maintain uniqueness
                val = f"{candidate}_{random.randint(1000, 99999)}"
                if is_unique:
                    seen_unique[col_name].add(val)

            row[col_name] = val
        generated_rows.append(row)

    return generated_rows, warnings


def insert_rows(
    table_name: str,
    rows: List[Dict[str, Any]],
    engine_or_mongo: Any,
    schema: Dict[str, Any],
    batch_size: int = 50,
    tracker_file: Path = TRACKER_FILE,
) -> List[Any]:
    """
    Insert generated rows into the database in batches inside a transaction.
    Returns the list of generated/inserted primary keys.
    """
    if not rows:
        return []

    inserted_pks: List[Any] = []
    table_info = schema.get("tables", {}).get(table_name, {})
    pk_cols = [c["name"] for c in table_info.get("columns", []) if c.get("primary_key")]
    pk_col = pk_cols[0] if pk_cols else "id"

    # SQL / SQLAlchemy branch
    if hasattr(engine_or_mongo, "begin"):
        dialect_name = getattr(getattr(engine_or_mongo, "dialect", None), "name", "").lower()
        with engine_or_mongo.begin() as conn:
            for i in range(0, len(rows), batch_size):
                batch = rows[i : i + batch_size]
                cols = list(batch[0].keys())
                col_names = ", ".join(cols)
                param_placeholders = ", ".join([f":{c}" for c in cols])

                if dialect_name == "postgresql":
                    stmt = text(
                        f"INSERT INTO {table_name} ({col_names}) "
                        f"VALUES ({param_placeholders}) RETURNING {pk_col};"
                    )
                    res = conn.execute(stmt, batch)
                    inserted_pks.extend([r[0] for r in res.fetchall()])
                else:
                    stmt = text(
                        f"INSERT INTO {table_name} ({col_names}) "
                        f"VALUES ({param_placeholders});"
                    )
                    for row_item in batch:
                        res = conn.execute(stmt, row_item)
                        pk_val = getattr(res, "lastrowid", None)
                        if pk_val is not None:
                            inserted_pks.append(pk_val)

        # Fallback if dialect didn't provide lastrowid
        if not inserted_pks:
            with engine_or_mongo.connect() as conn:
                try:
                    stmt = text(f"SELECT {pk_col} FROM {table_name} ORDER BY {pk_col} DESC LIMIT {len(rows)};")
                    pks = [r[0] for r in conn.execute(stmt).fetchall()]
                    inserted_pks = list(reversed(pks))
                except Exception:
                    pass

    else:
        # MongoDB branch
        db = getattr(engine_or_mongo, "db", engine_or_mongo)
        coll = db[table_name]
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            res = coll.insert_many(batch)
            inserted_pks.extend(res.inserted_ids)

    # Track inserted primary keys in tracker file
    track_inserted_keys(table_name, inserted_pks, tracker_file=tracker_file)
    return inserted_pks


def get_table_dependency_order(schema: Dict[str, Any]) -> List[str]:
    """
    Compute topological dependency order of tables based on foreign key relationships.
    Parent tables appear before child tables.
    """
    tables = schema.get("tables", {})
    dependencies: Dict[str, Set[str]] = {t: set() for t in tables}

    for t_name, t_info in tables.items():
        for fk in t_info.get("foreign_keys", []):
            ref_t = fk.get("referred_table")
            if ref_t and ref_t in tables and ref_t != t_name:
                dependencies[t_name].add(ref_t)

    # Topological sort (Kahn's algorithm)
    order: List[str] = []
    visited: Set[str] = set()

    def visit(node: str, ancestors: Set[str]):
        if node in visited:
            return
        ancestors.add(node)
        for dep in dependencies.get(node, []):
            if dep not in ancestors:
                visit(dep, ancestors)
        ancestors.remove(node)
        visited.add(node)
        order.append(node)

    for table in tables:
        visit(table, set())

    return order


def track_inserted_keys(
    table_name: str,
    primary_keys: List[Any],
    tracker_file: Path = TRACKER_FILE
) -> None:
    """Record inserted primary keys for the current session."""
    tracker_file.parent.mkdir(parents=True, exist_ok=True)
    data: Dict[str, List[Any]] = {}

    if tracker_file.exists():
        try:
            with open(tracker_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    if table_name not in data:
        data[table_name] = []

    data[table_name].extend(primary_keys)

    with open(tracker_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)


def get_tracked_keys(tracker_file: Path = TRACKER_FILE) -> Dict[str, List[Any]]:
    """Retrieve currently tracked primary keys from session tracker."""
    if not tracker_file.exists():
        return {}
    try:
        with open(tracker_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def clear_tracked_keys(tracker_file: Path = TRACKER_FILE) -> None:
    """Clear session fake data tracker."""
    if tracker_file.exists():
        tracker_file.unlink()


def delete_session_fake_data(
    engine_or_mongo: Any,
    schema: Optional[Dict[str, Any]] = None,
    tracker_file: Path = TRACKER_FILE
) -> Dict[str, int]:
    """
    Delete all fake data inserted in the current session.
    Deletes child tables first (reverse topological order) to protect foreign keys.
    """
    if schema is None:
        schema = {}
    tracked = get_tracked_keys(tracker_file)
    if not tracked:
        return {}

    # Reverse dependency order so children are deleted before parents
    dep_order = get_table_dependency_order(schema)
    delete_order = [t for t in reversed(dep_order) if t in tracked]

    # Include any remaining tracked tables not in dep_order
    for t in tracked:
        if t not in delete_order:
            delete_order.append(t)

    deletion_summary: Dict[str, int] = {}
    tables = schema.get("tables", {})

    if hasattr(engine_or_mongo, "begin"):
        # SQL database transaction
        with engine_or_mongo.begin() as conn:
            for table_name in delete_order:
                if tables and table_name not in tables:
                    continue
                pks = tracked.get(table_name, [])
                if not pks:
                    continue

                table_info = tables.get(table_name, {})
                pk_cols = [c["name"] for c in table_info.get("columns", []) if c.get("primary_key")]
                pk_col = pk_cols[0] if pk_cols else "id"

                # Batch delete in chunks of 500
                total_deleted = 0
                for i in range(0, len(pks), 500):
                    batch = pks[i : i + 500]
                    placeholders = ", ".join([f":id_{j}" for j in range(len(batch))])
                    params = {f"id_{j}": val for j, val in enumerate(batch)}
                    stmt = text(f"DELETE FROM {table_name} WHERE {pk_col} IN ({placeholders});")
                    try:
                        res = conn.execute(stmt, params)
                        total_deleted += res.rowcount if res.rowcount != -1 else len(batch)
                    except Exception:
                        pass

                deletion_summary[table_name] = total_deleted
    else:
        # MongoDB database
        db = getattr(engine_or_mongo, "db", engine_or_mongo)
        for table_name in delete_order:
            pks = tracked.get(table_name, [])
            if not pks:
                continue
            res = db[table_name].delete_many({"_id": {"$in": pks}})
            deletion_summary[table_name] = res.deleted_count

    clear_tracked_keys(tracker_file)
    return deletion_summary


def get_fake_data_spec(
    user_request: str,
    schema: Dict[str, Any],
    db_type: str,
    client: Optional[Any] = None,
    table_hint: Optional[str] = None
) -> Tuple[Dict[str, Any], List[str]]:
    """
    Query the LLM using prompts/fake_data_spec.txt to generate a declarative fake data specification,
    then validate the specification against the active database schema.
    """
    from devdb.schema import schema_to_text
    from devdb.llm import LLMClient

    if client is None:
        client = LLMClient()

    tables = schema.get("tables", {})
    if not tables:
        raise ValueError("Database schema has no tables available for fake data generation.")

    # Determine target table
    target_table = table_hint
    if not target_table:
        req_lower = user_request.lower()
        for t_name in tables:
            # Check plural and singular names
            t_base = t_name.lower().rstrip("s")
            if t_name.lower() in req_lower or t_base in req_lower:
                target_table = t_name
                break

    if not target_table:
        dep_order = get_table_dependency_order(schema)
        target_table = dep_order[0] if dep_order else list(tables.keys())[0]

    # Load prompt template
    prompt_file = Path("prompts/fake_data_spec.txt")
    if not prompt_file.exists():
        raise FileNotFoundError(f"Fake data specification prompt file '{prompt_file}' not found.")

    prompt_template = prompt_file.read_text(encoding="utf-8")
    schema_txt = schema_to_text(schema)
    system_prompt = (
        prompt_template.replace("{schema}", schema_txt)
        .replace("{table_name}", target_table)
        .replace("{db_type}", db_type)
    )

    raw_spec = client.ask_json(system_prompt=system_prompt, user_prompt=user_request)

    if not isinstance(raw_spec, dict):
        raw_spec = {}

    if "table" not in raw_spec or not raw_spec["table"]:
        raw_spec["table"] = target_table

    # If row count explicitly mentioned in user request, respect it
    count_match = re.search(r"\b(\d+)\s+fake\b", user_request, re.IGNORECASE)
    if count_match:
        raw_spec["rows"] = int(count_match.group(1))

    # If Indian locale requested in prompt
    if any(term in user_request.lower() for term in ["indian", "india", "en_in"]):
        raw_spec["locale"] = "en_IN"

    return validate_spec(raw_spec, schema)


def generate_whole_database(
    schema: Dict[str, Any],
    engine_or_mongo: Any,
    db_type: str,
    client: Optional[Any] = None,
    rows_per_table: int = 15,
    locale: str = "en_US",
    tracker_file: Path = TRACKER_FILE,
) -> Dict[str, int]:
    """
    Generate and insert fake data for all tables in topological dependency order
    (parent tables populated before child tables).
    """
    order = get_table_dependency_order(schema)
    summary: Dict[str, int] = {}

    for table in order:
        req = f"generate {rows_per_table} fake records for {table}"
        spec, _ = get_fake_data_spec(req, schema, db_type, client=client, table_hint=table)
        spec["rows"] = rows_per_table
        spec["locale"] = locale
        rows, _ = generate_rows(spec, schema, engine_or_mongo, auto_generate_parents=True)
        inserted = insert_rows(table, rows, engine_or_mongo, schema, tracker_file=tracker_file)
        summary[table] = len(inserted)

    return summary
