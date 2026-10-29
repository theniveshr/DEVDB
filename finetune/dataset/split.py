"""Dataset Splitter & Anti-Leak Validator for DevDB Fine-Tuning.

Responsibilities:
1. Loads validated_dataset.json and eval/test_set.json.
2. Performs strict leak detection:
   - Compares every training/validation candidate against all 40 eval/test_set.json requests.
   - Evaluates both exact normalized string match and fuzzy similarity (difflib SequenceMatcher).
   - Fails loudly (raises ValueError) if any leak or near-duplicate is detected.
3. Formats each record into the chat messages format:
   {
     "messages": [
       {"role": "system", "content": <3-line short system prompt>},
       {"role": "user", "content": "Database Schema:\n{schema}\n\nUser Request:\n{request}"},
       {"role": "assistant", "content": <assistant_json_response>}
     ]
   }
4. Splits dataset into train.jsonl (85%) and val.jsonl (15%) with fixed seed.
5. Computes comprehensive dataset statistics and writes finetune/dataset/STATS.md.
"""

import os
import re
import sys
import json
import random
import difflib
from pathlib import Path
from typing import Dict, Any, List, Tuple, Set

# Enable UTF-8 encoding on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from finetune.schemas.definitions import get_all_schemas
from devdb.schema import schema_to_text

BASE_DIR = Path(__file__).resolve().parent
VALIDATED_DATASET_FILE = BASE_DIR / "validated_dataset.json"
TEST_SET_FILE = BASE_DIR.parent.parent / "eval" / "test_set.json"
TRAIN_FILE = BASE_DIR / "train.jsonl"
VAL_FILE = BASE_DIR / "val.jsonl"
STATS_FILE = BASE_DIR / "STATS.md"

SHORT_SYSTEM_PROMPT = (
    "You are DevDB Assistant, an expert AI database engineer.\n"
    "Convert user database requests into structured JSON actions matching DevDB specifications (query, write, relationship, fake_data, create_user, export, clarify, refusal).\n"
    "Respond with valid JSON only, without markdown code fences or surrounding text."
)


