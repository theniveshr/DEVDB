"""Relationship management: foreign key visual map, orphan row detector, and safe-delete warnings."""

from typing import Any, Dict, List, Tuple
from rich.tree import Tree
from rich.table import Table
from src.db import DatabaseManager


class RelationshipManager:
    """Manages database schema relationships, orphan detection, and cascade impact analysis."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def get_all_relationships(self) -> List[Dict[str, Any]]:
        """Collect all foreign key links across all tables in the database."""
        relationships = []
        for table in self.db.get_tables():
            fks = self.db.get_foreign_keys(table)
            for fk in fks:
                relationships.append({
                    "child_table": table,
                    "child_columns": fk["constrained_columns"],
                    "parent_table": fk["referred_table"],
                    "parent_columns": fk["referred_columns"],
                })
        return relationships

    def build_relationship_tree(self) -> Tree:
        """Construct a Rich Tree visualization of database entity relationships."""
        root = Tree(f"[bold cyan]Database Relationships ({self.db.dialect_name})[/bold cyan]")
        tables = self.db.get_tables()
        rels = self.get_all_relationships()

        # Identify parent tables that are referenced
        parents = {r["parent_table"] for r in rels}

        for table in sorted(tables):
            table_node = root.add(f"[bold yellow]{table}[/bold yellow]")
            
            # Outgoing FKs (this table points to parents)
            outgoing = [r for r in rels if r["child_table"] == table]
            for r in outgoing:
                child_col = ", ".join(r["child_columns"])
                parent_col = ", ".join(r["parent_columns"])
                table_node.add(
                    f"[green]-> references[/green] [bold]{r['parent_table']}[/bold] ({child_col} -> {parent_col})"
                )

            # Incoming FKs (children pointing to this table)
            incoming = [r for r in rels if r["parent_table"] == table]
            for r in incoming:
                child_col = ", ".join(r["child_columns"])
                parent_col = ", ".join(r["parent_columns"])
                table_node.add(
                    f"[blue]<- referenced by[/blue] [bold]{r['child_table']}[/bold] ({r['child_table']}.{child_col} -> {parent_col})"
                )

        return root

    def find_orphan_rows(self) -> List[Dict[str, Any]]:
        """Identify foreign key records pointing to non-existent parent rows."""
        rels = self.get_all_relationships()
        orphans_found = []

        if self.db.dialect_name == "mongodb":
            for rel in rels:
                child = rel["child_table"]
                parent = rel["parent_table"]
                child_col = rel["child_columns"][0]
                parent_col = rel["parent_columns"][0]

                try:
                    valid_parent_ids = list(self.db.db[parent].distinct(parent_col))
                    cursor = self.db.db[child].aggregate([
                        {"$match": {child_col: {"$nin": valid_parent_ids, "$ne": None}}},
                        {"$group": {"_id": f"${child_col}", "orphan_count": {"$sum": 1}}},
                    ])
                    for doc in cursor:
                        orphans_found.append({
                            "child_table": child,
                            "child_column": child_col,
                            "parent_table": parent,
                            "parent_column": parent_col,
                            "orphan_value": doc["_id"],
                            "orphan_count": doc["orphan_count"],
                        })
                except Exception:
                    continue
            return orphans_found

        for rel in rels:
            child = rel["child_table"]
            parent = rel["parent_table"]
            child_col = rel["child_columns"][0]
            parent_col = rel["parent_columns"][0]

            query = f"""
            SELECT c.{child_col} AS orphan_fk_value, COUNT(*) AS row_count
            FROM {child} c
            LEFT JOIN {parent} p ON c.{child_col} = p.{parent_col}
            WHERE p.{parent_col} IS NULL AND c.{child_col} IS NOT NULL
            GROUP BY c.{child_col}
            """

            try:
                cols, rows = self.db.execute_query(query)
                if rows:
                    for row in rows:
                        orphans_found.append({
                            "child_table": child,
                            "child_column": child_col,
                            "parent_table": parent,
                            "parent_column": parent_col,
                            "orphan_value": row[0],
                            "orphan_count": row[1],
                        })
            except Exception:
                continue

        return orphans_found

    def analyze_safe_delete_impact(self, parent_table: str, condition: str = "") -> Dict[str, Any]:
        """Analyze potential cascade damage before executing a DELETE statement on a parent table."""
        rels = self.get_all_relationships()
        referencing_children = [r for r in rels if r["parent_table"] == parent_table]

        impact = {
            "parent_table": parent_table,
            "target_rows_count": 0,
            "dependent_children": [],
            "risk_level": "LOW",
        }

        if self.db.dialect_name == "mongodb":
            import json, re
            filter_doc = {}
            if condition:
                try:
                    filter_doc = json.loads(condition)
                except Exception:
                    m = re.match(r"(_?id)\s*=\s*(\d+)", condition.strip(), re.IGNORECASE)
                    if m:
                        filter_doc = {"_id": int(m.group(2))}
                    else:
                        m2 = re.match(r"([a-zA-Z0-9_]+)\s*=\s*['\"]?([^'\"]+)['\"]?", condition.strip())
                        if m2:
                            filter_doc = {m2.group(1): m2.group(2)}

            try:
                parent_docs = list(self.db.db[parent_table].find(filter_doc, {"_id": 1}))
                impact["target_rows_count"] = len(parent_docs)
                target_ids = [d["_id"] for d in parent_docs]

                total_child_impact = 0
                for rel in referencing_children:
                    child = rel["child_table"]
                    child_col = rel["child_columns"][0]
                    cnt = self.db.db[child].count_documents({child_col: {"$in": target_ids}})
                    if cnt > 0:
                        total_child_impact += cnt
                        impact["dependent_children"].append({
                            "child_table": child,
                            "foreign_key": child_col,
                            "affected_rows": cnt,
                        })

                if total_child_impact > 0 or impact["target_rows_count"] > 10:
                    impact["risk_level"] = "HIGH"
                elif impact["target_rows_count"] > 0:
                    impact["risk_level"] = "MEDIUM"
            except Exception:
                pass
            return impact

        # SQL Implementation
        where_clause = f" WHERE {condition}" if condition else ""
        count_query = f"SELECT COUNT(*) FROM {parent_table}{where_clause}"
        try:
            _, rows = self.db.execute_query(count_query)
            impact["target_rows_count"] = rows[0][0] if rows else 0
        except Exception:
            impact["target_rows_count"] = -1

        total_child_impact = 0
        for rel in referencing_children:
            child = rel["child_table"]
            child_col = rel["child_columns"][0]
            parent_col = rel["parent_columns"][0]

            dep_query = f"""
            SELECT COUNT(*)
            FROM {child} c
            WHERE c.{child_col} IN (
                SELECT p.{parent_col} FROM {parent_table} p{where_clause}
            )
            """
            try:
                _, dep_rows = self.db.execute_query(dep_query)
                count = dep_rows[0][0] if dep_rows else 0
                if count > 0:
                    total_child_impact += count
                    impact["dependent_children"].append({
                        "child_table": child,
                        "foreign_key": child_col,
                        "affected_rows": count,
                    })
            except Exception:
                continue

        if total_child_impact > 0 or impact["target_rows_count"] > 10:
            impact["risk_level"] = "HIGH"
        elif impact["target_rows_count"] > 0:
            impact["risk_level"] = "MEDIUM"

        return impact

    def generate_add_foreign_key_sql(
        self,
        child_table: str,
        child_col: str,
        parent_table: str,
        parent_col: str = "id",
        constraint_name: str = "",
    ) -> str:
        """Generate SQL to add a foreign key constraint based on database dialect."""
        name = constraint_name or f"fk_{child_table}_{child_col}"
        if self.db.dialect_name == "postgresql":
            return (
                f"ALTER TABLE {child_table} "
                f"ADD CONSTRAINT {name} FOREIGN KEY ({child_col}) "
                f"REFERENCES {parent_table}({parent_col}) ON DELETE RESTRICT;"
            )
        else:
            # SQLite does not support adding FK via ALTER TABLE; returns instruction/explanation
            return (
                f"-- SQLite Note: Foreign keys must be defined at table creation time.\n"
                f"-- Example definition:\n"
                f"-- CREATE TABLE {child_table} (\n"
                f"--     ...\n"
                f"--     {child_col} INTEGER,\n"
                f"--     FOREIGN KEY ({child_col}) REFERENCES {parent_table}({parent_col})\n"
                f"-- );\n"
                f"PRAGMA foreign_keys = ON;"
            )
