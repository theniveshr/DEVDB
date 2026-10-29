"""Relation-aware fake data generator driven by LLM JSON specs and the Faker library."""

import json
import random
import re
from typing import Any, Dict, List, Optional, Tuple
from faker import Faker
from sqlalchemy import text
from src.config import PROMPTS_DIR
from src.db import DatabaseManager
from src.llm import query_llm


class FakeDataGenerator:
    """Generates schema- and relation-aware synthetic test data."""

    def __init__(self, db_manager: DatabaseManager, locale: str = "en_US"):
        self.db = db_manager
        self.faker = Faker(locale)

    def _get_available_fk_ids(self, ref_table: str, ref_col: str) -> List[Any]:
        """Fetch existing IDs from a parent table to satisfy foreign key constraints."""
        try:
            if self.db.dialect_name == "mongodb":
                return [doc["_id"] for doc in self.db.db[ref_table].find({}, {"_id": 1}).limit(500)]
            _, rows = self.db.execute_query(f"SELECT {ref_col} FROM {ref_table} LIMIT 500")
            return [row[0] for row in rows if row[0] is not None]
        except Exception:
            return []

    def get_column_spec_from_llm(self, table_name: str, provider: Optional[str] = None) -> Dict[str, Any]:
        """Ask LLM for a JSON specification mapping table columns to Faker methods."""
        prompt_file = PROMPTS_DIR / "fake_data_spec.txt"
        if not prompt_file.exists():
            return self._build_heuristic_spec(table_name)

        columns = self.db.get_table_columns(table_name)
        fks = self.db.get_foreign_keys(table_name)
        pk_cols = set(self.db.get_primary_keys(table_name))

        col_descriptions = []
        for col in columns:
            name = col["name"]
            col_type = str(col["type"])
            is_pk = name in pk_cols
            nullable = col.get("nullable", True)
            col_descriptions.append(
                f"- {name} ({col_type}, Primary Key: {is_pk}, Nullable: {nullable})"
            )

        fk_descriptions = []
        for fk in fks:
            local = ", ".join(fk["constrained_columns"])
            ref = f"{fk['referred_table']}.{', '.join(fk['referred_columns'])}"
            fk_descriptions.append(f"- {local} references {ref}")

        rendered_prompt = (
            prompt_file.read_text(encoding="utf-8")
            .replace("{table_name}", table_name)
            .replace("{table_schema}", "\n".join(col_descriptions))
            .replace("{foreign_keys}", "\n".join(fk_descriptions) if fk_descriptions else "None")
        )

        try:
            response = query_llm(
                user_prompt=rendered_prompt,
                system_prompt="You generate strict JSON specifications for synthetic test data.",
                provider=provider,
            )
        except Exception:
            return self._build_heuristic_spec(table_name)

        # Parse JSON from response
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", response, flags=re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except json.JSONDecodeError:
                pass

        try:
            return json.loads(response.strip())
        except json.JSONDecodeError:
            # Fallback to smart heuristic spec if LLM JSON is malformed
            return self._build_heuristic_spec(table_name)

    def _build_heuristic_spec(self, table_name: str) -> Dict[str, Any]:
        """Rule-based fallback specification when LLM is unavailable or outputs invalid JSON."""
        columns = self.db.get_table_columns(table_name)
        fks = self.db.get_foreign_keys(table_name)
        pk_cols = set(self.db.get_primary_keys(table_name))

        fk_map = {}
        for fk in fks:
            for local, ref in zip(fk["constrained_columns"], fk["referred_columns"]):
                fk_map[local] = f"{fk['referred_table']}.{ref}"

        spec_columns = {}
        for col in columns:
            name = col["name"].lower()
            col_type = str(col["type"]).lower()

            if col["name"] in pk_cols:
                spec_columns[col["name"]] = {"rule": "auto_increment"}
            elif col["name"] in fk_map:
                spec_columns[col["name"]] = {"rule": "foreign_key", "references": fk_map[col["name"]]}
            elif "email" in name:
                spec_columns[col["name"]] = {"rule": "faker", "provider": "safe_email"}
            elif "name" in name:
                spec_columns[col["name"]] = {"rule": "faker", "provider": "name"}
            elif "phone" in name:
                spec_columns[col["name"]] = {"rule": "faker", "provider": "phone_number"}
            elif "price" in name or "amount" in name or "cost" in name or "float" in col_type or "decimal" in col_type:
                spec_columns[col["name"]] = {"rule": "faker", "provider": "pyfloat", "params": {"left_digits": 2, "right_digits": 2, "positive": True}}
            elif "date" in name or "time" in name or "created" in name or "date" in col_type:
                spec_columns[col["name"]] = {"rule": "faker", "provider": "date_time_this_year"}
            elif "quantity" in name or "count" in name or "stock" in name or "int" in col_type:
                spec_columns[col["name"]] = {"rule": "faker", "provider": "random_int", "params": {"min": 1, "max": 100}}
            elif "address" in name:
                spec_columns[col["name"]] = {"rule": "faker", "provider": "address"}
            elif "text" in col_type or "desc" in name or "bio" in name or "notes" in name:
                spec_columns[col["name"]] = {"rule": "faker", "provider": "sentence"}
            else:
                spec_columns[col["name"]] = {"rule": "faker", "provider": "word"}

        return {"table": table_name, "columns": spec_columns}

    def generate_rows(self, table_name: str, count: int = 5, provider: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Generate N fake rows for a table using Faker and relation-aware foreign key resolution."""
        spec = self.get_column_spec_from_llm(table_name, provider=provider)
        col_specs = spec.get("columns", {})

        # Pre-fetch existing foreign key candidate IDs
        fk_pools: Dict[str, List[Any]] = {}
        for col_name, rule_def in col_specs.items():
            if rule_def.get("rule") == "foreign_key":
                ref_target = rule_def.get("references", "")
                if "." in ref_target:
                    ref_table, ref_col = ref_target.split(".", 1)
                    ids = self._get_available_fk_ids(ref_table, ref_col)
                    fk_pools[col_name] = ids

        generated_rows: List[Dict[str, Any]] = []

        for _ in range(count):
            row_data = {}
            for col_name, rule_def in col_specs.items():
                rule = rule_def.get("rule", "faker")

                if rule == "auto_increment":
                    # Let DB assign auto-increment PKs
                    continue
                elif rule == "foreign_key":
                    available_ids = fk_pools.get(col_name, [])
                    if available_ids:
                        row_data[col_name] = random.choice(available_ids)
                    else:
                        # Fallback if parent table is empty: insert dummy 1 or None
                        row_data[col_name] = 1
                elif rule == "faker":
                    provider = rule_def.get("provider", "word")
                    params = rule_def.get("params", {})
                    if hasattr(self.faker, provider):
                        method = getattr(self.faker, provider)
                        try:
                            row_data[col_name] = method(**params)
                        except Exception:
                            row_data[col_name] = method()
                    else:
                        row_data[col_name] = self.faker.word()
                else:
                    row_data[col_name] = self.faker.word()

            generated_rows.append(row_data)

        return generated_rows, spec

    def insert_rows(self, table_name: str, rows: List[Dict[str, Any]]) -> int:
        """Insert generated rows into the database."""
        if not rows:
            return 0

        if self.db.dialect_name == "mongodb":
            res = self.db.db[table_name].insert_many(rows)
            return len(res.inserted_ids)

        columns = list(rows[0].keys())
        col_str = ", ".join(columns)
        param_str = ", ".join([f":{c}" for c in columns])
        sql = f"INSERT INTO {table_name} ({col_str}) VALUES ({param_str})"

        with self.db.engine.connect() as conn:
            conn.execute(text(sql), rows)
            conn.commit()

        return len(rows)
