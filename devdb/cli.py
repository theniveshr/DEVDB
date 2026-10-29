"""DevDB Interactive CLI using Typer and Rich.

Supports:
- Natural language to SQL translation
- Rich syntax-highlighted preview with risk classification (LOW, MEDIUM, HIGH)
- Instant execution for read-only queries (limit 50 rows)
- Interactive confirmation ("Run? [y/N]" or table-name typing for DROP TABLE)
- Transactional execution with automatic rollback on error
- CLI flags: --dry-run, --version, --url
- Session recording into logs/session.jsonl

"""

import sys
import os
import re
import json
import shlex
from typing import Optional, Any, List, Dict, Set, Tuple
from urllib.parse import urlparse
from pathlib import Path

# Enable UTF-8 encoding on Windows to support Rich glyphs
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.syntax import Syntax
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from devdb.config import PROMPT_VERSION
from devdb.safety import (
    is_dev_connection,
    check_sql,
    check_mongo_op,
    SafetyViolationError,
    SessionMode,
    validate_remote_atlas_connection,
    is_production_named,
    extract_cluster_or_host,
)
from devdb.connections import (
    default_connection_manager,
    ConnectionProfile,
    sanitize_url_for_storage,
    detect_connection_type,
)
from devdb.db_adapter import (
    create_adapter,
    BaseAdapter,
    MongoAdapter,
    SQLAdapter,
    SQLiteAdapter,
    PostgresAdapter,
    AtlasConnectionDiagnosticError,
)
from devdb.router import classify_intent, IntentClassification
from devdb.capabilities import render_capability_matrix, is_capability_supported
from devdb.actions.data_io import (
    export_data,
    preview_import_data,
    import_data,
    backup_database,
    copy_across_connections,
)
from devdb.schema import get_schema, get_mongo_schema, schema_to_text
from devdb.mongo_ops import to_mongosh_syntax, execute_mongo_operation
from devdb.actions.mongo_relationships import (
    build_mongo_ascii_map,
    render_mongo_rich_tree,
    validate_relationships_mongo,
    render_mongo_health_report,
    safe_delete_check_mongo,
)
from devdb.actions.mongo_fake_data import (
    generate_mongo_fake_data,
    delete_mongo_session_fake_data,
)
from devdb.actions.mongo_users import handle_mongo_user_op, drop_mongo_user
from devdb.prompt_loader import load_prompt
from devdb.llm import LLMClient
from devdb.logger import log_session_event, get_session_history, render_history_table
from devdb.actions.react import (
    is_retryable_error,
    format_beginner_friendly_error,
    build_react_correction_prompt,
    render_observation_panel,
    render_react_step,
)
from devdb.actions.fake_data import (
    validate_spec,
    generate_rows,
    insert_rows,
    get_table_dependency_order,
    delete_session_fake_data,
    get_fake_data_spec,
    generate_whole_database,
    EmptyParentTableError,
    TRACKER_FILE,
)
from devdb.actions.relationships import (
    generate_add_relationship_sql,
    execute_relationship_sql,
    show_relationships,
    validate_relationships,
    render_relationship_health_report,
    safe_delete_check,
)
from devdb.actions.users import (
    SQLITE_UNSUPPORTED_MESSAGE,
    build_user_management_sql,
    list_pg_users,
    render_users_table,
    execute_user_statements,
    parse_user_request_nl,
    mask_password_in_sql,
    CONFIRMATION_SUPERUSER_KEYWORD,
    CONFIRMATION_GRANT_ALL_KEYWORD,
)

app = typer.Typer(help="DevDB - AI-assisted development database management CLI", add_completion=False)
console = Console()


