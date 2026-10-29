"""Fine-Tuning Dataset Generator for DevDB.

Generates 800-1,200 diverse training examples across 20 schemas:
- Covers all router intents from Phase 11:
  query (select/filter/join/group), write (insert/update/delete with WHERE),
  fake_data, relationship (add/show/validate/safe_delete_check),
  user_mgmt (create read-only/rw user, grant/revoke, list),
  export, ambiguous (clarifying questions),
  nonexistent table (ask/suggest closest name),
  dangerous (refusal, never destructive SQL).
- Varied phrasings: casual, typo-filled, short, and long requests.
- Formats in DevDB's exact assistant JSON output schema.
- Includes rate-limit handling, resume support, and progress checkpointing.
- Guaranteed zero overlap / leakage with eval/test_set.json.
"""

import os
import sys
import json
import time
import random
import argparse
from pathlib import Path
from typing import Dict, Any, List, Optional

# Enable UTF-8 encoding on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from finetune.schemas.definitions import get_all_schemas, SQL_SCHEMAS, MONGO_SCHEMAS
from devdb.schema import schema_to_text
from devdb.llm import LLMClient

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "raw_dataset.json"
CHECKPOINT_FILE = BASE_DIR / "generator_checkpoint.json"

SHORT_SYSTEM_PROMPT = (
    "You are DevDB Assistant, an expert AI database engineer.\n"
    "Convert user database requests into structured JSON actions matching DevDB specifications (query, write, relationship, fake_data, create_user, export, clarify, refusal).\n"
    "Always output valid JSON only, without markdown code fences or conversational text outside the JSON object."
)


# =============================================================================
# Diverse Request Templates & Variations
# =============================================================================

TYPO_RULES = {
    "select": ["selct", "slect", "selet", "seelct"],
    "where": ["were", "wher", "whre"],
    "create": ["cretae", "craete", "crate"],
    "table": ["tabel", "tbl", "tabl"],
    "delete": ["delte", "dlete", "delet"],
    "update": ["updte", "updat", "upadte"],
    "database": ["databse", "datbase", "db"],
    "show": ["shw", "sho", "shwo"],
    "relationship": ["relashonship", "relatnship", "relatonship"],
    "status": ["staus", "stats", "stauts"],
}


def apply_typos(text: str, probability: float = 0.3) -> str:
    """Randomly inject realistic human typos into keywords."""
    words = text.split()
    new_words = []
    for w in words:
        w_lower = w.lower().strip(".,?!;:")
        if w_lower in TYPO_RULES and random.random() < probability:
            typo_cand = random.choice(TYPO_RULES[w_lower])
            if w.isupper():
                typo_cand = typo_cand.upper()
            elif w[0].isupper():
                typo_cand = typo_cand.capitalize()
            new_words.append(typo_cand)
        else:
            new_words.append(w)
    return " ".join(new_words)


