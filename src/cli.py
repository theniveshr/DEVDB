"""DevDB Assistant CLI - Powered by Typer and Rich."""

import sys
from typing import Optional
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from rich.prompt import Confirm, Prompt

from src.config import get_database_url, get_llm_provider, validate_dev_database_url, SafetyViolationError
from src.db import DatabaseManager
from src.sql_generator import SQLGenerator
from src.data_generator import FakeDataGenerator
from src.relations import RelationshipManager
from src.user_manager import UserManager

app = typer.Typer(
    help="DevDB Assistant - Natural Language CLI for Development Databases",
    add_completion=False,
)
console = Console()


def get_db() -> DatabaseManager:
    """Helper to instantiate DatabaseManager with safety checks."""
    try:
        url = get_database_url()
        return DatabaseManager(url)
    except SafetyViolationError as e:
        console.print(Panel(f"[bold red]SAFETY VIOLATION DETECTED[/bold red]\n{e}", border_style="red"))
        sys.exit(1)
    except Exception as e:
        console.print(Panel(f"[bold red]Database Connection Error[/bold red]\n{e}", border_style="red"))
        sys.exit(1)


@app.command("init-db")
def init_db_command():
    """Create and seed sample tables (users, products, orders, order_items, user_profiles)."""
    db = get_db()
    with console.status("[bold green]Creating sample database tables and seed records..."):
        db.init_sample_database()
    console.print(
        Panel(
            "[bold green]Sample database initialized successfully![/bold green]\n"
            f"Dialect: [cyan]{db.dialect_name}[/cyan]\n"
            f"Tables created: [yellow]{', '.join(db.get_tables())}[/yellow]",
            title="Database Ready",
            border_style="green",
        )
    )


@app.command("schema")
def schema_command():
    """Display current database tables, columns, types, and foreign key relations."""
    db = get_db()
    summary = db.get_schema_summary()
    console.print(
        Panel(
            summary,
            title=f"Schema Overview ({db.dialect_name})",
            border_style="cyan",
        )
    )


