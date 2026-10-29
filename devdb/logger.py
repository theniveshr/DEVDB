"""Session event logging module for DevDB."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


LOG_DIR = Path("logs")
SESSION_LOG = LOG_DIR / "session.jsonl"


def log_session_event(
    request: str,
    sql: str,
    ran: bool,
    success: bool,
    error: Optional[str] = None,
    action: Optional[str] = None,
    risk: Optional[str] = None,
    prompt_version: Optional[str] = None,
    db_type: Optional[str] = None,
    dry_run: bool = False,
) -> None:
    """Record an interaction event into logs/session.jsonl."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = SESSION_LOG

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request": request,
        "action": action,
        "sql": sql,
        "risk": risk,
        "ran": ran,
        "success": success,
        "error": error,
        "prompt_version": prompt_version,
        "db_type": db_type,
        "dry_run": dry_run,
    }

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def get_session_history(limit: int = 25) -> list[dict]:
    """Retrieve the most recent session interaction events from logs/session.jsonl."""
    if not SESSION_LOG.exists():
        return []

    entries = []
    with open(SESSION_LOG, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue

    return entries[-limit:]


def render_history_table(history: list[dict]):
    """Render Rich table of session execution history."""
    from rich.table import Table

    table = Table(title="DevDB Session Interaction History", show_lines=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=4)
    table.add_column("Timestamp", style="cyan", width=19)
    table.add_column("Request", style="white", max_width=35)
    table.add_column("Action", style="yellow", width=12)
    table.add_column("Risk", style="bold", width=8)
    table.add_column("Ran", style="green", width=5)
    table.add_column("Status", style="bold", width=8)

    for i, entry in enumerate(history, start=1):
        ts = entry.get("timestamp", "")
        if "T" in ts:
            ts = ts.split("T")[1][:8]

        req = entry.get("request", "")
        action = (entry.get("action") or "-").upper()
        risk = entry.get("risk") or "-"
        risk_styled = (
            f"[bold green]{risk}[/bold green]" if risk == "LOW"
            else (f"[bold yellow]{risk}[/bold yellow]" if risk == "MEDIUM" else f"[bold red]{risk}[/bold red]")
        )
        ran = "YES" if entry.get("ran") else "NO"
        success = entry.get("success", False)
        status_styled = "[bold green]OK[/bold green]" if success else "[bold red]FAIL[/bold red]"

        table.add_row(str(i), ts, req, action, risk_styled, ran, status_styled)

    return table
