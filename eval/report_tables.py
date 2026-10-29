"""Report generator for DevDB prompt evaluation (V1 vs V2 vs V3).

Reads eval/results/raw_<version>.json, produces:
- eval/results/comparison.csv: Machine-readable metrics and per-category breakdown.
- eval/results/comparison.md: Rich Markdown comparison table.
- eval/results/comparison.png: Professional multi-metric comparison chart using matplotlib.
"""

import os
import sys
import json
import csv
from pathlib import Path
from typing import Dict, Any, List

# Windows console encoding fix
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import matplotlib
matplotlib.use("Agg")  # Headless backend
import matplotlib.pyplot as plt
import numpy as np
from rich.console import Console
from rich.table import Table

console = Console()
RESULTS_DIR = Path("eval/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)


def load_raw_results(version: str) -> List[Dict[str, Any]]:
    path = RESULTS_DIR / f"raw_{version}.json"
    if not path.exists():
        console.print(f"[yellow]Warning: {path} not found. Skipping {version}.[/yellow]")
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def calculate_metrics(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not items:
        return {
            "total": 0,
            "exec_ok_count": 0,
            "exec_ok_pct": 0.0,
            "correct_count": 0,
            "correct_pct": 0.0,
            "hallucination_count": 0,
            "hallucination_pct": 0.0,
            "dialect_error_count": 0,
            "dialect_error_pct": 0.0,
            "safety_total": 0,
            "safety_pass_count": 0,
            "safety_pass_pct": 0.0,
            "avg_latency": 0.0,
            "categories": {},
        }

    total = len(items)
    exec_ok = sum(1 for x in items if x.get("executed_ok"))
    correct = sum(1 for x in items if x.get("correct"))
    halluc = sum(1 for x in items if x.get("hallucination"))
    dialect_err = sum(1 for x in items if x.get("wrong_dialect"))
    latencies = [x.get("latency_seconds", 0.0) for x in items]
    avg_latency = round(sum(latencies) / total, 3) if total else 0.0

    # Safety requests
    safety_items = [x for x in items if x.get("category") == "safety"]
    safety_total = len(safety_items)
    safety_pass = sum(1 for x in safety_items if x.get("safety_pass"))
    safety_pct = round((safety_pass / safety_total) * 100, 1) if safety_total else 100.0

    # Per-category metrics
    categories = {}
    for cat in ["fake_data", "user_mgmt", "relationship", "query", "safety"]:
        cat_items = [x for x in items if x.get("category") == cat]
        cat_tot = len(cat_items)
        if cat_tot == 0:
            continue
        c_exec = sum(1 for x in cat_items if x.get("executed_ok"))
        c_corr = sum(1 for x in cat_items if x.get("correct"))
        c_hal = sum(1 for x in cat_items if x.get("hallucination"))
        c_dia = sum(1 for x in cat_items if x.get("wrong_dialect"))
        c_lat = [x.get("latency_seconds", 0.0) for x in cat_items]
        categories[cat] = {
            "total": cat_tot,
            "exec_ok": c_exec,
            "exec_ok_pct": round((c_exec / cat_tot) * 100, 1),
            "correct": c_corr,
            "correct_pct": round((c_corr / cat_tot) * 100, 1),
            "hallucination": c_hal,
            "dialect_error": c_dia,
            "avg_latency": round(sum(c_lat) / cat_tot, 3) if cat_tot else 0.0,
        }

    return {
        "total": total,
        "exec_ok_count": exec_ok,
        "exec_ok_pct": round((exec_ok / total) * 100, 1),
        "correct_count": correct,
        "correct_pct": round((correct / total) * 100, 1),
        "hallucination_count": halluc,
        "hallucination_pct": round((halluc / total) * 100, 1),
        "dialect_error_count": dialect_err,
        "dialect_error_pct": round((dialect_err / total) * 100, 1),
        "safety_total": safety_total,
        "safety_pass_count": safety_pass,
        "safety_pass_pct": safety_pct,
        "avg_latency": avg_latency,
        "categories": categories,
    }


def generate_csv_report(data: Dict[str, Dict[str, Any]], out_path: Path):
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Metric", "V1 (Naive)", "V2 (Schema/Rules)", "V3 (Reasoning/CoT)"])
        writer.writerow([
            "Total Requests",
            data.get("v1", {}).get("total", 0),
            data.get("v2", {}).get("total", 0),
            data.get("v3", {}).get("total", 0),
        ])
        writer.writerow([
            "Execution Success Rate (%)",
            f"{data.get('v1', {}).get('exec_ok_pct', 0.0)}%",
            f"{data.get('v2', {}).get('exec_ok_pct', 0.0)}%",
            f"{data.get('v3', {}).get('exec_ok_pct', 0.0)}%",
        ])
        writer.writerow([
            "Correctness Rate (%)",
            f"{data.get('v1', {}).get('correct_pct', 0.0)}%",
            f"{data.get('v2', {}).get('correct_pct', 0.0)}%",
            f"{data.get('v3', {}).get('correct_pct', 0.0)}%",
        ])
        writer.writerow([
            "Hallucination Count",
            data.get("v1", {}).get("hallucination_count", 0),
            data.get("v2", {}).get("hallucination_count", 0),
            data.get("v3", {}).get("hallucination_count", 0),
        ])
        writer.writerow([
            "Dialect Syntax Errors",
            data.get("v1", {}).get("dialect_error_count", 0),
            data.get("v2", {}).get("dialect_error_count", 0),
            data.get("v3", {}).get("dialect_error_count", 0),
        ])
        writer.writerow([
            "Safety Pass Rate (%)",
            f"{data.get('v1', {}).get('safety_pass_pct', 0.0)}%",
            f"{data.get('v2', {}).get('safety_pass_pct', 0.0)}%",
            f"{data.get('v3', {}).get('safety_pass_pct', 0.0)}%",
        ])
        writer.writerow([
            "Average Latency (seconds)",
            f"{data.get('v1', {}).get('avg_latency', 0.0)}s",
            f"{data.get('v2', {}).get('avg_latency', 0.0)}s",
            f"{data.get('v3', {}).get('avg_latency', 0.0)}s",
        ])

        writer.writerow([])
        writer.writerow(["Category Breakdown: Correctness Rate (%)"])
        writer.writerow(["Category", "V1 (Naive)", "V2 (Schema/Rules)", "V3 (Reasoning/CoT)"])
        for cat in ["fake_data", "user_mgmt", "relationship", "query", "safety"]:
            v1_c = data.get("v1", {}).get("categories", {}).get(cat, {}).get("correct_pct", "N/A")
            v2_c = data.get("v2", {}).get("categories", {}).get(cat, {}).get("correct_pct", "N/A")
            v3_c = data.get("v3", {}).get("categories", {}).get(cat, {}).get("correct_pct", "N/A")
            writer.writerow([cat, f"{v1_c}%", f"{v2_c}%", f"{v3_c}%"])


def generate_markdown_report(data: Dict[str, Dict[str, Any]], out_path: Path):
    lines = [
        "# DevDB Prompt Benchmark Comparison: V1 vs V2 vs V3",
        "",
        "Comprehensive quantitative evaluation measuring prompt engineering improvements across ",
        "Naive baseline (V1), Schema-grounded with dialect rules (V2), and Chain-of-Thought reasoning (V3).",
        "",
        "## Overall Summary Table",
        "",
        "| Metric | V1 (Naive) | V2 (Schema-Grounded) | V3 (Chain-of-Thought) | Improvement (V3 vs V1) |",
        "| :--- | :---: | :---: | :---: | :---: |",
    ]

    v1 = data.get("v1", {})
    v2 = data.get("v2", {})
    v3 = data.get("v3", {})

    def diff_str(val3: float, val1: float, suffix: str = "%", invert: bool = False) -> str:
        diff = val3 - val1
        if invert:
            # For errors / hallucinations, lower is better
            return f"-{abs(diff):.1f}{suffix}" if diff <= 0 else f"+{diff:.1f}{suffix}"
        return f"+{diff:.1f}{suffix}" if diff >= 0 else f"{diff:.1f}{suffix}"

    lines.append(f"| **Total Requests** | {v1.get('total', 0)} | {v2.get('total', 0)} | {v3.get('total', 0)} | — |")
    lines.append(f"| **Execution Success Rate** | {v1.get('exec_ok_pct', 0.0)}% | {v2.get('exec_ok_pct', 0.0)}% | {v3.get('exec_ok_pct', 0.0)}% | **{diff_str(v3.get('exec_ok_pct', 0.0), v1.get('exec_ok_pct', 0.0))}** |")
    lines.append(f"| **Correctness Rate** | {v1.get('correct_pct', 0.0)}% | {v2.get('correct_pct', 0.0)}% | {v3.get('correct_pct', 0.0)}% | **{diff_str(v3.get('correct_pct', 0.0), v1.get('correct_pct', 0.0))}** |")
    lines.append(f"| **Hallucination Count** | {v1.get('hallucination_count', 0)} | {v2.get('hallucination_count', 0)} | {v3.get('hallucination_count', 0)} | **{v3.get('hallucination_count', 0) - v1.get('hallucination_count', 0)}** |")
    lines.append(f"| **Dialect Errors** | {v1.get('dialect_error_count', 0)} | {v2.get('dialect_error_count', 0)} | {v3.get('dialect_error_count', 0)} | **{v3.get('dialect_error_count', 0) - v1.get('dialect_error_count', 0)}** |")
    lines.append(f"| **Safety Pass Rate** | {v1.get('safety_pass_pct', 0.0)}% | {v2.get('safety_pass_pct', 0.0)}% | {v3.get('safety_pass_pct', 0.0)}% | **{diff_str(v3.get('safety_pass_pct', 0.0), v1.get('safety_pass_pct', 0.0))}** |")
    lines.append(f"| **Average Latency** | {v1.get('avg_latency', 0.0)}s | {v2.get('avg_latency', 0.0)}s | {v3.get('avg_latency', 0.0)}s | {diff_str(v3.get('avg_latency', 0.0), v1.get('avg_latency', 0.0), suffix='s')} |")

    lines.extend([
        "",
        "## Per-Category Correctness Breakdown",
        "",
        "| Category | Requests | V1 Correctness | V2 Correctness | V3 Correctness | Delta (V3 - V1) |",
        "| :--- | :---: | :---: | :---: | :---: | :---: |",
    ])

    for cat in ["fake_data", "user_mgmt", "relationship", "query", "safety"]:
        cat_title = cat.replace("_", " ").title()
        v1_cat = v1.get("categories", {}).get(cat, {})
        v2_cat = v2.get("categories", {}).get(cat, {})
        v3_cat = v3.get("categories", {}).get(cat, {})
        tot = v3_cat.get("total", v1_cat.get("total", 0))

        v1_pct = v1_cat.get("correct_pct", 0.0)
        v2_pct = v2_cat.get("correct_pct", 0.0)
        v3_pct = v3_cat.get("correct_pct", 0.0)
        delta = v3_pct - v1_pct
        delta_str = f"+{delta:.1f}%" if delta >= 0 else f"{delta:.1f}%"

        lines.append(f"| **{cat_title}** | {tot} | {v1_pct}% | {v2_pct}% | {v3_pct}% | **{delta_str}** |")

    lines.extend([
        "",
        "## Benchmark Visualization",
        "",
        "![Evaluation Comparison Chart](comparison.png)",
        "",
    ])

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def generate_comparison_chart(data: Dict[str, Dict[str, Any]], out_path: Path):
    """Generate high-resolution comparison chart using matplotlib."""
    v1 = data.get("v1", {})
    v2 = data.get("v2", {})
    v3 = data.get("v3", {})

    metrics = ["Execution OK", "Correctness", "Safety Pass", "Low Hallucination", "Dialect OK"]
    # Calculate inverted rate for hallucinations and dialect errors (higher is better for visualization)
    def calc_ok_rate(total: int, err_count: int) -> float:
        if not total:
            return 100.0
        return round(((total - err_count) / total) * 100, 1)

    v1_vals = [
        v1.get("exec_ok_pct", 0.0),
        v1.get("correct_pct", 0.0),
        v1.get("safety_pass_pct", 0.0),
        calc_ok_rate(v1.get("total", 0), v1.get("hallucination_count", 0)),
        calc_ok_rate(v1.get("total", 0), v1.get("dialect_error_count", 0)),
    ]
    v2_vals = [
        v2.get("exec_ok_pct", 0.0),
        v2.get("correct_pct", 0.0),
        v2.get("safety_pass_pct", 0.0),
        calc_ok_rate(v2.get("total", 0), v2.get("hallucination_count", 0)),
        calc_ok_rate(v2.get("total", 0), v2.get("dialect_error_count", 0)),
    ]
    v3_vals = [
        v3.get("exec_ok_pct", 0.0),
        v3.get("correct_pct", 0.0),
        v3.get("safety_pass_pct", 0.0),
        calc_ok_rate(v3.get("total", 0), v3.get("hallucination_count", 0)),
        calc_ok_rate(v3.get("total", 0), v3.get("dialect_error_count", 0)),
    ]

    x = np.arange(len(metrics))
    width = 0.25

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6), gridspec_kw={"width_ratios": [2.5, 1.2]})

    # Colors
    c_v1 = "#e74c3c"  # Coral / Red
    c_v2 = "#3498db"  # Blue
    c_v3 = "#2ecc71"  # Emerald Green

    # Bar chart
    rects1 = ax1.bar(x - width, v1_vals, width, label="V1: Naive Baseline", color=c_v1, alpha=0.9, edgecolor="black")
    rects2 = ax1.bar(x, v2_vals, width, label="V2: Schema-Grounded", color=c_v2, alpha=0.9, edgecolor="black")
    rects3 = ax1.bar(x + width, v3_vals, width, label="V3: Chain-of-Thought", color=c_v3, alpha=0.9, edgecolor="black")

    ax1.set_ylabel("Rate (%)", fontsize=12, fontweight="bold")
    ax1.set_title("DevDB Prompt Version Comparison (Higher is Better)", fontsize=14, fontweight="bold", pad=12)
    ax1.set_xticks(x)
    ax1.set_xticklabels(metrics, fontsize=10, fontweight="bold")
    ax1.set_ylim(0, 115)
    ax1.grid(axis="y", linestyle="--", alpha=0.5)
    ax1.legend(loc="upper left", framealpha=0.95, fontsize=10)

    # Add data labels
    def autolabel(rects):
        for rect in rects:
            h = rect.get_height()
            ax1.annotate(f"{h:.0f}%",
                         xy=(rect.get_x() + rect.get_width() / 2, h),
                         xytext=(0, 3),
                         textcoords="offset points",
                         ha="center", va="bottom", fontsize=8, fontweight="bold")

    autolabel(rects1)
    autolabel(rects2)
    autolabel(rects3)

    # Subplot: Latency comparison
    lat_labels = ["V1 (Naive)", "V2 (Schema)", "V3 (CoT)"]
    lat_vals = [v1.get("avg_latency", 0.0), v2.get("avg_latency", 0.0), v3.get("avg_latency", 0.0)]
    lat_colors = [c_v1, c_v2, c_v3]

    ax2.bar(lat_labels, lat_vals, color=lat_colors, width=0.55, edgecolor="black", alpha=0.9)
    ax2.set_ylabel("Seconds", fontsize=12, fontweight="bold")
    ax2.set_title("Avg Latency (Seconds)", fontsize=14, fontweight="bold", pad=12)
    ax2.grid(axis="y", linestyle="--", alpha=0.5)
    for i, v in enumerate(lat_vals):
        ax2.text(i, v + 0.05, f"{v:.2f}s", ha="center", va="bottom", fontsize=10, fontweight="bold")

    plt.tight_layout()
    plt.savefig(out_path, dpi=300)
    plt.close()
    console.print(f"[bold green]✓ Saved comparison chart to {out_path}[/bold green]")


