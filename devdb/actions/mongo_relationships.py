"""MongoDB relationship inspection, validation, and migration utilities.

Supports:
- Adding cross-collection reference fields (e.g. orders.customer_email -> customers.email)
- Visual relationship maps: ASCII chain maps and Rich hierarchy trees
- Referential integrity validation: orphan references, missing indexes on reference keys
- Cascading impact analysis: safe_delete_check before deleting documents
"""

from typing import Any, Dict, List, Optional, Set, Tuple
import pymongo
from bson import ObjectId
from rich.tree import Tree
from rich.console import Console

from devdb.mongo_schema import infer_mongo_schema

console = Console()


def add_reference_mongo(
    db: pymongo.database.Database,
    child_coll: str,
    child_match_field: str,
    parent_coll: str,
    parent_match_field: str,
    ref_field_name: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """
    Link existing documents in child_coll to parent_coll by matching an identifier.
    Writes the parent's _id into child_coll[ref_field_name].
    """
    ref_field = ref_field_name or f"{parent_coll.rstrip('s')}_id"
    child = db[child_coll]
    parent = db[parent_coll]

    # Build lookup table from parent collection
    parent_docs = list(parent.find({parent_match_field: {"$exists": True}}, {"_id": 1, parent_match_field: 1}))
    parent_lookup = {
        d[parent_match_field]: str(d["_id"])
        for d in parent_docs
        if d.get(parent_match_field) is not None
    }

    child_docs = list(child.find({child_match_field: {"$exists": True}}, {"_id": 1, child_match_field: 1}))
    matched_count = 0
    updated_count = 0

    for cd in child_docs:
        val = cd.get(child_match_field)
        if val in parent_lookup:
            matched_count += 1
            if not dry_run:
                child.update_one(
                    {"_id": cd["_id"]},
                    {"$set": {ref_field: parent_lookup[val]}}
                )
                updated_count += 1

    return {
        "success": True,
        "operation": "add_reference",
        "child_collection": child_coll,
        "parent_collection": parent_coll,
        "reference_field": ref_field,
        "matched_documents": matched_count,
        "updated_documents": updated_count,
        "dry_run": dry_run,
    }


def build_mongo_ascii_map(schema: Dict[str, Any]) -> str:
    """Generate ASCII representation of MongoDB references and embedded relationships."""
    tables = schema.get("tables", schema)
    lines: List[str] = []
    lines.append("MongoDB Document Relationship Topology:")
    lines.append("────────────────────────────────────────")

    found_rel = False
    for coll_name, meta in tables.items():
        # 1. References
        fks = meta.get("foreign_keys", [])
        for fk in fks:
            found_rel = True
            ref_col = ", ".join(fk.get("constrained_columns", []))
            parent = fk.get("referred_table", "unknown")
            lines.append(f"  {parent} (1) ──ref──< {coll_name} (N) [via {coll_name}.{ref_col}]")

        # 2. Embedded subdocuments / arrays
        for col in meta.get("columns", []):
            if col.get("is_embedded"):
                found_rel = True
                lines.append(f"  {coll_name} (1) ──embed──[ {col['name']} ] ({col['type']})")

    if not found_rel:
        lines.append("  (No cross-collection references or embedded structures detected)")

    return "\n".join(lines)


def render_mongo_rich_tree(schema: Dict[str, Any]) -> Tree:
    """Render Rich Tree showing MongoDB collections, references, and embeddings."""
    tree = Tree("[bold cyan]MongoDB Collections & Relationships[/bold cyan]")
    tables = schema.get("tables", schema)

    for coll_name, meta in tables.items():
        doc_count = meta.get("doc_count", 0)
        coll_node = tree.add(f"[bold yellow]{coll_name}[/bold yellow] [dim]({doc_count} docs)[/dim]")

        # References
        fks = meta.get("foreign_keys", [])
        for fk in fks:
            cols = ", ".join(fk.get("constrained_columns", []))
            ref_coll = fk.get("referred_table", "")
            coll_node.add(f"[blue]──ref──>[/blue] {ref_coll} [dim]({cols})[/dim]")

        # Embeddings
        for col in meta.get("columns", []):
            if col.get("is_embedded"):
                coll_node.add(f"[magenta]──embed──>[/magenta] {col['name']} [dim]({col['type']})[/dim]")

    return tree


def validate_relationships_mongo(db: pymongo.database.Database, schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Audit referential integrity across collections:
    - Orphan references (reference field points to non-existent parent _id)
    - Missing indexes on reference fields
    - Embedding vs. Referencing architectural suggestions
    """
    if not schema:
        schema = infer_mongo_schema(db)

    tables = schema.get("tables", schema)
    all_colls = set(tables.keys())

    orphans_found: List[Dict[str, Any]] = []
    missing_indexes: List[Dict[str, Any]] = []
    embed_suggestions: List[Dict[str, Any]] = []

    for coll_name, meta in tables.items():
        fks = meta.get("foreign_keys", [])
        indexes = meta.get("indexes", [])
        indexed_fields = {
            col for idx in indexes for col in idx.get("column_names", [])
        }

        for fk in fks:
            c_cols = fk.get("constrained_columns", [])
            ref_coll = fk.get("referred_table", "")
            if ref_coll not in all_colls or not c_cols:
                continue

            ref_field = c_cols[0]

            # 1. Missing index check
            if ref_field not in indexed_fields:
                missing_indexes.append({
                    "collection": coll_name,
                    "field": ref_field,
                    "severity": "WARNING",
                    "suggestion": f"Create index on {coll_name}.{ref_field} to optimize join queries."
                })

            # 2. Orphan document scan
            try:
                # Fetch all distinct referenced IDs from child collection
                child_ref_ids = db[coll_name].distinct(ref_field)
                if child_ref_ids:
                    # Query parent for these IDs
                    obj_ids = []
                    str_ids = []
                    for cid in child_ref_ids:
                        if cid is None:
                            continue
                        try:
                            obj_ids.append(ObjectId(str(cid)))
                        except Exception:
                            str_ids.append(cid)

                    parent_query = {"$or": []}
                    if obj_ids:
                        parent_query["$or"].append({"_id": {"$in": obj_ids}})
                    if str_ids:
                        parent_query["$or"].append({"_id": {"$in": str_ids}})

                    existing_parent_ids = set()
                    if parent_query["$or"]:
                        existing_parent_ids = {
                            str(doc["_id"])
                            for doc in db[ref_coll].find(parent_query, {"_id": 1})
                        }

                    # Find orphans
                    orphan_sample = []
                    for cid in child_ref_ids:
                        if cid is not None and str(cid) not in existing_parent_ids:
                            orphan_sample.append(str(cid))

                    if orphan_sample:
                        # Count total affected orphan documents
                        count_orphans = db[coll_name].count_documents({
                            ref_field: {"$in": orphan_sample}
                        })
                        orphans_found.append({
                            "child_collection": coll_name,
                            "reference_field": ref_field,
                            "parent_collection": ref_coll,
                            "orphan_count": count_orphans,
                            "sample_broken_ids": orphan_sample[:5],
                        })
            except Exception as e:
                pass

        # 3. Embed vs. Reference Suggestion
        for fk in fks:
            ref_coll = fk.get("referred_table", "")
            doc_cnt = meta.get("doc_count", 0)
            parent_cnt = tables.get(ref_coll, {}).get("doc_count", 1)
            ratio = (doc_cnt / parent_cnt) if parent_cnt else 0
            if ratio < 5 and doc_cnt < 100:
                embed_suggestions.append({
                    "child_collection": coll_name,
                    "parent_collection": ref_coll,
                    "reason": f"Average child-to-parent ratio is small ({ratio:.1f}:1). Consider embedding if accessed together."
                })

    is_healthy = len(orphans_found) == 0 and len(missing_indexes) == 0

    return {
        "healthy": is_healthy,
        "orphan_references": orphans_found,
        "missing_indexes": missing_indexes,
        "embed_suggestions": embed_suggestions,
    }


def safe_delete_check_mongo(
    db: pymongo.database.Database,
    coll_name: str,
    filter_spec: Dict[str, Any],
    schema: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Check if documents to be deleted in coll_name are referenced by other child collections.
    """
    if not schema:
        schema = infer_mongo_schema(db)

    tables = schema.get("tables", schema)

    # Normalize _id in filter_spec to match both ObjectId and str
    normalized_filter = dict(filter_spec)
    if "_id" in normalized_filter:
        val = normalized_filter["_id"]
        if isinstance(val, str):
            try:
                normalized_filter["_id"] = {"$in": [val, ObjectId(val)]}
            except Exception:
                pass
        elif isinstance(val, ObjectId):
            normalized_filter["_id"] = {"$in": [val, str(val)]}

    target_docs = list(db[coll_name].find(normalized_filter, {"_id": 1}).limit(100))
    if not target_docs:
        return {"allowed": True, "impacts": [], "warning": "No documents match the delete filter."}

    target_ids_str = [str(d["_id"]) for d in target_docs]
    target_ids_obj = [d["_id"] for d in target_docs if isinstance(d["_id"], ObjectId)]
    all_target_ids = list(set(target_ids_str + [str(x) for x in target_ids_obj]))
    all_match_vals = target_ids_str + target_ids_obj
    dependent_impacts = []

    for other_coll, meta in tables.items():
        if other_coll == coll_name:
            continue
        for fk in meta.get("foreign_keys", []):
            if fk.get("referred_table") == coll_name:
                ref_field = fk.get("constrained_columns", [])[0]
                child_count = db[other_coll].count_documents({
                    ref_field: {"$in": all_match_vals}
                })
                if child_count > 0:
                    dependent_impacts.append({
                        "child_collection": other_coll,
                        "reference_field": ref_field,
                        "dependent_documents": child_count,
                    })

    if dependent_impacts:
        impact_summary = ", ".join([f"{imp['child_collection']}: {imp['dependent_documents']}" for imp in dependent_impacts])
        return {
            "allowed": False,
            "impacts": dependent_impacts,
            "warning": f"Cannot safely delete: {len(all_target_ids)} documents are referenced by dependent records ({impact_summary})."
        }

    return {"allowed": True, "impacts": [], "warning": "Safe to delete; no dependent child records found."}


def handle_mongo_relationship_op(db: pymongo.database.Database, op: Dict[str, Any]) -> Dict[str, Any]:
    """Unified handler for relationship operations dispatched from CLI."""
    action = str(op.get("operation") or op.get("action") or "").lower()

    if action == "add_reference":
        return add_reference_mongo(
            db=db,
            child_coll=op.get("child_collection") or op.get("collection", "orders"),
            child_match_field=op.get("child_field", "customer_email"),
            parent_coll=op.get("parent_collection", "customers"),
            parent_match_field=op.get("parent_field", "email"),
            ref_field_name=op.get("ref_field"),
            dry_run=op.get("dry_run", False),
        )
    elif action == "show_relationships":
        schema = infer_mongo_schema(db)
        ascii_map = build_mongo_ascii_map(schema)
        return {
            "success": True,
            "operation": "show_relationships",
            "ascii_map": ascii_map,
            "schema": schema,
        }
    elif action == "validate_relationships":
        report = validate_relationships_mongo(db)
        return {
            "success": True,
            "operation": "validate_relationships",
            "report": report,
        }

    raise ValueError(f"Unknown relationship operation: {action}")


def render_mongo_health_report(report: Dict[str, Any]) -> None:
    """Render Rich formatted tables for MongoDB relationship health and referential integrity."""
    from rich.table import Table
    from rich.panel import Panel

    orphans = report.get("orphan_references", [])
    missing_indexes = report.get("missing_indexes", [])
    embed_sugg = report.get("embed_suggestions", [])

    if not orphans and not missing_indexes:
        console.print(Panel(
            "[bold green]✓ Referential Integrity Clean: All cross-collection references point to valid documents and are properly indexed.[/bold green]",
            title="MongoDB Relationship Health: PASS",
            border_style="green"
        ))
        console.print()
        return

    # 1. Orphan References Table
    if orphans:
        table = Table(
            title="[bold red]Orphan Cross-Collection References Detected[/bold red]",
            show_lines=True,
            header_style="bold red"
        )
        table.add_column("Child Collection", style="cyan")
        table.add_column("Reference Field", style="yellow")
        table.add_column("Parent Collection", style="green")
        table.add_column("Orphan Documents", justify="center", style="bold red")
        table.add_column("Sample Broken _ids", style="dim")

        for o in orphans:
            sample_str = ", ".join(o.get("sample_broken_ids", []))
            table.add_row(
                o["child_collection"],
                o["reference_field"],
                o["parent_collection"],
                str(o["orphan_count"]),
                sample_str
            )
        console.print(table)
        console.print()

    # 2. Missing Indexes Table
    if missing_indexes:
        table = Table(
            title="[bold yellow]Missing Foreign Reference Indexes[/bold yellow]",
            show_lines=True,
            header_style="bold yellow"
        )
        table.add_column("Collection", style="cyan")
        table.add_column("Field", style="yellow")
        table.add_column("Severity", justify="center", style="bold yellow")
        table.add_column("Recommendation", style="white")

        for m in missing_indexes:
            table.add_row(
                m["collection"],
                m["field"],
                m["severity"],
                m["suggestion"]
            )
        console.print(table)
        console.print()

    # 3. Architecture suggestions
    if embed_sugg:
        sugg_lines = []
        for s in embed_sugg:
            sugg_lines.append(f"• [bold cyan]{s['child_collection']}[/bold cyan] -> [bold cyan]{s['parent_collection']}[/bold cyan]: {s['reason']}")
        console.print(Panel(
            "\n".join(sugg_lines),
            title="MongoDB Schema Design Suggestions (Embed vs. Reference)",
            border_style="cyan"
        ))
        console.print()