class DevDBSession:
    """Maintains active database connection, schema, session configuration, and logger state."""

    def __init__(self):
        self.engine: Optional[Engine] = None
        self.mongo_client: Optional[Any] = None
        self.mongo_db: Optional[Any] = None
        self.adapter: Optional[BaseAdapter] = None
        self.url: Optional[str] = None
        self.db_type: Optional[str] = None
        self.active_profile_name: Optional[str] = None
        self.session_mode: SessionMode = SessionMode.READ_WRITE
        self.schema: Optional[dict] = None
        self.prompt_version: str = PROMPT_VERSION
        self.dry_run: bool = False
        self.verbose: bool = False
        self.undo_stack: List[Dict[str, Any]] = []
        self.last_explanation: str = ""
        self.last_reasoning: Any = None
        self.confirmed_clusters: set = set()
        self.llm_provider: str = os.getenv("LLM_PROVIDER", "ollama")
        self.llm_model: str = os.getenv("LLM_MODEL", "llama3.2")

    def get_llm_client(self) -> LLMClient:
        """Return an LLMClient instance configured with active provider and model overrides."""
        return LLMClient(provider=self.llm_provider, model=self.llm_model)

    def handle_model_info(self) -> None:
        """Display active LLM provider, model, endpoint, context options, and JSON retry statistics."""
        client = self.get_llm_client()
        prov_name = (client.provider or "ollama").upper()
        model_name = client.model or "llama3.2"
        endpoint = getattr(client.provider_instance, "url", "N/A") if hasattr(client, "provider_instance") and client.provider_instance else os.getenv("OLLAMA_URL", "http://localhost:11434")
        num_ctx = getattr(client.provider_instance, "num_ctx", 4096) if hasattr(client, "provider_instance") and client.provider_instance else 4096
        temp = getattr(client.provider_instance, "temperature", 0.1) if hasattr(client, "provider_instance") and client.provider_instance else 0.1
        retry_cnt = getattr(client, "json_retry_count", 0)
        total_retries = LLMClient.total_json_retries

        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Property", style="bold cyan")
        table.add_column("Value", style="bold white")
        table.add_row("LLM Provider", prov_name)
        table.add_row("LLM Model", model_name)
        table.add_row("Endpoint URL", str(endpoint))
        table.add_row("Context Window", f"{num_ctx} tokens (num_ctx)")
        table.add_row("Temperature", str(temp))
        table.add_row("JSON Retries Needed", f"{total_retries} total ({retry_cnt} in current client)")

        console.print(Panel(
            table,
            title="[bold green]Active LLM Model Information[/bold green]",
            border_style="cyan"
        ))
        console.print()

    @property
    def is_connected(self) -> bool:
        return self.engine is not None or self.mongo_client is not None or self.adapter is not None

    @property
    def is_atlas(self) -> bool:
        if self.url and (self.url.startswith("mongodb+srv://") or ".mongodb.net" in self.url):
            return True
        if self.adapter and getattr(self.adapter, "is_atlas", False):
            return True
        return False

    @property
    def prompt_prefix(self) -> str:
        if self.is_connected:
            p_label = self.active_profile_name or self.get_short_connection_name()
            m_label = self.session_mode
            return f"devdb[{p_label}|{m_label}]>"
        return "devdb>"

    def get_short_connection_name(self) -> str:
        """Derive a concise human-readable name for the prompt."""
        if self.active_profile_name:
            return self.active_profile_name
        if not self.url:
            return "disconnected"
        if "sqlite" in self.url:
            cleaned = self.url.replace("sqlite:///", "").replace("sqlite://", "")
            return os.path.basename(cleaned) or "sqlite"
        try:
            parsed = urlparse(self.url)
            if self.is_atlas:
                return "atlas"
            db = (parsed.path or "").lstrip("/").split("?")[0]
            if db:
                return db
        except Exception:
            pass
        return (self.db_type or "db").lower()

    def print_startup_examples(self) -> None:
        """Print 5 beginner-friendly example queries on startup/connect."""
        if self.is_atlas:
            examples = [
                "Count documents in all collections",
                "Find documents in customers with total_spent > 100",
                "Switch to read-write mode",
                "Export customers to customers.json",
                "What can you do on this database?",
            ]
        elif self.db_type == "MONGODB":
            examples = [
                "Show all collections",
                "Add 10 fake customers with address and tags",
                "Find all customers where city is 'Berlin' and order by name",
                "Create an index on email in the customers collection",
                "What can you do on this database?",
            ]
        elif self.db_type == "POSTGRESQL":
            examples = [
                "Show all tables and row counts",
                "Create a read-only user report_user for orders and customers",
                "Link orders.customer_id to customers.id",
                "Validate relationships and check for orphan rows",
                "Find the top 5 customers by total order amount",
            ]
        else:  # SQLite
            examples = [
                "Show all tables and their columns",
                "Add 20 fake customers",
                "Find all customers from London who signed up this month",
                "Export customers to customers.csv",
                "Show relationships between orders and customers",
            ]

        bullets = "\n".join([f"  [cyan]•[/cyan] [white]{ex}[/white]" for ex in examples])
        console.print(Panel(
            f"[bold]💡 5 things you can try in plain English:[/bold]\n{bullets}",
            title=f"Quick Start Examples ({self.db_type or 'DevDB'})",
            border_style="dim cyan"
        ))
        console.print()

    def connect(self, target: str, environment: Optional[str] = None, mode: Optional[str] = None) -> bool:
        """Connect to SQLite, PostgreSQL, local MongoDB, or MongoDB Atlas using URL or profile name."""
        clean_target = target.strip()
        profile_name = None
        raw_url = clean_target
        resolved_env = environment
        resolved_mode = mode

        # 1. Check if target is a saved profile name
        prof = default_connection_manager.get_profile(clean_target)
        if prof:
            profile_name = prof.name
            raw_url = default_connection_manager.resolve_url_with_password(prof.name)
            resolved_env = resolved_env or prof.environment
            resolved_mode = resolved_mode or prof.default_mode

        # 2. Normalize SQLite shorthand paths
        if (
            raw_url.endswith((".db", ".sqlite", ".sqlite3"))
            and not raw_url.startswith("sqlite:")
            and "://" not in raw_url
        ):
            raw_url = f"sqlite:///{raw_url}"

        # 3. Detect if Atlas
        is_atlas_target = BaseAdapter.is_atlas_url(raw_url)
        resolved_env = resolved_env or ("sandbox" if is_atlas_target else "dev")

        # 4. Safety Validation
        if is_atlas_target:
            ok, reason, cluster_host = validate_remote_atlas_connection(
                raw_url,
                environment=resolved_env,
                profile_name=profile_name or ""
            )
            if not ok:
                console.print(Panel(
                    f"[bold red]SAFETY GUARDRAIL - REMOTE ATLAS BLOCKED[/bold red]\n\n"
                    f"{reason}",
                    title="Refused by DevDB Safety",
                    border_style="red"
                ))
                return False

            # Atlas First Connect Confirmation: type cluster or db name
            cluster_name = cluster_host or extract_cluster_or_host(raw_url)
            parsed = urlparse(raw_url)
            db_name = (parsed.path or "").lstrip("/").split("?")[0] or "devdb"
            if cluster_name not in self.confirmed_clusters:
                console.print(Panel(
                    f"[bold yellow]REMOTE ATLAS CLUSTER FIRST CONNECT[/bold yellow]\n\n"
                    f"Cluster: [bold white]{cluster_name}[/bold white]\n"
                    f"Database: [bold white]{db_name}[/bold white]\n"
                    f"Environment: [bold green]{resolved_env}[/bold green]\n"
                    f"Initial Mode: [bold magenta]READ-ONLY[/bold magenta]\n\n"
                    f"[italic]To prevent accidental cloud operations, type the cluster or database name to confirm connecting.[/italic]",
                    title="Atlas Cloud Security Gate",
                    border_style="yellow"
                ))
                conf_val = console.input(f"Type '[bold cyan]{cluster_name}[/bold cyan]' or '[bold cyan]{db_name}[/bold cyan]' to confirm: ").strip()
                if conf_val != cluster_name and conf_val != db_name:
                    console.print("[bold red]Confirmation mismatch. Connection to Atlas cancelled.[/bold red]\n")
                    return False
                self.confirmed_clusters.add(cluster_name)
        else:
            if not is_dev_connection(raw_url):
                console.print(
                    f"[bold red]Connection Refused:[/bold red] '{target}' is not a permitted dev database.\n"
                    "[bold yellow]Safety Guardrail:[/bold yellow] Only SQLite files or localhost / 127.0.0.1 hosts are allowed.\n"
                )
                return False

        # 5. Create and connect adapter
        try:
            adapter = create_adapter(
                raw_url,
                environment=resolved_env,
                allow_atlas=True,
                profile_name=profile_name or ""
            )
            adapter.connect()
            schema = adapter.get_schema()
        except AtlasConnectionDiagnosticError as diag_err:
            console.print(Panel(
                str(diag_err),
                title="[bold red]MongoDB Atlas Connection Diagnosis[/bold red]",
                border_style="red"
            ))
            return False
        except Exception as e:
            console.print(f"[bold red]Connection Error:[/bold red] {e}\n")
            return False

        # 6. Update session state
        self.adapter = adapter
        self.url = raw_url
        self.active_profile_name = profile_name
        self.schema = schema

        if hasattr(adapter, "engine"):
            self.engine = adapter.engine
            self.mongo_client = None
            self.mongo_db = None
            self.db_type = adapter.engine.dialect.name.upper()
        elif hasattr(adapter, "client"):
            self.mongo_client = adapter.client
            self.mongo_db = adapter.db
            self.engine = None
            self.db_type = "MONGODB"

        # Mode setup: Atlas starts in READ-ONLY mode; local starts in READ-WRITE (or configured)
        if is_atlas_target:
            self.session_mode = SessionMode.READ_ONLY
        elif resolved_mode and resolved_mode in ("READ-ONLY", "READ-WRITE", "ADMIN"):
            self.session_mode = resolved_mode
        else:
            self.session_mode = SessionMode.READ_WRITE

        entity_count = len(schema.get("tables", {}))
        entity_name = "Collections" if self.db_type == "MONGODB" else "Tables"
        target_display = profile_name or raw_url.split("@")[-1]

        console.print(
            f"[bold green]Connected successfully![/bold green] "
            f"Target: [bold cyan]{target_display}[/bold cyan] ({self.db_type}) | "
            f"Mode: [bold magenta]{self.session_mode}[/bold magenta] | "
            f"{entity_name}: [bold yellow]{entity_count}[/bold yellow]\n"
        )
        self.print_startup_examples()
        return True

    def disconnect(self) -> None:
        """Disconnect active database connection and reset session state."""
        if self.adapter:
            try:
                self.adapter.close()
            except Exception:
                pass
        elif self.mongo_client:
            try:
                self.mongo_client.close()
            except Exception:
                pass
        elif self.engine:
            try:
                self.engine.dispose()
            except Exception:
                pass
        self.adapter = None
        self.engine = None
        self.mongo_client = None
        self.mongo_db = None
        self.url = None
        self.db_type = None
        self.schema = None
        self.active_profile_name = None
        self.session_mode = SessionMode.READ_WRITE
        console.print("[bold green]Disconnected from database.[/bold green]\n")

    def handle_save_connection(self, profile_name: str) -> None:
        """Save the active database connection as a named profile in ~/.devdb/profiles.json."""
        if not self.is_connected or not self.url:
            console.print("[bold red]Error:[/bold red] No active connection to save. Connect first with 'connect <url>'.\n")
            return
        clean_name = profile_name.strip()
        if not clean_name:
            console.print("[bold red]Error:[/bold red] Profile name cannot be empty.\n")
            return

        db_type_map = {
            "SQLITE": "sqlite",
            "POSTGRESQL": "postgres",
            "MONGODB": "mongo_atlas" if self.is_atlas else "mongo_local",
        }
        ptype = db_type_map.get((self.db_type or "").upper(), "sqlite")
        env_label = "sandbox" if self.is_atlas else "dev"
        default_m = self.session_mode

        prof = default_connection_manager.save_profile(
            name=clean_name,
            url=self.url or "",
            environment=env_label,
            default_mode=default_m,
            conn_type=ptype,
        )
        self.active_profile_name = prof.name
        console.print(Panel(
            f"[bold green]Saved connection profile '[bold white]{prof.name}[/bold white]'![/bold green]\n\n"
            f"Type: [bold cyan]{prof.type}[/bold cyan] | Environment: [bold yellow]{prof.environment}[/bold yellow] | Mode: [bold magenta]{prof.default_mode}[/bold magenta]\n"
            f"URL (password-less): [dim]{prof.url}[/dim]\n"
            f"File: [dim]{default_connection_manager.profiles_path}[/dim]",
            title="Connection Profile Saved",
            border_style="green"
        ))
        console.print()

    def handle_list_connections(self) -> None:
        """List saved database profiles in a Rich table."""
        profiles = default_connection_manager.list_profiles()
        if not profiles:
            console.print("[yellow]No saved connection profiles found in ~/.devdb/profiles.json.[/yellow]\n")
            return
        tbl = Table(
            title=f"DevDB Saved Connection Profiles ({len(profiles)})",
            show_lines=True,
            header_style="bold magenta",
            title_style="bold cyan"
        )
        tbl.add_column("Profile Name", style="bold cyan")
        tbl.add_column("Type", style="green")
        tbl.add_column("Environment", style="yellow")
        tbl.add_column("Default Mode", style="magenta")
        tbl.add_column("Sanitized URL", style="dim")
        tbl.add_column("Status", justify="center")

        for p in profiles:
            is_act = "[bold green]* ACTIVE[/bold green]" if p.name == self.active_profile_name else ""
            tbl.add_row(p.name, p.type, p.environment, p.default_mode, p.url, is_act)
        console.print(tbl)
        console.print()

    def handle_current_connection(self) -> None:
        """Display details about active database connection."""
        if not self.is_connected:
            console.print("[yellow]Not currently connected to any database. Use 'connect <url>' or 'switch to <profile>'.[/yellow]\n")
            return
        schema_count = len((self.schema or {}).get("tables", {}))
        ent = "collections" if self.db_type == "MONGODB" else "tables"
        console.print(Panel(
            f"Active Profile: [bold cyan]{self.active_profile_name or '(unnamed direct connection)'}[/bold cyan]\n"
            f"Database Type: [bold white]{self.db_type}[/bold white]\n"
            f"Is Atlas / Cloud: [bold {'yellow' if self.is_atlas else 'green'}]{'YES (MongoDB Atlas)' if self.is_atlas else 'NO (Localhost/SQLite)'}[/bold {'yellow' if self.is_atlas else 'green'}]\n"
            f"Session Mode: [bold magenta]{self.session_mode}[/bold magenta]\n"
            f"Active Schema: [bold yellow]{schema_count} {ent}[/bold yellow]\n"
            f"Target Endpoint: [dim]{sanitize_url_for_storage(self.url or '')}[/dim]",
            title="Active Database Connection",
            border_style="cyan"
        ))
        console.print()

    def handle_capabilities(self) -> None:
        """Display capability matrix for active database."""
        tbl = render_capability_matrix(self.db_type)
        console.print(tbl)
        console.print()

    def handle_explain(self) -> None:
        """Explain the most recent LLM decision or database execution in plain English."""
        if not self.last_explanation and not self.last_reasoning:
            console.print("[yellow]No recent command or LLM decision has been recorded yet to explain.[/yellow]\n")
            return
        lines = [f"[bold white]{self.last_explanation or 'Command executed successfully.'}[/bold white]"]
        if self.last_reasoning:
            r_str = json.dumps(self.last_reasoning, indent=2) if not isinstance(self.last_reasoning, str) else self.last_reasoning
            lines.append(f"\n[dim cyan]Reasoning Details:[/dim cyan]\n[dim]{r_str}[/dim]")
        console.print(Panel(
            "\n".join(lines),
            title="[bold cyan]DevDB Plain English Explanation[/bold cyan]",
            border_style="cyan"
        ))
        console.print()

    def set_mode(self, mode: str) -> None:
        """Switch session mode (READ-ONLY, READ-WRITE, ADMIN)."""
        self.session_mode = mode
        console.print(f"[bold green]Switched session mode to [bold magenta]{mode}[/bold magenta].[/bold green]\n")

    def prompt_admin_mode(self) -> None:
        """Safely confirm entering ADMIN mode."""
        confirm = console.input(
            "[bold red]Enter ADMIN mode? This permits schema changes, user management, and collection/index drops. Confirm [y/N]: [/bold red]"
        ).strip().lower()
        if confirm in ("y", "yes"):
            self.session_mode = SessionMode.ADMIN
            console.print("[bold red]Session mode is now ADMIN.[/bold red]\n")
        else:
            console.print("[yellow]Admin mode switch cancelled.[/yellow]\n")

    def handle_copy_command(self, entity: str, source_target: str = "active", dest_target: str = "active") -> None:
        """Handle cross-database copy with preview and progress bar."""
        # 1. Resolve source adapter
        src_adapter = None
        src_close_needed = False
        if not source_target or source_target.lower() in ("this", "active", "current"):
            if not self.adapter:
                console.print("[bold red]Error:[/bold red] No active connection for source.\n")
                return
            src_adapter = self.adapter
        else:
            prof = default_connection_manager.get_profile(source_target)
            src_url = default_connection_manager.resolve_url_with_password(prof.name) if prof else source_target
            src_env = prof.environment if prof else "dev"
            try:
                src_adapter = create_adapter(src_url, environment=src_env, allow_atlas=True)
                src_adapter.connect()
                src_close_needed = True
            except Exception as e:
                console.print(f"[bold red]Failed to connect to source '{source_target}':[/bold red] {e}\n")
                return

        # 2. Resolve destination adapter
        dst_adapter = None
        dst_close_needed = False
        if not dest_target or dest_target.lower() in ("this", "active", "current"):
            if not self.adapter:
                console.print("[bold red]Error:[/bold red] No active connection for destination.\n")
                if src_close_needed and src_adapter:
                    src_adapter.close()
                return
            dst_adapter = self.adapter
        else:
            prof = default_connection_manager.get_profile(dest_target)
            dst_url = default_connection_manager.resolve_url_with_password(prof.name) if prof else dest_target
            dst_env = prof.environment if prof else "dev"
            try:
                dst_adapter = create_adapter(dst_url, environment=dst_env, allow_atlas=True)
                dst_adapter.connect()
                dst_close_needed = True
            except Exception as e:
                console.print(f"[bold red]Failed to connect to destination '{dest_target}':[/bold red] {e}\n")
                if src_close_needed and src_adapter:
                    src_adapter.close()
                return

        try:
            # 3. Preview record count in source
            source_count = 0
            if isinstance(src_adapter, MongoAdapter):
                source_count = src_adapter.db[entity].count_documents({})
            else:
                from sqlalchemy import text
                with src_adapter.engine.connect() as conn:
                    res = conn.execute(text(f"SELECT COUNT(*) FROM {entity};"))
                    source_count = res.scalar() or 0

            console.print(Panel(
                f"Source: [bold cyan]{source_target}[/bold cyan] ({src_adapter.db_type})\n"
                f"Destination: [bold cyan]{dest_target}[/bold cyan] ({dst_adapter.db_type})\n"
                f"Entity to Copy: [bold white]{entity}[/bold white]\n"
                f"Records to Transfer: [bold yellow]{source_count}[/bold yellow]",
                title="Cross-Database Copy Preview",
                border_style="cyan"
            ))

            if source_count == 0:
                console.print(f"[yellow]Source entity '{entity}' has 0 records. Nothing to copy.[/yellow]\n")
                return

            confirm = console.input(f"Proceed with copying {source_count} records to '{dest_target}'? [y/N]: ").strip().lower()
            if confirm not in ("y", "yes"):
                console.print("[yellow]Copy cancelled by user.[/yellow]\n")
                return

            ok, copied, msg = copy_across_connections(
                source_adapter=src_adapter,
                dest_adapter=dst_adapter,
                entity_name=entity,
                batch_size=500,
                console=console
            )
            if ok:
                console.print(f"[bold green]{msg}[/bold green]\n")
            else:
                console.print(f"[bold red]Copy failed:[/bold red] {msg}\n")
        finally:
            if src_close_needed and src_adapter:
                src_adapter.close()
            if dst_close_needed and dst_adapter:
                dst_adapter.close()

    def handle_export_command(self, entity: str, destination: str, fmt: str = "csv") -> None:
        """Export table or collection to CSV or JSON."""
        if not self.is_connected or not self.adapter:
            console.print("[bold red]Error:[/bold red] No active connection. Connect first with 'connect <url>'.\n")
            return
        ok, cnt, msg = export_data(self.adapter, entity_name=entity, output_path=destination, format_type=fmt)
        if ok:
            console.print(f"[bold green]{msg}[/bold green]\n")
        else:
            console.print(f"[bold red]Export Failed:[/bold red] {msg}\n")

    def handle_import_command(self, source_file: str, entity: str, fmt: str = "csv") -> None:
        """Import CSV or JSON with 5-row preview and row count confirmation."""
        if not self.is_connected or not self.adapter:
            console.print("[bold red]Error:[/bold red] No active connection. Connect first with 'connect <url>'.\n")
            return

        ok, total, preview_rows, types = preview_import_data(source_file)
        if not ok or total == 0:
            console.print(f"[bold red]Cannot import:[/bold red] File '{source_file}' is empty or invalid.\n")
            return

        # Render preview table
        tbl = Table(
            title=f"Import Preview: '{source_file}' ({total} total rows)",
            show_lines=True,
            header_style="bold magenta",
            title_style="bold cyan"
        )
        col_names = list(preview_rows[0].keys()) if preview_rows else []
        for c in col_names:
            t_label = types.get(c, "str")
            tbl.add_column(f"{c}\n({t_label})", style="cyan")

        for r in preview_rows[:5]:
            tbl.add_row(*[str(r.get(c, "")) for c in col_names])

        console.print(tbl)
        confirm = console.input(f"Proceed with importing {total} records into '{entity}'? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            console.print("[yellow]Import cancelled by user.[/yellow]\n")
            return

        ok_imp, inserted, msg = import_data(self.adapter, in_file=source_file, entity_name=entity)
        if ok_imp:
            console.print(f"[bold green]{msg}[/bold green]\n")
            self.schema = self.adapter.get_schema()
        else:
            console.print(f"[bold red]Import Failed:[/bold red] {msg}\n")

    def handle_backup_command(self) -> None:
        """Create a timestamped backup of the active database."""
        if not self.is_connected or not self.adapter:
            console.print("[bold red]Error:[/bold red] No active connection. Connect first with 'connect <url>'.\n")
            return
        ok, msg = backup_database(self.adapter)
        if ok:
            console.print(Panel(
                f"[bold green]Database Backup Successful![/bold green]\n\n{msg}",
                title="DevDB Backup",
                border_style="green"
            ))
            console.print()
        else:
            console.print(f"[bold red]Backup Failed:[/bold red] {msg}\n")

    def print_schema(self) -> None:
        """Display database schema in a Rich formatted table."""
        if not self.is_connected:
            console.print("[bold red]Error:[/bold red] No active database connection. Use 'connect <url>' first.\n")
            return

        try:
            if self.mongo_client and self.mongo_db is not None:
                self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)
            elif self.engine:
                self.schema = get_schema(self.engine)

            tables = self.schema.get("tables", {})
            if not tables:
                entity_name = "collections" if self.db_type == "MONGODB" else "tables"
                console.print(f"[yellow]Connected to {self.db_type}, but no {entity_name} were found in the schema.[/yellow]\n")
                return

            entity_title = "Collections" if self.db_type == "MONGODB" else "Tables"
            table = Table(
                title=f"Database Schema: {self.db_type} ({len(tables)} {entity_title})",
                show_lines=True,
                header_style="bold magenta",
                title_style="bold cyan"
            )

            col_header = "Collection" if self.db_type == "MONGODB" else "Table"
            table.add_column(col_header, style="bold cyan", no_wrap=True)
            table.add_column("Field / Column", style="bold white", no_wrap=True)
            table.add_column("Type", style="green", no_wrap=True)
            table.add_column("Nullable", style="yellow", justify="center", no_wrap=True)
            table.add_column("PK", style="bold magenta", justify="center", no_wrap=True)
            table.add_column("Foreign Keys / References", style="blue")
            table.add_column("Indexes", style="dim")

            for table_name, table_info in tables.items():
                columns = table_info.get("columns", [])
                fks = table_info.get("foreign_keys", [])
                indexes = table_info.get("indexes", [])

                fk_map = {}
                for fk in fks:
                    c_cols = fk.get("constrained_columns", [])
                    ref_table = fk.get("referred_table", "")
                    ref_cols = ", ".join(fk.get("referred_columns", []))
                    desc = f"-> {ref_table}({ref_cols})"
                    for c in c_cols:
                        fk_map[c] = desc

                idx_map = {}
                for idx in indexes:
                    c_cols = idx.get("column_names", [])
                    unique = " [UNIQUE]" if idx.get("unique") else ""
                    idx_name = idx.get("name") or "idx"
                    desc = f"{idx_name}{unique}"
                    for c in c_cols:
                        if c in idx_map:
                            idx_map[c] += f", {desc}"
                        else:
                            idx_map[c] = desc

                if not columns:
                    table.add_row(table_name, "(empty)", "-", "-", "-", "-", "-")
                else:
                    for i, col in enumerate(columns):
                        col_name = col["name"]
                        table_cell = table_name if i == 0 else ""
                        nullable_str = "YES" if col.get("nullable") else "NO"
                        pk_str = "YES" if col.get("primary_key") else "-"
                        fk_str = fk_map.get(col_name, "-")
                        idx_str = idx_map.get(col_name, "-")

                        table.add_row(
                            table_cell,
                            col_name,
                            col.get("type", "UNKNOWN"),
                            nullable_str,
                            pk_str,
                            fk_str,
                            idx_str
                        )

            console.print(table)
            console.print()
        except Exception as e:
            console.print(f"[bold red]Failed to inspect schema:[/bold red] {e}\n")

    def handle_delete_session_fake_data(self, user_request: str) -> None:
        """Handle 'delete the fake data I added this session' with reverse dependency ordering."""
        if not self.is_connected:
            console.print("[bold red]Error:[/bold red] No active database connection. Use 'connect <url>' first.\n")
            return

        if self.engine:
            self.schema = get_schema(self.engine)
        elif self.mongo_client and self.mongo_db is not None:
            self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)

        if self.db_type == "MONGODB" and self.mongo_db is not None:
            deleted_summary = delete_mongo_session_fake_data(self.mongo_db)
        else:
            target = self.engine if self.engine else self.mongo_db
            deleted_summary = delete_session_fake_data(target, self.schema)

        if not deleted_summary:
            console.print("[yellow]No fake data was tracked for this session (logs/fake_data_tracker.json is empty).[/yellow]\n")
            return

        table = Table(
            title="Session Fake Data Cleanup Summary (Children Deleted First)",
            show_lines=True,
            header_style="bold magenta",
            title_style="bold green"
        )
        table.add_column("Table", style="bold cyan")
        table.add_column("Rows Deleted", style="bold red", justify="right")

        total_deleted = 0
        for t_name, count in deleted_summary.items():
            table.add_row(t_name, str(count))
            total_deleted += count

        console.print(table)
        console.print(f"[bold green]Successfully cleaned up {total_deleted} session fake records in dependency order.[/bold green]\n")

        # Refresh schema
        if self.engine:
            self.schema = get_schema(self.engine)
        elif self.mongo_client and self.mongo_db is not None:
            self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)

        log_session_event(
            request=user_request,
            sql=f"DELETE SESSION FAKE DATA ({total_deleted} rows)",
            ran=True,
            success=True,
            action="fake_data",
            risk="HIGH",
            prompt_version=self.prompt_version,
            db_type=self.db_type,
            dry_run=False
        )

    def handle_fake_data_generation(self, user_request: str) -> None:
        """Handle synthetic fake data generation request via Faker and declarative spec."""
        if not self.is_connected:
            console.print("[bold red]Error:[/bold red] No active database connection. Use 'connect <url>' first.\n")
            return

        if self.engine:
            self.schema = get_schema(self.engine)
        elif self.mongo_client and self.mongo_db is not None:
            self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)

        target = self.engine if self.engine else self.mongo_db
        db_type = (self.db_type or "sqlite").lower()
        req_lower = user_request.lower()

        if self.db_type == "MONGODB" and self.mongo_db is not None:
            client = self.get_llm_client()
            sys_prompt = load_prompt(version=self.prompt_version, db_type="mongodb", schema=self.schema)
            try:
                op = client.ask_json(system_prompt=sys_prompt, user_prompt=user_request)
            except Exception:
                coll = "customers"
                for c in ["customers", "orders", "products"]:
                    if c in req_lower:
                        coll = c
                m_cnt = re.search(r"\b(\d+)\b", req_lower)
                cnt = int(m_cnt.group(1)) if m_cnt else 10
                loc = "en_IN" if "indian" in req_lower else "en_US"
                op = {"operation": "insert_fake", "collection": coll, "count": cnt, "locale": loc, "explanation": f"Generate {cnt} fake {coll}"}
            self.handle_mongo_request(user_request, op)
            return

        # Check for whole database generation request
        if any(kw in req_lower for kw in ["whole database", "entire database", "all tables", "full database"]):
            dep_order = get_table_dependency_order(self.schema)
            console.print(
                f"[bold cyan]Dependency Graph Detected:[/bold cyan] Tables will be filled in parent-before-child order: "
                f"[bold yellow]{' -> '.join(dep_order)}[/bold yellow]\n"
            )

            if self.dry_run:
                console.print("[bold yellow][Dry-run mode enabled - fake data was not generated][/bold yellow]\n")
                return

            confirm = console.input("[bold yellow]Generate fake data for all tables? [y/N]:[/bold yellow] ").strip().lower()
            if confirm not in ("y", "yes"):
                console.print("[yellow]Database generation cancelled by user.[/yellow]\n")
                return

            client = self.get_llm_client()
            with console.status("[bold green]Generating fake data for entire database in dependency order...[/bold green]"):
                summary = generate_whole_database(
                    schema=self.schema,
                    engine_or_mongo=target,
                    db_type=db_type,
                    client=client,
                    rows_per_table=15,
                    locale="en_IN" if "indian" in req_lower else "en_US"
                )

            table = Table(
                title="Whole Database Fake Data Insertion Summary",
                show_lines=True,
                header_style="bold magenta",
                title_style="bold green"
            )
            table.add_column("Table (Topological Order)", style="bold cyan")
            table.add_column("Rows Generated & Inserted", style="bold green", justify="right")
            for t_name, count in summary.items():
                table.add_row(t_name, str(count))
            console.print(table)
            console.print("[bold green]All tables populated successfully in foreign key dependency order![/bold green]\n")
            self.undo_stack.append({
                "action": "fake_data",
                "table": "all_tables",
                "rows": sum(summary.values()) if summary else 0,
                "request": user_request
            })
            return

        # Single table generation
        client = self.get_llm_client()
        try:
            with console.status("[bold green]Generating declarative fake data specification from LLM...[/bold green]"):
                spec, warnings = get_fake_data_spec(user_request, self.schema, db_type, client=client)
        except Exception as err:
            console.print(f"[bold red]Failed to create fake data spec:[/bold red] {err}\n")
            return

        for w in warnings:
            console.print(f"[bold yellow]{w}[/bold yellow]")

        # Generate rows
        try:
            rows, gen_warnings = generate_rows(spec, self.schema, target, auto_generate_parents=False)
        except EmptyParentTableError as err:
            console.print(f"[bold yellow]Notice:[/bold yellow] {err}")
            answer = console.input(
                f"[bold yellow]Parent table '{err.parent_table}' has no rows. Generate parent rows first? [Y/n]:[/bold yellow] "
            ).strip().lower()
            if answer in ("", "y", "yes"):
                rows, gen_warnings = generate_rows(spec, self.schema, target, auto_generate_parents=True)
            else:
                console.print("[yellow]Fake data generation aborted.[/yellow]\n")
                return
        except Exception as err:
            console.print(f"[bold red]Generation Error:[/bold red] {err}\n")
            return

        for w in gen_warnings:
            console.print(f"[bold yellow]{w}[/bold yellow]")

        # Preview step: show first 5 rows
        preview_count = min(5, len(rows))
        table_name = spec["table"]
        locale = spec.get("locale", "en_US")
        preview_table = Table(
            title=f"Fake Data Preview: '{table_name}' ({len(rows)} rows to insert, locale: {locale}, showing first {preview_count})",
            show_lines=True,
            header_style="bold magenta",
            title_style="bold cyan"
        )
        if rows:
            for col_name in rows[0].keys():
                preview_table.add_column(col_name, style="cyan")
            for r in rows[:preview_count]:
                preview_table.add_row(*[str(r[c]) if r[c] is not None else "[dim]NULL[/dim]" for c in rows[0].keys()])

        console.print(preview_table)
        console.print()

        if self.dry_run:
            console.print("[bold yellow][Dry-run mode enabled - fake data was not inserted][/bold yellow]\n")
            return

        confirm = console.input("[bold yellow]Run? [y/N]:[/bold yellow] ").strip().lower()
        if confirm not in ("y", "yes"):
            console.print("[yellow]Fake data insertion cancelled by user.[/yellow]\n")
            return

        # Insert rows inside transaction
        try:
            inserted_pks = insert_rows(table_name, rows, target, self.schema)
            console.print(
                f"[bold green]Successfully inserted {len(rows)} fake rows into '{table_name}' inside a transaction![/bold green]"
            )
            console.print(
                f"[dim]Tracked {len(inserted_pks)} primary keys in {TRACKER_FILE}[/dim]\n"
            )

            # Refresh schema
            if self.engine:
                self.schema = get_schema(self.engine)

            self.undo_stack.append({
                "action": "fake_data",
                "table": table_name,
                "rows": len(rows),
                "request": user_request
            })

            log_session_event(
                request=user_request,
                sql=f"INSERT FAKE DATA into {table_name} ({len(rows)} rows)",
                ran=True,
                success=True,
                action="fake_data",
                risk="MEDIUM",
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=False
            )
        except Exception as err:
            console.print(f"[bold red]Transaction rolled back due to error:[/bold red] {err}\n")

    def handle_show_relationships(self, export_mermaid: bool = False, mermaid_file: str = "relationships.md") -> None:
        """Display ASCII relationship map, Rich tree view, and optionally export Mermaid diagram."""
        if not self.is_connected:
            console.print("[bold red]Error:[/bold red] No active database connection. Use 'connect <url>' first.\n")
            return

        if self.db_type == "MONGODB" and self.mongo_db is not None:
            self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)
            ascii_map = build_mongo_ascii_map(self.schema)
            console.print(Panel(ascii_map, title="MongoDB Relationship Map", border_style="cyan"))
            tree = render_mongo_rich_tree(self.schema)
            console.print(tree)
            console.print()
            return

        if self.engine:
            self.schema = get_schema(self.engine)
        elif self.mongo_client and self.mongo_db is not None:
            self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)

        show_relationships(self.schema, export_mermaid=export_mermaid, mermaid_file=mermaid_file)

    def handle_validate_relationships(self) -> Dict[str, Any]:
        """Validate relational integrity: orphan rows, missing indexes, and unconstrained foreign keys."""
        if not self.is_connected:
            console.print("[bold red]Error:[/bold red] No active database connection. Use 'connect <url>' first.\n")
            return {}

        if self.db_type == "MONGODB" and self.mongo_db is not None:
            self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)
            report = validate_relationships_mongo(self.mongo_db, self.schema)
            render_mongo_health_report(report)
            return report

        if self.engine:
            self.schema = get_schema(self.engine)
        elif self.mongo_client and self.mongo_db is not None:
            self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)

        target = self.engine if self.engine else self.mongo_db
        report = validate_relationships(target, self.schema)
        render_relationship_health_report(report)
        return report

    def handle_add_relationship(
        self,
        child_table: str,
        child_col: str,
        parent_table: str,
        parent_col: str,
        rel_type: str = "one_to_many",
        on_delete: str = "RESTRICT"
    ) -> bool:
        """Add relationship (1:N, 1:1, or N:M) with SQL preview, safety check, and transactional execution."""
        if not self.is_connected:
            console.print("[bold red]Error:[/bold red] No active database connection. Use 'connect <url>' first.\n")
            return False

        if self.db_type == "MONGODB" and self.mongo_db is not None:
            op = {
                "operation": "add_reference",
                "child_collection": child_table,
                "child_field": child_col,
                "parent_collection": parent_table,
                "parent_field": parent_col,
                "explanation": f"Link collection '{child_table}.{child_col}' to '{parent_table}.{parent_col}'"
            }
            return self.handle_mongo_request(f"link {child_table}.{child_col} -> {parent_table}.{parent_col}", op)

        if self.engine:
            self.schema = get_schema(self.engine)
        elif self.mongo_client and self.mongo_db is not None:
            self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)

        target = self.engine if self.engine else self.mongo_db
        db_type = (self.db_type or "sqlite").lower()

        try:
            sql, explanation = generate_add_relationship_sql(
                child_table=child_table,
                child_col=child_col,
                parent_table=parent_table,
                parent_col=parent_col,
                schema=self.schema,
                rel_type=rel_type,
                on_delete=on_delete,
                db_type=db_type
            )
        except Exception as err:
            console.print(f"[bold red]Failed to generate relationship SQL:[/bold red] {err}\n")
            return False

        # Preview SQL first
        console.print(Panel(
            f"[bold cyan]Action:[/bold cyan] RELATIONSHIP\n"
            f"[bold cyan]Type:[/bold cyan] {rel_type.upper().replace('_', '-')}\n"
            f"[bold cyan]Risk Level:[/bold cyan] [bold red]HIGH (Schema change / Table migration)[/bold red]\n"
            f"[bold cyan]Explanation:[/bold cyan] {explanation}\n\n"
            f"[bold cyan]Generated Migration SQL:[/bold cyan]",
            title="DevDB Relationship Migration Preview",
            border_style="red"
        ))
        syntax = Syntax(sql, "sql", theme="monokai", line_numbers=False)
        console.print(syntax)
        console.print()

        if self.dry_run:
            console.print("[bold yellow][Dry-run mode enabled - migration was not executed][/bold yellow]\n")
            log_session_event(
                request=f"add_relationship {child_table}.{child_col} -> {parent_table}.{parent_col}",
                sql=sql,
                ran=False,
                success=True,
                action="relationship",
                risk="HIGH",
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=True
            )
            return False

        confirm = console.input("[bold yellow]Run migration? [y/N]:[/bold yellow] ").strip().lower()
        if confirm not in ("y", "yes"):
            console.print("[yellow]Migration cancelled by user.[/yellow]\n")
            return False

        try:
            execute_relationship_sql(sql, target)
            console.print(f"[bold green]Relationship successfully established between '{child_table}' and '{parent_table}'![/bold green]\n")

            if self.engine:
                self.schema = get_schema(self.engine)

            self.undo_stack.append({
                "action": "relationship",
                "sql": sql,
                "child_table": child_table,
                "child_col": child_col,
                "parent_table": parent_table,
                "parent_col": parent_col,
                "request": f"add_relationship {child_table}.{child_col} -> {parent_table}.{parent_col}"
            })

            log_session_event(
                request=f"add_relationship {child_table}.{child_col} -> {parent_table}.{parent_col}",
                sql=sql,
                ran=True,
                success=True,
                action="relationship",
                risk="HIGH",
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=False
            )
            return True
        except Exception as err:
            console.print(f"[bold red]Migration Error:[/bold red] {err}\n")
            log_session_event(
                request=f"add_relationship {child_table}.{child_col} -> {parent_table}.{parent_col}",
                sql=sql,
                ran=True,
                success=False,
                error=str(err),
                action="relationship",
                risk="HIGH",
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=False
            )
            return False

    def handle_safe_delete_check(self, table_name: str, target_filter: Any) -> Dict[str, Any]:
        """Perform pre-deletion impact analysis considering ON DELETE CASCADE / RESTRICT."""
        if not self.is_connected:
            console.print("[bold red]Error:[/bold red] No active database connection. Use 'connect <url>' first.\n")
            return {}

        if self.db_type == "MONGODB" and self.mongo_db is not None:
            self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)
            flt = target_filter if isinstance(target_filter, dict) else {"_id": target_filter}
            res = safe_delete_check_mongo(self.mongo_db, table_name, flt, self.schema)
        else:
            if self.engine:
                self.schema = get_schema(self.engine)
            target = self.engine if self.engine else self.mongo_db
            res = safe_delete_check(table_name, target_filter, target, self.schema)

        if not res.get("allowed"):
            console.print(Panel(
                f"[bold red]DELETE BLOCKED BY REFERENTIAL INTEGRITY[/bold red]\n\n"
                f"[bold white]{res.get('warning')}[/bold white]",
                title=f"Safe Delete Check: '{table_name}'",
                border_style="red"
            ))
        elif res.get("impacts"):
            impact_lines = [f"[bold yellow]{res.get('warning')}[/bold yellow]\n"]
            for imp in res.get("impacts", []):
                impact_lines.append(
                    f"  • Table [bold cyan]{imp['table']}[/bold cyan]: {imp['count']} rows affected "
                    f"([dim]{imp['child_col']} -> {imp['parent_table']}[/dim], ON DELETE {imp['on_delete']})"
                )
            console.print(Panel(
                "\n".join(impact_lines),
                title=f"Safe Delete Check: '{table_name}' (Cascading Impact)",
                border_style="yellow"
            ))
        else:
            console.print(Panel(
                f"[bold green]{res.get('warning')}[/bold green]",
                title=f"Safe Delete Check: '{table_name}'",
                border_style="green"
            ))

        console.print()
        return res

    def handle_safe_delete_check_nl(self, user_request: str) -> None:
        """Parse natural language delete check query like 'safe delete check customer 5' or 'what happens if i delete customer 5'."""
        m = re.search(r"(?:customer|order|product|user)s?\s+(\d+)", user_request, re.IGNORECASE)
        if m:
            word = user_request.lower()
            if "customer" in word:
                tbl = "customers"
            elif "order_item" in word:
                tbl = "order_items"
            elif "order" in word:
                tbl = "orders"
            elif "product" in word:
                tbl = "products"
            elif "user" in word:
                tbl = "users"
            else:
                tbl = "customers"
            target_id = m.group(1)
            self.handle_safe_delete_check(tbl, target_id)
            return

        parts = user_request.split()
        if len(parts) >= 2:
            tbl = parts[-2]
            flt = parts[-1]
            self.handle_safe_delete_check(tbl, flt)
        else:
            console.print("[yellow]Could not determine table and target ID for safe delete check. Usage: safe_delete_check <table> <id>[/yellow]\n")

    def handle_user_management(self, user_request: str, spec: Optional[Dict[str, Any]] = None) -> bool:
        """
        Handle user and permission management for PostgreSQL.
        SQLite and non-PostgreSQL databases are politely refused as unsupported.
        """
        if not self.is_connected:
            console.print("[bold red]Error:[/bold red] No active database connection. Use 'connect <url>' first.\n")
            return False

        db_type = (self.db_type or "").lower()
        if self.db_type == "MONGODB" and self.mongo_db is not None:
            if spec is None:
                if any(k in user_request.lower() for k in ["list users", "list all users", "show users", "show all users"]):
                    spec = {"operation": "list_users"}
                else:
                    client = self.get_llm_client()
                    sys_prompt = load_prompt(version=self.prompt_version, db_type="mongodb", schema=self.schema)
                    try:
                        spec = client.ask_json(system_prompt=sys_prompt, user_prompt=user_request)
                    except Exception as err:
                        console.print(f"[bold red]LLM Error:[/bold red] {err}\n")
                        return False
            return self.handle_mongo_request(user_request, spec)

        # SQLite has no users - reply politely that this is unsupported
        if "postgres" not in db_type:
            console.print(f"[bold yellow]Notice:[/bold yellow] {SQLITE_UNSUPPORTED_MESSAGE}\n")
            log_session_event(
                request=user_request,
                sql="",
                ran=False,
                success=True,
                error="SQLite unsupported: no user management",
                action="create_user",
                risk="LOW",
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=self.dry_run
            )
            return False

        if self.engine:
            self.schema = get_schema(self.engine)

        if spec is None:
            try:
                spec = parse_user_request_nl(user_request, schema=self.schema)
            except Exception as err:
                console.print(f"[bold red]User request parsing error:[/bold red] {err}\n")
                return False

        op = (spec.get("operation") or spec.get("action") or "create_user").lower()

        # Handle List Users
        if op == "list":
            try:
                users = list_pg_users(self.engine)
                table = render_users_table(users)
                console.print(table)
                console.print()
                log_session_event(
                    request=user_request,
                    sql="SELECT rolname FROM pg_roles ...",
                    ran=True,
                    success=True,
                    action="create_user",
                    risk="LOW",
                    prompt_version=self.prompt_version,
                    db_type=self.db_type,
                    dry_run=False
                )
                return True
            except Exception as err:
                console.print(f"[bold red]Failed to list database users:[/bold red] {err}\n")
                return False

        # Build SQL safely with safe identifier quoting
        try:
            statements, gen_pwd, explanation, req_confirm = build_user_management_sql(spec, schema=self.schema)
        except Exception as err:
            console.print(f"[bold red]Validation / SQL Generation Error:[/bold red] {err}\n")
            return False

        masked_sql = mask_password_in_sql("\n".join(statements))

        # Show Rich Preview Panel
        console.print(Panel(
            f"[bold cyan]Action:[/bold cyan] USER MANAGEMENT ({op.upper()})\n"
            f"[bold cyan]Target User:[/bold cyan] {spec.get('username')}\n"
            f"[bold cyan]Risk Level:[/bold cyan] [bold red]HIGH (Authentication / Privilege modification)[/bold red]\n"
            f"[bold cyan]Explanation:[/bold cyan] {explanation}\n\n"
            f"[bold cyan]Generated SQL (Safe Quoting & Sanitized):[/bold cyan]",
            title="DevDB User Management Preview",
            border_style="red"
        ))
        syntax = Syntax(masked_sql, "sql", theme="monokai", line_numbers=False)
        console.print(syntax)
        console.print()

        if self.dry_run:
            console.print("[bold yellow][Dry-run mode enabled - user operation was not executed][/bold yellow]\n")
            log_session_event(
                request=user_request,
                sql=masked_sql,
                ran=False,
                success=True,
                action="create_user",
                risk="HIGH",
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=True
            )
            return False

        # Guardrail confirmation
        if req_confirm:
            keyword = CONFIRMATION_SUPERUSER_KEYWORD if spec.get("is_superuser") else CONFIRMATION_GRANT_ALL_KEYWORD
            confirm_input = console.input(
                f"[bold red]DANGER GUARDRAIL:[/bold red] Type '[bold yellow]{keyword}[/bold yellow]' to confirm superuser / blanket privileges: "
            ).strip()
            if confirm_input != keyword:
                console.print(f"[yellow]Confirmation mismatch. Required '{keyword}'. Operation aborted.[/yellow]\n")
                log_session_event(
                    request=user_request,
                    sql=masked_sql,
                    ran=False,
                    success=False,
                    error="Confirmation mismatch for superuser/blanket privileges",
                    action="create_user",
                    risk="HIGH",
                    prompt_version=self.prompt_version,
                    db_type=self.db_type,
                    dry_run=False
                )
                return False
        else:
            confirm = console.input("[bold yellow]Run? [y/N]:[/bold yellow] ").strip().lower()
            if confirm not in ("y", "yes"):
                console.print("[yellow]Operation cancelled by user.[/yellow]\n")
                log_session_event(
                    request=user_request,
                    sql=masked_sql,
                    ran=False,
                    success=False,
                    error="Cancelled by user",
                    action="create_user",
                    risk="HIGH",
                    prompt_version=self.prompt_version,
                    db_type=self.db_type,
                    dry_run=False
                )
                return False

        # Execute statements in transaction
        try:
            execute_user_statements(statements, self.engine)
            console.print(f"[bold green]User operation '{op}' completed successfully![/bold green]\n")

            # Print generated credentials ONCE in a highlighted panel
            if gen_pwd:
                console.print(Panel(
                    f"[bold green]Username:[/bold green] [bold white]{spec.get('username')}[/bold white]\n"
                    f"[bold green]Generated Password:[/bold green] [bold yellow]{gen_pwd}[/bold yellow]\n\n"
                    f"[dim italic]SECURITY NOTE: This password is shown ONCE now and is NEVER saved in logs.[/dim italic]",
                    title="Database User Credentials (Printed ONCE)",
                    border_style="green"
                ))
                console.print()

            # Refresh schema
            if self.engine:
                self.schema = get_schema(self.engine)

            if op == "create_user":
                self.undo_stack.append({
                    "action": "create_user",
                    "username": spec.get("username"),
                    "request": user_request
                })

            # Log with MASKED password only!
            log_session_event(
                request=user_request,
                sql=masked_sql,
                ran=True,
                success=True,
                action="create_user",
                risk="HIGH",
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=False
            )
            return True
        except Exception as err:
            console.print(f"[bold red]User Operation Error:[/bold red] {err}\n")
            log_session_event(
                request=user_request,
                sql=masked_sql,
                ran=True,
                success=False,
                error=str(err),
                action="create_user",
                risk="HIGH",
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=False
            )
            return False

    def handle_history(self, limit: int = 20) -> None:
        """Display recent interaction history in a Rich table."""
        history = get_session_history(limit=limit)
        if not history:
            console.print("[yellow]No interaction history recorded in logs/session.jsonl yet.[/yellow]\n")
            return
        table = render_history_table(history)
        console.print(table)
        console.print()

    def handle_undo_last(self, target_action: Optional[str] = None) -> bool:
        """Undo the most recent reversible write operation (fake data, user, insert, relationship)."""
        if not self.is_connected:
            console.print("[bold red]Error:[/bold red] No active database connection. Use 'connect <url>' first.\n")
            return False

        target = self.engine if self.engine else self.mongo_db
        last_op = None

        if target_action:
            for i in range(len(self.undo_stack) - 1, -1, -1):
                if self.undo_stack[i].get("action") == target_action:
                    last_op = self.undo_stack.pop(i)
                    break
        else:
            last_op = self.undo_stack.pop() if self.undo_stack else None

        # Check if fake data tracker has records if in-memory stack is empty or target is fake_data
        if (not last_op or target_action == "fake_data") and Path(TRACKER_FILE).exists():
            try:
                import json as _json
                with open(TRACKER_FILE, "r", encoding="utf-8") as f:
                    t_data = _json.load(f)
                if t_data:
                    last_op = {"action": "fake_data", "details": t_data}
            except Exception:
                pass

        if not last_op:
            console.print("[yellow]No reversible operations found in session history.[/yellow]\n")
            return False

        op_action = last_op.get("action")

        # Case A: Fake Data Generation Undo
        if op_action == "fake_data":
            tbl = last_op.get("table", "recent tables")
            rows_count = last_op.get("rows", "all tracked")
            console.print(Panel(
                f"[bold cyan]Action to Revert:[/bold cyan] FAKE DATA GENERATION\n"
                f"[bold cyan]Target Table:[/bold cyan] {tbl}\n"
                f"[bold cyan]Records Tracked:[/bold cyan] {rows_count}\n"
                f"[bold cyan]Reverse Operation:[/bold cyan] Delete all synthetic rows generated in this session",
                title="DevDB Undo Operation (Fake Data)",
                border_style="yellow"
            ))
            confirm = console.input("[bold yellow]Revert this fake data generation? [y/N]:[/bold yellow] ").strip().lower()
            if confirm not in ("y", "yes"):
                console.print("[yellow]Undo cancelled by user.[/yellow]\n")
                self.undo_stack.append(last_op)
                return False

            try:
                if self.db_type == "MONGODB" and self.mongo_db is not None:
                    counts = delete_mongo_session_fake_data(self.mongo_db)
                else:
                    counts = delete_session_fake_data(target, self.schema, tracker_file=Path(TRACKER_FILE))
                total_deleted = sum(counts.values()) if isinstance(counts, dict) else 0
                console.print(f"[bold green]Undo successful! Deleted {total_deleted} fake records ({counts}).[/bold green]\n")
                if self.engine:
                    self.schema = get_schema(self.engine)
                elif self.mongo_client and self.mongo_db is not None:
                    self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)
                log_session_event(
                    request="undo last (fake_data)",
                    sql=f"DELETE FAKE DATA: {counts}",
                    ran=True,
                    success=True,
                    action="undo",
                    risk="MEDIUM",
                    prompt_version=self.prompt_version,
                    db_type=self.db_type,
                    dry_run=False
                )
                return True
            except Exception as err:
                console.print(f"[bold red]Undo failed:[/bold red] {err}\n")
                return False

        # Case B: User Creation Undo (PostgreSQL & MongoDB)
        elif op_action in ("create_user", "create_mongo_user"):
            username = last_op.get("username")
            console.print(Panel(
                f"[bold cyan]Action to Revert:[/bold cyan] CREATE USER\n"
                f"[bold cyan]Target User:[/bold cyan] {username}\n"
                f"[bold cyan]Reverse Operation:[/bold cyan] DROP USER \"{username}\"",
                title="DevDB Undo Operation (Drop Created User)",
                border_style="yellow"
            ))
            confirm = console.input(f"[bold yellow]Drop user '{username}'? [y/N]:[/bold yellow] ").strip().lower()
            if confirm not in ("y", "yes"):
                console.print("[yellow]Undo cancelled by user.[/yellow]\n")
                self.undo_stack.append(last_op)
                return False

            try:
                if self.db_type == "MONGODB" and self.mongo_db is not None:
                    drop_mongo_user(self.mongo_db, username)
                else:
                    drop_stmts, _, _, _ = build_user_management_sql({"operation": "drop_user", "username": username}, schema=self.schema)
                    execute_user_statements(drop_stmts, self.engine)
                console.print(f"[bold green]Undo successful! Dropped user '{username}'.[/bold green]\n")
                log_session_event(
                    request=f"undo last (create_user {username})",
                    sql=f"DROP ROLE \"{username}\";",
                    ran=True,
                    success=True,
                    action="undo",
                    risk="HIGH",
                    prompt_version=self.prompt_version,
                    db_type=self.db_type,
                    dry_run=False
                )
                return True
            except Exception as err:
                console.print(f"[bold red]Undo failed:[/bold red] {err}\n")
                return False

        # Case C: Single INSERT statement undo
        elif op_action == "write":
            raw_sql = last_op.get("sql", "").strip()
            ins_match = re.match(r"^\s*INSERT\s+INTO\s+([`\"\w]+)", raw_sql, re.IGNORECASE)
            if ins_match:
                tbl = ins_match.group(1).strip("`\"")
                console.print(Panel(
                    f"[bold cyan]Action to Revert:[/bold cyan] INSERT STATEMENT\n"
                    f"[bold cyan]Target Table:[/bold cyan] {tbl}\n"
                    f"[bold cyan]Original SQL:[/bold cyan] {raw_sql}\n"
                    f"[bold cyan]Reverse Strategy:[/bold cyan] Delete the most recently inserted row in '{tbl}'",
                    title="DevDB Undo Operation (Revert INSERT)",
                    border_style="yellow"
                ))
                confirm = console.input(f"[bold yellow]Delete the latest row inserted into '{tbl}'? [y/N]:[/bold yellow] ").strip().lower()
                if confirm not in ("y", "yes"):
                    console.print("[yellow]Undo cancelled by user.[/yellow]\n")
                    self.undo_stack.append(last_op)
                    return False

                try:
                    if self.engine:
                        pk_col = "id"
                        if self.schema and tbl in self.schema.get("tables", {}):
                            cols = self.schema["tables"][tbl].get("columns", [])
                            pks = [c["name"] for c in cols if c.get("primary_key")]
                            if pks:
                                pk_col = pks[0]
                        with self.engine.begin() as conn:
                            del_sql = f"DELETE FROM {tbl} WHERE {pk_col} = (SELECT MAX({pk_col}) FROM {tbl});"
                            conn.execute(text(del_sql))
                        console.print(f"[bold green]Undo successful! Removed last inserted row from '{tbl}'.[/bold green]\n")
                        self.schema = get_schema(self.engine)
                        return True
                except Exception as err:
                    console.print(f"[bold red]Undo failed:[/bold red] {err}\n")
                    return False
            else:
                console.print(
                    "[bold yellow]Notice:[/bold yellow] Automatic undo is not supported for arbitrary UPDATE or DELETE statements without a snapshot.\n"
                    "Supported undo operations: fake data generation, user creation, relationship migration, and INSERT statements.\n"
                )
                return False

        # Case D: Relationship Undo
        elif op_action == "relationship":
            c_tbl = last_op.get("child_table")
            c_col = last_op.get("child_col")
            p_tbl = last_op.get("parent_table")
            p_col = last_op.get("parent_col")
            console.print(Panel(
                f"[bold cyan]Action to Revert:[/bold cyan] RELATIONSHIP MIGRATION\n"
                f"[bold cyan]Relationship:[/bold cyan] {c_tbl}.{c_col} -> {p_tbl}.{p_col}\n"
                f"[bold cyan]Reverse Strategy:[/bold cyan] Remove foreign key constraint from '{c_tbl}'",
                title="DevDB Undo Operation (Revert Relationship)",
                border_style="yellow"
            ))
            confirm = console.input(f"[bold yellow]Revert foreign key relationship on '{c_tbl}'? [y/N]:[/bold yellow] ").strip().lower()
            if confirm not in ("y", "yes"):
                console.print("[yellow]Undo cancelled by user.[/yellow]\n")
                self.undo_stack.append(last_op)
                return False

            try:
                if self.engine:
                    is_pg = "postgres" in (self.db_type or "").lower()
                    if is_pg:
                        with self.engine.begin() as conn:
                            conn.execute(text(f"ALTER TABLE \"{c_tbl}\" DROP CONSTRAINT IF EXISTS \"fk_{c_tbl}_{p_tbl}\";"))
                    else:
                        with self.engine.begin() as conn:
                            conn.execute(text("PRAGMA foreign_keys = OFF;"))
                            conn.execute(text(f"CREATE TABLE {c_tbl}_undo AS SELECT * FROM {c_tbl};"))
                            conn.execute(text(f"DROP TABLE {c_tbl};"))
                            conn.execute(text(f"ALTER TABLE {c_tbl}_undo RENAME TO {c_tbl};"))
                            conn.execute(text("PRAGMA foreign_keys = ON;"))
                    console.print(f"[bold green]Undo successful! Removed foreign key constraint on '{c_tbl}'.[/bold green]\n")
                    self.schema = get_schema(self.engine)
                    log_session_event(
                        request=f"undo last (relationship {c_tbl} -> {p_tbl})",
                        sql=f"REVERT RELATIONSHIP {c_tbl} -> {p_tbl}",
                        ran=True,
                        success=True,
                        action="undo",
                        risk="HIGH",
                        prompt_version=self.prompt_version,
                        db_type=self.db_type,
                        dry_run=False
                    )
                    return True
            except Exception as err:
                console.print(f"[bold red]Undo failed:[/bold red] {err}\n")
                return False

        else:
            console.print("[yellow]No reversible action recognized.[/yellow]\n")
            return False

    def handle_react_self_correction(
        self,
        err: Exception,
        failed_sql: str,
        action: str,
        user_request: str,
        max_attempts: int = 2,
    ) -> bool:
        """
        ReAct self-correction loop (Thought -> Action -> Observation).
        Retries up to max_attempts times for syntax, unknown column/table, or type errors.
        Never retries safety blocks.
        """
        can_retry, category = is_retryable_error(err)
        if not can_retry:
            friendly = format_beginner_friendly_error(err, db_type=self.db_type or "sqlite")
            console.print(Panel(friendly, title="Database Error", border_style="red"))
            console.print()
            return False

        render_observation_panel(failed_sql=failed_sql, error=err, attempt=1, db_type=self.db_type or "sqlite")

        client = self.get_llm_client()
        current_sql = failed_sql
        current_err = str(err)

        for attempt in range(1, max_attempts + 1):
            with console.status(f"[bold cyan]DevDB ReAct Loop: Reflecting on error (Attempt {attempt}/{max_attempts})...[/bold cyan]"):
                sys_prompt, corr_prompt = build_react_correction_prompt(
                    original_request=user_request,
                    failed_sql=current_sql,
                    db_error=current_err,
                    schema=self.schema,
                    db_type=self.db_type or "sqlite",
                    attempt=attempt,
                    max_attempts=max_attempts
                )
                try:
                    resp = client.ask_json(system_prompt=sys_prompt, user_prompt=corr_prompt)
                except Exception as llm_err:
                    console.print(f"[bold red]ReAct Loop LLM Error:[/bold red] {llm_err}\n")
                    break

            thought = resp.get("thought") or resp.get("explanation") or "Diagnosed schema structure and corrected SQL."
            corrected_sql = (resp.get("sql") or "").strip()
            new_action = resp.get("action") or action
            explanation = resp.get("explanation") or ""
            reasoning = resp.get("reasoning")

            if not corrected_sql:
                console.print("[yellow]ReAct correction loop: No corrected SQL was generated.[/yellow]\n")
                break

            allowed, risk, safety_msg = check_sql(corrected_sql)
            if not allowed:
                console.print(Panel(
                    f"[bold red]SAFETY GUARDRAIL - CORRECTED SQL BLOCKED[/bold red]\n\n"
                    f"{safety_msg}\n\n"
                    f"[dim]Attempted SQL:[/dim] {corrected_sql}",
                    title="Safety Guardrail (ReAct Interrupted)",
                    border_style="red"
                ))
                log_session_event(
                    request=user_request,
                    sql=corrected_sql,
                    ran=False,
                    success=False,
                    error=f"ReAct safety block: {safety_msg}",
                    action=new_action,
                    risk=risk,
                    prompt_version="v4",
                    db_type=self.db_type,
                    dry_run=self.dry_run
                )
                break

            render_react_step(
                attempt=attempt,
                max_attempts=max_attempts,
                thought=thought,
                action=new_action,
                sql=corrected_sql,
                explanation=explanation,
                reasoning=reasoning,
                verbose=self.verbose
            )

            if self.dry_run:
                console.print("[bold yellow][Dry-run mode enabled - corrected SQL was not executed][/bold yellow]\n")
                return True

            if new_action == "query" or (risk == "LOW" and re.match(r"^\s*(?:SELECT|EXPLAIN|WITH)\b", corrected_sql, re.IGNORECASE)):
                try:
                    if self.engine:
                        with self.engine.connect() as conn:
                            res = conn.execute(text(corrected_sql))
                            rows = res.fetchmany(50)
                            keys = list(res.keys())

                        if not rows:
                            console.print("[yellow]Observation: Corrected query executed successfully (0 rows returned).[/yellow]\n")
                        else:
                            result_table = Table(
                                title=f"Observation: Corrected Query Succeeded ({len(rows)} rows)",
                                show_lines=True,
                                header_style="bold green"
                            )
                            for k in keys:
                                result_table.add_column(str(k), style="cyan")
                            for row in rows:
                                result_table.add_row(*[str(val) if val is not None else "[dim]NULL[/dim]" for val in row])
                            console.print(result_table)
                            console.print()

                    log_session_event(
                        request=user_request,
                        sql=corrected_sql,
                        ran=True,
                        success=True,
                        action=new_action,
                        risk=risk,
                        prompt_version="v4",
                        db_type=self.db_type,
                        dry_run=False
                    )
                    return True
                except Exception as sub_err:
                    can_sub_retry, _ = is_retryable_error(sub_err)
                    render_observation_panel(failed_sql=corrected_sql, error=sub_err, attempt=attempt, db_type=self.db_type or "sqlite")
                    if not can_sub_retry:
                        break
                    current_sql = corrected_sql
                    current_err = str(sub_err)

            else:
                confirm = console.input("[bold yellow]Run corrected write? [y/N]:[/bold yellow] ").strip().lower()
                if confirm not in ("y", "yes"):
                    console.print("[yellow]Corrected write cancelled by user.[/yellow]\n")
                    return False

                try:
                    if self.engine:
                        with self.engine.begin() as conn:
                            res = conn.execute(text(corrected_sql))
                            affected = getattr(res, "rowcount", None)
                        aff_str = f" ({affected} rows affected)" if affected is not None and affected != -1 else ""
                        console.print(Panel(
                            f"[bold green]Observation: Transaction committed successfully!{aff_str}[/bold green]",
                            title="Observation: Success",
                            border_style="green"
                        ))
                        console.print()
                        self.schema = get_schema(self.engine)
                        self.undo_stack.append({"action": "write", "sql": corrected_sql, "request": user_request})

                    log_session_event(
                        request=user_request,
                        sql=corrected_sql,
                        ran=True,
                        success=True,
                        action=new_action,
                        risk=risk,
                        prompt_version="v4",
                        db_type=self.db_type,
                        dry_run=False
                    )
                    return True
                except Exception as sub_err:
                    can_sub_retry, _ = is_retryable_error(sub_err)
                    render_observation_panel(failed_sql=corrected_sql, error=sub_err, attempt=attempt, db_type=self.db_type or "sqlite")
                    if not can_sub_retry:
                        break
                    current_sql = corrected_sql
                    current_err = str(sub_err)

        console.print(f"[bold red]ReAct Self-Correction could not resolve the error after {max_attempts} attempts.[/bold red]\n")
        return False

    def handle_mongo_react_self_correction(
        self,
        err: Exception,
        failed_op: Dict[str, Any],
        user_request: str,
        max_attempts: int = 2,
    ) -> bool:
        """ReAct self-correction loop for MongoDB JSON operations."""
        can_retry, category = is_retryable_error(err)
        if not can_retry:
            friendly = format_beginner_friendly_error(err, db_type="mongodb")
            console.print(Panel(friendly, title="MongoDB Error", border_style="red"))
            console.print()
            return False

        failed_str = json.dumps(failed_op, indent=2)
        render_observation_panel(failed_sql=failed_str, error=err, attempt=1, db_type="mongodb")

        client = self.get_llm_client()
        current_op = failed_op
        current_err = str(err)

        for attempt in range(1, max_attempts + 1):
            with console.status(f"[bold cyan]DevDB ReAct Loop: Reflecting on MongoDB error (Attempt {attempt}/{max_attempts})...[/bold cyan]"):
                sys_prompt, corr_prompt = build_react_correction_prompt(
                    original_request=user_request,
                    failed_sql=json.dumps(current_op),
                    db_error=current_err,
                    schema=self.schema,
                    db_type="mongodb",
                    attempt=attempt,
                    max_attempts=max_attempts
                )
                try:
                    resp = client.ask_json(system_prompt=sys_prompt, user_prompt=corr_prompt)
                except Exception as llm_err:
                    console.print(f"[bold red]ReAct Loop LLM Error:[/bold red] {llm_err}\n")
                    break

            thought = resp.get("thought") or resp.get("explanation") or "Diagnosed schema structure and corrected MongoDB operation."
            corrected_op = resp
            explanation = resp.get("explanation") or ""
            reasoning = resp.get("reasoning")

            allowed, risk, reason = check_mongo_op(corrected_op)
            if not allowed:
                console.print(Panel(
                    f"[bold red]SAFETY GUARDRAIL - CORRECTED OPERATION BLOCKED[/bold red]\n\n{reason}",
                    title="Safety Guardrail (ReAct Interrupted)",
                    border_style="red"
                ))
                break

            render_react_step(
                attempt=attempt,
                max_attempts=max_attempts,
                thought=thought,
                action=str(corrected_op.get("operation", "mongo_op")),
                sql=to_mongosh_syntax(corrected_op),
                explanation=explanation,
                reasoning=reasoning,
                verbose=self.verbose
            )

            if self.dry_run:
                console.print("[bold yellow][Dry-run mode enabled - corrected operation was not executed][/bold yellow]\n")
                return True

            try:
                execute_mongo_operation(self.mongo_db, corrected_op)
                console.print("[bold green]Observation: Corrected MongoDB operation executed successfully![/bold green]\n")
                if self.mongo_client and self.mongo_db is not None:
                    self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)
                log_session_event(
                    request=user_request,
                    sql=to_mongosh_syntax(corrected_op),
                    ran=True,
                    success=True,
                    action=str(corrected_op.get("operation", "mongo_op")),
                    risk=risk,
                    prompt_version="v4",
                    db_type="MONGODB",
                    dry_run=False
                )
                return True
            except Exception as sub_err:
                can_sub_retry, _ = is_retryable_error(sub_err)
                render_observation_panel(failed_sql=json.dumps(corrected_op), error=sub_err, attempt=attempt, db_type="mongodb")
                if not can_sub_retry:
                    break
                current_op = corrected_op
                current_err = str(sub_err)

        console.print(f"[bold red]ReAct Self-Correction could not resolve MongoDB error after {max_attempts} attempts.[/bold red]\n")
        return False

    def handle_mongo_request(self, user_request: str, op: Dict[str, Any]) -> bool:
        """
        Execute structured MongoDB JSON operation safely using PyMongo.
        No SQL, no eval/exec.
        """
        operation = str(op.get("operation") or op.get("action") or "").lower()
        coll_name = op.get("collection", "")
        explanation = op.get("explanation", "")
        reasoning = op.get("reasoning")
        self.last_explanation = explanation
        self.last_reasoning = reasoning

        # 1. Safety check
        allowed, risk, reason = check_mongo_op(op, schema=self.schema, current_mode=self.session_mode)
        if not allowed:
            console.print(Panel(
                f"[bold red]SAFETY GUARDRAIL - OPERATION BLOCKED[/bold red]\n\n"
                f"[bold white]{reason}[/bold white]\n\n"
                f"[dim]Attempted Operation:[/dim] {json.dumps(op, indent=2)}",
                border_style="red",
                title="Refused by DevDB Safety"
            ))
            console.print()
            log_session_event(
                request=user_request,
                sql=json.dumps(op),
                ran=False,
                success=False,
                error=reason,
                action=operation,
                risk=risk,
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=self.dry_run
            )
            return False

        # 2. Preview Panel with mongosh syntax
        mongosh_cmd = to_mongosh_syntax(op)
        risk_color = "green" if risk == "LOW" else ("yellow" if risk == "MEDIUM" else "red")
        risk_display = (
            f"[bold green]LOW (Read-only query / inspection)[/bold green]" if risk == "LOW"
            else (f"[bold yellow]MEDIUM (Write / Modification)[/bold yellow]" if risk == "MEDIUM"
                  else f"[bold red]HIGH (Destructive / User Management)[/bold red]")
        )

        preview_lines = [
            f"[bold cyan]Action / Operation:[/bold cyan] {operation.upper()}",
            f"[bold cyan]Target Collection:[/bold cyan] {coll_name or '(none / database)'}",
            f"[bold cyan]Risk Level:[/bold cyan] {risk_display}",
            f"[bold cyan]Explanation:[/bold cyan] {explanation}",
        ]
        if reasoning and isinstance(reasoning, dict):
            preview_lines.append(
                f"[dim cyan]Reasoning:[/dim cyan] [dim]"
                f"Collections: {', '.join(reasoning.get('collections_involved', []))} | "
                f"Keys: {', '.join(reasoning.get('keys', []))}[/dim]"
            )
        preview_lines.append("\n[bold cyan]Equivalent mongosh Syntax (For Display Only):[/bold cyan]")

        console.print(Panel(
            "\n".join(preview_lines),
            title="DevDB MongoDB Operation Preview",
            border_style=risk_color
        ))
        syntax = Syntax(mongosh_cmd, "javascript", theme="monokai", line_numbers=False)
        console.print(syntax)
        console.print()

        if self.verbose and reasoning:
            reasoning_str = json.dumps(reasoning, indent=2) if not isinstance(reasoning, str) else reasoning
            console.print(Panel(
                f"[white]{reasoning_str}[/white]",
                title="[bold magenta]DevDB MongoDB Reasoning Breakdown (--verbose)[/bold magenta]",
                border_style="magenta"
            ))

        # 3. Dry-run check
        if self.dry_run:
            console.print("[bold yellow][Dry-run mode enabled - MongoDB operation was not executed][/bold yellow]\n")
            log_session_event(
                request=user_request,
                sql=mongosh_cmd,
                ran=False,
                success=True,
                action=operation,
                risk=risk,
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=True
            )
            return True

        # 4. Confirmation for writes / destructive
        is_read_only = operation in ("find", "count", "aggregate", "show_relationships", "validate_relationships", "list_users", "list")
        if not is_read_only:
            if self.is_atlas:
                cluster_name = extract_cluster_or_host(self.url or "")
                console.print(Panel(
                    f"[bold red]REMOTE DATABASE: {cluster_name}[/bold red]\n\n"
                    f"Collection: [bold white]{coll_name}[/bold white] | Operation: [bold yellow]{operation}[/bold yellow]\n"
                    f"[bold yellow]CAUTION:[/bold yellow] You are modifying a remote cloud database.\n"
                    f"To proceed, you must type '[bold green]yes[/bold green]' (not just y).",
                    title="[bold red]REMOTE WRITE CONFIRMATION[/bold red]",
                    border_style="red"
                ))
                confirm = console.input("[bold red]Type 'yes' to confirm remote write: [/bold red]").strip()
                if confirm != "yes":
                    console.print("[yellow]Remote Atlas write cancelled by user.[/yellow]\n")
                    log_session_event(
                        request=user_request,
                        sql=mongosh_cmd,
                        ran=False,
                        success=False,
                        error="Cancelled by user on Atlas write",
                        action=operation,
                        risk=risk,
                        prompt_version=self.prompt_version,
                        db_type=self.db_type,
                        dry_run=False
                    )
                    return False
            else:
                confirm = console.input("[bold yellow]Run? [y/N]: [/bold yellow]").strip().lower()
                if confirm not in ("y", "yes"):
                    console.print("[yellow]Operation cancelled by user.[/yellow]\n")
                    log_session_event(
                        request=user_request,
                        sql=mongosh_cmd,
                        ran=False,
                        success=False,
                        error="Cancelled by user",
                        action=operation,
                        risk=risk,
                        prompt_version=self.prompt_version,
                        db_type=self.db_type,
                        dry_run=False
                    )
                    return False

        # 5. Execute via execute_mongo_operation
        try:
            res = execute_mongo_operation(self.mongo_db, op)

            if operation == "find":
                docs = res.get("documents", [])
                if not docs:
                    console.print(f"[yellow]0 documents returned from '{coll_name}'.[/yellow]\n")
                else:
                    table = Table(
                        title=f"MongoDB Query Results: '{coll_name}' ({len(docs)} documents, limit {op.get('limit', 50)})",
                        show_lines=True,
                        header_style="bold magenta",
                        title_style="bold cyan"
                    )
                    col_names = list(docs[0].keys())
                    for c in col_names:
                        table.add_column(str(c), style="cyan")
                    for d in docs:
                        row_vals = []
                        for c in col_names:
                            val = d.get(c)
                            if val is None:
                                row_vals.append("[dim]NULL[/dim]")
                            elif isinstance(val, (dict, list)):
                                row_vals.append(json.dumps(val))
                            else:
                                row_vals.append(str(val))
                        table.add_row(*row_vals)
                    console.print(table)
                    console.print()

            elif operation == "count":
                console.print(Panel(
                    f"[bold green]Count result for '{coll_name}': {res.get('count')} documents[/bold green]",
                    title="MongoDB Count Result",
                    border_style="green"
                ))
                console.print()

            elif operation == "aggregate":
                docs = res.get("documents", [])
                if not docs:
                    console.print("[yellow]Aggregation returned 0 documents.[/yellow]\n")
                else:
                    table = Table(
                        title=f"MongoDB Aggregation Results ({len(docs)} documents)",
                        show_lines=True,
                        header_style="bold magenta",
                        title_style="bold cyan"
                    )
                    col_names = list(docs[0].keys())
                    for c in col_names:
                        table.add_column(str(c), style="cyan")
                    for d in docs:
                        row_vals = []
                        for c in col_names:
                            val = d.get(c)
                            if val is None:
                                row_vals.append("[dim]NULL[/dim]")
                            elif isinstance(val, (dict, list)):
                                row_vals.append(json.dumps(val))
                            else:
                                row_vals.append(str(val))
                        table.add_row(*row_vals)
                    console.print(table)
                    console.print()

            elif operation == "update":
                console.print(Panel(
                    f"[bold green]Updated documents successfully![/bold green]\n"
                    f"Matched count: {res.get('matched_count')}\n"
                    f"Modified count: {res.get('modified_count')}",
                    title="MongoDB Update Success",
                    border_style="green"
                ))
                console.print()

            elif operation == "delete":
                console.print(Panel(
                    f"[bold green]Deleted {res.get('deleted_count')} documents successfully![/bold green]",
                    title="MongoDB Delete Success",
                    border_style="green"
                ))
                console.print()

            elif operation == "create_index":
                console.print(Panel(
                    f"[bold green]Index '{res.get('index_name')}' created on collection '{coll_name}'![/bold green]\n"
                    f"Keys: {res.get('keys')} | Unique: {res.get('unique')}",
                    title="MongoDB Index Created",
                    border_style="green"
                ))
                console.print()

            elif operation in ("insert", "insert_one", "insert_many"):
                inserted_count = res.get("inserted_count", 0)
                inserted_ids = res.get("inserted_ids", [])
                console.print(Panel(
                    f"[bold green]Inserted {inserted_count} document(s) into '{coll_name}' successfully![/bold green]\n"
                    f"[dim]IDs: {', '.join(inserted_ids[:5])}{'...' if len(inserted_ids) > 5 else ''}[/dim]",
                    title="MongoDB Insert Success",
                    border_style="green"
                ))
                console.print()
                self.undo_stack.append({
                    "action": "fake_data",
                    "table": coll_name,
                    "rows": inserted_count,
                    "request": user_request
                })

            elif operation == "insert_fake":
                docs = res.get("documents", [])
                table = Table(
                    title=f"Synthetic Fake Data Generated: '{coll_name}' ({len(docs)} inserted)",
                    show_lines=True,
                    header_style="bold magenta",
                    title_style="bold green"
                )
                if docs:
                    keys = list(docs[0].keys())[:6]
                    for k in keys:
                        table.add_column(str(k), style="cyan")
                    for d in docs[:5]:
                        table.add_row(*[str(d.get(k)) for k in keys])
                console.print(table)
                console.print(f"[bold green]Inserted {len(docs)} documents into collection '{coll_name}'.[/bold green]\n")
                self.undo_stack.append({
                    "action": "fake_data",
                    "table": coll_name,
                    "rows": len(docs),
                    "request": user_request
                })

            elif operation == "create_user":
                if res.get("password"):
                    console.print(Panel(
                        f"[bold green]Username:[/bold green] [bold white]{res.get('username')}[/bold white]\n"
                        f"[bold green]Generated Password:[/bold green] [bold yellow]{res.get('password')}[/bold yellow]\n"
                        f"[bold green]Assigned Roles:[/bold green] [bold cyan]{res.get('roles')}[/bold cyan]\n\n"
                        f"[dim italic]SECURITY NOTE: This password is shown ONCE now and is NEVER saved in logs.[/dim italic]",
                        title="MongoDB User Credentials (Printed ONCE)",
                        border_style="green"
                    ))
                    console.print()
                else:
                    console.print(f"[bold green]MongoDB user '{res.get('username')}' created successfully.[/bold green]\n")
                self.undo_stack.append({
                    "action": "create_mongo_user",
                    "username": res.get("username"),
                    "request": user_request
                })

            elif operation in ("list_users", "list"):
                users = res.get("users", [])
                table = Table(
                    title=f"MongoDB Database Users ({len(users)} users)",
                    show_lines=True,
                    header_style="bold magenta",
                    title_style="bold cyan"
                )
                table.add_column("Username", style="bold cyan")
                table.add_column("Roles", style="green")
                for u in users:
                    roles_list = u.get("roles", [])
                    roles_strs = []
                    for r in roles_list:
                        if isinstance(r, str):
                            roles_strs.append(r)
                        elif isinstance(r, dict):
                            roles_strs.append(f"{r.get('role', '')}@{r.get('db', '')}")
                        else:
                            roles_strs.append(str(r))
                    roles_str = ", ".join(roles_strs) if roles_strs else "(none)"
                    uname = str(u.get("username") or u.get("user") or "")
                    table.add_row(uname, roles_str)
                console.print(table)
                console.print()

            elif operation in ("grant_role", "revoke_role", "drop_user"):
                console.print(f"[bold green]MongoDB operation '{operation}' completed successfully![/bold green]\n")

            elif operation == "add_reference":
                console.print(Panel(
                    f"[bold green]Reference established between collections![/bold green]\n"
                    f"Child: {res.get('child_collection')}.{res.get('ref_field')} -> "
                    f"Parent: {res.get('parent_collection')}._id\n"
                    f"Documents updated: {res.get('matched_count')} | Index created: {res.get('index_created')}",
                    title="MongoDB Cross-Collection Reference",
                    border_style="green"
                ))
                console.print()

            # Refresh schema cache
            if self.mongo_client and self.mongo_db is not None:
                self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)

            log_session_event(
                request=user_request,
                sql=mongosh_cmd,
                ran=True,
                success=True,
                action=operation,
                risk=risk,
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=False
            )
            return True

        except Exception as err:
            log_session_event(
                request=user_request,
                sql=mongosh_cmd,
                ran=True,
                success=False,
                error=str(err),
                action=operation,
                risk=risk,
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=False
            )
            return self.handle_mongo_react_self_correction(err=err, failed_op=op, user_request=user_request)

    def handle_natural_language_request(self, user_request: str) -> None:
        """
        Process natural language request:
        1. Load schema
        2. Check for fake data or session cleanup commands
        3. Check for relationship commands (show, validate, add, safe_delete_check)
        4. Check for user management commands (create user, grant, revoke, list, drop)
        5. Build prompt using prompt_version
        6. Call LLM and parse JSON
        7. Run safety checks
        8. Show Rich preview panel with SQL & risk level (LOW/MEDIUM/HIGH)
        9. Execute SELECT immediately (limit 50 rows)
        10. Confirm writes/destructive ops and execute inside a transaction
        """
        # Check for model inquiry ("which model am I using?" or "what model am I using?")

        req_clean = user_request.lower().strip().rstrip("?.!")
        if any(p in req_clean for p in [
            "which model am i using",
            "what model am i using",
            "which model is this",
            "what model is this",
            "which llm am i using",
            "what llm am i using",
            "which model",
            "what model",
            "current model",
            "show model",
            "model info",
        ]):
            self.handle_model_info()
            return

        if not self.is_connected:
            console.print("[bold red]Error:[/bold red] No active database connection. Use 'connect <url>' first.\n")
            return

        # a. Check for history and undo commands
        req_lower = user_request.lower().strip()

        # Check for explain command
        if req_lower in ("explain", "explain that", "explain last", "why did you do that", "explain this"):
            self.handle_explain()
            return

        # Check for capabilities / help
        if any(p in req_lower for p in [
            "what can you do", "what can you do on this database", "capabilities", "capability matrix", "supported features"
        ]):
            self.handle_capabilities()
            return

        # Check connection status inquiry
        if any(p in req_lower for p in ["which database am i connected to", "current connection", "connection status", "what database am i on"]):
            self.handle_current_connection()
            return

        if any(p in req_lower for p in ["list my connections", "list connections", "show connections", "saved profiles", "list profiles"]):
            self.handle_list_connections()
            return

        if req_lower in ("disconnect", "disconnect database", "close connection"):
            self.disconnect()
            return

        # Check for mode changes in natural language
        if "switch to read-only" in req_lower or "switch to read only" in req_lower:
            self.set_mode(SessionMode.READ_ONLY)
            return
        if "switch to read-write" in req_lower or "switch to read write" in req_lower:
            self.set_mode(SessionMode.READ_WRITE)
            return
        if "enter admin mode" in req_lower or "switch to admin mode" in req_lower or req_lower == "admin mode":
            self.prompt_admin_mode()
            return

        # Classify intent via router
        classification = classify_intent(
            user_request=user_request,
            schema=self.schema,
            db_type=self.db_type or "sqlite",
            prompt_version=self.prompt_version,
        )

        if classification.suggested_clarification:
            console.print(Panel(
                f"[bold yellow]{classification.suggested_clarification}[/bold yellow]",
                title="[bold yellow]DevDB Needs Clarification[/bold yellow]",
                border_style="yellow"
            ))
            console.print()
            return

        if classification.intent == "connection":
            params = classification.params
            action = params.get("action")
            if action == "mode":
                target_m = params.get("target_mode")
                if target_m == "ADMIN":
                    self.prompt_admin_mode()
                elif target_m == "READ-ONLY":
                    self.set_mode(SessionMode.READ_ONLY)
                else:
                    self.set_mode(SessionMode.READ_WRITE)
                return
            elif action == "save":
                self.handle_save_connection(params.get("name", ""))
                return
            elif action == "list":
                self.handle_list_connections()
                return
            elif action == "current":
                self.handle_current_connection()
                return
            elif action == "disconnect":
                self.disconnect()
                return
            elif action == "switch":
                self.connect(params.get("profile", ""))
                return

        if classification.intent == "copy":
            params = classification.params
            self.handle_copy_command(
                entity=params.get("entity", ""),
                source_target=params.get("source", "active"),
                dest_target=params.get("destination", "active")
            )
            return

        if classification.intent == "data_io":
            params = classification.params
            action = params.get("action")
            if action == "export":
                self.handle_export_command(
                    entity=params.get("entity", ""),
                    destination=params.get("destination", ""),
                    fmt=params.get("format", "csv")
                )
                return
            elif action == "import":
                self.handle_import_command(
                    source_file=params.get("source", ""),
                    entity=params.get("entity", ""),
                    fmt=params.get("format", "csv")
                )
                return
            elif action == "backup":
                self.handle_backup_command()
                return
        if classification.intent == "users":
            self.handle_user_management(user_request)
            return

        if classification.intent == "relationships":
            if any(p in req_lower for p in ["show relationship", "show relationships", "relationship map", "display relationship", "display relationships"]):
                export_mermaid = "--mermaid" in req_lower or "mermaid" in req_lower
                self.handle_show_relationships(export_mermaid=export_mermaid)
                return
            if any(p in req_lower for p in ["validate relationship", "validate relationships", "check relationship", "check relationships", "check orphan", "find orphan"]):
                self.handle_validate_relationships()
                return
            if any(p in req_lower for p in ["safe delete check", "check delete", "what happens if i delete", "can i delete"]):
                self.handle_safe_delete_check_nl(user_request)
                return
            rel_match = re.search(r"(?:add[_\s]+relationship|link)\s+([\w]+)(?:\.([\w]+))?\s*(?:to|->|and)\s*([\w]+)(?:\.([\w]+))?", user_request, re.IGNORECASE)
            if rel_match:
                c_tbl, c_col, p_tbl, p_col = rel_match.group(1), rel_match.group(2), rel_match.group(3), rel_match.group(4)
                if not c_col or not p_col:
                    if self.engine:
                        self.schema = get_schema(self.engine)
                    tables = (self.schema or {}).get("tables", {})
                    if not p_col and p_tbl in tables:
                        pk_cols = [c["name"] for c in tables[p_tbl].get("columns", []) if c.get("primary_key")]
                        p_col = pk_cols[0] if pk_cols else "id"
                    if not c_col and c_tbl in tables:
                        prefix = p_tbl.rstrip("s").lower()
                        for col in tables[c_tbl].get("columns", []):
                            if col["name"].lower() in (f"{prefix}_id", f"{p_tbl.lower()}_id"):
                                c_col = col["name"]
                                break
                if c_tbl and c_col and p_tbl and p_col:
                    rel_t = "one_to_one" if any(k in req_lower for k in ["one-to-one", "one to one", "1:1"]) else (
                        "many_to_many" if any(k in req_lower for k in ["many-to-many", "many to many", "m2m"]) else "one_to_many"
                    )
                    on_d = "CASCADE" if "cascade" in req_lower else ("SET NULL" if "set null" in req_lower else "RESTRICT")
                    self.handle_add_relationship(c_tbl, c_col, p_tbl, p_col, rel_type=rel_t, on_delete=on_d)
                    return

        if classification.intent == "explore":
            params = classification.params
            action = params.get("action")
            if action == "stats":
                if self.adapter:
                    st = self.adapter.stats()
                    console.print(Panel(
                        f"Database Type: [bold cyan]{st.get('db_type')}[/bold cyan]\n"
                        f"Target: [bold white]{st.get('target')}[/bold white]\n"
                        f"Containers: [bold yellow]{st.get('container_count')}[/bold yellow] ({', '.join(st.get('containers', []))})\n"
                        f"Size on Disk: [bold green]{st.get('size_bytes', 0):,} bytes[/bold green]",
                        title="Database Storage & Statistics",
                        border_style="cyan"
                    ))
                    console.print()
                    return
            if action == "count" and params.get("entity"):
                ent = params["entity"]
                cnt = 0
                try:
                    if isinstance(self.adapter, MongoAdapter) or self.db_type == "MONGODB":
                        if self.adapter and hasattr(self.adapter, "db") and self.adapter.db is not None:
                            cnt = self.adapter.db[ent].count_documents({})
                        elif self.mongo_db is not None:
                            cnt = self.mongo_db[ent].count_documents({})
                    elif self.adapter and hasattr(self.adapter, "engine") and self.adapter.engine:
                        from sqlalchemy import text
                        with self.adapter.engine.connect() as conn:
                            cnt = conn.execute(text(f"SELECT COUNT(*) FROM {ent};")).scalar() or 0
                    elif self.engine:
                        from sqlalchemy import text
                        with self.engine.connect() as conn:
                            cnt = conn.execute(text(f"SELECT COUNT(*) FROM {ent};")).scalar() or 0
                except Exception:
                    cnt = 0
                console.print(Panel(
                    f"Count result for '{ent}': [bold green]{cnt}[/bold green] records / documents",
                    title="DevDB Count Result",
                    border_style="green"
                ))
                console.print()
                return
            if not params.get("entity") or any(k in req_lower for k in ["list tables", "show tables", "list collections", "show collections", "all tables", "all collections", "schema"]):
                self.print_schema()
                return

        if req_lower in ("history", "show history", "view history", "recent history"):
            self.handle_history()
            return

        if req_lower in ("undo", "undo last", "revert", "undo latest"):
            self.handle_undo_last()
            return

        # b. Check for session fake data cleanup request
        if any(pattern in req_lower for pattern in [
            "delete the fake data", "delete fake data", "remove fake data", "cleanup fake data", "clean fake data"
        ]):
            self.handle_delete_session_fake_data(user_request)
            return

        # b. Check for explicit fake data generation request
        if any(pattern in req_lower for pattern in [
            "fake data", "fake customer", "fake order", "fake product", "add 50 fake", "add 200 fake", "generate fake", "add fake"
        ]):
            self.handle_fake_data_generation(user_request)
            return

        # c. Check for relationship display / map requests
        if any(pattern in req_lower for pattern in [
            "show relationship", "show relationships", "relationship map", "display relationship", "display relationships"
        ]):
            export_mermaid = "--mermaid" in req_lower or "mermaid" in req_lower
            self.handle_show_relationships(export_mermaid=export_mermaid)
            return

        # d. Check for relationship validation / orphan check requests
        if any(pattern in req_lower for pattern in [
            "validate relationship", "validate relationships", "check relationship", "check relationships", "check orphan", "find orphan"
        ]):
            self.handle_validate_relationships()
            return

        # e. Check for safe delete checks
        if any(pattern in req_lower for pattern in [
            "safe delete check", "check delete", "what happens if i delete", "can i delete"
        ]):
            self.handle_safe_delete_check_nl(user_request)
            return

        # f. Check for direct 'link <child> -> <parent>' or 'add relationship <child> to <parent>'
        rel_match = re.search(r"(?:add[_\s]+relationship|link)\s+([\w]+)(?:\.([\w]+))?\s*(?:to|->|and)\s*([\w]+)(?:\.([\w]+))?", user_request, re.IGNORECASE)
        if rel_match:
            c_tbl = rel_match.group(1)
            c_col = rel_match.group(2)
            p_tbl = rel_match.group(3)
            p_col = rel_match.group(4)

            # Infer columns if not explicitly provided
            if not c_col or not p_col:
                if self.engine:
                    self.schema = get_schema(self.engine)
                tables = (self.schema or {}).get("tables", {})
                if not p_col and p_tbl in tables:
                    pk_cols = [c["name"] for c in tables[p_tbl].get("columns", []) if c.get("primary_key")]
                    p_col = pk_cols[0] if pk_cols else "id"
                if not c_col and c_tbl in tables:
                    prefix = p_tbl.rstrip("s").lower()
                    for col in tables[c_tbl].get("columns", []):
                        if col["name"].lower() in (f"{prefix}_id", f"{p_tbl.lower()}_id"):
                            c_col = col["name"]
                            break

            if c_tbl and c_col and p_tbl and p_col:
                rel_t = "one_to_one" if any(k in req_lower for k in ["one-to-one", "one to one", "1:1"]) else (
                    "many_to_many" if any(k in req_lower for k in ["many-to-many", "many to many", "m2m"]) else "one_to_many"
                )
                on_d = "CASCADE" if "cascade" in req_lower else ("SET NULL" if "set null" in req_lower else "RESTRICT")
                self.handle_add_relationship(c_tbl, c_col, p_tbl, p_col, rel_type=rel_t, on_delete=on_d)
                return

        # Load schema
        if self.mongo_client and self.mongo_db is not None:
            self.schema = get_mongo_schema(self.mongo_client, self.mongo_db.name)
        elif self.engine:
            self.schema = get_schema(self.engine)

        has_users_entity = bool(self.schema and "users" in (self.schema.get("tables") or {}))

        # g. Pre-check for user management requests
        is_explicit_db_user_request = bool(re.search(
            r"\b(?:create\s+(?:a\s+)?(?:[\w\-]+\s+)?(?:user|role)|grant\b|revoke\b|drop\s+(?:user|role)|(?:list|show)\s+(?:all\s+)?(?:database|db|mongo|postgres|auth)\s+users|list_users|list-users)\b",
            req_lower
        ))
        is_generic_user_list = bool(re.search(r"\b(?:list|show)\s+(?:all\s+)?users\b", req_lower))
        is_user_mgmt_request = is_explicit_db_user_request or (is_generic_user_list and not has_users_entity)

        if is_user_mgmt_request:
            # SQLite does not have users: reject politely immediately
            if "postgres" not in (self.db_type or "").lower() and self.db_type != "MONGODB":
                self.handle_user_management(user_request)
                return
            # Fast-path for listing database auth users
            if is_explicit_db_user_request or any(k in req_lower for k in ["list users", "list all users", "show users", "show all users"]):
                self.handle_user_management(user_request, spec={"operation": "list"})
                return

        # Query fast-path for simple collection/table display (e.g. 'show all users', 'show all products')
        if classification.intent == "query" and classification.target_entity:
            ent = classification.target_entity
            known_tables = list((self.schema or {}).get("tables", {}).keys())
            matched_ent = next((t for t in known_tables if t.lower() == ent.lower()), ent)
            simple_query_verbs = ["show all", "show", "list all", "list", "display all", "display", "get all", "find all", "query"]
            if any(f"{v} {ent.lower()}" in req_lower or f"{v} {matched_ent.lower()}" in req_lower for v in simple_query_verbs) and not any(w in req_lower for w in ["where", "filter", "order by", "sort", "group by", "limit", ">", "<", "="]):
                if self.db_type == "MONGODB":
                    op = {
                        "operation": "find",
                        "collection": matched_ent,
                        "filter": {},
                        "limit": 50,
                        "explanation": f"Retrieve all documents from '{matched_ent}' collection."
                    }
                    self.handle_mongo_request(user_request, op)
                    if matched_ent.lower() == "users":
                        console.print("[dim]Tip: To view database auth accounts and roles, use 'list_users' or 'show database users'.[/dim]\n")
                    return
                elif self.engine:
                    from sqlalchemy import text
                    sql = f"SELECT * FROM {matched_ent} LIMIT 50;"
                    preview_lines = [
                        "[bold cyan]Action:[/bold cyan] QUERY",
                        "[bold cyan]Risk Level:[/bold cyan] [bold green]LOW (Read-only)[/bold green]",
                        f"[bold cyan]Explanation:[/bold cyan] Retrieve all rows from '{matched_ent}' table (limit 50).",
                        "\n[bold cyan]Generated SQL:[/bold cyan]",
                    ]
                    console.print(Panel(
                        "\n".join(preview_lines),
                        title="DevDB Query Preview",
                        border_style="green"
                    ))
                    syntax = Syntax(sql, "sql", theme="monokai", line_numbers=False)
                    console.print(syntax)
                    console.print()
                    console.print("[dim]Executing query immediately...[/dim]")
                    with self.engine.connect() as conn:
                        res = conn.execute(text(sql))
                        rows = res.fetchmany(50)
                        keys = list(res.keys())
                    if not rows:
                        console.print(f"[yellow]0 rows returned from '{matched_ent}'.[/yellow]\n")
                    else:
                        tbl = Table(
                            title=f"Query Results: '{matched_ent}' ({len(rows)} rows, limit 50)",
                            show_lines=True,
                            header_style="bold magenta",
                            title_style="bold cyan"
                        )
                        for k in keys:
                            tbl.add_column(str(k), style="cyan")
                        for row in rows:
                            tbl.add_row(*[str(val) if val is not None else "[dim]NULL[/dim]" for val in row])
                        console.print(tbl)
                        console.print()
                    if matched_ent.lower() == "users":
                        console.print("[dim]Tip: To view database auth accounts and roles, use 'list_users' or 'show database users'.[/dim]\n")
                    log_session_event(
                        request=user_request,
                        sql=sql,
                        ran=True,
                        success=True,
                        action="query",
                        risk="LOW",
                        prompt_version=self.prompt_version,
                        db_type=self.db_type,
                        dry_run=False
                    )
                    return

        # Build prompt using prompt_version
        db_dialect = (self.db_type or "sqlite").lower()
        try:
            system_prompt = load_prompt(
                version=self.prompt_version,
                db_type=db_dialect,
                schema=self.schema
            )
        except Exception as err:
            console.print(f"[bold red]Failed to load prompt ({self.prompt_version}):[/bold red] {err}\n")
            return

        # Call LLM and parse JSON
        client = self.get_llm_client()
        try:
            with console.status(f"[bold green]Consulting DevDB Architect ({self.prompt_version})...[/bold green]"):
                llm_response = client.ask_json(system_prompt=system_prompt, user_prompt=user_request)
        except Exception as err:
            if is_user_mgmt_request and "postgres" in (self.db_type or "").lower():
                console.print(f"[dim yellow]LLM unavailable ({err}), parsing user command directly...[/dim yellow]")
                self.handle_user_management(user_request)
                return
            console.print(f"[bold red]LLM Error:[/bold red] {err}\n")
            log_session_event(
                request=user_request,
                sql="",
                ran=False,
                success=False,
                error=str(err),
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=self.dry_run
            )
            return

        if self.db_type == "MONGODB":
            self.handle_mongo_request(user_request, llm_response)
            return

        action = llm_response.get("action", "query")
        sql = (llm_response.get("sql") or "").strip()
        explanation = llm_response.get("explanation") or ""
        reasoning = llm_response.get("reasoning")
        self.last_explanation = explanation
        self.last_reasoning = reasoning

        if action == "fake_data":
            self.handle_fake_data_generation(user_request)
            return

        # Rule 1: Check for user management structured JSON returned by LLM
        if action == "create_user" or llm_response.get("operation") in ("create_user", "grant", "revoke", "list", "drop_user"):
            self.handle_user_management(user_request, spec=llm_response)
            return

        # d. Run safety checks
        allowed = True
        risk = "LOW"
        safety_message = "Safe"

        if sql:
            allowed, risk, safety_message = check_sql(sql, current_mode=self.session_mode)

        if not allowed:
            console.print(Panel(
                f"[bold red]SAFETY GUARDRAIL - OPERATION BLOCKED[/bold red]\n\n"
                f"[bold white]{safety_message}[/bold white]\n\n"
                f"[dim]Attempted SQL:[/dim] {sql or '(none)'}",
                border_style="red",
                title="Refused by DevDB Safety"
            ))
            console.print()
            log_session_event(
                request=user_request,
                sql=sql,
                ran=False,
                success=False,
                error=safety_message,
                action=action,
                risk=risk,
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=self.dry_run
            )
            return

        # e. Show Rich preview panel
        risk_color = "green" if risk == "LOW" else ("yellow" if risk == "MEDIUM" else "red")
        risk_display = (
            f"[bold green]LOW (Read-only)[/bold green]" if risk == "LOW"
            else (f"[bold yellow]MEDIUM (Write)[/bold yellow]" if risk == "MEDIUM"
                  else f"[bold red]HIGH (Destructive / Schema change)[/bold red]")
        )

        preview_lines = [
            f"[bold cyan]Action:[/bold cyan] {action.upper()}",
            f"[bold cyan]Risk Level:[/bold cyan] {risk_display}",
            f"[bold cyan]Explanation:[/bold cyan] {explanation}",
        ]

        if reasoning and isinstance(reasoning, dict):
            preview_lines.append(
                f"[dim cyan]Reasoning:[/dim cyan] [dim]"
                f"Tables: {', '.join(reasoning.get('tables_involved', []))} | "
                f"Keys: {', '.join(reasoning.get('keys', []))} | "
                f"Order: {reasoning.get('dependency_order', 'N/A')}[/dim]"
            )

        if sql:
            preview_lines.append("\n[bold cyan]Generated SQL:[/bold cyan]")

        console.print(Panel(
            "\n".join(preview_lines),
            title="DevDB Execution Preview",
            border_style=risk_color
        ))

        if self.verbose and reasoning:
            reasoning_str = json.dumps(reasoning, indent=2) if not isinstance(reasoning, str) else reasoning
            console.print(Panel(
                f"[white]{reasoning_str}[/white]",
                title="[bold magenta]DevDB Reasoning Breakdown (--verbose)[/bold magenta]",
                border_style="magenta"
            ))

        if sql:
            syntax = Syntax(sql, "sql", theme="monokai", line_numbers=False)
            console.print(syntax)
            console.print()

        # Check for dry-run flag
        if self.dry_run:
            console.print("[bold yellow][Dry-run mode enabled - statement was not executed][/bold yellow]\n")
            log_session_event(
                request=user_request,
                sql=sql,
                ran=False,
                success=True,
                action=action,
                risk=risk,
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=True
            )
            return

        # If no executable SQL returned (e.g. refused by prompt or unsupported feature note)
        if not sql:
            log_session_event(
                request=user_request,
                sql="",
                ran=False,
                success=True,
                action=action,
                risk=risk,
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=False
            )
            return

        # f. SELECT queries run immediately (limit 50 rows)
        if action == "query" or (risk == "LOW" and re.match(r"^\s*(?:SELECT|EXPLAIN|WITH)\b", sql, re.IGNORECASE)):
            console.print("[dim]Executing query immediately...[/dim]")
            try:
                if self.engine:
                    with self.engine.connect() as conn:
                        res = conn.execute(text(sql))
                        rows = res.fetchmany(50)
                        keys = list(res.keys())

                    if not rows:
                        console.print("[yellow]Query executed successfully: 0 rows returned.[/yellow]\n")
                    else:
                        result_table = Table(
                            title=f"Query Results ({len(rows)} rows, limit 50)",
                            show_lines=True,
                            header_style="bold magenta"
                        )
                        for k in keys:
                            result_table.add_column(str(k), style="cyan")
                        for row in rows:
                            result_table.add_row(*[str(val) if val is not None else "[dim]NULL[/dim]" for val in row])
                        console.print(result_table)
                        console.print()

                log_session_event(
                    request=user_request,
                    sql=sql,
                    ran=True,
                    success=True,
                    action=action,
                    risk=risk,
                    prompt_version=self.prompt_version,
                    db_type=self.db_type,
                    dry_run=False
                )
            except Exception as err:
                log_session_event(
                    request=user_request,
                    sql=sql,
                    ran=True,
                    success=False,
                    error=str(err),
                    action=action,
                    risk=risk,
                    prompt_version=self.prompt_version,
                    db_type=self.db_type,
                    dry_run=False
                )
                self.handle_react_self_correction(
                    err=err,
                    failed_sql=sql,
                    action=action,
                    user_request=user_request
                )
            return

        # Check for DELETE statement: run safe_delete_check if targeting table with dependencies
        del_match = re.match(r"^\s*DELETE\s+FROM\s+([`\"\w]+)\s+WHERE\s+(.+);?\s*$", sql, re.IGNORECASE)
        if del_match and self.engine:
            del_table = del_match.group(1).strip("`\"")
            del_where = del_match.group(2).strip("; ")
            check_res = safe_delete_check(del_table, del_where, self.engine, self.schema)
            if not check_res.get("allowed"):
                console.print(Panel(
                    f"[bold red]REFERENTIAL INTEGRITY WARNING - DELETE WILL BE BLOCKED BY FOREIGN KEYS:[/bold red]\n"
                    f"{check_res.get('warning')}",
                    title="Foreign Key Safety Guardrail",
                    border_style="red"
                ))
            elif check_res.get("impacts"):
                console.print(Panel(
                    f"[bold yellow]CASCADE WARNING:[/bold yellow] {check_res.get('warning')}",
                    title="Cascading Deletion Impact",
                    border_style="yellow"
                ))

        # g. Any write asks "Run? [y/N]" or requires typing table name for DROP TABLE
        if "typing the table name" in safety_message:
            match = re.search(r"typing the table name '([\w\.\-]+)'", safety_message)
            required_table = match.group(1) if match else ""
            confirmed_input = console.input(
                f"[bold red]DANGER:[/bold red] Type '[bold yellow]{required_table}[/bold yellow]' to confirm DROP TABLE: "
            ).strip()

            if confirmed_input != required_table:
                console.print("[yellow]Confirmation mismatch. DROP TABLE aborted.[/yellow]\n")
                log_session_event(
                    request=user_request,
                    sql=sql,
                    ran=False,
                    success=False,
                    error="User aborted DROP TABLE confirmation",
                    action=action,
                    risk=risk,
                    prompt_version=self.prompt_version,
                    db_type=self.db_type,
                    dry_run=False
                )
                return
        else:
            if self.is_atlas or (self.url and not is_dev_connection(self.url)):
                cluster_name = extract_cluster_or_host(self.url or "")
                console.print(Panel(
                    f"[bold red]REMOTE DATABASE: {cluster_name}[/bold red]\n\n"
                    f"[bold yellow]CAUTION:[/bold yellow] You are modifying a remote database.\n"
                    f"To proceed, you must type '[bold green]yes[/bold green]' (not just y).",
                    title="[bold red]REMOTE WRITE CONFIRMATION[/bold red]",
                    border_style="red"
                ))
                answer = console.input("[bold red]Type 'yes' to confirm remote write: [/bold red]").strip()
                if answer != "yes":
                    console.print("[yellow]Remote write cancelled by user.[/yellow]\n")
                    log_session_event(
                        request=user_request,
                        sql=sql,
                        ran=False,
                        success=False,
                        error="User cancelled remote write confirmation",
                        action=action,
                        risk=risk,
                        prompt_version=self.prompt_version,
                        db_type=self.db_type,
                        dry_run=False
                    )
                    return
            else:
                prompt_text = "Run migration? [y/N]: " if action == "relationship" else "Run? [y/N]: "
                answer = console.input(f"[bold yellow]{prompt_text}[/bold yellow]").strip().lower()
                if answer not in ("y", "yes"):
                    console.print("[yellow]Operation cancelled by user.[/yellow]\n")
                    log_session_event(
                        request=user_request,
                        sql=sql,
                        ran=False,
                        success=False,
                        error="User cancelled write confirmation",
                        action=action,
                        risk=risk,
                        prompt_version=self.prompt_version,
                        db_type=self.db_type,
                        dry_run=False
                    )
                    return

        # Execute inside a transaction or relationship migration
        try:
            if action == "relationship" or (self.db_type == "SQLITE" and "table recreation migration" in safety_message.lower()):
                execute_relationship_sql(sql, self.engine)
                console.print("[bold green]Relationship migration executed and committed successfully![/bold green]\n")
                self.schema = get_schema(self.engine)
            elif self.engine:
                with self.engine.begin() as conn:
                    # Execute write operation within automatic transaction
                    result = conn.execute(text(sql))
                    affected = getattr(result, "rowcount", None)

                affected_str = f" (Rows affected: {affected})" if affected is not None and affected != -1 else ""
                console.print(f"[bold green]Transaction committed successfully!{affected_str}[/bold green]\n")

                # Refresh schema cache after successful write/DDL
                self.schema = get_schema(self.engine)
                self.undo_stack.append({
                    "action": "write",
                    "sql": sql,
                    "request": user_request
                })

            log_session_event(
                request=user_request,
                sql=sql,
                ran=True,
                success=True,
                action=action,
                risk=risk,
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=False
            )
        except Exception as err:
            console.print(f"[bold red]Transaction rolled back due to error:[/bold red] {err}\n")
            log_session_event(
                request=user_request,
                sql=sql,
                ran=True,
                success=False,
                error=str(err),
                action=action,
                risk=risk,
                prompt_version=self.prompt_version,
                db_type=self.db_type,
                dry_run=False
            )
            self.handle_react_self_correction(
                err=err,
                failed_sql=sql,
                action=action,
                user_request=user_request
            )


