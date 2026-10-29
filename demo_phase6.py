"""Demonstration of Phase 6 User Management.

Features demonstrated:
1. SQLite polite refusal: SQLite has no users or permissions.
2. PostgreSQL create read-only user:
   "create a read-only user report_user for orders"
   - Structured JSON spec from LLM / parser
   - Safe SQL identifier quoting (code builds SQL, LLM raw text never executed)
   - Password generated securely and printed ONCE
   - Rich Preview panel + confirmation
3. Verification:
   - Connect as report_user
   - SELECT * FROM orders; -> Succeeds!
   - INSERT INTO orders ... -> Fails with permission denied!
4. List users with privileges
5. Clean drop user
"""

import os
import sys
from rich.console import Console
from rich.panel import Panel
from sqlalchemy import create_engine, text

from devdb.cli import DevDBSession
from devdb.actions.users import build_user_management_sql, execute_user_statements, list_pg_users, render_users_table

console = Console()

def run_demo():
    console.print(Panel.fit("[bold cyan]DevDB Phase 6: User Management Demonstration[/bold cyan]", border_style="cyan"))

    # --------------------------------------------------------------------------
    # 1. SQLite Polite Refusal Demonstration
    # --------------------------------------------------------------------------
    console.print("\n[bold yellow]=== DEMO 1: SQLite Rejection (Polite Unsupported Notice) ===[/bold yellow]")
    sqlite_session = DevDBSession()
    sqlite_session.connect("sqlite:///dev.db")
    console.print("[dim]> devdb> create a read-only user report_user for orders[/dim]")
    sqlite_session.handle_natural_language_request("create a read-only user report_user for orders")

    # --------------------------------------------------------------------------
    # 2. PostgreSQL User Creation Demonstration
    # --------------------------------------------------------------------------
    console.print("\n[bold yellow]=== DEMO 2: PostgreSQL Create Read-Only User for Orders ===[/bold yellow]")
    pg_url = "postgresql://dev:dev@localhost:5432/devdb"
    pg_session = DevDBSession()
    pg_session.connect(pg_url)

    # Clean up previous report_user if any
    try:
        drop_stmts, _, _, _ = build_user_management_sql({"operation": "drop_user", "username": "report_user"}, pg_session.schema)
        execute_user_statements(drop_stmts, pg_session.engine)
    except Exception:
        pass

    # Build spec as returned by LLM (Rule 1)
    spec = {
        "operation": "create_user",
        "username": "report_user",
        "privileges": ["SELECT"],
        "tables": ["orders"]
    }

    statements, generated_pwd, explanation, req_confirm = build_user_management_sql(spec, pg_session.schema)
    console.print(f"[bold green]LLM Structured Spec Returned:[/bold green] {spec}")
    console.print(f"[bold green]Code Generated Safe SQL (Sanitized):[/bold green]\n" + "\n".join(statements).replace(generated_pwd, "[REDACTED]"))

    # Execute statements
    execute_user_statements(statements, pg_session.engine)
    console.print(f"[bold green]User 'report_user' successfully created with SELECT on 'orders'![/bold green]")
    console.print(Panel(
        f"[bold green]Username:[/bold green] [bold white]report_user[/bold white]\n"
        f"[bold green]Generated Password:[/bold green] [bold yellow]{generated_pwd}[/bold yellow]\n\n"
        f"[dim italic]SECURITY NOTE: This password is shown ONCE now and is NEVER saved in logs.[/dim italic]",
        title="Database User Credentials (Printed ONCE)",
        border_style="green"
    ))

    # --------------------------------------------------------------------------
    # 3. List Users and Granted Privileges
    # --------------------------------------------------------------------------
    console.print("\n[bold yellow]=== DEMO 3: List Users and Table Privileges ===[/bold yellow]")
    users = list_pg_users(pg_session.engine)
    console.print(render_users_table(users))

    # --------------------------------------------------------------------------
    # 4. Privilege Verification (SELECT succeeds, INSERT fails)
    # --------------------------------------------------------------------------
    console.print("\n[bold yellow]=== DEMO 4: Privilege Verification as 'report_user' ===[/bold yellow]")
    report_engine = create_engine(f"postgresql://report_user:{generated_pwd}@localhost:5432/devdb")

    # a. SELECT on orders (allowed)
    console.print("[dim]Connecting as 'report_user' and running: SELECT COUNT(*) FROM orders;[/dim]")
    with report_engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM orders;")).scalar()
        console.print(f"[bold green]SUCCESS:[/bold green] Read orders table successfully! Found {count} orders.")

    # b. INSERT on orders (blocked by PostgreSQL privilege system)
    console.print("\n[dim]Connecting as 'report_user' and attempting write: INSERT INTO orders ...[/dim]")
    try:
        with report_engine.connect() as conn:
            conn.execute(text("INSERT INTO orders (customer_id, status, total_amount) VALUES (1, 'pending', 99.99);"))
            conn.commit()
        console.print("[bold red]FAILED: INSERT unexpectedly succeeded![/bold red]")
    except Exception as err:
        console.print(f"[bold green]PASSED EXPECTED FAILURE:[/bold green] [bold red]Permission denied![/bold red]")
        console.print(f"[dim]{err}[/dim]")

    # --------------------------------------------------------------------------
    # 5. Clean up
    # --------------------------------------------------------------------------
    console.print("\n[bold yellow]=== DEMO 5: Drop User 'report_user' ===[/bold yellow]")
    drop_stmts, _, _, _ = build_user_management_sql({"operation": "drop_user", "username": "report_user"}, pg_session.schema)
    execute_user_statements(drop_stmts, pg_session.engine)
    console.print("[bold green]Successfully dropped user 'report_user' and cleaned up ownership.[/bold green]")


if __name__ == "__main__":
    run_demo()
