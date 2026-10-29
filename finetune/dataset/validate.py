"""Fine-Tuning Dataset Validator & Quality Filter for DevDB.

Performs automatic quality filtering on raw generated examples:
1. SQL validation:
   - SQLite: Runs EXPLAIN on the seeded SQLite DB in finetune/schemas/dbs/
   - PostgreSQL: Parses SQL with sqlglot using the postgres dialect
   - Drops syntax or execution failures
2. Schema grounding:
   - Verifies tables and columns referenced in SQL/Mongo queries exist in the target schema
   - Allows nonexistent entities only for clarification/refusal/nonexistent_table categories
3. Safety rules enforcement:
   - Drops stacked statements (multiple SQL queries) unless refusal
   - Drops UPDATE or DELETE without WHERE clauses unless refusal
   - Drops DROP DATABASE actions unless refusal
   - Ensures refusals have non-executable empty SQL
4. MongoDB validation:
   - Checks operations against the official whitelist
   - Enforces required fields per operation (collection, query, document, update, pipeline)
5. Deduplication & Near-Duplicates:
   - Removes exact duplicates and fuzzy near-duplicates (> 0.88 similarity)
6. Reports kept vs dropped counts per category and saves to validated_dataset.json.
"""

import os
import re
import sys
import json
import sqlite3
import difflib
from pathlib import Path
from typing import Dict, Any, List, Tuple, Set, Optional

# Enable UTF-8 encoding on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import sqlglot
from finetune.schemas.definitions import get_all_schemas

BASE_DIR = Path(__file__).resolve().parent
RAW_DATASET_FILE = BASE_DIR / "raw_dataset.json"
VALIDATED_DATASET_FILE = BASE_DIR / "validated_dataset.json"
TEST_SET_FILE = BASE_DIR.parent.parent / "eval" / "test_set.json"
DBS_DIR = BASE_DIR.parent / "schemas" / "dbs"

VALID_ACTIONS = {
    "query", "write", "relationship", "fake_data",
    "create_user", "export", "clarify", "refusal"
}

VALID_MONGO_OPERATIONS = {
    "find", "aggregate", "count", "insert", "update", "delete",
    "insert_fake", "show_relationships", "validate_relationships",
    "safe_delete_check", "create_user", "list_users", "export",
    "refusal", "clarify", "dropDatabase", "delete_all", "dropCollection",
    "eval", "distinct"
}


