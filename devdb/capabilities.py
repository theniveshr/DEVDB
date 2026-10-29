"""Capability matrix and feature inquiry engine for DevDB Assistant (Phase 11).

Defines feature coverage across SQLite, PostgreSQL, Local MongoDB, and MongoDB Atlas.
"""

import os
from typing import Any, Dict, List, Optional, Tuple
from rich.console import Console
from rich.table import Table
from rich.panel import Panel


CAPABILITY_MATRIX = {
    "explore": {
        "name": "Schema & Entity Exploration",
        "description": "List tables/collections, column types, sample rows, storage stats",
        "SQLITE": True,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": True,
    },
    "query": {
        "name": "Natural Language Querying",
        "description": "Filters, sorting, multi-table joins, grouped aggregations",
        "SQLITE": True,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": True,
    },
    "insert": {
        "name": "Direct Record Insertion",
        "description": "Add individual records manually with instant confirmation",
        "SQLITE": True,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": True,
    },
    "fake_data": {
        "name": "Synthetic Fake Data Generation",
        "description": "Topological dependency ordering, Faker locales (en_IN, en_US)",
        "SQLITE": True,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": True,
    },
    "update_delete": {
        "name": "Guarded Updates & Deletions",
        "description": "WHERE/filter verification, safe_delete cascading checks",
        "SQLITE": True,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": True,
    },
    "schema_changes": {
        "name": "DDL & Schema Evolution",
        "description": "Create/alter/drop tables or collections, add columns",
        "SQLITE": True,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": True,
    },
    "indexes": {
        "name": "Index Management",
        "description": "Create single, compound, or unique indexes; detect missing indexes",
        "SQLITE": True,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": True,
    },
    "relationships": {
        "name": "Relationship Engine",
        "description": "Add FK links, ASCII chain maps, Mermaid diagrams, orphan audits",
        "SQLITE": True,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": True,
    },
    "data_io": {
        "name": "Data Import, Export, & Backup",
        "description": "CSV/JSON export, CSV/JSON import with preview, full backups",
        "SQLITE": True,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": True,
    },
    "cross_copy": {
        "name": "Cross-Database Copy",
        "description": "Copy tables/collections across any two saved connections",
        "SQLITE": True,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": True,
    },
    "history_undo": {
        "name": "Session History & Undo",
        "description": "Audit trails in session.jsonl, reversible fake data and writes",
        "SQLITE": True,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": True,
    },
    "user_management": {
        "name": "User & Role Administration",
        "description": "Create users, grant/revoke roles, strong 16-character passwords",
        "SQLITE": False,
        "POSTGRESQL": True,
        "MONGO_LOCAL": True,
        "MONGO_ATLAS": "conditional",  # Requires Atlas Admin API keys
    },
}


def has_atlas_admin_api_credentials() -> bool:
    """Check if Atlas Admin API credentials are provided in environment."""
    return bool(
        os.getenv("ATLAS_PUBLIC_KEY")
        and os.getenv("ATLAS_PRIVATE_KEY")
        and os.getenv("ATLAS_PROJECT_ID")
    )


def is_capability_supported(capability: str, db_type: str) -> Tuple[bool, str]:
    """
    Check if a capability is supported on the active database type.
    Returns: (supported: bool, message: str)
    """
    db_key = db_type.upper().strip()
    if db_key not in ("SQLITE", "POSTGRESQL", "MONGO_LOCAL", "MONGO_ATLAS"):
        db_key = "SQLITE"

    alias_map = {
        "users": "user_management",
        "user": "user_management",
        "roles": "user_management",
        "copy": "cross_copy",
        "cross_database_copy": "cross_copy",
        "backup": "data_io",
        "import": "data_io",
        "export": "data_io",
        "schema": "schema_changes",
        "ddl": "schema_changes",
        "indexes": "indexes",
        "index": "indexes",
        "relationships": "relationships",
        "relation": "relationships",
    }
    normalized_cap = alias_map.get(capability.lower().strip(), capability.lower().strip())

    cap_info = CAPABILITY_MATRIX.get(normalized_cap)
    if not cap_info:
        return True, "Standard capability."

    status = cap_info.get(db_key, False)

    if status is True:
        return True, f"{cap_info['name']} is fully supported on {db_key}."
    elif status is False:
        if normalized_cap == "user_management" and db_key == "SQLITE":
            return False, "SQLite is a single-user file-based database and does not support user management."
        return False, f"{cap_info['name']} is not supported on {db_key}."
    elif status == "conditional":
        if has_atlas_admin_api_credentials():
            return True, "Atlas user management available via Atlas Admin API keys."
        return False, (
            "MongoDB Atlas user management requires Atlas Admin API credentials "
            "(ATLAS_PUBLIC_KEY, ATLAS_PRIVATE_KEY, ATLAS_PROJECT_ID in .env). "
            "Otherwise, database users must be managed in the MongoDB Atlas Web Console."
        )

    return True, "Supported."


def render_capability_matrix(current_db_type: str = "SQLITE", console: Optional[Console] = None) -> Table:
    """Render Rich table displaying capability matrix for all databases, highlighting the active one."""
    c = console or Console()
    cur_key = current_db_type.upper().strip()

    table = Table(
        title="DevDB Assistant - Database Capability Matrix",
        show_lines=True,
        header_style="bold magenta",
        title_style="bold cyan"
    )
    table.add_column("Feature / Capability", style="white", min_width=24)
    table.add_column("SQLite", justify="center")
    table.add_column("PostgreSQL", justify="center")
    table.add_column("Local Mongo (Compass)", justify="center")
    table.add_column("MongoDB Atlas", justify="center")

    def format_status(val: Any, target_db: str) -> str:
        is_active = (target_db == cur_key)
        prefix = "[bold underline]" if is_active else ""
        suffix = "[/bold underline]" if is_active else ""

        if val is True:
            return f"{prefix}[green]✔ Supported[/green]{suffix}"
        elif val is False:
            return f"{prefix}[dim red]✘ Unsupported[/dim red]{suffix}"
        elif val == "conditional":
            has_keys = has_atlas_admin_api_credentials()
            color = "green" if has_keys else "yellow"
            lbl = "✔ API Keys" if has_keys else "⚠ Atlas Dashboard"
            return f"{prefix}[{color}]{lbl}[/{color}]{suffix}"
        return str(val)

    for cap_key, info in CAPABILITY_MATRIX.items():
        table.add_row(
            f"[bold]{info['name']}[/bold]\n[dim]{info['description']}[/dim]",
            format_status(info["SQLITE"], "SQLITE"),
            format_status(info["POSTGRESQL"], "POSTGRESQL"),
            format_status(info["MONGO_LOCAL"], "MONGO_LOCAL"),
            format_status(info["MONGO_ATLAS"], "MONGO_ATLAS"),
        )

    return table
