import os
import sys
import json
import sqlite3
from pathlib import Path

# Enable UTF-8 encoding on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from faker import Faker

from finetune.schemas.definitions import SQL_SCHEMAS, MONGO_SCHEMAS, get_all_schemas

fake = Faker("en_US")
Faker.seed(42)

BASE_DIR = Path(__file__).resolve().parent
DEFINITIONS_DIR = BASE_DIR / "definitions"
DBS_DIR = BASE_DIR / "dbs"


def export_json_schemas() -> None:
    """Save all 20 schema definitions as individual JSON files."""
    DEFINITIONS_DIR.mkdir(parents=True, exist_ok=True)
    all_schemas = get_all_schemas()
    for name, schema in all_schemas.items():
        out_path = DEFINITIONS_DIR / f"{name}.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(schema, f, indent=2)
    print(f"✓ Exported {len(all_schemas)} schema JSON files to {DEFINITIONS_DIR}")


def build_create_table_sql(table_name: str, table_info: dict) -> str:
    """Generate dialect-valid SQLite CREATE TABLE statement."""
    cols_def = []
    for col in table_info.get("columns", []):
        col_type = col.get("type", "TEXT")
        # In SQLite, autoincrement primary key requires INTEGER PRIMARY KEY
        if col.get("primary_key"):
            cols_def.append(f'"{col["name"]}" INTEGER PRIMARY KEY AUTOINCREMENT')
        else:
            nullable = "" if col.get("nullable", True) else " NOT NULL"
            cols_def.append(f'"{col["name"]}" {col_type}{nullable}')

    # Add foreign key constraints if defined
    for fk in table_info.get("foreign_keys", []):
        constrained = ", ".join(f'"{c}"' for c in fk.get("constrained_columns", []))
        referred = fk.get("referred_table")
        referred_cols = ", ".join(f'"{c}"' for c in fk.get("referred_columns", []))
        cols_def.append(f"FOREIGN KEY ({constrained}) REFERENCES {referred}({referred_cols}) ON DELETE CASCADE")

    return f"CREATE TABLE IF NOT EXISTS {table_name} (\n  " + ",\n  ".join(cols_def) + "\n);"


def seed_single_sql_db(schema_name: str, schema_info: dict) -> Path:
    """Build and populate a SQLite database file for a SQL schema."""
    DBS_DIR.mkdir(parents=True, exist_ok=True)
    db_file = DBS_DIR / f"{schema_name}.db"
    if db_file.exists():
        try:
            db_file.unlink()
        except Exception:
            pass

    conn = sqlite3.connect(str(db_file))
    conn.execute("PRAGMA foreign_keys = ON;")
    cursor = conn.cursor()

    tables = schema_info.get("tables", {})
    # 1. Create tables in topological/declaration order
    for t_name, t_info in tables.items():
        create_sql = build_create_table_sql(t_name, t_info)
        cursor.execute(create_sql)

    # 2. Populate sample rows with Faker
    id_pools: dict[str, list[int]] = {}
    for t_name, t_info in tables.items():
        cols = t_info.get("columns", [])
        num_rows = 20
        id_pools[t_name] = []

        # Determine insertable columns (excluding PK if autoincrement)
        insert_cols = [c for c in cols if not c.get("primary_key")]
        col_names = [f'"{c["name"]}"' for c in insert_cols]
        placeholders = ", ".join(["?"] * len(insert_cols))
        insert_sql = f"INSERT INTO {t_name} ({', '.join(col_names)}) VALUES ({placeholders})"

        # FK mapping
        fk_map = {}
        for fk in t_info.get("foreign_keys", []):
            c_col = fk["constrained_columns"][0]
            r_tbl = fk["referred_table"]
            fk_map[c_col] = r_tbl

        for _ in range(num_rows):
            row_vals = []
            for col in insert_cols:
                c_name = col["name"]
                c_type = col.get("type", "TEXT").upper()

                if c_name in fk_map:
                    ref_tbl = fk_map[c_name]
                    pool = id_pools.get(ref_tbl, [1])
                    row_vals.append(fake.random_element(pool) if pool else 1)
                elif "id" in c_name and c_name.endswith("_id"):
                    # Loose FK (in schemas without FK constraints)
                    cand_table = c_name.replace("_id", "") + "s"
                    pool = id_pools.get(cand_table, id_pools.get(c_name.replace("_id", ""), [1, 2, 3]))
                    row_vals.append(fake.random_element(pool) if pool else fake.random_int(1, 10))
                elif "email" in c_name:
                    row_vals.append(fake.unique.safe_email())
                elif "name" in c_name or "title" in c_name or "comment" in c_name or "diagnosis" in c_name:
                    if "first" in c_name:
                        row_vals.append(fake.first_name())
                    elif "last" in c_name:
                        row_vals.append(fake.last_name())
                    elif "title" in c_name:
                        row_vals.append(fake.catch_phrase())
                    else:
                        row_vals.append(fake.name())
                elif "city" in c_name or "country" in c_name or "nationality" in c_name:
                    row_vals.append(fake.city() if "city" in c_name else fake.country())
                elif "phone" in c_name:
                    row_vals.append(fake.phone_number())
                elif "price" in c_name or "amount" in c_name or "balance" in c_name or "fare" in c_name or "budget" in c_name or "salary" in c_name:
                    row_vals.append(round(fake.pyfloat(left_digits=3, right_digits=2, positive=True, min_value=10.0), 2))
                elif "rating" in c_name or "gpa" in c_name or "score" in c_name:
                    row_vals.append(round(fake.pyfloat(left_digits=1, right_digits=1, positive=True, min_value=1.0, max_value=5.0), 1))
                elif "stock" in c_name or "quantity" in c_name or "capacity" in c_name or "credits" in c_name or "grade_level" in c_name:
                    row_vals.append(fake.random_int(1, 100))
                elif "date" in c_name or "time" in c_name or "at" in c_name or "period" in c_name or "year" in c_name:
                    if "year" in c_name:
                        row_vals.append(fake.random_int(1990, 2026))
                    else:
                        row_vals.append(str(fake.date_time_this_year()))
                elif "status" in c_name or "type" in c_name or "role" in c_name or "plan" in c_name or "tier" in c_name or "genre" in c_name:
                    row_vals.append(fake.random_element(["active", "pending", "completed", "standard", "premium"]))
                elif "REAL" in c_type or "FLOAT" in c_type:
                    row_vals.append(round(fake.pyfloat(left_digits=2, right_digits=2, positive=True), 2))
                elif "INT" in c_type:
                    row_vals.append(fake.random_int(1, 50))
                else:
                    row_vals.append(fake.word())

            cursor.execute(insert_sql, row_vals)
            id_pools[t_name].append(cursor.lastrowid)

    conn.commit()
    conn.close()
    return db_file


def seed_all_sql_dbs() -> None:
    """Build and seed all 14 SQLite database files."""
    for s_name, s_info in SQL_SCHEMAS.items():
        db_path = seed_single_sql_db(s_name, s_info)
        print(f"  ✓ Seeded SQLite database: {db_path.name}")
    print(f"✓ All {len(SQL_SCHEMAS)} SQLite databases successfully built and seeded.")


if __name__ == "__main__":
    print("▶ Generating JSON schema definitions...")
    export_json_schemas()
    print("▶ Seeding SQLite sample databases with Faker...")
    seed_all_sql_dbs()