@app.command("query")
def query_command(
    question: str = typer.Argument(..., help="Plain English database question or command"),
    version: str = typer.Option("v3", "--version", "-v", help="Prompt version: v1 (naive), v2 (few-shot), v3 (CoT)"),
    execute: bool = typer.Option(True, "--execute/--no-execute", help="Execute SQL after generation"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM Provider override (openai, gemini, mock)"),
):
    """Translate plain English into SQL and execute with safety checks."""
    db = get_db()
    generator = SQLGenerator(db)

    with console.status(f"[bold blue]Consulting DevDB Assistant ({version.upper()})..."):
        try:
            result = generator.generate(question, version=version, provider=provider)
        except Exception as e:
            console.print(Panel(f"[bold red]LLM Generation Error[/bold red]\n{e}", border_style="red"))
            return

    # If V3 Chain of Thought thinking is present, display it
    if result.thinking:
        console.print(
            Panel(
                result.thinking,
                title="[bold magenta]Chain of Thought Reasoning (V3 CoT)[/bold magenta]",
                border_style="magenta",
            )
        )

    # Display generated SQL / MQL
    syntax_lang = "javascript" if db.dialect_name == "mongodb" else "sql"
    syntax = Syntax(result.sql, syntax_lang, theme="monokai", line_numbers=True)
    op_color = "red" if result.is_destructive else ("yellow" if result.is_write else "green")
    query_type_label = "MQL" if db.dialect_name == "mongodb" else "SQL"
    console.print(
        Panel(
            syntax,
            title=f"[bold]Generated {query_type_label} ({version.upper()})[/bold] - Operation: [{op_color}]{result.operation_type}[/{op_color}]",
            border_style=op_color,
        )
    )

    if not execute:
        return

    # Safety confirmation checks
    if result.is_destructive:
        console.print(
            Panel(
                "[bold red]WARNING: DESTRUCTIVE STATEMENT DETECTED[/bold red]\n"
                "This statement may delete or alter table schemas or rows.",
                border_style="red",
            )
        )
        confirm_text = Prompt.ask("Type [bold red]CONFIRM[/bold red] to execute this destructive command", default="no")
        if confirm_text != "CONFIRM":
            console.print("[yellow]Destructive execution cancelled by user.[/yellow]")
            return
    elif result.is_write:
        if not Confirm.ask("Do you want to execute this write statement?"):
            console.print("[yellow]Write operation cancelled by user.[/yellow]")
            return

    # Execute SQL
    with console.status("[bold green]Executing SQL query..."):
        try:
            columns, rows = db.execute_query(result.sql)
        except Exception as e:
            console.print(Panel(f"[bold red]Query Execution Failed[/bold red]\n{e}", border_style="red"))
            return

    # Render results in a Rich Table
    table = Table(title="Query Results", border_style="cyan")
    for col in columns:
        table.add_column(str(col), style="bold")

    for row in rows:
        table.add_row(*[str(val) if val is not None else "[dim]NULL[/dim]" for val in row])

    console.print(table)
    console.print(f"[dim]Total rows returned: {len(rows)}[/dim]\n")


@app.command("generate-data")
def generate_data_command(
    table_name: str = typer.Argument(..., help="Name of table to populate"),
    rows: int = typer.Option(5, "--rows", "-r", help="Number of fake rows to generate"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM Provider override (openai, gemini, mock)"),
):
    """Generate relation-aware synthetic records using LLM spec + Faker."""
    db = get_db()
    if table_name not in db.get_tables():
        console.print(f"[bold red]Error:[/bold red] Table '{table_name}' does not exist in database.")
        console.print(f"Available tables: {', '.join(db.get_tables())}")
        return

    data_gen = FakeDataGenerator(db)

    with console.status(f"[bold blue]Generating Faker spec and {rows} synthetic rows..."):
        try:
            generated_rows, spec = data_gen.generate_rows(table_name, count=rows, provider=provider)
        except Exception as e:
            console.print(Panel(f"[bold red]Generation Error[/bold red]\n{e}", border_style="red"))
            return

    # Display preview table
    if not generated_rows:
        console.print("[yellow]No rows could be generated.[/yellow]")
        return

    table = Table(title=f"Synthetic Data Preview: {table_name} ({len(generated_rows)} rows)", border_style="green")
    cols = list(generated_rows[0].keys())
    for col in cols:
        table.add_column(col, style="bold")

    for row in generated_rows:
        table.add_row(*[str(row.get(c, "")) for c in cols])

    console.print(table)

    if Confirm.ask(f"Insert these {len(generated_rows)} records into '{table_name}'?"):
        inserted = data_gen.insert_rows(table_name, generated_rows)
        console.print(f"[bold green]Successfully inserted {inserted} rows into {table_name}![/bold green]")
    else:
        console.print("[yellow]Insertion cancelled. No records were written.[/yellow]")


@app.command("relations")
def relations_command(
    action: str = typer.Argument("map", help="Action: 'map' (visual graph), 'orphans' (find orphans), 'safe-delete'"),
    table_name: Optional[str] = typer.Option(None, "--table", "-t", help="Target table for safe-delete"),
    condition: Optional[str] = typer.Option("", "--condition", "-c", help="WHERE condition for safe-delete"),
):
    """Inspect foreign keys, visualize relationships, find orphan rows, and verify safe deletes."""
    db = get_db()
    rel_mgr = RelationshipManager(db)

    if action == "map":
        tree = rel_mgr.build_relationship_tree()
        console.print(Panel(tree, border_style="cyan"))

    elif action == "orphans":
        with console.status("[bold blue]Scanning database for orphan foreign keys..."):
            orphans = rel_mgr.find_orphan_rows()

        if not orphans:
            console.print(Panel("[bold green]All clear! No orphan rows found across foreign key relations.[/bold green]", border_style="green"))
        else:
            table = Table(title="[bold red]Orphan Rows Detected[/bold red]", border_style="red")
            table.add_column("Child Table", style="bold red")
            table.add_column("Child Column")
            table.add_column("Parent Table")
            table.add_column("Orphan FK Value")
            table.add_column("Count", justify="right")

            for o in orphans:
                table.add_row(
                    o["child_table"],
                    o["child_column"],
                    o["parent_table"],
                    str(o["orphan_value"]),
                    str(o["orphan_count"]),
                )
            console.print(table)

    elif action == "safe-delete":
        if not table_name:
            console.print("[bold red]Please specify target table with --table <table_name>[/bold red]")
            return

        with console.status(f"[bold blue]Analyzing cascade impact on '{table_name}'..."):
            impact = rel_mgr.analyze_safe_delete_impact(table_name, condition=condition or "")

        risk_color = "red" if impact["risk_level"] == "HIGH" else ("yellow" if impact["risk_level"] == "MEDIUM" else "green")
        details = [
            f"Parent Table: [bold]{impact['parent_table']}[/bold]",
            f"Target Rows in Parent: [bold]{impact['target_rows_count']}[/bold]",
            f"Risk Level: [{risk_color} bold]{impact['risk_level']}[/{risk_color} bold]",
        ]

        if impact["dependent_children"]:
            details.append("\n[bold red]Dependent Child Tables Affected:[/bold red]")
            for dep in impact["dependent_children"]:
                details.append(
                    f"  - Table: [bold]{dep['child_table']}[/bold] via '{dep['foreign_key']}' -> [red]{dep['affected_rows']} child rows affected[/red]"
                )
        else:
            details.append("\n[green]No dependent child records will be broken by this delete.[/green]")

        console.print(Panel("\n".join(details), title="Safe-Delete Cascade Impact Report", border_style=risk_color))

    else:
        console.print(f"[bold red]Unknown action '{action}'. Choose from 'map', 'orphans', 'safe-delete'.[/bold red]")


@app.command("users")
def users_command(
    request: str = typer.Argument(..., help="Plain English user/role access management request"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="LLM Provider override (openai, gemini, mock)"),
):
    """Manage database users, roles, and permissions (DCL)."""
    db = get_db()
    mgr = UserManager(db)

    with console.status("[bold blue]Generating access control statements..."):
        result = mgr.generate_user_management_sql(request, provider=provider)

    if result.get("warning"):
        console.print(Panel(f"[bold yellow]Dialect Warning:[/bold yellow]\n{result['warning']}", border_style="yellow"))

    syntax = Syntax(result["sql"], "sql", theme="monokai", line_numbers=True)
    console.print(Panel(syntax, title=f"User Management DCL ({result['dialect']})", border_style="cyan"))

    if result.get("is_supported_on_current_db"):
        if Confirm.ask("Execute these user administration statements on PostgreSQL?"):
            with console.status("Executing DCL..."):
                cols, rows = db.execute_query(result["sql"])
            console.print("[bold green]User permissions updated successfully![/bold green]")


@app.command("eval")
def eval_command(
    provider: str = typer.Option("mock", "--provider", "-p", help="LLM provider for evaluation (mock, openai, gemini)"),
):
    """Run prompt engineering evaluation benchmark comparing V1, V2, and V3."""
    from evaluation.eval_runner import run_benchmark
    with console.status(f"[bold blue]Running prompt evaluation benchmark using {provider}..."):
        run_benchmark(provider=provider)


@app.command("interactive")
def interactive_command():
    """Start an interactive chat session with DevDB Assistant."""
    console.print(
        Panel(
            "[bold cyan]DevDB Assistant Interactive Shell[/bold cyan]\n"
            "Ask database questions, generate synthetic data, inspect relations, and manage permissions.\n"
            "Type [yellow]help[/yellow] for examples or [yellow]exit[/yellow] to quit.",
            border_style="cyan",
        )
    )

    db = get_db()
    generator = SQLGenerator(db)

    while True:
        try:
            user_input = Prompt.ask("\n[bold green]DevDB>[/bold green]").strip()
            if not user_input:
                continue

            lower = user_input.lower()
            if lower in ("exit", "quit", "q"):
                console.print("[cyan]Goodbye![/cyan]")
                break
            elif lower == "help":
                console.print(
                    "[bold]Interactive Commands:[/bold]\n"
                    "  - [yellow]init[/yellow] : Initialize sample e-commerce database\n"
                    "  - [yellow]schema[/yellow] : Show schema summary\n"
                    "  - [yellow]relations[/yellow] : Show visual relationship map\n"
                    "  - [yellow]orphans[/yellow] : Check for orphan records\n"
                    "  - Or simply type any question in plain English, e.g.:\n"
                    "    - [cyan]Who are our top spending customers?[/cyan]\n"
                    "    - [cyan]Show me pending orders with customer email[/cyan]\n"
                    "    - [cyan]Delete cancelled orders[/cyan]"
                )
                continue
            elif lower == "init":
                init_db_command()
                continue
            elif lower == "schema":
                schema_command()
                continue
            elif lower == "relations":
                relations_command("map", None, "")
                continue
            elif lower == "orphans":
                relations_command("orphans", None, "")
                continue

            # Standard NL query
            query_command(question=user_input, version="v3", execute=True, provider=None)

        except (KeyboardInterrupt, EOFError):
            console.print("\n[cyan]Session ended.[/cyan]")
            break


def main():
    """Main CLI entrypoint."""
    if len(sys.argv) == 1:
        # Default to interactive mode if no arguments provided
        interactive_command()
    else:
        app()


if __name__ == "__main__":
    main()