def normalize_text(text: str) -> str:
    """Normalize text for leak detection."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def estimate_tokens(text: str) -> int:
    """Estimate token count for LLM chat messages (~4 chars or word regex)."""
    # Regex splits by words and punctuation tokens
    tokens = re.findall(r"\w+|[^\w\s]", text)
    return max(1, len(tokens))


def check_for_eval_leaks(
    examples: List[Dict[str, Any]],
    eval_requests: List[Dict[str, Any]],
    similarity_threshold: float = 0.75,
) -> None:
    """Strictly verify that no evaluation request or near-duplicate appears in the dataset.
    
    Fails loudly if any leak is detected.
    """
    print(f"▶ Running anti-leak verification against {len(eval_requests)} eval test cases...")

    normalized_evals = []
    for item in eval_requests:
        req = item.get("request", "")
        normalized_evals.append((req, normalize_text(req), item.get("id", "EVAL")))

    leak_count = 0
    leaks = []

    for item in examples:
        cand_req = item.get("user_request", "")
        norm_cand = normalize_text(cand_req)

        for raw_eval, norm_eval, eval_id in normalized_evals:
            # 1. Exact normalized match
            if norm_cand == norm_eval:
                leak_count += 1
                leaks.append(f"EXACT LEAK [{eval_id}]: '{cand_req}' matches eval '{raw_eval}'")
                continue

            # 2. Fuzzy similarity check
            sim = difflib.SequenceMatcher(None, norm_cand, norm_eval).ratio()
            if sim >= similarity_threshold:
                leak_count += 1
                leaks.append(
                    f"FUZZY LEAK [{eval_id}] (similarity {sim:.2f}): '{cand_req}' near-duplicate of eval '{raw_eval}'"
                )

    if leak_count > 0:
        error_msg = f"CRITICAL: Found {leak_count} test set leaks in dataset!\n" + "\n".join(leaks[:10])
        raise ValueError(error_msg)

    print(f"✓ Anti-leak verification PASSED: 0 leaks found across {len(examples)} examples.")


def format_example_as_chat_message(
    item: Dict[str, Any],
    schemas: Dict[str, Any],
) -> Dict[str, Any]:
    """Convert an example to OpenAI/HuggingFace chat format with short system prompt and schema in user message."""
    schema_name = item.get("schema_name", "")
    schema = schemas.get(schema_name, {})
    schema_str = schema_to_text(schema)

    user_req = item.get("user_request", "")
    expected_resp = item.get("expected_response", {})

    user_content = f"Database Schema:\n{schema_str}\n\nUser Request:\n{user_req}"
    assistant_content = json.dumps(expected_resp, ensure_ascii=False)

    return {
        "messages": [
            {"role": "system", "content": SHORT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": assistant_content},
        ]
    }


def compute_statistics(
    train_records: List[Dict[str, Any]],
    val_records: List[Dict[str, Any]],
    all_raw_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute token lengths, category distributions, and database type counts."""
    total_records = train_records + val_records

    # Token length metrics
    prompt_tokens = []
    response_tokens = []
    total_tokens = []

    for rec in total_records:
        msgs = rec["messages"]
        sys_toks = estimate_tokens(msgs[0]["content"])
        user_toks = estimate_tokens(msgs[1]["content"])
        asst_toks = estimate_tokens(msgs[2]["content"])

        p_toks = sys_toks + user_toks
        prompt_tokens.append(p_toks)
        response_tokens.append(asst_toks)
        total_tokens.append(p_toks + asst_toks)

    # Categories and DB Types from raw items
    category_counts: Dict[str, int] = {}
    db_type_counts: Dict[str, int] = {}
    phrasing_counts = {"casual": 0, "typo": 0, "short": 0, "long": 0, "standard": 0}

    for item in all_raw_items:
        cat = item.get("category", "unknown")
        db = item.get("db_type", "unknown")
        req = item.get("user_request", "")

        category_counts[cat] = category_counts.get(cat, 0) + 1
        db_type_counts[db] = db_type_counts.get(db, 0) + 1

        # Classify phrasing style
        req_lower = req.lower()
        words_set = set(re.findall(r"\b\w+\b", req_lower))
        typo_words = {"selct", "slect", "selet", "seelct", "were", "wher", "whre", "cretae", "craete", "crate", "tabel", "tbl", "tabl", "delte", "dlete", "delet", "updte", "upadte", "relashonship", "relatnship", "relatonship"}

        if any(w in req_lower for w in ["gimme", "pls", "dump"]):
            phrasing_counts["casual"] += 1
        elif bool(words_set.intersection(typo_words)):
            phrasing_counts["typo"] += 1
        elif len(req.split()) <= 4:
            phrasing_counts["short"] += 1
        elif len(req.split()) >= 15:
            phrasing_counts["long"] += 1
        else:
            phrasing_counts["standard"] += 1

    return {
        "total_examples": len(total_records),
        "train_count": len(train_records),
        "val_count": len(val_records),
        "train_pct": (len(train_records) / len(total_records)) * 100 if total_records else 0,
        "val_pct": (len(val_records) / len(total_records)) * 100 if total_records else 0,
        "avg_prompt_tokens": sum(prompt_tokens) / len(prompt_tokens) if prompt_tokens else 0,
        "max_prompt_tokens": max(prompt_tokens) if prompt_tokens else 0,
        "min_prompt_tokens": min(prompt_tokens) if prompt_tokens else 0,
        "avg_response_tokens": sum(response_tokens) / len(response_tokens) if response_tokens else 0,
        "max_response_tokens": max(response_tokens) if response_tokens else 0,
        "min_response_tokens": min(response_tokens) if response_tokens else 0,
        "avg_total_tokens": sum(total_tokens) / len(total_tokens) if total_tokens else 0,
        "max_total_tokens": max(total_tokens) if total_tokens else 0,
        "min_total_tokens": min(total_tokens) if total_tokens else 0,
        "category_counts": category_counts,
        "db_type_counts": db_type_counts,
        "phrasing_counts": phrasing_counts,
    }