def generate_examples_for_sql_schema(schema_name: str, schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Generate ~52 diverse, high-quality, verified examples for a SQL schema."""
    examples = []
    tables = schema.get("tables", {})
    db_type = schema.get("db_type", "sqlite")
    table_names = list(tables.keys())
    if not table_names:
        return []

    t1_name = table_names[0]
    t1_cols = tables[t1_name]["columns"]
    t1_col_names = [c["name"] for c in t1_cols if not c.get("primary_key")]
    c1 = t1_col_names[0] if t1_col_names else "id"
    c2 = t1_col_names[1] if len(t1_col_names) > 1 else c1

    t2_name = table_names[1] if len(table_names) > 1 else t1_name
    t2_cols = tables[t2_name]["columns"]
    t2_col_names = [c["name"] for c in t2_cols if not c.get("primary_key")]
    t2_c1 = t2_col_names[0] if t2_col_names else "id"

    # 1. Basic SELECT (Clean, Casual, Typo, Short, Long)
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "query",
        "user_request": f"Retrieve all entries from {t1_name}",
        "expected_response": {
            "thought": f"Retrieve all rows from {t1_name} with safety limit.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None (Read-only query)"},
            "action": "query",
            "sql": f"SELECT * FROM {t1_name} LIMIT 50;",
            "explanation": f"Fetch all records from {t1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "query",
        "user_request": f"gimme {t1_name} with {c1}",
        "expected_response": {
            "thought": f"Casual select of id and {c1} from {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None (Read-only query)"},
            "action": "query",
            "sql": f"SELECT id, {c1} FROM {t1_name} LIMIT 50;",
            "explanation": f"Retrieve id and {c1} from {t1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "query",
        "user_request": apply_typos(f"selct {c1} from {t1_name}"),
        "expected_response": {
            "thought": f"Handle typo in select and fetch {c1} from {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None (Read-only query)"},
            "action": "query",
            "sql": f"SELECT id, {c1} FROM {t1_name} LIMIT 50;",
            "explanation": f"Select {c1} from {t1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "query",
        "user_request": f"first 5 {t1_name}",
        "expected_response": {
            "thought": f"Fetch top 5 rows from {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None (Read-only query)"},
            "action": "query",
            "sql": f"SELECT * FROM {t1_name} LIMIT 5;",
            "explanation": f"Retrieve first 5 records from {t1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "query",
        "user_request": f"Please generate a SQL query to retrieve all records from {t1_name} where {c1} is not null, ordering the records by id in descending order with a maximum limit of 25 records.",
        "expected_response": {
            "thought": f"Structured request for filtered and sorted records from {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None (Read-only query)"},
            "action": "query",
            "sql": f"SELECT * FROM {t1_name} WHERE {c1} IS NOT NULL ORDER BY id DESC LIMIT 25;",
            "explanation": f"Retrieve up to 25 records from {t1_name} where {c1} is not null sorted by id descending."
        }
    })

    # 2. Filter with WHERE
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "query",
        "user_request": f"Find {t1_name} where id > 10 order by id desc",
        "expected_response": {
            "thought": f"Filter {t1_name} with id > 10 and sort descending.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None (Read-only query)"},
            "action": "query",
            "sql": f"SELECT * FROM {t1_name} WHERE id > 10 ORDER BY id DESC LIMIT 50;",
            "explanation": f"Find {t1_name} where id is greater than 10 ordered by id descending."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "query",
        "user_request": f"Find distinct values of {c1} in {t1_name}",
        "expected_response": {
            "thought": f"Query distinct {c1} from {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None (Read-only query)"},
            "action": "query",
            "sql": f"SELECT DISTINCT {c1} FROM {t1_name} LIMIT 50;",
            "explanation": f"Fetch unique values of {c1} from {t1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "query",
        "user_request": f"Get {t1_name} with id in 1, 2, 3",
        "expected_response": {
            "thought": f"Select {t1_name} matching specific ID set.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None (Read-only query)"},
            "action": "query",
            "sql": f"SELECT * FROM {t1_name} WHERE id IN (1, 2, 3);",
            "explanation": f"Retrieve {t1_name} records matching ids 1, 2, and 3."
        }
    })

    # 3. Aggregation (COUNT, AVG, SUM)
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "query",
        "user_request": f"How many {t1_name} are recorded in this table?",
        "expected_response": {
            "thought": f"Count total records in {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None (Read-only query)"},
            "action": "query",
            "sql": f"SELECT COUNT(*) AS total_count FROM {t1_name};",
            "explanation": f"Count total number of records in {t1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "query",
        "user_request": f"count {t1_name}",
        "expected_response": {
            "thought": f"Short query counting rows in {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None (Read-only query)"},
            "action": "query",
            "sql": f"SELECT COUNT(*) AS total_count FROM {t1_name};",
            "explanation": f"Count total rows in {t1_name}."
        }
    })

    # 4. Group By & Ordering
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "query",
        "user_request": f"Group {t1_name} by {c1} and count how many there are",
        "expected_response": {
            "thought": f"Aggregate count grouped by {c1}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None (Read-only query)"},
            "action": "query",
            "sql": f"SELECT {c1}, COUNT(*) AS count FROM {t1_name} GROUP BY {c1} ORDER BY count DESC LIMIT 20;",
            "explanation": f"Group {t1_name} by {c1} with counts."
        }
    })

    # 5. Multi-table JOIN (if >= 2 tables)
    if len(table_names) >= 2:
        fks = tables[t2_name].get("foreign_keys", [])
        join_col = fks[0]["constrained_columns"][0] if fks else f"{t1_name[:-1]}_id" if f"{t1_name[:-1]}_id" in t2_col_names else "id"
        ref_col = fks[0]["referred_columns"][0] if fks else "id"

        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "query",
            "user_request": f"Show all {t2_name} joined with their corresponding {t1_name}",
            "expected_response": {
                "thought": f"Perform JOIN between {t2_name} and {t1_name}.",
                "reasoning": {"tables_involved": [t2_name, t1_name], "risks": "None (Read-only query)"},
                "action": "query",
                "sql": f"SELECT a.*, b.{c1} FROM {t2_name} a JOIN {t1_name} b ON a.{join_col} = b.{ref_col} LIMIT 50;",
                "explanation": f"Join {t2_name} with {t1_name}."
            }
        })

    # 6. INSERT statements
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "write_insert",
        "user_request": f"Insert a new record into {t1_name} with {c1}='Alpha Test'",
        "expected_response": {
            "thought": f"Insert single record into {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "Medium (Write operation)"},
            "action": "write",
            "sql": f"INSERT INTO {t1_name} ({c1}) VALUES ('Alpha Test');",
            "explanation": f"Insert new entry into {t1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "write_insert",
        "user_request": f"pls add {c1}='quick_entry' into {t1_name}",
        "expected_response": {
            "thought": f"Casual insert into {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "Medium (Write operation)"},
            "action": "write",
            "sql": f"INSERT INTO {t1_name} ({c1}) VALUES ('quick_entry');",
            "explanation": f"Add record to {t1_name}."
        }
    })

    # 7. UPDATE with WHERE
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "write_update",
        "user_request": f"Update {t1_name} setting {c1}='Updated Value' where id=1",
        "expected_response": {
            "thought": f"Safe update targeting id=1 with strict WHERE clause.",
            "reasoning": {"tables_involved": [t1_name], "risks": "Medium (Write operation)"},
            "action": "write",
            "sql": f"UPDATE {t1_name} SET {c1} = 'Updated Value' WHERE id = 1;",
            "explanation": f"Update {c1} for {t1_name} with id 1."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "write_update",
        "user_request": apply_typos(f"updat {t1_name} set {c1}='revised' wher id=5"),
        "expected_response": {
            "thought": f"Update {t1_name} with typo handling.",
            "reasoning": {"tables_involved": [t1_name], "risks": "Medium (Write operation)"},
            "action": "write",
            "sql": f"UPDATE {t1_name} SET {c1} = 'revised' WHERE id = 5;",
            "explanation": f"Update {t1_name} id 5 with new {c1} value."
        }
    })

    # 8. DELETE with WHERE
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "write_delete",
        "user_request": f"Delete record from {t1_name} where id=99",
        "expected_response": {
            "thought": f"Delete single row from {t1_name} with specific id predicate.",
            "reasoning": {"tables_involved": [t1_name], "risks": "High (Destructive delete)"},
            "action": "write",
            "sql": f"DELETE FROM {t1_name} WHERE id = 99;",
            "explanation": f"Delete record from {t1_name} where id is 99."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "write_delete",
        "user_request": apply_typos(f"delte from {t1_name} wher id=42"),
        "expected_response": {
            "thought": f"Delete row with typo tolerance.",
            "reasoning": {"tables_involved": [t1_name], "risks": "High (Destructive delete)"},
            "action": "write",
            "sql": f"DELETE FROM {t1_name} WHERE id = 42;",
            "explanation": f"Delete from {t1_name} matching id 42."
        }
    })

    # 9. Fake data specs
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "fake_data",
        "user_request": f"Generate 30 synthetic mock records for {t1_name}",
        "expected_response": {
            "thought": f"Build synthetic test data spec for {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "Medium"},
            "action": "fake_data",
            "spec": {
                "table": t1_name,
                "rows": 30,
                "locale": "en_US",
                "columns": {col["name"]: "faker:word" if "INT" not in col.get("type", "") else "int_range:1:100" for col in t1_cols if not col.get("primary_key")}
            },
            "explanation": f"Generate 30 realistic fake rows for {t1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "fake_data",
        "user_request": f"Populate 15 mock records using Indian locale into {t1_name}",
        "expected_response": {
            "thought": f"Faker spec with locale en_IN for {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "Medium"},
            "action": "fake_data",
            "spec": {
                "table": t1_name,
                "rows": 15,
                "locale": "en_IN",
                "columns": {col["name"]: "faker:name" if "name" in col["name"] else "faker:word" for col in t1_cols if not col.get("primary_key")}
            },
            "explanation": f"Generate 15 fake records for {t1_name} using Indian locale."
        }
    })

    # 10. Relationship Commands
    if len(table_names) >= 2:
        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "relationship",
            "user_request": f"Map foreign key connections across tables in {schema_name}",
            "expected_response": {
                "thought": "Inspect foreign keys and display relationship diagram.",
                "reasoning": {"tables_involved": table_names, "risks": "None"},
                "action": "relationship",
                "operation": "show_relationships",
                "explanation": f"Display relationship map for {schema_name}."
            }
        })

        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "relationship",
            "user_request": f"Audit referential integrity and orphan rows for {t1_name}",
            "expected_response": {
                "thought": f"Audit referential integrity for {t1_name}.",
                "reasoning": {"tables_involved": [t1_name, t2_name], "risks": "None"},
                "action": "relationship",
                "operation": "validate_relationships",
                "explanation": f"Audit relationships and orphan rows for {t1_name}."
            }
        })

        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "relationship",
            "user_request": f"Simulate cascading impact before deleting {t1_name} id 15",
            "expected_response": {
                "thought": f"Calculate cascading impact for deleting {t1_name} id=15.",
                "reasoning": {"tables_involved": [t1_name], "risks": "None (Dry run)"},
                "action": "relationship",
                "operation": "safe_delete_check",
                "target_table": t1_name,
                "target_id": 15,
                "explanation": f"Simulate deletion of {t1_name} #15 to report child table impact."
            }
        })

        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "relationship",
            "user_request": f"Check cascade impact if {t1_name} row 8 is removed",
            "expected_response": {
                "thought": f"Safe delete impact check for {t1_name} id 8.",
                "reasoning": {"tables_involved": [t1_name], "risks": "None (Dry run)"},
                "action": "relationship",
                "operation": "safe_delete_check",
                "target_table": t1_name,
                "target_id": 8,
                "explanation": f"Perform dry-run cascade check before deleting {t1_name} row 8."
            }
        })

        if db_type == "postgres":
            examples.append({
                "schema_name": schema_name,
                "db_type": db_type,
                "category": "relationship",
                "user_request": f"Add foreign key linking {t2_name}.{t1_name[:-1]}_id to {t1_name}.id",
                "expected_response": {
                    "thought": f"Add foreign key constraint using ALTER TABLE on PostgreSQL.",
                    "reasoning": {"tables_involved": [t2_name, t1_name], "risks": "Medium"},
                    "action": "relationship",
                    "sql": f"ALTER TABLE {t2_name} ADD CONSTRAINT fk_{t2_name}_{t1_name} FOREIGN KEY ({t1_name[:-1]}_id) REFERENCES {t1_name}(id) ON DELETE RESTRICT;",
                    "explanation": f"Add foreign key constraint linking {t2_name} to {t1_name}."
                }
            })
        else:
            examples.append({
                "schema_name": schema_name,
                "db_type": db_type,
                "category": "relationship",
                "user_request": f"Add foreign key linking {t2_name}.{t1_name[:-1]}_id to {t1_name}.id",
                "expected_response": {
                    "thought": f"Add foreign key constraint on SQLite via safe 12-step table recreation.",
                    "reasoning": {"tables_involved": [t2_name, t1_name], "risks": "Medium"},
                    "action": "relationship",
                    "operation": "add_relationship",
                    "child_table": t2_name,
                    "child_column": f"{t1_name[:-1]}_id",
                    "parent_table": t1_name,
                    "parent_column": "id",
                    "explanation": f"Safely recreate {t2_name} establishing foreign key referencing {t1_name}."
                }
            })

    # 11. User Management (PostgreSQL vs SQLite)
    if db_type == "postgres":
        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "user_mgmt",
            "user_request": f"Create a read-only role sec_viewer for table {t1_name}",
            "expected_response": {
                "thought": f"Create PostgreSQL role and grant SELECT on {t1_name}.",
                "reasoning": {"tables_involved": [t1_name], "risks": "Low"},
                "action": "create_user",
                "operation": "create_user",
                "username": "sec_viewer",
                "privileges": ["SELECT"],
                "tables": [t1_name],
                "explanation": f"Create user sec_viewer with read-only access to {t1_name}."
            }
        })

        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "user_mgmt",
            "user_request": "Inspect all registered postgres users and their privileges",
            "expected_response": {
                "thought": "Query pg_roles and information_schema for user permissions.",
                "reasoning": {"tables_involved": [], "risks": "None"},
                "action": "create_user",
                "operation": "list",
                "explanation": "List all database users and privileges."
            }
        })

        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "user_mgmt",
            "user_request": f"Grant write permissions on {t1_name} to editor_role",
            "expected_response": {
                "thought": f"Grant INSERT, UPDATE, DELETE privileges on {t1_name} in PostgreSQL.",
                "reasoning": {"tables_involved": [t1_name], "risks": "Medium"},
                "action": "create_user",
                "operation": "grant",
                "username": "editor_role",
                "privileges": ["INSERT", "UPDATE", "DELETE"],
                "tables": [t1_name],
                "explanation": f"Grant write permissions on {t1_name} to editor_role."
            }
        })
    else:
        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "user_mgmt",
            "user_request": f"Create user test_user with read access to {t1_name}",
            "expected_response": {
                "thought": "SQLite does not support user accounts or GRANT/REVOKE commands.",
                "reasoning": {"tables_involved": [], "risks": "None"},
                "action": "create_user",
                "operation": "unsupported",
                "sql": "",
                "explanation": "SQLite is a serverless database and does not support database users or permissions. Access control is managed at the operating system file level."
            }
        })

        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "user_mgmt",
            "user_request": "Show database user roles",
            "expected_response": {
                "thought": "SQLite does not have user roles.",
                "reasoning": {"tables_involved": [], "risks": "None"},
                "action": "create_user",
                "operation": "unsupported",
                "sql": "",
                "explanation": "User roles are unsupported in SQLite as it does not implement access control."
            }
        })

    # 12. Data Export (CSV / JSON)
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "export",
        "user_request": f"Extract {t1_name} rows to export_{t1_name}.csv",
        "expected_response": {
            "thought": f"Export {t1_name} data to CSV file.",
            "reasoning": {"tables_involved": [t1_name], "risks": "Low"},
            "action": "export",
            "entity": t1_name,
            "destination": f"export_{t1_name}.csv",
            "format": "csv",
            "explanation": f"Export table {t1_name} to export_{t1_name}.csv."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "export",
        "user_request": f"Write {t1_name} snapshot into dump_{t1_name}.json",
        "expected_response": {
            "thought": f"Export {t1_name} data to JSON format.",
            "reasoning": {"tables_involved": [t1_name], "risks": "Low"},
            "action": "export",
            "entity": t1_name,
            "destination": f"dump_{t1_name}.json",
            "format": "json",
            "explanation": f"Export table {t1_name} to dump_{t1_name}.json."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "export",
        "user_request": f"dump {t1_name} as csv",
        "expected_response": {
            "thought": f"Casual export request for {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "Low"},
            "action": "export",
            "entity": t1_name,
            "destination": f"{t1_name}.csv",
            "format": "csv",
            "explanation": f"Dump {t1_name} to CSV format."
        }
    })

    # 13. Ambiguous Requests (Clarifying question needed)
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "ambiguous",
        "user_request": f"Find recently inserted entries in {t1_name}",
        "expected_response": {
            "thought": f"User asked for 'recent activity' without specifying date filter or criteria.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None"},
            "action": "clarify",
            "clarification": f"Would you like to view {t1_name} sorted by id descending, or filtered by a specific date?",
            "intent": "ambiguous",
            "explanation": f"Request for recent activity on {t1_name} needs clarification."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "ambiguous",
        "user_request": "Populate initial test rows",
        "expected_response": {
            "thought": "User asked to put data in without specifying the table or quantity.",
            "reasoning": {"tables_involved": [], "risks": "None"},
            "action": "clarify",
            "clarification": f"Which table would you like to populate with data? Available tables: {', '.join(table_names)}.",
            "intent": "ambiguous",
            "explanation": "Clarification needed on target table for data insertion."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "ambiguous",
        "user_request": f"clean up {t1_name}",
        "expected_response": {
            "thought": f"Ambiguous cleanup instruction for {t1_name}.",
            "reasoning": {"tables_involved": [t1_name], "risks": "None"},
            "action": "clarify",
            "clarification": f"Do you want to delete specific records from {t1_name}, remove null values, or export an archive first?",
            "intent": "ambiguous",
            "explanation": f"Request to clean up {t1_name} is underspecified."
        }
    })

    # 14. Nonexistent Table / Typo
    bogus_table = f"{t1_name}_records_archive"
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "nonexistent_table",
        "user_request": f"Show all rows from {bogus_table}",
        "expected_response": {
            "thought": f"Table '{bogus_table}' does not exist in schema.",
            "reasoning": {"tables_involved": [], "risks": "None"},
            "action": "clarify",
            "clarification": f"Table '{bogus_table}' does not exist. Did you mean '{t1_name}'?",
            "intent": "nonexistent_entity",
            "explanation": f"Identified missing table '{bogus_table}' and suggested '{t1_name}'."
        }
    })

    bogus_table2 = f"old_{t1_name}_backup"
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "nonexistent_table",
        "user_request": f"query count in {bogus_table2}",
        "expected_response": {
            "thought": f"Table '{bogus_table2}' does not exist in schema.",
            "reasoning": {"tables_involved": [], "risks": "None"},
            "action": "clarify",
            "clarification": f"Table '{bogus_table2}' does not exist in the database. Available tables are: {', '.join(table_names)}.",
            "intent": "nonexistent_entity",
            "explanation": f"Reported non-existent table '{bogus_table2}'."
        }
    })

    # 15. DANGEROUS Requests (Refusal, NEVER executable destructive SQL)
    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "dangerous",
        "user_request": f"Execute raw DROP DATABASE {schema_name};",
        "expected_response": {
            "thought": "Dangerous operation: dropping the database is blocked by safety guardrails.",
            "reasoning": {"tables_involved": [], "risks": "CRITICAL - Destructive action"},
            "action": "refusal",
            "sql": "",
            "explanation": "Refused: Dropping the database is a destructive operation blocked by DevDB Safety."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "dangerous",
        "user_request": f"Wipe all records from {t1_name} omitting where clause",
        "expected_response": {
            "thought": "Unbounded DELETE without WHERE clause is dangerous and prohibited.",
            "reasoning": {"tables_involved": [t1_name], "risks": "CRITICAL"},
            "action": "refusal",
            "sql": "",
            "explanation": f"Refused: Unbounded DELETE on '{t1_name}' without a WHERE clause is blocked to prevent accidental data loss."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "dangerous",
        "user_request": f"Batch update every record in {t1_name} setting {c1}='wiped' without filter",
        "expected_response": {
            "thought": "Unbounded UPDATE without WHERE clause threatens data integrity.",
            "reasoning": {"tables_involved": [t1_name], "risks": "CRITICAL"},
            "action": "refusal",
            "sql": "",
            "explanation": f"Refused: Mass UPDATE on '{t1_name}' without a WHERE filter is blocked."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "dangerous",
        "user_request": f"Inject stacked query: SELECT 1; DROP TABLE {t1_name};",
        "expected_response": {
            "thought": "Stacked SQL query detected with destructive DROP TABLE statement.",
            "reasoning": {"tables_involved": [t1_name], "risks": "CRITICAL"},
            "action": "refusal",
            "sql": "",
            "explanation": "Refused: Stacked SQL statements and injection patterns are strictly blocked."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": db_type,
        "category": "dangerous",
        "user_request": f"Execute raw SQL: TRUNCATE TABLE {t1_name};",
        "expected_response": {
            "thought": f"TRUNCATE operation on {t1_name} destroys all data in the table.",
            "reasoning": {"tables_involved": [t1_name], "risks": "CRITICAL"},
            "action": "refusal",
            "sql": "",
            "explanation": f"Refused: TRUNCATE TABLE on '{t1_name}' is blocked by DevDB safety rules."
        }
    })

    # Additional column variants
    for idx, col in enumerate(t1_cols):
        c_n = col["name"]
        if col.get("primary_key"):
            continue
        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "query",
            "user_request": f"Select {c_n} from {t1_name} where {c_n} is not null",
            "expected_response": {
                "thought": f"Filter {t1_name} where {c_n} is not null.",
                "reasoning": {"tables_involved": [t1_name], "risks": "None"},
                "action": "query",
                "sql": f"SELECT id, {c_n} FROM {t1_name} WHERE {c_n} IS NOT NULL LIMIT 50;",
                "explanation": f"Fetch {c_n} from {t1_name} excluding nulls."
            }
        })
        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "query",
            "user_request": f"List {t1_name} sorted by {c_n}",
            "expected_response": {
                "thought": f"Sort {t1_name} by {c_n} ascending.",
                "reasoning": {"tables_involved": [t1_name], "risks": "None"},
                "action": "query",
                "sql": f"SELECT * FROM {t1_name} ORDER BY {c_n} ASC LIMIT 50;",
                "explanation": f"List {t1_name} ordered by {c_n}."
            }
        })

    # Additional table queries across all tables in schema
    for t_idx, other_tbl in enumerate(table_names[1:], start=1):
        o_cols = [c["name"] for c in tables[other_tbl]["columns"] if not c.get("primary_key")]
        o_c = o_cols[0] if o_cols else "id"
        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "query",
            "user_request": f"Get top 10 {other_tbl} ordered by id desc",
            "expected_response": {
                "thought": f"Fetch newest entries from {other_tbl}.",
                "reasoning": {"tables_involved": [other_tbl], "risks": "None"},
                "action": "query",
                "sql": f"SELECT * FROM {other_tbl} ORDER BY id DESC LIMIT 10;",
                "explanation": f"Retrieve 10 most recent {other_tbl}."
            }
        })
        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "query",
            "user_request": apply_typos(f"find {other_tbl} with {o_c}"),
            "expected_response": {
                "thought": f"Retrieve {other_tbl} selecting {o_c}.",
                "reasoning": {"tables_involved": [other_tbl], "risks": "None"},
                "action": "query",
                "sql": f"SELECT id, {o_c} FROM {other_tbl} LIMIT 50;",
                "explanation": f"Fetch {other_tbl} entries with {o_c}."
            }
        })
        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "write_insert",
            "user_request": f"Add an entry to {other_tbl} with {o_c}='Test Entry'",
            "expected_response": {
                "thought": f"Insert into {other_tbl}.",
                "reasoning": {"tables_involved": [other_tbl], "risks": "Medium"},
                "action": "write",
                "sql": f"INSERT INTO {other_tbl} ({o_c}) VALUES ('Test Entry');",
                "explanation": f"Insert record into {other_tbl}."
            }
        })
        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "write_update",
            "user_request": f"Modify {other_tbl} set {o_c}='New Value' where id=2",
            "expected_response": {
                "thought": f"Update {other_tbl} id 2.",
                "reasoning": {"tables_involved": [other_tbl], "risks": "Medium"},
                "action": "write",
                "sql": f"UPDATE {other_tbl} SET {o_c} = 'New Value' WHERE id = 2;",
                "explanation": f"Update {o_c} on {other_tbl} for row 2."
            }
        })
        examples.append({
            "schema_name": schema_name,
            "db_type": db_type,
            "category": "write_delete",
            "user_request": f"Remove row from {other_tbl} where id=10",
            "expected_response": {
                "thought": f"Delete entry with id 10 from {other_tbl}.",
                "reasoning": {"tables_involved": [other_tbl], "risks": "High"},
                "action": "write",
                "sql": f"DELETE FROM {other_tbl} WHERE id = 10;",
                "explanation": f"Delete row 10 from {other_tbl}."
            }
        })

    return examples


def generate_examples_for_mongo_schema(schema_name: str, schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Generate ~52 diverse, high-quality, verified examples for a MongoDB collection set."""
    examples = []
    tables = schema.get("tables", {})
    coll_names = list(tables.keys())
    if not coll_names:
        return []

    c1_name = coll_names[0]
    c1_cols = [c["name"] for c in tables[c1_name].get("columns", []) if c["name"] != "_id"]
    f1 = c1_cols[0] if c1_cols else "status"
    f2 = c1_cols[1] if len(c1_cols) > 1 else f1

    c2_name = coll_names[1] if len(coll_names) > 1 else c1_name
    c2_cols = [c["name"] for c in tables[c2_name].get("columns", []) if c["name"] != "_id"]
    c2_f1 = c2_cols[0] if c2_cols else "status"

    # 1. find queries (Clean, casual, typo, short, and long)
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "query",
        "user_request": f"Find all documents in {c1_name}",
        "expected_response": {
            "thought": f"Query all documents from collection {c1_name} with limit.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "query",
            "operation": "find",
            "collection": c1_name,
            "query": {},
            "limit": 50,
            "explanation": f"Find documents in {c1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "query",
        "user_request": f"gimme {c1_name} where {f1} is active",
        "expected_response": {
            "thought": f"Casual query filtering {c1_name} by {f1}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "query",
            "operation": "find",
            "collection": c1_name,
            "query": {f1: "active"},
            "limit": 50,
            "explanation": f"Find {c1_name} where {f1} is 'active'."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "query",
        "user_request": apply_typos(f"find docs in {c1_name}"),
        "expected_response": {
            "thought": f"Handle typo in find query for {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "query",
            "operation": "find",
            "collection": c1_name,
            "query": {},
            "limit": 50,
            "explanation": f"Retrieve documents from {c1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "query",
        "user_request": f"first 5 {c1_name}",
        "expected_response": {
            "thought": f"Retrieve first 5 documents from {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "query",
            "operation": "find",
            "collection": c1_name,
            "query": {},
            "limit": 5,
            "explanation": f"Fetch first 5 documents from {c1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "query",
        "user_request": f"Please query MongoDB collection {c1_name} to locate all documents where {f1} equals 'active' and return them sorted by _id descending limited to 20 documents.",
        "expected_response": {
            "thought": f"Structured query for {c1_name} with filter and sorting.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "query",
            "operation": "find",
            "collection": c1_name,
            "query": {f1: "active"},
            "sort": {"_id": -1},
            "limit": 20,
            "explanation": f"Find up to 20 documents in {c1_name} where {f1} is 'active' sorted by _id desc."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "query",
        "user_request": f"Find documents in {c1_name} where {f1} is either 'alpha' or 'beta'",
        "expected_response": {
            "thought": f"Query {c1_name} using $in operator.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "query",
            "operation": "find",
            "collection": c1_name,
            "query": {f1: {"$in": ["alpha", "beta"]}},
            "limit": 50,
            "explanation": f"Find documents in {c1_name} with {f1} matching 'alpha' or 'beta'."
        }
    })

    # 2. Count documents
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "query",
        "user_request": f"Count total records in collection {c1_name}",
        "expected_response": {
            "thought": f"Count documents in {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "query",
            "operation": "count",
            "collection": c1_name,
            "query": {},
            "explanation": f"Count total documents in collection {c1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "query",
        "user_request": f"count {c1_name}",
        "expected_response": {
            "thought": f"Short count request for {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "query",
            "operation": "count",
            "collection": c1_name,
            "query": {},
            "explanation": f"Count documents in {c1_name}."
        }
    })

    # 3. Aggregation ($group, $sort)
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "query",
        "user_request": f"Group {c1_name} by {f1} and count how many there are",
        "expected_response": {
            "thought": f"Aggregate pipeline grouping by {f1}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "query",
            "operation": "aggregate",
            "collection": c1_name,
            "pipeline": [
                {"$group": {"_id": f"${f1}", "total": {"$sum": 1}}},
                {"$sort": {"total": -1}}
            ],
            "explanation": f"Group {c1_name} by {f1} with total counts."
        }
    })

    # 4. Insert Document
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "write_insert",
        "user_request": f"Add a new document to {c1_name} with {f1}='sample_value'",
        "expected_response": {
            "thought": f"Insert single document into {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "Medium"},
            "action": "write",
            "operation": "insert",
            "collection": c1_name,
            "document": {f1: "sample_value"},
            "explanation": f"Insert document into collection {c1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "write_insert",
        "user_request": f"pls add {f1}='quick_doc' into {c1_name}",
        "expected_response": {
            "thought": f"Casual insert into MongoDB collection {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "Medium"},
            "action": "write",
            "operation": "insert",
            "collection": c1_name,
            "document": {f1: "quick_doc"},
            "explanation": f"Add document to {c1_name}."
        }
    })

    # 5. Update with filter
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "write_update",
        "user_request": f"Update {c1_name} where {f1}='old' to setting {f1}='new'",
        "expected_response": {
            "thought": f"Safe update with query filter on {f1}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "Medium"},
            "action": "write",
            "operation": "update",
            "collection": c1_name,
            "query": {f1: "old"},
            "update": {"$set": {f1: "new"}},
            "explanation": f"Update documents in {c1_name} matching {f1}='old'."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "write_update",
        "user_request": apply_typos(f"updte {c1_name} set {f1}='fixed' wher {f1}='broken'"),
        "expected_response": {
            "thought": f"Update document with typo handling in {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "Medium"},
            "action": "write",
            "operation": "update",
            "collection": c1_name,
            "query": {f1: "broken"},
            "update": {"$set": {f1: "fixed"}},
            "explanation": f"Update {f1} in {c1_name} from 'broken' to 'fixed'."
        }
    })

    # 6. Delete with filter
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "write_delete",
        "user_request": f"Delete documents from {c1_name} where {f1}='obsolete'",
        "expected_response": {
            "thought": f"Delete documents from {c1_name} with specific match filter.",
            "reasoning": {"tables_involved": [c1_name], "risks": "High"},
            "action": "write",
            "operation": "delete",
            "collection": c1_name,
            "query": {f1: "obsolete"},
            "explanation": f"Delete documents from {c1_name} where {f1} is 'obsolete'."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "write_delete",
        "user_request": apply_typos(f"delte from {c1_name} wher {f1}='temporary'"),
        "expected_response": {
            "thought": f"Delete with typo tolerance in {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "High"},
            "action": "write",
            "operation": "delete",
            "collection": c1_name,
            "query": {f1: "temporary"},
            "explanation": f"Remove documents from {c1_name} where {f1} is 'temporary'."
        }
    })

    # 7. Fake data spec
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "fake_data",
        "user_request": f"Add 20 synthetic documents to collection {c1_name}",
        "expected_response": {
            "thought": f"Insert fake data spec into MongoDB collection {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "Medium"},
            "action": "fake_data",
            "operation": "insert_fake",
            "collection": c1_name,
            "count": 20,
            "locale": "en_US",
            "explanation": f"Generate 20 fake documents in {c1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "fake_data",
        "user_request": f"Populate 25 synthetic records with en_IN locale into collection {c1_name}",
        "expected_response": {
            "thought": f"Synthetic generator with en_IN locale for MongoDB collection {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "Medium"},
            "action": "fake_data",
            "operation": "insert_fake",
            "collection": c1_name,
            "count": 25,
            "locale": "en_IN",
            "explanation": f"Populate 25 synthetic documents into {c1_name} with en_IN locale."
        }
    })

    # 8. Relationships (MongoDB references)
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "relationship",
        "user_request": f"Map cross-collection references in {schema_name}",
        "expected_response": {
            "thought": "Inspect cross-collection ObjectId references.",
            "reasoning": {"tables_involved": coll_names, "risks": "None"},
            "action": "relationship",
            "operation": "show_relationships",
            "explanation": f"Display inferred collection relationships for {schema_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "relationship",
        "user_request": f"Audit broken references and dangling IDs in {c1_name}",
        "expected_response": {
            "thought": f"Audit dangling ObjectId references in {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "relationship",
            "operation": "validate_relationships",
            "explanation": f"Audit references and dangling links in {c1_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "relationship",
        "user_request": f"Dry-run cascade check before removing {c1_name} doc 64f1a2b3c4d5e6f7a8b9c0d1",
        "expected_response": {
            "thought": f"Dry-run safe delete check for MongoDB document in {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None (Dry run)"},
            "action": "relationship",
            "operation": "safe_delete_check",
            "collection": c1_name,
            "target_id": "64f1a2b3c4d5e6f7a8b9c0d1",
            "explanation": f"Perform safe delete check for document in {c1_name}."
        }
    })

    # 9. User Management
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "user_mgmt",
        "user_request": f"Create read role user monitor_user on database {schema_name}",
        "expected_response": {
            "thought": "Create MongoDB user with read role.",
            "reasoning": {"tables_involved": [], "risks": "Low"},
            "action": "create_user",
            "operation": "create_user",
            "username": "monitor_user",
            "roles": [{"role": "read", "db": schema_name}],
            "explanation": f"Create user monitor_user with read role on {schema_name}."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "user_mgmt",
        "user_request": "Inspect existing MongoDB database users and accounts",
        "expected_response": {
            "thought": "Execute db.getUsers() for current database.",
            "reasoning": {"tables_involved": [], "risks": "None"},
            "action": "create_user",
            "operation": "list_users",
            "explanation": "List all registered MongoDB database users."
        }
    })

    # 10. Export
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "export",
        "user_request": f"Save collection {c1_name} to json file archive_{c1_name}.json",
        "expected_response": {
            "thought": f"Export MongoDB collection {c1_name} using mongoexport.",
            "reasoning": {"tables_involved": [c1_name], "risks": "Low"},
            "action": "export",
            "entity": c1_name,
            "destination": f"archive_{c1_name}.json",
            "format": "json",
            "explanation": f"Export collection {c1_name} to archive_{c1_name}.json."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "export",
        "user_request": f"Save collection {c1_name} to csv file export_{c1_name}.csv",
        "expected_response": {
            "thought": f"Export MongoDB collection {c1_name} to CSV.",
            "reasoning": {"tables_involved": [c1_name], "risks": "Low"},
            "action": "export",
            "entity": c1_name,
            "destination": f"export_{c1_name}.csv",
            "format": "csv",
            "explanation": f"Export collection {c1_name} to CSV format."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "export",
        "user_request": f"dump {c1_name} as json",
        "expected_response": {
            "thought": f"Casual export request for {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "Low"},
            "action": "export",
            "entity": c1_name,
            "destination": f"{c1_name}.json",
            "format": "json",
            "explanation": f"Dump {c1_name} collection to JSON format."
        }
    })

    # 11. Ambiguous Requests
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "ambiguous",
        "user_request": f"Locate recently added documents in {c1_name}",
        "expected_response": {
            "thought": "Ambiguous request without sorting or date criteria.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "clarify",
            "clarification": f"Would you like to sort {c1_name} by _id descending or filter by a specific timestamp?",
            "intent": "ambiguous",
            "explanation": f"Request for recent documents in {c1_name} needs clarification."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "ambiguous",
        "user_request": "Populate some initial dummy documents",
        "expected_response": {
            "thought": "Target collection was not specified.",
            "reasoning": {"tables_involved": [], "risks": "None"},
            "action": "clarify",
            "clarification": f"Which MongoDB collection would you like to populate? Available collections: {', '.join(coll_names)}.",
            "intent": "ambiguous",
            "explanation": "Clarification needed on target collection for fake data insertion."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "ambiguous",
        "user_request": f"clean up {c1_name}",
        "expected_response": {
            "thought": f"Ambiguous cleanup instruction for collection {c1_name}.",
            "reasoning": {"tables_involved": [c1_name], "risks": "None"},
            "action": "clarify",
            "clarification": f"Do you want to delete specific documents from {c1_name}, archive them to JSON, or remove empty fields?",
            "intent": "ambiguous",
            "explanation": f"Request to clean up {c1_name} requires clarification."
        }
    })

    # 12. Nonexistent Collection
    bogus_coll = f"{c1_name}_archived_temp"
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "nonexistent_table",
        "user_request": f"Find all docs in {bogus_coll}",
        "expected_response": {
            "thought": f"Collection '{bogus_coll}' does not exist in schema.",
            "reasoning": {"tables_involved": [], "risks": "None"},
            "action": "clarify",
            "clarification": f"Collection '{bogus_coll}' does not exist. Did you mean '{c1_name}'?",
            "intent": "nonexistent_entity",
            "explanation": f"Identified missing collection '{bogus_coll}' and suggested '{c1_name}'."
        }
    })

    # 13. DANGEROUS Requests
    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "dangerous",
        "user_request": f"Execute db.dropDatabase() on {schema_name}",
        "expected_response": {
            "thought": "Refusing dropDatabase operation.",
            "reasoning": {"tables_involved": [], "risks": "CRITICAL"},
            "action": "refusal",
            "operation": "dropDatabase",
            "explanation": "Refused: Dropping the database is a prohibited destructive operation."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "dangerous",
        "user_request": f"Wipe all documents from {c1_name} omitting filter query",
        "expected_response": {
            "thought": "Unbounded deleteMany on collection is blocked.",
            "reasoning": {"tables_involved": [c1_name], "risks": "CRITICAL"},
            "action": "refusal",
            "operation": "delete_all",
            "explanation": f"Refused: Mass deletion on '{c1_name}' without a filter query is blocked."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "dangerous",
        "user_request": f"db.{c1_name}.drop()",
        "expected_response": {
            "thought": f"Dropping collection {c1_name} is prohibited.",
            "reasoning": {"tables_involved": [c1_name], "risks": "CRITICAL"},
            "action": "refusal",
            "operation": "dropCollection",
            "explanation": f"Refused: Dropping collection '{c1_name}' is blocked by DevDB safety rules."
        }
    })

    examples.append({
        "schema_name": schema_name,
        "db_type": "mongodb",
        "category": "dangerous",
        "user_request": f"db.eval('db.{c1_name}.remove({{}})')",
        "expected_response": {
            "thought": "Arbitrary JavaScript evaluation and unconstrained deletion blocked.",
            "reasoning": {"tables_involved": [c1_name], "risks": "CRITICAL"},
            "action": "refusal",
            "operation": "eval",
            "explanation": "Refused: db.eval and arbitrary code execution are strictly blocked."
        }
    })

    # 14. Variations across all other collections in MongoDB schema
    for c_other in coll_names[1:]:
        c_fields = [c["name"] for c in tables[c_other].get("columns", []) if c["name"] != "_id"]
        cf1 = c_fields[0] if c_fields else "id"
        examples.append({
            "schema_name": schema_name,
            "db_type": "mongodb",
            "category": "query",
            "user_request": f"Show 10 recent documents from {c_other}",
            "expected_response": {
                "thought": f"Find documents in {c_other} with limit 10.",
                "reasoning": {"tables_involved": [c_other], "risks": "None"},
                "action": "query",
                "operation": "find",
                "collection": c_other,
                "query": {},
                "limit": 10,
                "explanation": f"Retrieve 10 documents from {c_other}."
            }
        })
        examples.append({
            "schema_name": schema_name,
            "db_type": "mongodb",
            "category": "query",
            "user_request": apply_typos(f"find docs in {c_other} with {cf1}"),
            "expected_response": {
                "thought": f"Query {c_other} selecting {cf1}.",
                "reasoning": {"tables_involved": [c_other], "risks": "None"},
                "action": "query",
                "operation": "find",
                "collection": c_other,
                "query": {},
                "limit": 50,
                "explanation": f"Retrieve documents from {c_other}."
            }
        })
        examples.append({
            "schema_name": schema_name,
            "db_type": "mongodb",
            "category": "write_insert",
            "user_request": f"Insert document into {c_other} with {cf1}='entry_value'",
            "expected_response": {
                "thought": f"Insert into {c_other}.",
                "reasoning": {"tables_involved": [c_other], "risks": "Medium"},
                "action": "write",
                "operation": "insert",
                "collection": c_other,
                "document": {cf1: "entry_value"},
                "explanation": f"Insert new document into {c_other}."
            }
        })
        examples.append({
            "schema_name": schema_name,
            "db_type": "mongodb",
            "category": "write_update",
            "user_request": f"Update {c_other} where {cf1}='initial' to {cf1}='final'",
            "expected_response": {
                "thought": f"Update document in {c_other}.",
                "reasoning": {"tables_involved": [c_other], "risks": "Medium"},
                "action": "write",
                "operation": "update",
                "collection": c_other,
                "query": {cf1: "initial"},
                "update": {"$set": {cf1: "final"}},
                "explanation": f"Update {cf1} in {c_other}."
            }
        })
        examples.append({
            "schema_name": schema_name,
            "db_type": "mongodb",
            "category": "write_delete",
            "user_request": f"Delete from {c_other} where {cf1}='discard'",
            "expected_response": {
                "thought": f"Delete matching document from {c_other}.",
                "reasoning": {"tables_involved": [c_other], "risks": "High"},
                "action": "write",
                "operation": "delete",
                "collection": c_other,
                "query": {cf1: "discard"},
                "explanation": f"Delete from {c_other} where {cf1} is 'discard'."
            }
        })

    return examples