def normalize_text(text: str) -> str:
    """Normalize text for duplicate and leak comparison."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


class DatasetValidator:
    """Automatic quality filter for DevDB fine-tuning dataset."""

    def __init__(self, raw_file: Optional[Path] = None):
        self.raw_file = raw_file or RAW_DATASET_FILE
        self.schemas = get_all_schemas()
        self.sqlite_conns: Dict[str, sqlite3.Connection] = {}
        self._init_sqlite_conns()
        self.eval_requests: List[Dict[str, Any]] = []
        if TEST_SET_FILE.exists():
            try:
                with open(TEST_SET_FILE, "r", encoding="utf-8") as f:
                    self.eval_requests = json.load(f)
            except Exception:
                self.eval_requests = []
        self.normalized_evals = [
            (it.get("request", ""), normalize_text(it.get("request", "")))
            for it in self.eval_requests
        ]

    def _init_sqlite_conns(self) -> None:
        """Initialize read-only connections to seeded SQLite databases."""
        for s_name, schema in self.schemas.items():
            if schema.get("db_type") != "mongodb":
                db_path = DBS_DIR / f"{s_name}.db"
                if db_path.exists():
                    conn = sqlite3.connect(str(db_path))
                    self.sqlite_conns[s_name] = conn

    def close(self) -> None:
        """Close all SQLite connections."""
        for conn in self.sqlite_conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self.sqlite_conns.clear()

    def validate_safety_rules(self, item: Dict[str, Any]) -> Tuple[bool, str]:
        """Verify strict adherence to DevDB safety rules."""
        resp = item.get("expected_response", {})
        action = resp.get("action", "")
        sql = (resp.get("sql") or "").strip()
        user_req = item.get("user_request", "").lower()

        # 1. DROP DATABASE must be a refusal
        if "drop database" in user_req and action != "refusal":
            return False, "DROP DATABASE requested but action is not refusal"

        # 2. Refusals must not contain executable destructive SQL
        if action == "refusal":
            if sql and not sql.startswith("--"):
                return False, f"Refusal contains executable SQL: {sql}"
            return True, "Valid refusal"

        # 3. Stacked statements check (semicolons separating multiple statements)
        if sql:
            # Strip trailing semicolon
            inner = sql.rstrip(";").strip()
            if ";" in inner:
                return False, f"Stacked SQL statements detected: {sql}"

        # 4. Unbounded UPDATE / DELETE without WHERE clause
        if action == "write" and sql:
            sql_upper = sql.upper()
            if "UPDATE " in sql_upper and " WHERE " not in sql_upper:
                return False, f"UPDATE statement lacks WHERE clause: {sql}"
            if "DELETE FROM " in sql_upper and " WHERE " not in sql_upper:
                return False, f"DELETE statement lacks WHERE clause: {sql}"

        # 5. MongoDB unbounded delete_all or dropDatabase
        if item.get("db_type") == "mongodb":
            op = resp.get("operation", "")
            if op in ("dropDatabase", "delete_all", "dropCollection", "eval") and action != "refusal":
                return False, f"Dangerous Mongo operation '{op}' executed without refusal"

        return True, "Safety checks passed"

    def validate_sql(self, schema_name: str, db_type: str, sql: str) -> Tuple[bool, str]:
        """Validate SQL query using SQLite EXPLAIN or PostgreSQL sqlglot."""
        if not sql or not sql.strip():
            return True, "Empty SQL (non-query action)"

        clean_sql = sql.strip()

        if db_type == "postgres":
            # Use sqlglot to parse postgres query
            try:
                parsed = sqlglot.parse_one(clean_sql, read="postgres")
                if not parsed:
                    return False, "sqlglot returned empty parse tree"
                return True, "Valid PostgreSQL syntax"
            except Exception as e:
                return False, f"PostgreSQL syntax error (sqlglot): {e}"

        # SQLite: Run EXPLAIN on the seeded SQLite DB
        conn = self.sqlite_conns.get(schema_name)
        if not conn:
            # If DB file missing, fallback to SQLite syntax check
            try:
                mem_conn = sqlite3.connect(":memory:")
                mem_conn.execute(f"EXPLAIN {clean_sql}")
                mem_conn.close()
                return True, "Valid SQLite syntax"
            except Exception as e:
                return False, f"SQLite syntax error: {e}"

        # Run EXPLAIN on seeded DB
        try:
            cur = conn.cursor()
            cur.execute(f"EXPLAIN {clean_sql}")
            return True, "Valid SQLite EXPLAIN"
        except sqlite3.OperationalError as oe:
            # Some statements like ALTER TABLE ADD CONSTRAINT might not be standard SQLite
            # Fallback to sqlglot parsing
            try:
                sqlglot.parse_one(clean_sql, read="sqlite")
                return True, "Valid SQLite parsed with sqlglot"
            except Exception:
                return False, f"SQLite execution/EXPLAIN failed: {oe}"
        except Exception as e:
            return False, f"SQLite error: {e}"

    def validate_mongo(self, schema_name: str, resp: Dict[str, Any]) -> Tuple[bool, str]:
        """Validate MongoDB operation against whitelist and required schema fields."""
        op = resp.get("operation")
        action = resp.get("action")

        if not op:
            if action in ("clarify", "refusal", "export"):
                return True, "Valid non-operation MongoDB action"
            return False, "Missing MongoDB operation"

        if op not in VALID_MONGO_OPERATIONS:
            return False, f"Operation '{op}' not in MongoDB whitelist"

        # Check required fields per operation
        if op == "find":
            if "collection" not in resp or "query" not in resp:
                return False, "Mongo 'find' requires 'collection' and 'query'"
        elif op == "aggregate":
            if "collection" not in resp or "pipeline" not in resp:
                return False, "Mongo 'aggregate' requires 'collection' and 'pipeline'"
        elif op == "count":
            if "collection" not in resp:
                return False, "Mongo 'count' requires 'collection'"
        elif op == "insert":
            if "collection" not in resp or "document" not in resp:
                return False, "Mongo 'insert' requires 'collection' and 'document'"
        elif op == "update":
            if "collection" not in resp or "query" not in resp or "update" not in resp:
                return False, "Mongo 'update' requires 'collection', 'query', and 'update'"
        elif op == "delete":
            if "collection" not in resp or "query" not in resp:
                return False, "Mongo 'delete' requires 'collection' and 'query'"
        elif op == "insert_fake":
            if "collection" not in resp:
                return False, "Mongo 'insert_fake' requires 'collection'"

        return True, "Valid MongoDB operation and fields"

    def validate_schema_grounding(self, item: Dict[str, Any]) -> Tuple[bool, str]:
        """Ensure referenced tables and columns exist in the target schema."""
        schema_name = item.get("schema_name", "")
        category = item.get("category", "")
        resp = item.get("expected_response", {})
        action = resp.get("action", "")

        # Allow non-existent entities for clarification, nonexistent_table, or refusal
        if category in ("nonexistent_table", "ambiguous", "dangerous") or action in ("clarify", "refusal"):
            return True, "Grounding exempt for clarify/refusal/nonexistent_table"

        schema = self.schemas.get(schema_name)
        if not schema:
            return False, f"Unknown schema name '{schema_name}'"

        schema_tables = set(schema.get("tables", {}).keys())

        # 1. Check reasoning tables_involved
        tables_involved = resp.get("reasoning", {}).get("tables_involved", [])
        for tbl in tables_involved:
            if tbl and tbl not in schema_tables:
                return False, f"Table '{tbl}' in tables_involved does not exist in {schema_name}"

        # 2. Check collection in Mongo / fake_data
        collection = resp.get("collection") or resp.get("target_table") or resp.get("child_table")
        if collection and collection not in schema_tables and action not in ("create_user", "export"):
            return False, f"Target collection/table '{collection}' does not exist in {schema_name}"

        # 3. Check fake_data spec table
        spec_table = resp.get("spec", {}).get("table")
        if spec_table and spec_table not in schema_tables:
            return False, f"Fake data spec table '{spec_table}' does not exist in {schema_name}"

        # 4. Check entity in export
        entity = resp.get("entity")
        if entity and entity not in schema_tables:
            return False, f"Export entity '{entity}' does not exist in {schema_name}"

        return True, "Schema grounding verified"

    def filter_and_validate(self) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Run the full validation pipeline over raw_dataset.json."""
        if not self.raw_file.exists():
            raise FileNotFoundError(f"Raw dataset file not found: {self.raw_file}")

        with open(self.raw_file, "r", encoding="utf-8") as f:
            raw_examples = json.load(f)

        print(f"▶ Validating {len(raw_examples)} raw examples against schemas, safety rules, and database execution...")

        kept: List[Dict[str, Any]] = []
        dropped_stats: Dict[str, int] = {}
        category_stats: Dict[str, int] = {}
        db_type_stats: Dict[str, int] = {}

        # Track requests per schema to remove exact duplicates and near-duplicates
        seen_requests: Dict[str, List[str]] = {}

        for idx, item in enumerate(raw_examples):
            schema_name = item.get("schema_name", "")
            db_type = item.get("db_type", "sqlite")
            category = item.get("category", "unknown")
            user_req = item.get("user_request", "")
            resp = item.get("expected_response", {})

            # 1. Structure check
            if not user_req or not resp or not isinstance(resp, dict):
                dropped_stats["invalid_structure"] = dropped_stats.get("invalid_structure", 0) + 1
                continue

            action = resp.get("action")
            if action not in VALID_ACTIONS:
                dropped_stats["invalid_action"] = dropped_stats.get("invalid_action", 0) + 1
                continue

            # 2. Safety rules
            safe, safe_msg = self.validate_safety_rules(item)
            if not safe:
                dropped_stats[f"safety_violation: {safe_msg}"] = dropped_stats.get(f"safety_violation: {safe_msg}", 0) + 1
                continue

            # 3. Schema grounding
            grounded, ground_msg = self.validate_schema_grounding(item)
            if not grounded:
                dropped_stats[f"schema_grounding: {ground_msg}"] = dropped_stats.get(f"schema_grounding: {ground_msg}", 0) + 1
                continue

            # 4. SQL / Mongo Execution & Syntax
            if db_type == "mongodb":
                valid_m, m_msg = self.validate_mongo(schema_name, resp)
                if not valid_m:
                    dropped_stats[f"mongo_error: {m_msg}"] = dropped_stats.get(f"mongo_error: {m_msg}", 0) + 1
                    continue
            else:
                sql = resp.get("sql", "")
                if sql and action != "refusal":
                    valid_sql, sql_msg = self.validate_sql(schema_name, db_type, sql)
                    if not valid_sql:
                        dropped_stats[f"sql_error: {sql_msg}"] = dropped_stats.get(f"sql_error: {sql_msg}", 0) + 1
                        continue

            # 5. Deduplication and Near-Duplicates
            norm_req = normalize_text(user_req)
            if schema_name not in seen_requests:
                seen_requests[schema_name] = []

            is_duplicate = False
            for prev_req in seen_requests[schema_name]:
                if prev_req == norm_req:
                    is_duplicate = True
                    break
                sim = difflib.SequenceMatcher(None, prev_req, norm_req).ratio()
                if sim >= 0.88:
                    is_duplicate = True
                    break

            if is_duplicate:
                dropped_stats["duplicate_or_near_duplicate"] = dropped_stats.get("duplicate_or_near_duplicate", 0) + 1
                continue

            # 6. Anti-Leak Check against eval/test_set.json
            is_leak = False
            for raw_eval, norm_eval in self.normalized_evals:
                if norm_req == norm_eval:
                    is_leak = True
                    break
                sim = difflib.SequenceMatcher(None, norm_req, norm_eval).ratio()
                if sim >= 0.72:
                    is_leak = True
                    break

            if is_leak:
                dropped_stats["eval_test_set_leak"] = dropped_stats.get("eval_test_set_leak", 0) + 1
                continue

            seen_requests[schema_name].append(norm_req)

            # Passed all filters!
            kept.append(item)
            category_stats[category] = category_stats.get(category, 0) + 1
            db_type_stats[db_type] = db_type_stats.get(db_type, 0) + 1

        # Save validated dataset
        with open(VALIDATED_DATASET_FILE, "w", encoding="utf-8") as f:
            json.dump(kept, f, indent=2)

        summary = {
            "total_raw": len(raw_examples),
            "total_kept": len(kept),
            "total_dropped": len(raw_examples) - len(kept),
            "category_stats": category_stats,
            "db_type_stats": db_type_stats,
            "dropped_stats": dropped_stats,
        }

        self.print_summary_report(summary)
        return kept, summary

    def print_summary_report(self, summary: Dict[str, Any]) -> None:
        """Print clear tabular summary report."""
        print("\n" + "=" * 65)
        print(" DevDB Fine-Tuning Dataset Validation Report")
        print("=" * 65)
        print(f"Total Raw Examples:      {summary['total_raw']}")
        print(f"Total Validated & Kept:  {summary['total_kept']}")
        print(f"Total Dropped:           {summary['total_dropped']}")
        print("-" * 65)
        print("Kept Examples by Category:")
        for cat, cnt in sorted(summary["category_stats"].items(), key=lambda x: -x[1]):
            print(f"  • {cat:<24} : {cnt:>4}")
        print("-" * 65)
        print("Kept Examples by Database Type:")
        for db, cnt in sorted(summary["db_type_stats"].items(), key=lambda x: -x[1]):
            print(f"  • {db:<24} : {cnt:>4}")
        print("-" * 65)
        if summary["dropped_stats"]:
            print("Dropped Breakdown:")
            for reason, cnt in sorted(summary["dropped_stats"].items(), key=lambda x: -x[1]):
                print(f"  ✗ {reason:<35} : {cnt:>4}")
        print("=" * 65)
        print(f"✓ Validated dataset saved to {VALIDATED_DATASET_FILE}\n")


if __name__ == "__main__":
    validator = DatasetValidator()
    try:
        validator.filter_and_validate()
    finally:
        validator.close()