def generate_stats_markdown(stats: Dict[str, Any]) -> str:
    """Format dataset statistics into GitHub-flavored Markdown."""
    md = []
    md.append("# DevDB Fine-Tuning Dataset Statistics")
    md.append("")
    md.append("This document provides detailed metrics, splits, token lengths, and category distributions for the fine-tuning dataset generated for small model distillation across SQLite, PostgreSQL, and MongoDB.")
    md.append("")
    md.append("## 1. Overview & Dataset Splits")
    md.append("")
    md.append("| Metric | Count | Percentage |")
    md.append("| :--- | :--- | :--- |")
    md.append(f"| **Total Validated Examples** | `{stats['total_examples']}` | `100.0%` |")
    md.append(f"| **Training Split (`train.jsonl`)** | `{stats['train_count']}` | `{stats['train_pct']:.1f}%` |")
    md.append(f"| **Validation Split (`val.jsonl`)** | `{stats['val_count']}` | `{stats['val_pct']:.1f}%` |")
    md.append(f"| **Eval Test Set Leak Count** | `0` | `0.0% (Clean)` |")
    md.append("")
    md.append("## 2. Token Length Distribution")
    md.append("")
    md.append("| Component | Average Tokens | Min Tokens | Max Tokens |")
    md.append("| :--- | :--- | :--- | :--- |")
    md.append(f"| **Prompt (System + Schema + Request)** | `{stats['avg_prompt_tokens']:.1f}` | `{stats['min_prompt_tokens']}` | `{stats['max_prompt_tokens']}` |")
    md.append(f"| **Assistant Response (JSON)** | `{stats['avg_response_tokens']:.1f}` | `{stats['min_response_tokens']}` | `{stats['max_response_tokens']}` |")
    md.append(f"| **Total Conversation Sequence** | `{stats['avg_total_tokens']:.1f}` | `{stats['min_total_tokens']}` | `{stats['max_total_tokens']}` |")
    md.append("")
    md.append("## 3. Database Type Distribution")
    md.append("")
    md.append("| Database Engine | Example Count | Percentage | Seed Verification |")
    md.append("| :--- | :--- | :--- | :--- |")
    for db, cnt in sorted(stats["db_type_counts"].items(), key=lambda x: -x[1]):
        pct = (cnt / stats["total_examples"]) * 100
        verify = "SQLite EXPLAIN on Seeded DB" if db == "sqlite" else ("sqlglot Postgres Dialect" if db == "postgres" else "MongoDB Whitelist & Fields")
        md.append(f"| **{db.upper()}** | `{cnt}` | `{pct:.1f}%` | {verify} |")
    md.append("")
    md.append("## 4. Intent & Category Distribution")
    md.append("")
    md.append("| Category / Router Intent | Count | Description |")
    md.append("| :--- | :--- | :--- |")
    cat_desc = {
        "query": "SELECT, filtering, joins, aggregations, GROUP BY, MongoDB find/aggregate",
        "dangerous": "Refusals for DROP DATABASE, mass deletes, stacked queries, truncate",
        "relationship": "Foreign key generation, safe delete simulation, relationship diagram",
        "write_update": "Targeted updates strictly constrained by WHERE clauses",
        "write_insert": "Single and multi-row document/record insertions",
        "write_delete": "Targeted record deletions strictly constrained by WHERE clauses",
        "ambiguous": "Clarifying questions for underspecified requests",
        "export": "CSV and JSON export actions",
        "user_mgmt": "User role creation, permissions, privileges (or unsupported on SQLite)",
        "fake_data": "Synthetic data generation specs with locales and column rules",
        "nonexistent_table": "Did-you-mean suggestions and clarification for missing tables",
    }
    for cat, cnt in sorted(stats["category_counts"].items(), key=lambda x: -x[1]):
        desc = cat_desc.get(cat, "Database operation")
        md.append(f"| **`{cat}`** | `{cnt}` | {desc} |")
    md.append("")
    md.append("## 5. Phrasing Style Distribution")
    md.append("")
    md.append("| Request Style | Count | Example |")
    md.append("| :--- | :--- | :--- |")
    md.append(f"| **Casual** | `{stats['phrasing_counts']['casual']}` | `\"gimme customers where status is active\"` |")
    md.append(f"| **Typo-filled** | `{stats['phrasing_counts']['typo']}` | `\"selct name from customrs wher id=10\"` |")
    md.append(f"| **Short** | `{stats['phrasing_counts']['short']}` | `\"count orders\"`, `\"first 5 users\"` |")
    md.append(f"| **Long / Formal** | `{stats['phrasing_counts']['long']}` | `\"Please generate a SQL query to retrieve all records...\"` |")
    md.append(f"| **Standard** | `{stats['phrasing_counts']['standard']}` | Standard natural language engineering requests |")
    md.append("")
    md.append("## 6. System Prompt & Prompt Format")
    md.append("")
    md.append("Fine-tuning uses a 3-line concise system prompt:")
    md.append("```text")
    md.append(SHORT_SYSTEM_PROMPT)
    md.append("```")
    md.append("The database schema is passed inside the user turn, keeping inference lean and context-efficient.")
    return "\n".join(md) + "\n"