def main():
    versions = ["v1", "v2", "v3"]
    all_data = {}
    for v in versions:
        raw = load_raw_results(v)
        all_data[v] = calculate_metrics(raw)

    csv_path = RESULTS_DIR / "comparison.csv"
    md_path = RESULTS_DIR / "comparison.md"
    png_path = RESULTS_DIR / "comparison.png"

    generate_csv_report(all_data, csv_path)
    console.print(f"[bold green]✓ Saved CSV report to {csv_path}[/bold green]")

    generate_markdown_report(all_data, md_path)
    console.print(f"[bold green]✓ Saved Markdown report to {md_path}[/bold green]")

    generate_comparison_chart(all_data, png_path)

    # Render summary table in terminal
    t = Table(title="DevDB Prompt Versions Benchmark Summary", border_style="cyan")
    t.add_column("Metric", style="bold white", width=26)
    t.add_column("V1 (Naive)", justify="center", width=16)
    t.add_column("V2 (Schema)", justify="center", width=16)
    t.add_column("V3 (Reasoning)", justify="center", width=16)

    t.add_row("Execution Success Rate", f"{all_data['v1']['exec_ok_pct']}%", f"{all_data['v2']['exec_ok_pct']}%", f"{all_data['v3']['exec_ok_pct']}%")
    t.add_row("Correctness Rate", f"{all_data['v1']['correct_pct']}%", f"{all_data['v2']['correct_pct']}%", f"{all_data['v3']['correct_pct']}%")
    t.add_row("Hallucination Count", str(all_data["v1"]["hallucination_count"]), str(all_data["v2"]["hallucination_count"]), str(all_data["v3"]["hallucination_count"]))
    t.add_row("Dialect Errors", str(all_data["v1"]["dialect_error_count"]), str(all_data["v2"]["dialect_error_count"]), str(all_data["v3"]["dialect_error_count"]))
    t.add_row("Safety Pass Rate", f"{all_data['v1']['safety_pass_pct']}%", f"{all_data['v2']['safety_pass_pct']}%", f"{all_data['v3']['safety_pass_pct']}%")
    t.add_row("Average Latency", f"{all_data['v1']['avg_latency']}s", f"{all_data['v2']['avg_latency']}s", f"{all_data['v3']['avg_latency']}s")

    console.print()
    console.print(t)
    console.print()


if __name__ == "__main__":
    main()
