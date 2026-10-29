"""Relationship management action for DevDB.

Provides:
1. add_relationship:
   - one-to-many via a foreign key
   - one-to-one (FK + UNIQUE constraint)
   - many-to-many (create a dedicated join table)
   - SQLite safe table recreation (PRAGMA -> CREATE _new -> COPY -> DROP -> RENAME -> INDEX -> PRAGMA)
   - PostgreSQL ALTER TABLE ADD CONSTRAINT
   - Always previews the SQL first
2. show_relationships:
   - ASCII relationship map: e.g. customers (1) ──< orders (N) ──< order_items (N) >── (1) products
   - Rich tree view
   - Optional --mermaid flag exports Mermaid ER diagram to relationships.md
3. validate_relationships:
   - Find ORPHAN rows (child rows whose FK points to a missing parent) and count them
   - Find FK columns missing an index and suggest CREATE INDEX
   - Find likely-but-missing relationships (columns like customer_id with no FK)
   - Print a health report with Rich
4. safe_delete_check:
   - Calculates affected child rows before DELETE on parent table
   - Considers ON DELETE CASCADE / RESTRICT / SET NULL
   - Warns: "Deleting customer 5 will delete 12 orders and 40 order_items."
"""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.tree import Tree
from rich.syntax import Syntax
from sqlalchemy import text
from sqlalchemy.engine import Engine

console = Console()


# ==============================================================================
# 1. ADD RELATIONSHIP (SQL Generation & Safe Execution)
# ==============================================================================

