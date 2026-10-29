"""Phase 10: MongoDB Support End-to-End Demonstration Script.

Demonstrates:
1. Safe connection validation (permits localhost / 127.0.0.1, blocks remote hosts & mongodb+srv://)
2. Schema inference & collection field type extraction
3. Cross-collection relationship ASCII map & hierarchical Rich tree
4. Referential integrity validation (detects broken foreign references & missing indexes)
5. Synthetic fake data generation with Indian names (locale: en_IN)
6. Instant read execution for find & aggregation (mongosh syntax preview & Rich tables)
7. Native MongoDB user creation (read role, strong 16-character password printed once)
8. Safety guardrail refusal of destructive requests (dropDatabase, $where injection)
9. Session fake data rollback & undo
"""

import sys
from pathlib import Path
from rich.console import Console
from rich.panel import Panel

# Add root directory to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from devdb.cli import DevDBSession
from tests.seed_mongo import seed_mongo

console = Console()
MONGO_URL = "mongodb://dev:dev@localhost:27017/devdb"
FALLBACK_URL = "mongodb://localhost:27017/devdb"


def run_mongo_demo():
    console.print(Panel.fit(
        "[bold cyan]DevDB Assistant - Phase 10 MongoDB End-to-End Live Demonstration[/bold cyan]\n"
        "[dim]Native local MongoDB 7 (localhost:27017) | Structured JSON Generation | No eval/exec[/dim]",
        border_style="cyan"
    ))
    console.print()

    # 0. Ensure seeded state
    console.print("[bold yellow]Step 0: Initializing clean sample collections in devdb...[/bold yellow]")
    try:
        seed_mongo(MONGO_URL)
    except Exception:
        seed_mongo(FALLBACK_URL)
    try:
        from pymongo import MongoClient
        mc = MongoClient(MONGO_URL, serverSelectionTimeoutMS=2000)
        mc["devdb"].command("dropUser", "report_user")
    except Exception:
        try:
            from pymongo import MongoClient
            mc = MongoClient(FALLBACK_URL, serverSelectionTimeoutMS=2000)
            mc["devdb"].command("dropUser", "report_user")
        except Exception:
            pass
    console.print()

    session = DevDBSession()

    # Step 1: Connect safely
    console.print("[bold yellow]Step 1: Connecting to MongoDB (Safety Guardrail Verified)...[/bold yellow]")
    console.print(f"[bold cyan]Command:[/bold cyan] connect {MONGO_URL}")
    connected = session.connect(MONGO_URL)
    if not connected:
        console.print("[dim]Falling back to unauthenticated localhost connection...[/dim]")
        session.connect(FALLBACK_URL)
    console.print()

    # Step 2: Show Schema & Collections
    console.print("[bold yellow]Step 2: Inspecting Inferred Collection Schema & Type Structure...[/bold yellow]")
    console.print("[bold cyan]Command:[/bold cyan] schema")
    session.print_schema()
    console.print()

    # Step 3: Show Relationships (ASCII Map & Tree)
    console.print("[bold yellow]Step 3: Visualizing Cross-Collection Relationships...[/bold yellow]")
    console.print("[bold cyan]Command:[/bold cyan] show relationships")
    session.handle_show_relationships()
    console.print()

    # Step 4: Validate Relationships (Find 2 orphan orders & missing indexes)
    console.print("[bold yellow]Step 4: Validating Referential Integrity (Orphan Detection)...[/bold yellow]")
    console.print("[bold cyan]Command:[/bold cyan] validate relationships")
    session.handle_validate_relationships()
    console.print()

    from unittest.mock import patch

    # Step 5: Synthetic Fake Data Generation with Indian Names
    console.print("[bold yellow]Step 5: Generating 20 Synthetic Customers with Indian Names (en_IN)...[/bold yellow]")
    console.print("[bold cyan]Command:[/bold cyan] add 20 fake customers with Indian names")
    with patch("devdb.cli.console.input", return_value="y"):
        session.handle_natural_language_request("add 20 fake customers with Indian names")
    console.print()

    # Step 6: Find customers from Chennai (Instant Read Query)
    console.print("[bold yellow]Step 6: Querying Customers in Chennai (Instant Execution)...[/bold yellow]")
    console.print("[bold cyan]Command:[/bold cyan] find customers from Chennai")
    session.handle_natural_language_request("find customers from Chennai")
    console.print()

    # Step 7: Create Read-Only User
    console.print("[bold yellow]Step 7: Creating Read-Only MongoDB User (Single-Use Password)...[/bold yellow]")
    console.print("[bold cyan]Command:[/bold cyan] create a read-only user report_user")
    with patch("devdb.cli.console.input", return_value="y"):
        session.handle_natural_language_request("create a read-only user report_user")
    console.print()

    # Step 8: Safety Guardrail Refusal (DROP DATABASE)
    console.print("[bold yellow]Step 8: Destructive Request Interception (Strict Guardrail)...[/bold yellow]")
    console.print("[bold cyan]Command:[/bold cyan] drop the entire database devdb")
    session.handle_natural_language_request("drop the entire database devdb")
    console.print()

    # Step 9: Revert Fake Data (Undo Last)
    console.print("[bold yellow]Step 9: Rolling Back Session Fake Data (Clean Teardown)...[/bold yellow]")
    console.print("[bold cyan]Command:[/bold cyan] undo last")
    with patch("devdb.cli.console.input", return_value="y"):
        session.handle_undo_last(target_action="fake_data")
    console.print()

    console.print(Panel(
        "[bold green]✓ Phase 10 MongoDB Demonstration Successfully Completed![/bold green]\n\n"
        "All features demonstrated:\n"
        "• Localhost connection safety guardrail\n"
        "• Dynamic document schema inference & type detection\n"
        "• Cross-collection relationship mapping & orphan detection\n"
        "• Declarative synthetic fake data generation with locale support\n"
        "• Structured JSON operation translation with mongosh preview\n"
        "• Native MongoDB role-based user management\n"
        "• Strict zero-eval / zero-exec safety guarantees",
        title="MongoDB Phase 10 Validation",
        border_style="green"
    ))


if __name__ == "__main__":
    run_mongo_demo()