# Global CLI session instance
session = DevDBSession()


def run_interactive_loop(
    initial_url: Optional[str] = None,
    dry_run: bool = False,
    prompt_version: Optional[str] = None,
    verbose: bool = False,
    provider: Optional[str] = None,
    model: Optional[str] = None,
):
    """Run interactive REPL loop with prompt 'devdb>'."""
    session.dry_run = dry_run
    session.verbose = verbose
    if prompt_version:
        session.prompt_version = prompt_version
    if provider:
        session.llm_provider = provider
    if model:
        session.llm_model = model

    mode_info = f"Prompt: [bold]{session.prompt_version}[/bold] | LLM: [bold green]{session.llm_provider}/{session.llm_model}[/bold green]"
    if dry_run:
        mode_info += " | [bold yellow]DRY-RUN MODE[/bold yellow]"
    if session.verbose:
        mode_info += " | [bold magenta]VERBOSE[/bold magenta]"


    console.print(Panel.fit(
        f"[bold cyan]DevDB Assistant[/bold cyan] - Natural Language Database CLI\n"
        f"[dim]{mode_info}[/dim]\n\n"
        "Commands:\n"
        "  [bold green]connect <url>[/bold green]                  - Connect to SQLite or PostgreSQL\n"
        "  [bold green]schema[/bold green]                         - Print Rich schema table\n"
        "  [bold green]show_relationships [--mermaid][/bold green]  - Draw ASCII map, Rich tree, and Mermaid\n"
        "  [bold green]validate_relationships[/bold green]          - Health report: orphan rows, indexes, FKs\n"
        "  [bold green]add_relationship <c.fk> <p.pk>[/bold green] - Add 1:N, 1:1, or N:M relationship\n"
        "  [bold green]safe_delete_check <table> <id>[/bold green]  - Calculate cascading delete impact\n"
        "  [bold green]list_users[/bold green]                         - List database users and table permissions (Postgres)\n"
        "  [bold green]create_user <user> [priv] [tbl][/bold green]   - Create user/role with safe password (Postgres)\n"
        "  [bold green]drop_user <user>[/bold green]                  - Safely drop database user (Postgres)\n"
        "  [bold green]history [limit][/bold green]                  - View session interaction history\n"
        "  [bold green]undo [last][/bold green]                      - Revert last write (fake data, user, insert)\n"
        "  [bold green]verbose[/bold green]                        - Toggle reasoning breakdown visibility\n"
        "  [bold green]exit[/bold green]                           - Quit DevDB\n\n"
        "[italic]Type any natural language request at 'devdb>' to generate and run SQL![/italic]",
        border_style="cyan"
    ))

    if initial_url:
        session.connect(initial_url)

    while True:
        try:
            if session.is_connected:
                p_label = session.active_profile_name or session.get_short_connection_name()
                m_label = session.session_mode
                prompt_str = f"[bold cyan]devdb[{p_label}|{m_label}]>[/bold cyan] "
            else:
                prompt_str = "[bold cyan]devdb>[/bold cyan] "
            line = console.input(prompt_str).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Exiting DevDB.[/dim]")
            break

        if not line:
            continue

        # Check for built-in single word or shell commands
        parts = line.split(maxsplit=1)
        first_word = parts[0].lower()

        line_clean = line.lower().strip()
        if first_word in ("exit", "quit", "q"):
            console.print("[green]Goodbye![/green]")
            break
        elif first_word in ("disconnect", "close"):
            session.disconnect()
        elif first_word == "explain":
            session.handle_explain()
        elif first_word in ("capabilities", "matrix"):
            session.handle_capabilities()
        elif first_word in ("profiles", "connections"):
            session.handle_list_connections()
        elif line_clean in ("which database am i connected to", "current connection", "whoami", "status"):
            session.handle_current_connection()
        elif first_word == "switch" and len(parts) > 1 and "mode" not in parts[1].lower():
            target_p = parts[1].replace("to ", "").strip()
            session.connect(target_p)
        elif first_word == "mode":
            if len(parts) < 2:
                console.print(f"Current mode: [bold magenta]{session.session_mode}[/bold magenta]\n")
            else:
                m_str = parts[1].upper().strip()
                if "ADMIN" in m_str:
                    session.prompt_admin_mode()
                elif "READ-ONLY" in m_str or "READ ONLY" in m_str:
                    session.set_mode(SessionMode.READ_ONLY)
                else:
                    session.set_mode(SessionMode.READ_WRITE)
        elif first_word == "connect":
            if len(parts) < 2:
                console.print("[bold red]Usage:[/bold red] connect <url_or_profile>\n")
            else:
                session.connect(parts[1].strip())
        elif line_clean in ("schema", "tables", "show tables", "show tabels", "list tables", "collections", "show collections", "list collections"):
            session.print_schema()
        elif line_clean in ("show_relationships", "relationships", "show relationships", "show relationship", "relationship map", "display relationships"):
            sub_args = parts[1] if len(parts) > 1 else ""
            export_mermaid = "--mermaid" in line_clean
            session.handle_show_relationships(export_mermaid=export_mermaid)
        elif line_clean in ("validate_relationships", "validate", "validate relationships", "check relationships", "check orphans"):
            session.handle_validate_relationships()
        elif first_word == "add_relationship":
            if len(parts) < 2:
                console.print("[bold red]Usage:[/bold red] add_relationship <child_table.col> <parent_table.col> [one_to_many|one_to_one|many_to_many] [RESTRICT|CASCADE|SET NULL]\n")
            else:
                arg_tokens = parts[1].split()
                if len(arg_tokens) >= 2:
                    child_parts = arg_tokens[0].split(".")
                    parent_parts = arg_tokens[1].split(".")
                    if len(child_parts) == 2 and len(parent_parts) == 2:
                        rel_t = arg_tokens[2] if len(arg_tokens) > 2 else "one_to_many"
                        on_d = arg_tokens[3] if len(arg_tokens) > 3 else "RESTRICT"
                        session.handle_add_relationship(child_parts[0], child_parts[1], parent_parts[0], parent_parts[1], rel_type=rel_t, on_delete=on_d)
                    else:
                        console.print("[bold red]Invalid column format. Use table.column for both child and parent.[/bold red]\n")
                else:
                    console.print("[bold red]Usage:[/bold red] add_relationship <child.col> <parent.col>\n")
        elif first_word in ("safe_delete_check", "check_delete"):
            if len(parts) < 2:
                console.print("[bold red]Usage:[/bold red] safe_delete_check <table> <id_or_filter>\n")
            else:
                arg_tokens = parts[1].split(maxsplit=1)
                tbl = arg_tokens[0]
                flt = arg_tokens[1] if len(arg_tokens) > 1 else ""
                session.handle_safe_delete_check(tbl, flt)
        elif first_word in ("list_users", "list-users") or (first_word == "users" and len(parts) == 1 and not (session.schema and "users" in (session.schema.get("tables") or {}))):
            session.handle_user_management("list users", spec={"operation": "list"})
        elif first_word in ("create_user", "create-user"):
            if len(parts) < 2:
                console.print("[bold red]Usage:[/bold red] create_user <username> [privileges] [tables]\n")
            else:
                session.handle_user_management(line)
        elif first_word in ("drop_user", "drop-user"):
            if len(parts) < 2:
                console.print("[bold red]Usage:[/bold red] drop_user <username>\n")
            else:
                session.handle_user_management(line)
        elif first_word == "grant":
            if len(parts) < 2:
                console.print("[bold red]Usage:[/bold red] grant <privileges> on <tables> to <username>\n")
            else:
                session.handle_user_management(line)
        elif first_word == "revoke":
            if len(parts) < 2:
                console.print("[bold red]Usage:[/bold red] revoke <privileges> on <tables> from <username>\n")
            else:
                session.handle_user_management(line)
        elif first_word in ("history", "hist"):
            sub_args = parts[1] if len(parts) > 1 else ""
            lim = int(sub_args) if sub_args.isdigit() else 20
            session.handle_history(limit=lim)
        elif first_word in ("model", "llm"):
            session.handle_model_info()
        elif first_word in ("undo", "revert"):
            session.handle_undo_last()
        elif first_word == "verbose":
            session.verbose = not session.verbose
            v_status = "ENABLED" if session.verbose else "DISABLED"
            console.print(f"[bold magenta]Verbose reasoning mode {v_status}.[/bold magenta]\n")
        elif first_word == "help":
            console.print(
                "Available commands:\n"
                "  connect <url|profile>          Connect to SQLite, Postgres, Mongo local, or Atlas\n"
                "  switch to <profile>            Switch to a saved connection profile\n"
                "  save connection as <name>      Save current connection to ~/.devdb/profiles.json\n"
                "  list connections / profiles    List saved connection profiles\n"
                "  mode <read-only|read-write|admin> Switch session mode\n"
                "  which model am I using? / model Show active LLM provider, model, and stats\n"
                "  explain                        Explain last LLM decision/query in simple words\n"
                "  capabilities                   Show feature matrix for active database\n"
                "  copy <table> from <s> to <d>   Cross-database table copy with progress bar\n"
                "  export <table> to <file>       Export table/collection to CSV or JSON\n"
                "  import <file> into <table>     Import CSV or JSON with 5-row preview\n"
                "  backup database                Create a timestamped backup\n"
                "  schema                         Show Rich table of the database schema\n"
                "  show_relationships [--mermaid] Show ASCII chain & Rich tree, export Mermaid\n"
                "  validate_relationships         Health report: orphan rows, indexes, missing FKs\n"
                "  add_relationship <c.fk> <p.pk> Add 1:N, 1:1, or N:M relationship\n"
                "  safe_delete_check <table> <id> Calculate cascading delete impact\n"
                "  list_users                     List database users and table permissions (Postgres)\n"
                "  create_user <user> [priv] [tbl]Create user/role with safe password (Postgres)\n"
                "  drop_user <user>               Safely drop database user (Postgres)\n"
                "  grant <priv> on <tbl> to <usr> Grant table privileges\n"
                "  revoke <priv> on <tbl> from <u>Revoke table privileges\n"
                "  history [limit]                Show recent interaction history table\n"
                "  undo                           Revert last write (fake data, user, insert)\n"
                "  verbose                        Toggle LLM reasoning breakdown\n"
                "  <any natural language query>   Plain English database management request\n"
                "  exit / quit                    Exit the shell\n"
            )

        else:
            # Natural language request pipeline
            session.handle_natural_language_request(line)


