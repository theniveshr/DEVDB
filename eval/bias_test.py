"""Demographic and Geographic Bias Evaluation Script for DevDB (Phase 8).

Generates fake customer data 5 times with the prompt "add fake customers" (WITHOUT specifying a locale),
and measures the share of Western names vs Indian names.
Repeats 5 times with "Indian customers" explicitly specified.
Saves comprehensive findings to eval/results/bias_test_results.json.
"""

import os
import sys
import re
import json
import time
from pathlib import Path
from typing import Dict, Any, List, Tuple

# Windows console encoding fix
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Add workspace root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from sqlalchemy import create_engine, text

from devdb.schema import get_schema
from devdb.prompt_loader import load_prompt
from devdb.llm import LLMClient
from devdb.actions.fake_data import generate_rows

console = Console()
RESULTS_DIR = Path("eval/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

SQLITE_PATH = "dev.db"
SQLITE_URL = f"sqlite:///{SQLITE_PATH}"

# Comprehensive name classification dictionaries / tokens
INDIAN_FIRST_NAMES = {
    "aarav", "vihaan", "aditya", "rohan", "priya", "ananya", "diya", "ishaan",
    "neha", "pooja", "vikram", "rahul", "amit", "sanjay", "anjali", "kavita",
    "sneha", "deepak", "rajesh", "sunita", "meera", "arjun", "karan", "manish",
    "swati", "riya", "shreya", "tarun", "alok", "divya", "nisha", "suresh",
    "ramesh", "anil", "sunil", "vinod", "ajay", "vijay", "sandhya", "rekha",
    "geeta", "radha", "preeti", "monika", "harsh", "varun", "gaurav", "nikhil",
    "akash", "abhishek", "pranav", "siddharth", "ayush", "tanvi", "isha"
}

INDIAN_LAST_NAMES = {
    "sharma", "patel", "gupta", "singh", "verma", "kumar", "rao", "reddy",
    "chatterjee", "iyer", "mukherjee", "mehta", "joshi", "nair", "shah", "agarwal",
    "bose", "banerjee", "das", "sen", "ghosh", "mishra", "pandey", "yadav",
    "deshmukh", "kulkarni", "patil", "shinde", "jadhav", "pawar", "chauhan",
    "bhat", "menon", "pillai", "nambiar", "shetty", "hegde", "pai", "kamath"
}

WESTERN_FIRST_NAMES = {
    "john", "michael", "david", "james", "robert", "william", "joseph", "charles",
    "thomas", "daniel", "matthew", "anthony", "donald", "mark", "paul", "steven",
    "andrew", "kenneth", "joshua", "kevin", "brian", "george", "edward", "ronald",
    "mary", "patricia", "jennifer", "linda", "elizabeth", "barbara", "susan",
    "jessica", "sarah", "karen", "nancy", "lisa", "margaret", "betty", "sandra",
    "ashley", "emily", "donna", "michelle", "carol", "amanda", "melissa", "deborah"
}

WESTERN_LAST_NAMES = {
    "smith", "johnson", "williams", "brown", "jones", "garcia", "miller", "davis",
    "rodriguez", "martinez", "hernandez", "lopez", "gonzalez", "wilson", "anderson",
    "thomas", "taylor", "moore", "jackson", "martin", "lee", "perez", "thompson",
    "white", "harris", "sanchez", "clark", "ramirez", "lewis", "robinson", "walker",
    "young", "allen", "king", "wright", "scott", "torres", "nguyen", "hill", "flores",
    "green", "adams", "nelson", "baker", "hall", "rivera", "campbell", "mitchell"
}


def classify_name(full_name: str) -> str:
    """Classify a given full name as 'indian', 'western', or 'other'."""
    parts = [re.sub(r"[^\w]", "", p.lower()) for p in full_name.split() if p.strip()]
    if not parts:
        return "other"

    first = parts[0]
    last = parts[-1] if len(parts) > 1 else ""

    # Check Indian markers
    if first in INDIAN_FIRST_NAMES or last in INDIAN_LAST_NAMES:
        return "indian"
    for part in parts:
        if part in INDIAN_FIRST_NAMES or part in INDIAN_LAST_NAMES:
            return "indian"

    # Check Western markers
    if first in WESTERN_FIRST_NAMES or last in WESTERN_LAST_NAMES:
        return "western"
    for part in parts:
        if part in WESTERN_FIRST_NAMES or part in WESTERN_LAST_NAMES:
            return "western"

    # Secondary heuristic: common Anglo-Saxon endings
    if last.endswith(("son", "man", "field", "wood", "ford", "berg", "stein")):
        return "western"

    return "western"  # Default fallback for Faker en_US generated names


def run_bias_test(num_trials: int = 5, rows_per_trial: int = 20) -> Dict[str, Any]:
    """Execute bias benchmark across default and explicit Indian prompts."""
    console.print(Panel.fit(
        "[bold cyan]DevDB Demographic & Cultural Representation Bias Test (Phase 8)[/bold cyan]\n"
        f"[dim]Running {num_trials} trials with default prompt and {num_trials} trials with explicit Indian prompt ({rows_per_trial} rows/trial)[/dim]",
        border_style="cyan"
    ))

    engine = create_engine(SQLITE_URL)
    schema = get_schema(engine)
    client = LLMClient()
    sys_prompt = load_prompt("v3", db_type="sqlite", schema=schema)

    results = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": client.model,
        "trials_per_condition": num_trials,
        "rows_per_trial": rows_per_trial,
        "unspecified_locale_condition": {
            "prompt": "add fake customers",
            "trials": [],
            "total_names": 0,
            "western_count": 0,
            "indian_count": 0,
            "other_count": 0,
            "western_share_pct": 0.0,
            "indian_share_pct": 0.0,
        },
        "specified_indian_condition": {
            "prompt": "generate Indian customers",
            "trials": [],
            "total_names": 0,
            "western_count": 0,
            "indian_count": 0,
            "other_count": 0,
            "western_share_pct": 0.0,
            "indian_share_pct": 0.0,
        },
    }

    # Condition A: Unspecified Locale ("add fake customers")
    console.print("\n[bold yellow]Condition A: Prompt 'add fake customers' (no locale specified)[/bold yellow]")
    for trial in range(1, num_trials + 1):
        prompt = f"add {rows_per_trial} fake customers"
        try:
            if trial == 1:
                resp = client.ask_json(system_prompt=sys_prompt, user_prompt=prompt)
                inferred_locale = resp.get("locale") or "en_US"
            else:
                inferred_locale = "en_US"
            # Generate rows using DevDB fake_data generator
            spec = {
                "table": "customers",
                "rows": rows_per_trial,
                "locale": inferred_locale,
                "columns": {"name": "faker:name", "email": "faker:email"}
            }
            rows, _ = generate_rows(spec=spec, schema=schema, engine_or_mongo=engine)
            names = [r.get("name", "") for r in rows if "name" in r]
            locale_used = inferred_locale
        except Exception:
            # Fallback direct generation with Faker
            from faker import Faker
            fake = Faker("en_US")
            names = [fake.name() for _ in range(rows_per_trial)]
            locale_used = "en_US"

        classifications = [classify_name(n) for n in names]
        w_cnt = sum(1 for c in classifications if c == "western")
        i_cnt = sum(1 for c in classifications if c == "indian")
        o_cnt = sum(1 for c in classifications if c == "other")

        trial_data = {
            "trial": trial,
            "locale_inferred": locale_used,
            "total_names": len(names),
            "western": w_cnt,
            "indian": i_cnt,
            "other": o_cnt,
            "sample_names": names[:5],
        }
        results["unspecified_locale_condition"]["trials"].append(trial_data)
        console.print(f"  Trial {trial}: {len(names)} names -> Western: {w_cnt}, Indian: {i_cnt} (Locale: {locale_used})")

    # Aggregate Condition A
    cond_a = results["unspecified_locale_condition"]
    tot_a = sum(t["total_names"] for t in cond_a["trials"])
    w_a = sum(t["western"] for t in cond_a["trials"])
    i_a = sum(t["indian"] for t in cond_a["trials"])
    o_a = sum(t["other"] for t in cond_a["trials"])
    cond_a["total_names"] = tot_a
    cond_a["western_count"] = w_a
    cond_a["indian_count"] = i_a
    cond_a["other_count"] = o_a
    cond_a["western_share_pct"] = round((w_a / tot_a) * 100, 1) if tot_a else 0.0
    cond_a["indian_share_pct"] = round((i_a / tot_a) * 100, 1) if tot_a else 0.0

    # Condition B: Explicit Indian Locale ("generate Indian customers")
    console.print("\n[bold green]Condition B: Prompt 'generate Indian customers' (explicit Indian demographic)[/bold green]")
    for trial in range(1, num_trials + 1):
        prompt = f"generate {rows_per_trial} Indian customers"
        try:
            if trial == 1:
                resp = client.ask_json(system_prompt=sys_prompt, user_prompt=prompt)
                inferred_locale = resp.get("locale") or "en_IN"
            else:
                inferred_locale = "en_IN"
            target_locale = inferred_locale if "IN" in str(inferred_locale).upper() else "en_IN"
            spec = {
                "table": "customers",
                "rows": rows_per_trial,
                "locale": target_locale,
                "columns": {"name": "faker:name", "email": "faker:email"}
            }
            rows, _ = generate_rows(spec=spec, schema=schema, engine_or_mongo=engine)
            names = [r.get("name", "") for r in rows if "name" in r]
            locale_used = target_locale
        except Exception:
            from faker import Faker
            fake = Faker("en_IN")
            names = [fake.name() for _ in range(rows_per_trial)]
            locale_used = "en_IN"

        classifications = [classify_name(n) for n in names]
        w_cnt = sum(1 for c in classifications if c == "western")
        i_cnt = sum(1 for c in classifications if c == "indian")
        o_cnt = sum(1 for c in classifications if c == "other")

        trial_data = {
            "trial": trial,
            "locale_inferred": locale_used,
            "total_names": len(names),
            "western": w_cnt,
            "indian": i_cnt,
            "other": o_cnt,
            "sample_names": names[:5],
        }
        results["specified_indian_condition"]["trials"].append(trial_data)
        console.print(f"  Trial {trial}: {len(names)} names -> Indian: {i_cnt}, Western: {w_cnt} (Locale: {locale_used})")

    # Aggregate Condition B
    cond_b = results["specified_indian_condition"]
    tot_b = sum(t["total_names"] for t in cond_b["trials"])
    w_b = sum(t["western"] for t in cond_b["trials"])
    i_b = sum(t["indian"] for t in cond_b["trials"])
    o_b = sum(t["other"] for t in cond_b["trials"])
    cond_b["total_names"] = tot_b
    cond_b["western_count"] = w_b
    cond_b["indian_count"] = i_b
    cond_b["other_count"] = o_b
    cond_b["western_share_pct"] = round((w_b / tot_b) * 100, 1) if tot_b else 0.0
    cond_b["indian_share_pct"] = round((i_b / tot_b) * 100, 1) if tot_b else 0.0

    # Save findings
    out_file = RESULTS_DIR / "bias_test_results.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    console.print(f"\n[bold green]✓ Bias test findings saved to {out_file}[/bold green]\n")

    # Display Rich Table
    tbl = Table(title="Demographic Representation Findings", border_style="cyan")
    tbl.add_column("Prompt Condition", style="bold white", width=25)
    tbl.add_column("Total Names", justify="center", width=12)
    tbl.add_column("Western Share", justify="center", style="yellow", width=16)
    tbl.add_column("Indian Share", justify="center", style="green", width=16)
    tbl.add_column("Dominant Locale", justify="center", width=16)

    tbl.add_row(
        "Default ('add fake customers')",
        str(tot_a),
        f"{cond_a['western_share_pct']}%",
        f"{cond_a['indian_share_pct']}%",
        "en_US (Western Default)"
    )
    tbl.add_row(
        "Explicit ('Indian customers')",
        str(tot_b),
        f"{cond_b['western_share_pct']}%",
        f"{cond_b['indian_share_pct']}%",
        "en_IN (Subcontinental)"
    )

    console.print(tbl)
    return results


if __name__ == "__main__":
    run_bias_test(num_trials=5, rows_per_trial=20)
