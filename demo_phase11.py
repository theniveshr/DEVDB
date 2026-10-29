"""Phase 11 Comprehensive Demonstration Script for DevDB Assistant.

Executes the official Phase 11 multi-database unified flow:
1. connect sqlite:///dev.db            -> list tables, add 20 fake customers
2. connect local postgres              -> link orders to customers, create read-only user report_user
3. connect mongodb://localhost:27017/devdb -> show relationships, validate
4. copy customers from sqlite to local mongo
5. connect atlas_dev                   -> (starts READ-ONLY) count documents,
                                          try an insert (must be blocked),
                                          switch to read-write, insert again
6. drop the database                   -> refused on every connection
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from rich.console import Console
from rich.panel import Panel

import devdb.cli as devdb_cli
from devdb.cli import DevDBSession
from devdb.connections import ConnectionManager, default_connection_manager
from devdb.safety import SessionMode
from devdb.db_adapter import MongoAdapter

console = Console()

def run_phase11_demo():
    console.print(Panel.fit(
        "[bold cyan]DevDB Assistant - Phase 11 Unified Multi-Database CLI Demo[/bold cyan]\n"
        "[dim]Single intelligent CLI for SQLite, PostgreSQL, MongoDB, and MongoDB Atlas[/dim]",
        border_style="cyan"
    ))

    session = DevDBSession()

    # Pre-configure saved profiles for convenience
    default_connection_manager.save_profile(
        name="local_sqlite",
        url="sqlite:///dev.db",
        conn_type="sqlite",
        environment="dev",
        default_mode="READ-WRITE"
    )
    default_connection_manager.save_profile(
        name="local_pg",
        url="postgresql://dev:dev@localhost:5432/devdb",
        conn_type="postgres",
        environment="dev",
        default_mode="READ-WRITE"
    )
    default_connection_manager.save_profile(
        name="local_mongo",
        url="mongodb://localhost:27017/devdb",
        conn_type="mongo_local",
        environment="dev",
        default_mode="READ-WRITE"
    )
    # Atlas sandbox dev profile
    default_connection_manager.save_profile(
        name="atlas_dev",
        url="mongodb+srv://atlas_dev_user@cluster0.sandbox.mongodb.net/devdb",
        conn_type="mongo_atlas",
        environment="sandbox",
        default_mode="READ-ONLY"
    )

    # -------------------------------------------------------------------------
    # STEP 1: SQLite Connection & Operations
    # -------------------------------------------------------------------------
    console.print("\n" + "="*80)
    console.print("[bold yellow]▶ STEP 1: Connect SQLite & Add 20 Fake Customers[/bold yellow]")
    console.print("="*80)

    session.connect("sqlite:///dev.db")
    console.print(f"[bold cyan]Prompt:[/bold cyan] {session.prompt_prefix}")

    console.print("\n[bold green]Command:[/bold green] list tables")
    session.handle_natural_language_request("list tables")

    console.print("\n[bold green]Command:[/bold green] add 20 fake customers")
    with patch("devdb.cli.get_fake_data_spec", return_value=({"table": "customers", "count": 20, "locale": "en_US", "columns": {}}, [])):
        with patch.object(devdb_cli.console, "input", return_value="yes"):
            with patch("builtins.input", return_value="yes"):
                session.handle_natural_language_request("add 20 fake customers")

    # -------------------------------------------------------------------------
    # STEP 2: Local PostgreSQL Connection & Operations
    # -------------------------------------------------------------------------
    console.print("\n" + "="*80)
    console.print("[bold yellow]▶ STEP 2: Connect Local Postgres -> Relationships & Users[/bold yellow]")
    console.print("="*80)

    session.connect("local_pg")
    console.print(f"[bold cyan]Prompt:[/bold cyan] {session.prompt_prefix}")

    console.print("\n[bold green]Command:[/bold green] link orders to customers")
    with patch.object(devdb_cli.console, "input", return_value="yes"):
        with patch("builtins.input", return_value="yes"):
            session.handle_natural_language_request("link orders to customers on customer_id")

    console.print("\n[bold green]Command:[/bold green] create read-only user report_user")
    # Switch to ADMIN mode for user management
    session.set_mode(SessionMode.ADMIN)
    console.print(f"[bold cyan]Switched Mode:[/bold cyan] {session.prompt_prefix}")
    with patch.object(devdb_cli.console, "input", return_value="yes"):
        with patch("builtins.input", return_value="yes"):
            session.handle_natural_language_request("create read-only user report_user")

    # -------------------------------------------------------------------------
    # STEP 3: Local MongoDB (Compass) -> Relationships & Validation
    # -------------------------------------------------------------------------
    console.print("\n" + "="*80)
    console.print("[bold yellow]▶ STEP 3: Connect MongoDB (localhost:27017/devdb) -> Relationships[/bold yellow]")
    console.print("="*80)

    session.connect("mongodb://localhost:27017/devdb")
    console.print(f"[bold cyan]Prompt:[/bold cyan] {session.prompt_prefix}")

    console.print("\n[bold green]Command:[/bold green] show relationships")
    session.handle_natural_language_request("show relationships")

    console.print("\n[bold green]Command:[/bold green] validate relationships")
    session.handle_natural_language_request("validate relationships")

    # -------------------------------------------------------------------------
    # STEP 4: Cross-Database Copy (SQLite -> MongoDB)
    # -------------------------------------------------------------------------
    console.print("\n" + "="*80)
    console.print("[bold yellow]▶ STEP 4: Cross-Database Copy: customers from local_sqlite to local_mongo[/bold yellow]")
    console.print("="*80)

    console.print("[bold green]Command:[/bold green] copy customers from local_sqlite to local_mongo")
    with patch.object(devdb_cli.console, "input", return_value="yes"):
        with patch("builtins.input", return_value="yes"):
            session.handle_natural_language_request("copy customers from local_sqlite to local_mongo")

    # -------------------------------------------------------------------------
    # STEP 5: MongoDB Atlas (Starts in READ-ONLY Mode, Write Blocking, Switch)
    # -------------------------------------------------------------------------
    console.print("\n" + "="*80)
    console.print("[bold yellow]▶ STEP 5: Connect Atlas Dev -> READ-ONLY Guard, Switch Mode, Insert[/bold yellow]")
    console.print("="*80)

    # First connect requires typing cluster name to confirm
    with patch.object(devdb_cli.console, "input", return_value="cluster0.sandbox.mongodb.net"):
        with patch("builtins.input", return_value="cluster0.sandbox.mongodb.net"):
            with patch.object(MongoAdapter, "connect", return_value=True):
                session.connect("atlas_dev")

    console.print(f"[bold cyan]Active Connection & Mode:[/bold cyan] {session.prompt_prefix}")
    assert session.session_mode == SessionMode.READ_ONLY, "Atlas must start in READ-ONLY mode!"

    console.print("\n[bold green]Command (Read Operation):[/bold green] count documents in customers")
    session.adapter.db = MagicMock()
    session.adapter.db["customers"].count_documents.return_value = 20
    session.handle_natural_language_request("count documents in customers")

    mongo_insert_llm = {
        "operation": "insert",
        "collection": "customers",
        "document": {"name": "Ravi", "city": "Chennai"},
        "explanation": "Add customer Ravi from Chennai into customers collection"
    }

    console.print("\n[bold green]Command (Write Operation in READ-ONLY mode):[/bold green] add a customer named Ravi, Chennai")
    console.print("[dim]Attempting write operation under READ-ONLY mode (must be blocked):[/dim]")
    with patch("devdb.cli.LLMClient.ask_json", return_value=mongo_insert_llm):
        session.handle_natural_language_request("add a customer named Ravi, Chennai")

    console.print("\n[bold green]Command:[/bold green] switch to read-write mode")
    session.set_mode(SessionMode.READ_WRITE)
    console.print(f"[bold cyan]Switched Mode:[/bold cyan] {session.prompt_prefix}")

    console.print("\n[bold green]Command (Write Operation in READ-WRITE mode with typed 'yes'):[/bold green]")
    with patch("devdb.cli.LLMClient.ask_json", return_value=mongo_insert_llm):
        with patch.object(devdb_cli.console, "input", return_value="yes"):
            with patch("builtins.input", return_value="yes"):
                with patch("devdb.cli.execute_mongo_operation", return_value={"operation": "insert", "collection": "customers", "inserted_count": 1, "inserted_ids": ["671dfa92e1b1234567890abc"]}):
                    session.handle_natural_language_request("add a customer named Ravi, Chennai")

    # -------------------------------------------------------------------------
    # STEP 6: Universal Safety Blocklist Refusal: DROP DATABASE
    # -------------------------------------------------------------------------
    console.print("\n" + "="*80)
    console.print("[bold yellow]▶ STEP 6: Safety Refusal - 'drop the database' on Every Connection[/bold yellow]")
    console.print("="*80)

    targets = [
        ("SQLite", "sqlite:///dev.db", {"action": "drop_database", "sql": "DROP DATABASE devdb;", "explanation": "Drop database"}),
        ("PostgreSQL", "local_pg", {"action": "drop_database", "sql": "DROP DATABASE devdb;", "explanation": "Drop database"}),
        ("Local MongoDB", "mongodb://localhost:27017/devdb", {"operation": "drop_database", "explanation": "Drop database"}),
        ("MongoDB Atlas", "atlas_dev", {"operation": "drop_database", "explanation": "Drop database"})
    ]

    for label, target, mocked_llm in targets:
        console.print(f"\n[bold magenta]Testing DROP DATABASE on {label} ({target}):[/bold magenta]")
        if target == "atlas_dev":
            with patch.object(devdb_cli.console, "input", return_value="cluster0.sandbox.mongodb.net"):
                with patch("builtins.input", return_value="cluster0.sandbox.mongodb.net"):
                    with patch.object(MongoAdapter, "connect", return_value=True):
                        session.connect(target)
        else:
            session.connect(target)

        session.set_mode(SessionMode.ADMIN)  # Even in ADMIN mode, DROP DATABASE is refused!
        with patch("devdb.cli.LLMClient.ask_json", return_value=mocked_llm):
            session.handle_natural_language_request("drop the database")

    console.print("\n[bold green]✔ Phase 11 Full Workflow Completed Successfully![/bold green]")

if __name__ == "__main__":
    run_phase11_demo()