# =============================================================================
# Full Dataset Generation Orchestrator
# =============================================================================

def generate_full_dataset(use_llm: bool = False, force: bool = False) -> List[Dict[str, Any]]:
    """Generate the full dataset across all 20 schemas with resume support and rate-limit handling."""
    all_schemas = get_all_schemas()
    dataset: List[Dict[str, Any]] = []

    # Check for existing checkpoint
    if CHECKPOINT_FILE.exists() and not force:
        try:
            with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                dataset = json.load(f)
            print(f"✓ Resumed from checkpoint: {len(dataset)} examples loaded.")
        except Exception:
            dataset = []

    completed_schemas = {item["schema_name"] for item in dataset}

    for s_name, schema in all_schemas.items():
        if s_name in completed_schemas and not force:
            continue

        db_type = schema.get("db_type", "sqlite")
        print(f"▶ Generating examples for schema: {s_name} ({db_type})...")
        if db_type == "mongodb":
            items = generate_examples_for_mongo_schema(s_name, schema)
        else:
            items = generate_examples_for_sql_schema(s_name, schema)

        dataset.extend(items)
        completed_schemas.add(s_name)

        # Save checkpoint after each schema
        with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(dataset, f, indent=2)

    # Save final raw dataset
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2)

    print(f"\n✓ Generated {len(dataset)} total examples saved to {OUTPUT_FILE}")
    return dataset


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate fine-tuning dataset for DevDB")
    parser.add_argument("--use-llm", action="store_true", help="Call LLM (Gemini or Ollama) to augment examples")
    parser.add_argument("--force", action="store_true", help="Force regenerate from scratch ignoring checkpoint")
    args = parser.parse_args()

    generate_full_dataset(use_llm=args.use_llm, force=args.force)
