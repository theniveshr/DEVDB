"""Benchmark evaluation runner comparing prompt versions V1, V2, and V3."""

import json
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from src.config import PROJECT_ROOT
from src.db import DatabaseManager
from src.sql_generator import SQLGenerator

console = Console()


def run_benchmark(provider: str = "mock") -> None:
    """Run benchmark queries across V1, V2, and V3 and output a comparative scorecard."""
    benchmark_file = PROJECT_ROOT / "evaluation" / "benchmark_queries.json"
    if not benchmark_file.exists():
        console.print(f"[bold red]Benchmark file not found at {benchmark_file}[/bold red]")
        return

    queries = json.loads(benchmark_file.read_text(encoding="utf-8"))

    # Use in-memory SQLite database for benchmark
    db = DatabaseManager("sqlite://")
    db.init_sample_database()
    generator = SQLGenerator(db)

    versions = ["v1", "v2", "v3"]
    scorecard = {v: {"total": 0, "syntax_valid": 0, "no_hallucinations": 0, "executed_ok": 0} for v in versions}

    results_table = Table(title=f"DevDB Prompt Engineering Benchmark ({provider.upper()} mode)", border_style="cyan")
    results_table.add_column("ID", justify="center", style="bold")
    results_table.add_column("Category", style="cyan")
    results_table.add_column("V1: Naive", style="yellow")
    results_table.add_column("V2: Few-Shot", style="blue")
    results_table.add_column("V3: CoT + Guardrails", style="green")

    for item in queries:
        qid = str(item["id"])
        cat = item["category"]
        q_text = item["question"]

        row_status = [qid, cat]

        for v in versions:
            scorecard[v]["total"] += 1
            try:
                res = generator.generate(q_text, version=v, provider=provider)
                sql = res.sql.strip()

                # 1. Syntax check via EXPLAIN
                is_valid = False
                try:
                    # Test execution in transaction rollback
                    test_sql = sql.rstrip(";")
                    # SQLite EXPLAIN
                    db.execute_query(f"EXPLAIN {test_sql}")
                    is_valid = True
                    scorecard[v]["syntax_valid"] += 1
                except Exception:
                    is_valid = False

                # 2. Hallucination check: look for 'phone' on 'users' table in query #6
                has_hallucination = False
                if item["id"] == 6:
                    if "users.phone" in sql.lower() or ("phone" in sql.lower() and "user_profiles" not in sql.lower()):
                        has_hallucination = True

                if not has_hallucination:
                    scorecard[v]["no_hallucinations"] += 1

                if is_valid and not has_hallucination:
                    scorecard[v]["executed_ok"] += 1
                    status_badge = "[bold green]PASS[/bold green]"
                elif has_hallucination:
                    status_badge = "[bold red]HALLUCINATION[/bold red]"
                else:
                    status_badge = "[bold yellow]SYNTAX ERR[/bold yellow]"

                row_status.append(status_badge)

            except Exception as e:
                row_status.append(f"[bold red]FAIL ({type(e).__name__})[/bold red]")

        results_table.add_row(*row_status)

    console.print(results_table)

    # Summary table
    summary_table = Table(title="Overall Prompt Performance Summary", border_style="magenta")
    summary_table.add_column("Prompt Version", style="bold")
    summary_table.add_column("Prompting Techniques Used", style="cyan")
    summary_table.add_column("Syntax Validity", justify="center")
    summary_table.add_column("Hallucination Resistance", justify="center")
    summary_table.add_column("End-to-End Success Rate", justify="center")

    techniques = {
        "v1": "Zero-shot baseline (no persona, no examples)",
        "v2": "Role/Persona + 4 Few-Shot Examples",
        "v3": "Role + Chain of Thought (CoT) + Schema Grounding",
    }

    for v in versions:
        tot = scorecard[v]["total"] or 1
        syn_pct = f"{(scorecard[v]['syntax_valid'] / tot) * 100:.1f}%"
        hal_pct = f"{(scorecard[v]['no_hallucinations'] / tot) * 100:.1f}%"
        e2e_pct = f"{(scorecard[v]['executed_ok'] / tot) * 100:.1f}%"

        summary_table.add_row(
            v.upper(),
            techniques[v],
            syn_pct,
            hal_pct,
            e2e_pct,
        )

    console.print(summary_table)


if __name__ == "__main__":
    import sys
    prov = sys.argv[1] if len(sys.argv) > 1 else "mock"
    run_benchmark(provider=prov)