@app.command()
def connect(
    url: str,
    dry_run: bool = typer.Option(False, "--dry-run", help="Never execute SQL queries"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
    verbose: bool = typer.Option(False, "--verbose", help="Show LLM's reasoning field and internal thought steps"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Override LLM provider (ollama, gemini, mock)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override LLM model (e.g. llama3.2, gemini-1.5-flash)"),
):
    """Connect to a database URL and start interactive session."""
    run_interactive_loop(initial_url=url, dry_run=dry_run, prompt_version=version, verbose=verbose, provider=provider, model=model)


@app.command()
def schema(
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Optional database URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Never execute SQL queries"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
    verbose: bool = typer.Option(False, "--verbose", help="Show LLM's reasoning field and internal thought steps"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Override LLM provider (ollama, gemini, mock)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override LLM model (e.g. llama3.2, gemini-1.5-flash)"),
):
    """Print the database schema table."""
    if provider:
        session.llm_provider = provider
    if model:
        session.llm_model = model
    if url:
        session.dry_run = dry_run
        session.verbose = verbose
        if version:
            session.prompt_version = version
        if session.connect(url):
            session.print_schema()
    else:
        run_interactive_loop(dry_run=dry_run, prompt_version=version, verbose=verbose, provider=provider, model=model)



@app.command("history")
def history_cli(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of history records to show"),
):
    """Display recent DevDB session query and command history."""
    session.handle_history(limit=limit)


@app.command("undo")
def undo_cli(
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Optional database URL"),
):
    """Undo the most recent reversible write operation (fake data, user creation, or INSERT)."""
    if url:
        session.connect(url)
    session.handle_undo_last()


@app.command("show-relationships")
@app.command("show_relationships")
def show_relationships_cli(
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Optional database URL"),
    mermaid: bool = typer.Option(False, "--mermaid", help="Export Mermaid ER diagram to relationships.md"),
    out: str = typer.Option("relationships.md", "--out", help="Mermaid output file path"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
):
    """Show ASCII relationship map, Rich tree view, and optionally export Mermaid diagram."""
    if url:
        session.connect(url)
    session.handle_show_relationships(export_mermaid=mermaid, mermaid_file=out)


@app.command("validate-relationships")
@app.command("validate_relationships")
def validate_relationships_cli(
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Optional database URL"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
):
    """Validate referential integrity: detect orphan rows, unindexed FKs, and likely missing FKs."""
    if url:
        session.connect(url)
    session.handle_validate_relationships()


@app.command("add-relationship")
@app.command("add_relationship")
def add_relationship_cli(
    child: str = typer.Argument(..., help="Child column in format table.column (e.g. orders.customer_id)"),
    parent: str = typer.Argument(..., help="Parent column in format table.column (e.g. customers.id)"),
    rel_type: str = typer.Option("one_to_many", "--type", "-t", help="Relationship type: one_to_many, one_to_one, many_to_many"),
    on_delete: str = typer.Option("RESTRICT", "--on-delete", help="ON DELETE rule: RESTRICT, CASCADE, SET NULL"),
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Optional database URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview SQL without executing"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
):
    """Add a relationship between tables with SQL preview and safe transactional execution."""
    if url:
        session.connect(url)
    session.dry_run = dry_run
    c_parts = child.split(".")
    p_parts = parent.split(".")
    if len(c_parts) != 2 or len(p_parts) != 2:
        console.print("[bold red]Error:[/bold red] Arguments must be in format table.column (e.g. orders.customer_id customers.id)\n")
        return
    session.handle_add_relationship(c_parts[0], c_parts[1], p_parts[0], p_parts[1], rel_type=rel_type, on_delete=on_delete)


@app.command("safe-delete-check")
@app.command("safe_delete_check")
@app.command("check-delete")
def safe_delete_check_cli(
    table: str = typer.Argument(..., help="Parent table name (e.g. customers)"),
    target_filter: str = typer.Argument(..., help="Row ID or WHERE filter (e.g. 5 or id=5)"),
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Optional database URL"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
):
    """Check cascading impact before deleting rows from a parent table."""
    if url:
        session.connect(url)
    session.handle_safe_delete_check(table, target_filter)


@app.command("create-user")
@app.command("create_user")
def create_user_cli(
    username: str = typer.Argument(..., help="Database username (letters, digits, underscores)"),
    privileges: Optional[str] = typer.Option("read-only", "--privileges", "-p", help="Privileges (e.g. read-only, read-write, SELECT)"),
    tables: Optional[str] = typer.Option(None, "--tables", "-t", help="Comma-separated table names (e.g. orders,customers)"),
    password: Optional[str] = typer.Option(None, "--password", help="User password (generated securely if omitted)"),
    superuser: bool = typer.Option(False, "--superuser", help="Create as superuser (requires confirmation)"),
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Optional database URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview SQL without executing"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
):
    """Create a database user with password and table-level privileges (PostgreSQL only)."""
    if url:
        session.connect(url)
    session.dry_run = dry_run
    tbl_list = [t.strip() for t in tables.split(",")] if tables else []
    priv_list = [p.strip() for p in privileges.split(",")] if privileges else ["SELECT"]
    spec = {
        "operation": "create_user",
        "username": username,
        "privileges": priv_list,
        "tables": tbl_list,
        "password": password,
        "is_superuser": superuser,
    }
    session.handle_user_management(f"create user {username}", spec=spec)


@app.command("list-users")
@app.command("list_users")
def list_users_cli(
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Optional database URL"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
):
    """List PostgreSQL database users, roles, and table permissions."""
    if url:
        session.connect(url)
    session.handle_user_management("list users", spec={"operation": "list"})


@app.command("drop-user")
@app.command("drop_user")
def drop_user_cli(
    username: str = typer.Argument(..., help="Database username to drop"),
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Optional database URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview SQL without executing"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
):
    """Drop a PostgreSQL database user safely."""
    if url:
        session.connect(url)
    session.dry_run = dry_run
    spec = {
        "operation": "drop_user",
        "username": username,
    }
    session.handle_user_management(f"drop user {username}", spec=spec)


@app.command("grant")
def grant_privileges_cli(
    username: str = typer.Argument(..., help="Database username"),
    privileges: str = typer.Option(..., "--privileges", "-p", help="Privileges to grant (e.g. SELECT, INSERT, read-only)"),
    tables: Optional[str] = typer.Option(None, "--tables", "-t", help="Comma-separated table names"),
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Optional database URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview SQL without executing"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
):
    """Grant table privileges to a database user (PostgreSQL only)."""
    if url:
        session.connect(url)
    session.dry_run = dry_run
    tbl_list = [t.strip() for t in tables.split(",")] if tables else []
    priv_list = [p.strip() for p in privileges.split(",")] if privileges else ["SELECT"]
    spec = {
        "operation": "grant",
        "username": username,
        "privileges": priv_list,
        "tables": tbl_list,
    }
    session.handle_user_management(f"grant {privileges} on {tables or 'public'} to {username}", spec=spec)


@app.command("revoke")
def revoke_privileges_cli(
    username: str = typer.Argument(..., help="Database username"),
    privileges: str = typer.Option(..., "--privileges", "-p", help="Privileges to revoke (e.g. SELECT, ALL)"),
    tables: Optional[str] = typer.Option(None, "--tables", "-t", help="Comma-separated table names"),
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Optional database URL"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview SQL without executing"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
):
    """Revoke privileges from a database user (PostgreSQL only)."""
    if url:
        session.connect(url)
    session.dry_run = dry_run
    tbl_list = [t.strip() for t in tables.split(",")] if tables else []
    priv_list = [p.strip() for p in privileges.split(",")] if privileges else ["ALL"]
    spec = {
        "operation": "revoke",
        "username": username,
        "privileges": priv_list,
        "tables": tbl_list,
    }
    session.handle_user_management(f"revoke {privileges} on {tables or 'public'} from {username}", spec=spec)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Never execute SQL queries"),
    version: Optional[str] = typer.Option(None, "--version", "-v", help="Override prompt version (v1, v2, v3, v4)"),
    verbose: bool = typer.Option(False, "--verbose", help="Show LLM's reasoning field and internal thought steps"),
    provider: Optional[str] = typer.Option(None, "--provider", "-p", help="Override LLM provider (ollama, gemini, mock)"),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Override LLM model (e.g. llama3.2, gemini-1.5-flash)"),
):
    """DevDB CLI - run with no arguments to start interactive loop."""
    if ctx.invoked_subcommand is None:
        run_interactive_loop(dry_run=dry_run, prompt_version=version, verbose=verbose, provider=provider, model=model)



if __name__ == "__main__":
    app()