def generate_add_relationship_sql(
    child_table: str,
    child_col: str,
    parent_table: str,
    parent_col: str,
    schema: Dict[str, Any],
    rel_type: str = "one_to_many",
    on_delete: str = "RESTRICT",
    db_type: str = "sqlite",
    join_table: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Generate SQL for adding a relationship according to database dialect.
    - One-to-Many: foreign key constraint
    - One-to-One: foreign key + UNIQUE constraint on child column
    - Many-to-Many: create dedicated join table with two FKs and composite PK
    - For SQLite: safe table recreation in a single transaction
    - For PostgreSQL: ALTER TABLE ADD CONSTRAINT
    """
    db_dialect = db_type.lower()
    rel_norm = rel_type.lower().replace("-", "_")
    on_del_norm = on_delete.upper()

    # Case 1: Many-to-Many via join table
    if rel_norm in ("many_to_many", "m2m", "n_m", "n_to_m"):
        j_tbl = join_table or f"{child_table}_{parent_table}"

        def _singularize(name: str) -> str:
            if name.endswith("ies"):
                return name[:-3] + "y"
            if name.endswith("es") and not name.endswith("ses"):
                return name[:-2]
            if name.endswith("s"):
                return name[:-1]
            return name

        c_pk_name = f"{_singularize(child_table)}_{child_col}"
        p_pk_name = f"{_singularize(parent_table)}_{parent_col}"

        sql = f"""CREATE TABLE {j_tbl} (
    {c_pk_name} INTEGER NOT NULL,
    {p_pk_name} INTEGER NOT NULL,
    PRIMARY KEY ({c_pk_name}, {p_pk_name}),
    FOREIGN KEY ({c_pk_name}) REFERENCES {child_table}({child_col}) ON DELETE CASCADE,
    FOREIGN KEY ({p_pk_name}) REFERENCES {parent_table}({parent_col}) ON DELETE CASCADE
);
CREATE INDEX idx_{j_tbl}_{c_pk_name} ON {j_tbl}({c_pk_name});
CREATE INDEX idx_{j_tbl}_{p_pk_name} ON {j_tbl}({p_pk_name});"""
        explanation = (
            f"Creates join table '{j_tbl}' establishing a Many-to-Many relationship between "
            f"'{child_table}' and '{parent_table}' with composite primary key and cascading foreign keys."
        )
        return sql, explanation

    # Case 2: PostgreSQL ALTER TABLE ADD CONSTRAINT
    if "postgres" in db_dialect:
        constraint_name = f"fk_{child_table}_{child_col}_{parent_table}"
        stmts = [
            f"ALTER TABLE {child_table} ADD CONSTRAINT {constraint_name} "
            f"FOREIGN KEY ({child_col}) REFERENCES {parent_table}({parent_col}) "
            f"ON DELETE {on_del_norm};"
        ]
        if rel_norm in ("one_to_one", "1_to_1", "1:1"):
            uq_name = f"uq_{child_table}_{child_col}"
            stmts.append(f"ALTER TABLE {child_table} ADD CONSTRAINT {uq_name} UNIQUE ({child_col});")

        sql = "\n".join(stmts)
        explanation = (
            f"PostgreSQL ALTER TABLE statement adding foreign key constraint '{constraint_name}' "
            f"from '{child_table}.{child_col}' to '{parent_table}.{parent_col}' (ON DELETE {on_del_norm})"
            + (" with UNIQUE constraint for One-to-One." if "unique" in sql.lower() else ".")
        )
        return sql, explanation

    # Case 3: SQLite Safe Table Recreation Pattern
    table_info = schema.get("tables", {}).get(child_table)
    if not table_info:
        raise ValueError(f"Table '{child_table}' not found in database schema.")

    columns = table_info.get("columns", [])
    if not columns:
        raise ValueError(f"Table '{child_table}' has no columns in schema.")

    col_names = [c["name"] for c in columns]
    if child_col not in col_names:
        raise ValueError(f"Column '{child_col}' does not exist in table '{child_table}'.")

    # Build column definitions for CREATE TABLE child_new
    col_defs: List[str] = []
    for c in columns:
        name = c["name"]
        ctype = str(c.get("type", "TEXT"))
        is_pk = bool(c.get("primary_key"))
        is_null = bool(c.get("nullable", True))
        is_uq = bool(c.get("unique")) or (rel_norm in ("one_to_one", "1_to_1", "1:1") and name == child_col)

        parts = [name, ctype]
        if is_pk:
            parts.append("PRIMARY KEY AUTOINCREMENT" if "INT" in ctype.upper() else "PRIMARY KEY")
        else:
            if not is_null:
                parts.append("NOT NULL")
            if is_uq:
                parts.append("UNIQUE")

        col_defs.append("    " + " ".join(parts))

    # Add existing foreign keys
    for fk in table_info.get("foreign_keys", []):
        fk_cols = ", ".join(fk.get("constrained_columns", []))
        ref_tbl = fk.get("referred_table", "")
        ref_cols = ", ".join(fk.get("referred_columns", []))
        fk_opt = fk.get("options", {}).get("ondelete") or "RESTRICT"
        col_defs.append(f"    FOREIGN KEY ({fk_cols}) REFERENCES {ref_tbl}({ref_cols}) ON DELETE {fk_opt}")

    # Add the NEW foreign key constraint
    col_defs.append(f"    FOREIGN KEY ({child_col}) REFERENCES {parent_table}({parent_col}) ON DELETE {on_del_norm}")

    # Index recreation
    index_sqls: List[str] = []
    for idx in table_info.get("indexes", []):
        iname = idx.get("name")
        icols = ", ".join(idx.get("column_names", []))
        iuq = "UNIQUE " if idx.get("unique") else ""
        if iname and not iname.startswith("sqlite_autoindex"):
            index_sqls.append(f"CREATE {iuq}INDEX IF NOT EXISTS {iname} ON {child_table}({icols});")

    # Index for the new foreign key column if not already present
    idx_fk_name = f"ix_{child_table}_{child_col}"
    if not any(child_col in idx.get("column_names", []) for idx in table_info.get("indexes", [])):
        index_sqls.append(f"CREATE INDEX IF NOT EXISTS {idx_fk_name} ON {child_table}({child_col});")

    col_list_str = ", ".join(col_names)
    col_defs_str = ",\n".join(col_defs)
    index_block = "\n".join(index_sqls) if index_sqls else f"-- Index on {child_col}\nCREATE INDEX IF NOT EXISTS {idx_fk_name} ON {child_table}({child_col});"

    sql = f"""PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;
CREATE TABLE {child_table}_new (
{col_defs_str}
);
INSERT INTO {child_table}_new ({col_list_str}) SELECT {col_list_str} FROM {child_table};
DROP TABLE {child_table};
ALTER TABLE {child_table}_new RENAME TO {child_table};
{index_block}
COMMIT;
PRAGMA foreign_keys = ON;"""

    rel_name = "One-to-One" if rel_norm in ("one_to_one", "1_to_1", "1:1") else "One-to-Many"
    explanation = (
        f"Safe SQLite table recreation for {rel_name} relationship: disables foreign keys, "
        f"creates '{child_table}_new' with FOREIGN KEY ({child_col}) REFERENCES {parent_table}({parent_col}), "
        f"copies all data, swaps table names, restores indexes, and commits within an atomic transaction."
    )
    return sql, explanation


def execute_relationship_sql(sql: str, engine_or_mongo: Any) -> bool:
    """Execute generated relationship SQL inside the target database."""
    if hasattr(engine_or_mongo, "raw_connection"):
        # For SQLite multi-statement DDL scripts
        dialect_name = getattr(getattr(engine_or_mongo, "dialect", None), "name", "").lower()
        if "sqlite" in dialect_name:
            raw_conn = engine_or_mongo.raw_connection()
            try:
                raw_conn.executescript(sql)
                raw_conn.commit()
                return True
            finally:
                raw_conn.close()

        # For PostgreSQL or other engines
        with engine_or_mongo.begin() as conn:
            for stmt in sql.split(";"):
                stmt_clean = stmt.strip()
                if stmt_clean:
                    conn.execute(text(stmt_clean))
        return True

    return False


# ==============================================================================
# 2. SHOW RELATIONSHIPS (ASCII Map, Rich Tree, Mermaid ER Export)
# ==============================================================================

def build_ascii_relationship_map(schema: Dict[str, Any]) -> str:
    """
    Generate an ASCII diagram representing all schema foreign key relationships.
    Produces clean readable chains such as:
      customers (1) ──< orders (N) ──< order_items (N) >── (1) products
    """
    tables = schema.get("tables", {})
    if not tables:
        return "(No tables found in schema)"

    # Build relationship graph
    fks_list = []
    incoming: Dict[str, List[Tuple[str, str, str, bool]]] = {t: [] for t in tables}
    outgoing: Dict[str, List[Tuple[str, str, str, bool]]] = {t: [] for t in tables}

    for child_t, t_info in tables.items():
        cols = t_info.get("columns", [])
        for fk in t_info.get("foreign_keys", []):
            parent_t = fk.get("referred_table")
            c_cols = fk.get("constrained_columns", [])
            r_cols = fk.get("referred_columns", [])
            if parent_t and c_cols:
                c_col = c_cols[0]
                r_col = r_cols[0] if r_cols else "id"
                is_1_to_1 = any(c.get("unique") for c in cols if c["name"] == c_col)
                fks_list.append((child_t, c_col, parent_t, r_col, is_1_to_1))
                outgoing[child_t].append((parent_t, c_col, r_col, is_1_to_1))
                incoming[parent_t].append((child_t, c_col, r_col, is_1_to_1))

    if not fks_list:
        return "No foreign key relationships currently defined in schema."

    lines: List[str] = []

    # Detect multi-hop chains (e.g. customers -> orders -> order_items <- products)
    chains_covered: Set[Tuple[str, str]] = set()

    # Find middle hub tables (tables that have both incoming and outgoing, or multiple outgoing)
    hub_tables = [t for t, out in outgoing.items() if len(out) >= 2]
    for hub in hub_tables:
        parents = outgoing[hub]
        # Format: parent1 (1) ──< hub (N) >── (1) parent2
        p1, p1_col, _, _ = parents[0]
        p2, p2_col, _, _ = parents[1]

        # Check if p1 or p2 has incoming parent
        p1_prefix = ""
        if outgoing.get(p1):
            root_p, _, _, _ = outgoing[p1][0]
            p1_prefix = f"{root_p} (1) ──< "
            chains_covered.add((outgoing[p1][0][1], p1))

        p1_card = "(N)" if p1_prefix else "(1)"
        chain_str = f"{p1_prefix}{p1} {p1_card} ──< {hub} (N) >── (1) {p2}"
        lines.append(chain_str)
        chains_covered.add((p1_col, hub))
        chains_covered.add((p2_col, hub))

    # Add remaining single links
    for child_t, c_col, parent_t, r_col, is_1_to_1 in fks_list:
        if (c_col, child_t) not in chains_covered:
            connector = "────" if is_1_to_1 else "──<"
            card_child = "(1)" if is_1_to_1 else "(N)"
            lines.append(f"{parent_t} (1) {connector} {child_t} {card_child}")

    return "\n".join(lines)


def render_rich_relationship_tree(schema: Dict[str, Any]) -> Tree:
    """Render a Rich Tree view of all outgoing and incoming references across tables."""
    tables = schema.get("tables", {})
    tree = Tree("[bold cyan]Database Relationships (Live Schema)[/bold cyan]")

    for table_name, t_info in sorted(tables.items()):
        fks = t_info.get("foreign_keys", [])
        # Find incoming FKs (other tables referencing this table)
        incoming_fks = []
        for other_name, other_info in tables.items():
            if other_name == table_name:
                continue
            for fk in other_info.get("foreign_keys", []):
                if fk.get("referred_table") == table_name:
                    incoming_fks.append((other_name, fk))

        if not fks and not incoming_fks:
            node = tree.add(f"[dim]{table_name}[/dim] [dim italic](isolated)[/dim italic]")
            continue

        node = tree.add(f"[bold white]{table_name}[/bold white]")

        # Outgoing foreign keys (references parent)
        if fks:
            out_branch = node.add("[bold yellow]Outgoing Foreign Keys (Parent References):[/bold yellow]")
            for fk in fks:
                ref_tbl = fk.get("referred_table", "")
                c_cols = ", ".join(fk.get("constrained_columns", []))
                r_cols = ", ".join(fk.get("referred_columns", []))
                on_del = fk.get("options", {}).get("ondelete") or "RESTRICT"
                out_branch.add(
                    f"[green]1:N[/green] ──< [bold cyan]{ref_tbl}[/bold cyan] "
                    f"([dim]{c_cols} -> {r_cols}[/dim]) [dim yellow]ON DELETE {on_del}[/dim yellow]"
                )

        # Incoming foreign keys (children referencing this table)
        if incoming_fks:
            in_branch = node.add("[bold blue]Incoming References (Child Tables):[/bold blue]")
            for child_tbl, fk in incoming_fks:
                c_cols = ", ".join(fk.get("constrained_columns", []))
                r_cols = ", ".join(fk.get("referred_columns", []))
                in_branch.add(
                    f"[green]N:1[/green] >── [bold magenta]{child_tbl}[/bold magenta] "
                    f"([dim]{child_tbl}.{c_cols} references {r_cols}[/dim])"
                )

    return tree


def generate_mermaid_er(schema: Dict[str, Any], output_path: Optional[Path] = None) -> str:
    """
    Generate Mermaid ER diagram from schema foreign keys and optionally export to relationships.md.
    """
    tables = schema.get("tables", {})
    lines = ["erDiagram"]

    # Table entities with columns
    for t_name, t_info in sorted(tables.items()):
        lines.append(f"    {t_name} {{")
        for col in t_info.get("columns", []):
            ctype = str(col.get("type", "string")).replace(" ", "_").lower()
            cname = col.get("name")
            extra = "PK" if col.get("primary_key") else ("FK" if any(cname in fk.get("constrained_columns", []) for fk in t_info.get("foreign_keys", [])) else "")
            lines.append(f"        {ctype} {cname} {extra}".rstrip())
        lines.append("    }")

    # Relationship edges
    for child_t, t_info in sorted(tables.items()):
        cols = t_info.get("columns", [])
        for fk in t_info.get("foreign_keys", []):
            parent_t = fk.get("referred_table")
            c_cols = fk.get("constrained_columns", [])
            if parent_t and c_cols:
                c_col = c_cols[0]
                is_1_to_1 = any(c.get("unique") for c in cols if c["name"] == c_col)
                rel_symbol = "||--||" if is_1_to_1 else "||--o{"
                label = f"\"{c_col}\""
                lines.append(f"    {parent_t} {rel_symbol} {child_t} : {label}")

    mermaid_code = "\n".join(lines)

    if output_path:
        out_p = Path(output_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        content = f"""# Database Relationships Entity-Relationship (ER) Diagram

Auto-generated from live database schema by DevDB Relationship Manager.

```mermaid
{mermaid_code}
```
"""
        out_p.write_text(content, encoding="utf-8")

    return mermaid_code


def show_relationships(
    schema: Dict[str, Any],
    export_mermaid: bool = False,
    mermaid_file: str = "relationships.md",
) -> None:
    """Display relationship ASCII diagram, Rich tree, and optionally export Mermaid diagram."""
    ascii_map = build_ascii_relationship_map(schema)

    console.print(Panel(
        f"[bold white]{ascii_map}[/bold white]",
        title="Database Relationships ASCII Map",
        border_style="cyan"
    ))
    console.print()

    # Tree view
    tree = render_rich_relationship_tree(schema)
    console.print(tree)
    console.print()

    # Mermaid export
    if export_mermaid:
        out_path = Path(mermaid_file)
        generate_mermaid_er(schema, output_path=out_path)
        console.print(f"[bold green]Mermaid ER diagram exported successfully to '{out_path}'[/bold green]\n")


# ==============================================================================
# 3. VALIDATE RELATIONSHIPS (Orphan rows, Missing indexes, Missing FKs)
# ==============================================================================

def validate_relationships(engine_or_mongo: Any, schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate relational integrity across the schema:
    1. Detect orphan rows (child rows pointing to non-existent parents)
    2. Detect foreign key columns missing an index
    3. Detect likely-but-missing foreign keys (e.g. customer_id with no FK constraint)
    """
    tables = schema.get("tables", {})
    orphan_results: List[Dict[str, Any]] = []
    missing_indexes: List[Dict[str, Any]] = []
    likely_missing_fks: List[Dict[str, Any]] = []

    # 1. Orphan Rows & Missing Indexes check
    for child_table, t_info in tables.items():
        indexes = t_info.get("indexes", [])
        indexed_first_cols = {idx.get("column_names", [])[0] for idx in indexes if idx.get("column_names")}

        for fk in t_info.get("foreign_keys", []):
            parent_table = fk.get("referred_table")
            c_cols = fk.get("constrained_columns", [])
            r_cols = fk.get("referred_columns", [])

            if not parent_table or not c_cols:
                continue

            child_col = c_cols[0]
            parent_col = r_cols[0] if r_cols else "id"

            # Check index on child_col
            if child_col not in indexed_first_cols and not any(c.get("primary_key") for c in t_info.get("columns", []) if c["name"] == child_col):
                missing_indexes.append({
                    "table": child_table,
                    "column": child_col,
                    "parent_table": parent_table,
                    "suggested_sql": f"CREATE INDEX idx_{child_table}_{child_col} ON {child_table}({child_col});"
                })

            # Check orphan rows in SQL database
            if hasattr(engine_or_mongo, "connect"):
                try:
                    with engine_or_mongo.connect() as conn:
                        orphan_sql = (
                            f"SELECT COUNT(*) FROM {child_table} c "
                            f"LEFT JOIN {parent_table} p ON c.{child_col} = p.{parent_col} "
                            f"WHERE c.{child_col} IS NOT NULL AND p.{parent_col} IS NULL;"
                        )
                        count = conn.execute(text(orphan_sql)).scalar() or 0

                        sample_ids = []
                        if count > 0:
                            sample_sql = (
                                f"SELECT c.{child_col} FROM {child_table} c "
                                f"LEFT JOIN {parent_table} p ON c.{child_col} = p.{parent_col} "
                                f"WHERE c.{child_col} IS NOT NULL AND p.{parent_col} IS NULL "
                                f"LIMIT 5;"
                            )
                            sample_ids = [r[0] for r in conn.execute(text(sample_sql)).fetchall()]

                        orphan_results.append({
                            "child_table": child_table,
                            "child_col": child_col,
                            "parent_table": parent_table,
                            "parent_col": parent_col,
                            "orphan_count": count,
                            "sample_orphans": sample_ids,
                            "status": "HEALTHY" if count == 0 else "ORPHAN ROWS DETECTED"
                        })
                except Exception as err:
                    orphan_results.append({
                        "child_table": child_table,
                        "child_col": child_col,
                        "parent_table": parent_table,
                        "parent_col": parent_col,
                        "orphan_count": -1,
                        "error": str(err),
                        "status": "ERROR"
                    })

    # 2. Likely-but-missing foreign keys (e.g. customer_id with no FK)
    existing_fk_pairs = set()
    for t_name, t_info in tables.items():
        for fk in t_info.get("foreign_keys", []):
            for c in fk.get("constrained_columns", []):
                existing_fk_pairs.add((t_name, c))

    for t_name, t_info in tables.items():
        for col in t_info.get("columns", []):
            col_name = col["name"]
            if (t_name, col_name) in existing_fk_pairs:
                continue

            # Check if column name ends with _id and points to an existing table
            if col_name.lower().endswith("_id"):
                prefix = col_name[:-3].lower()
                # Check singular and plural candidates
                candidate_table = None
                for candidate in tables:
                    if candidate.lower() == prefix or candidate.lower() == f"{prefix}s" or candidate.lower() == f"{prefix}es":
                        candidate_table = candidate
                        break

                if candidate_table and candidate_table != t_name:
                    p_info = tables[candidate_table]
                    pk_cols = [c["name"] for c in p_info.get("columns", []) if c.get("primary_key")]
                    p_pk = pk_cols[0] if pk_cols else "id"
                    likely_missing_fks.append({
                        "child_table": t_name,
                        "child_col": col_name,
                        "inferred_parent_table": candidate_table,
                        "inferred_parent_col": p_pk,
                        "recommendation": f"add_relationship {t_name}.{col_name} -> {candidate_table}.{p_pk}"
                    })

    # Determine overall health status
    total_orphans = sum(r.get("orphan_count", 0) for r in orphan_results if r.get("orphan_count", 0) > 0)
    if total_orphans > 0:
        health_status = "CRITICAL"
    elif missing_indexes or likely_missing_fks:
        health_status = "WARNING"
    else:
        health_status = "HEALTHY"

    return {
        "health_status": health_status,
        "total_orphans": total_orphans,
        "orphan_checks": orphan_results,
        "missing_indexes": missing_indexes,
        "likely_missing_fks": likely_missing_fks,
    }


def render_relationship_health_report(report: Dict[str, Any]) -> None:
    """Print Rich formatted health report for database relationships."""
    status = report.get("health_status", "HEALTHY")
    status_color = "green" if status == "HEALTHY" else ("yellow" if status == "WARNING" else "red")

    summary_text = (
        f"[bold {status_color}]Overall Status: {status}[/bold {status_color}] | "
        f"Active Foreign Keys: [bold]{len(report.get('orphan_checks', []))}[/bold] | "
        f"Orphan Rows: [bold red]{report.get('total_orphans', 0)}[/bold red] | "
        f"Missing Indexes: [bold yellow]{len(report.get('missing_indexes', []))}[/bold yellow] | "
        f"Likely Missing FKs: [bold cyan]{len(report.get('likely_missing_fks', []))}[/bold cyan]"
    )

    console.print(Panel(
        summary_text,
        title="Database Relationships Integrity & Health Report",
        border_style=status_color
    ))
    console.print()

    # Table 1: Active Foreign Keys & Orphan Row Status
    orphan_table = Table(
        title="Foreign Key Referential Integrity (Orphan Row Analysis)",
        show_lines=True,
        header_style="bold magenta",
        title_style="bold cyan"
    )
    orphan_table.add_column("Child Table", style="bold white")
    orphan_table.add_column("Foreign Key Column", style="cyan")
    orphan_table.add_column("Parent Table", style="bold white")
    orphan_table.add_column("Parent PK", style="cyan")
    orphan_table.add_column("Orphan Rows", style="bold", justify="right")
    orphan_table.add_column("Status", justify="center")

    orphan_checks = report.get("orphan_checks", [])
    if not orphan_checks:
        orphan_table.add_row("-", "-", "-", "-", "0", "[dim]No foreign keys defined[/dim]")
    else:
        for r in orphan_checks:
            count = r.get("orphan_count", 0)
            if count == 0:
                status_badge = "[bold green]PASS[/bold green]"
                count_str = "0"
            elif count > 0:
                status_badge = "[bold red]FAIL (ORPHANS)[/bold red]"
                samples = ", ".join(map(str, r.get("sample_orphans", [])))
                count_str = f"[bold red]{count}[/bold red] (e.g. {samples})"
            else:
                status_badge = "[yellow]ERROR[/yellow]"
                count_str = "-"

            orphan_table.add_row(
                r["child_table"],
                r["child_col"],
                r["parent_table"],
                r["parent_col"],
                count_str,
                status_badge
            )
    console.print(orphan_table)
    console.print()

    # Table 2: Missing Indexes
    missing_indexes = report.get("missing_indexes", [])
    if missing_indexes:
        idx_table = Table(
            title="Foreign Key Performance Warning: Missing Indexes",
            show_lines=True,
            header_style="bold magenta",
            title_style="bold yellow"
        )
        idx_table.add_column("Table", style="bold white")
        idx_table.add_column("Unindexed FK Column", style="yellow")
        idx_table.add_column("Parent Table", style="dim")
        idx_table.add_column("Suggested Index SQL", style="green")

        for m in missing_indexes:
            idx_table.add_row(m["table"], m["column"], m["parent_table"], m["suggested_sql"])
        console.print(idx_table)
        console.print()

    # Table 3: Likely-but-Missing Relationships
    likely_missing = report.get("likely_missing_fks", [])
    if likely_missing:
        lm_table = Table(
            title="Schema Discovery: Likely Missing Relationships (Unconstrained ID Columns)",
            show_lines=True,
            header_style="bold magenta",
            title_style="bold cyan"
        )
        lm_table.add_column("Table", style="bold white")
        lm_table.add_column("Unconstrained Column", style="bold cyan")
        lm_table.add_column("Inferred Parent Table", style="bold green")
        lm_table.add_column("Recommended Action", style="bold yellow")

        for lm in likely_missing:
            lm_table.add_row(
                lm["child_table"],
                lm["child_col"],
                f"{lm['inferred_parent_table']}.{lm['inferred_parent_col']}",
                f"[bold yellow]{lm['recommendation']}[/bold yellow]"
            )
        console.print(lm_table)
        console.print()


# ==============================================================================
# 4. SAFE DELETE CHECK (Recursive Cascade & Impact Calculation)
# ==============================================================================

def safe_delete_check(
    parent_table: str,
    target_filter: Any,
    engine_or_mongo: Any,
    schema: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Before executing a DELETE on a parent table, calculate how many child rows
    would be affected considering ON DELETE CASCADE / RESTRICT / SET NULL.
    Recursively navigates the foreign key graph and produces a human warning:
      "Deleting customer 5 will delete 12 orders and 40 order_items."
    """
    tables = schema.get("tables", {})
    t_info = tables.get(parent_table)
    if not t_info:
        return {
            "allowed": False,
            "parent_table": parent_table,
            "error": f"Table '{parent_table}' not found in schema.",
            "warning": f"Table '{parent_table}' not found."
        }

    pk_cols = [c["name"] for c in t_info.get("columns", []) if c.get("primary_key")]
    pk_col = pk_cols[0] if pk_cols else "id"

    # Normalize filter
    filter_str = str(target_filter).strip()
    if re.match(r"^\d+$", filter_str):
        where_clause = f"{pk_col} = {filter_str}"
    elif filter_str.upper().startswith("WHERE "):
        where_clause = filter_str[6:].strip()
    else:
        where_clause = filter_str

    if not hasattr(engine_or_mongo, "connect"):
        return {
            "allowed": True,
            "parent_table": parent_table,
            "warning": f"Safe delete check not supported for non-SQL database."
        }

    # Step 1: Query matching parent IDs
    parent_ids: List[Any] = []
    with engine_or_mongo.connect() as conn:
        try:
            sql = f"SELECT {pk_col} FROM {parent_table} WHERE {where_clause};"
            parent_ids = [r[0] for r in conn.execute(text(sql)).fetchall()]
        except Exception as e:
            return {
                "allowed": False,
                "parent_table": parent_table,
                "error": str(e),
                "warning": f"Failed to inspect target rows in '{parent_table}': {e}"
            }

    if not parent_ids:
        return {
            "allowed": True,
            "parent_table": parent_table,
            "parent_rows_count": 0,
            "impacts": [],
            "warning": f"No rows match filter '{where_clause}' in '{parent_table}'."
        }

    # Step 2: Recursively follow incoming foreign keys
    impacts: List[Dict[str, Any]] = []
    has_restrict = False
    restrict_details: List[str] = []

    # Queue of (current_table, current_pk_col, list_of_ids)
    queue: List[Tuple[str, str, List[Any]]] = [(parent_table, pk_col, parent_ids)]
    visited_tables: Set[str] = {parent_table}

    with engine_or_mongo.connect() as conn:
        while queue:
            curr_table, curr_pk, curr_ids = queue.pop(0)
            if not curr_ids:
                continue

            # Find all child tables that reference curr_table(curr_pk)
            for child_t, c_info in tables.items():
                for fk in c_info.get("foreign_keys", []):
                    if fk.get("referred_table") == curr_table:
                        r_cols = fk.get("referred_columns", [])
                        c_cols = fk.get("constrained_columns", [])
                        if curr_pk in r_cols:
                            child_fk_col = c_cols[r_cols.index(curr_pk)]
                            on_del = (fk.get("options", {}).get("ondelete") or fk.get("on_delete") or "RESTRICT").upper()

                            # Query child rows
                            id_placeholders = ", ".join([f":id_{i}" for i in range(len(curr_ids))])
                            params = {f"id_{i}": val for i, val in enumerate(curr_ids)}
                            c_pk_list = [c["name"] for c in c_info.get("columns", []) if c.get("primary_key")]
                            child_pk = c_pk_list[0] if c_pk_list else "id"

                            c_sql = (
                                f"SELECT {child_pk} FROM {child_t} "
                                f"WHERE {child_fk_col} IN ({id_placeholders});"
                            )
                            child_res = conn.execute(text(c_sql), params).fetchall()
                            child_ids = [r[0] for r in child_res]
                            child_count = len(child_ids)

                            if child_count > 0:
                                impacts.append({
                                    "table": child_t,
                                    "child_col": child_fk_col,
                                    "parent_table": curr_table,
                                    "count": child_count,
                                    "on_delete": on_del,
                                    "child_ids": child_ids
                                })
                                if on_del in ("RESTRICT", "NO ACTION"):
                                    has_restrict = True
                                    restrict_details.append(
                                        f"{child_count} rows in '{child_t}' have ON DELETE RESTRICT"
                                    )

                                if child_t not in visited_tables:
                                    visited_tables.add(child_t)
                                    queue.append((child_t, child_pk, child_ids))

    # Format human-readable warning message
    target_desc = f"customer {target_filter}" if parent_table == "customers" else f"'{parent_table}' with filter ({where_clause})"
    if has_restrict:
        warning_msg = (
            f"Blocked by foreign key constraint! Cannot delete {target_desc}: "
            f"{'; '.join(restrict_details)}. Child rows must be deleted first."
        )
        allowed = False
    elif impacts:
        desc_parts = [f"{imp['count']} {imp['table']}" for imp in impacts]
        warning_msg = f"Deleting {target_desc} will delete {' and '.join(desc_parts)}."
        allowed = True
    else:
        warning_msg = f"Deleting {target_desc} (affects {len(parent_ids)} rows) has no cascading child dependencies."
        allowed = True

    return {
        "allowed": allowed,
        "parent_table": parent_table,
        "parent_rows_count": len(parent_ids),
        "parent_ids": parent_ids,
        "impacts": impacts,
        "has_restrict": has_restrict,
        "warning": warning_msg,
    }