def split_dataset(split_ratio: float = 0.85, seed: int = 42) -> None:
    """Split validated dataset into train.jsonl and val.jsonl after anti-leak checks."""
    if not VALIDATED_DATASET_FILE.exists():
        raise FileNotFoundError(f"Validated dataset not found: {VALIDATED_DATASET_FILE}")

    with open(VALIDATED_DATASET_FILE, "r", encoding="utf-8") as f:
        validated_items = json.load(f)

    # Load eval test set
    if not TEST_SET_FILE.exists():
        raise FileNotFoundError(f"Evaluation test set file not found: {TEST_SET_FILE}")

    with open(TEST_SET_FILE, "r", encoding="utf-8") as f:
        eval_requests = json.load(f)

    # Anti-leak verification
    check_for_eval_leaks(validated_items, eval_requests, similarity_threshold=0.75)

    # Format into chat messages
    schemas = get_all_schemas()
    chat_records = [format_example_as_chat_message(item, schemas) for item in validated_items]

    # Shuffle with fixed seed for reproducibility
    random.seed(seed)
    indices = list(range(len(chat_records)))
    random.shuffle(indices)

    split_idx = int(len(chat_records) * split_ratio)
    train_indices = indices[:split_idx]
    val_indices = indices[split_idx:]

    train_records = [chat_records[i] for i in train_indices]
    val_records = [chat_records[i] for i in val_indices]

    # Write train.jsonl
    with open(TRAIN_FILE, "w", encoding="utf-8") as f:
        for rec in train_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # Write val.jsonl
    with open(VAL_FILE, "w", encoding="utf-8") as f:
        for rec in val_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"✓ Wrote {len(train_records)} examples to {TRAIN_FILE}")
    print(f"✓ Wrote {len(val_records)} examples to {VAL_FILE}")

    # Compute and save statistics
    stats = compute_statistics(train_records, val_records, validated_items)
    stats_md = generate_stats_markdown(stats)

    with open(STATS_FILE, "w", encoding="utf-8") as f:
        f.write(stats_md)

    print(f"✓ Generated comprehensive statistics report saved to {STATS_FILE}")

    # Print summary to console
    print("\n" + "=" * 65)
    print(" DevDB Fine-Tuning Dataset Statistics Summary")
    print("=" * 65)
    print(f"Total Examples:          {stats['total_examples']}")
    print(f"Train Split (85%):       {stats['train_count']}")
    print(f"Val Split (15%):         {stats['val_count']}")
    print(f"Avg Prompt Tokens:       {stats['avg_prompt_tokens']:.1f}")
    print(f"Avg Response Tokens:     {stats['avg_response_tokens']:.1f}")
    print(f"Avg Total Tokens:        {stats['avg_total_tokens']:.1f}")
    print("=" * 65)


if __name__ == "__main__":
    split_dataset()
