# DevDB Fine-Tuning Dataset Statistics

This document provides detailed metrics, splits, token lengths, and category distributions for the fine-tuning dataset generated for small model distillation across SQLite, PostgreSQL, and MongoDB.

## 1. Overview & Dataset Splits

| Metric | Count | Percentage |
| :--- | :--- | :--- |
| **Total Validated Examples** | `1051` | `100.0%` |z
| **Training Split (`train.jsonl`)** | `893` | `85.0%` |
| **Validation Split (`val.jsonl`)** | `158` | `15.0%` |
| **Eval Test Set Leak Count** | `0` | `0.0% (Clean)` |

## 2. Token Length Distribution

| Component | Average Tokens | Min Tokens | Max Tokens |
| :--- | :--- | :--- | :--- |
| **Prompt (System + Schema + Request)** | `267.4` | `200` | `426` |
| **Assistant Response (JSON)** | `87.9` | `67` | `151` |
| **Total Conversation Sequence** | `355.2` | `275` | `541` |

## 3. Database Type Distribution

| Database Engine | Example Count | Percentage | Seed Verification |
| :--- | :--- | :--- | :--- |
| **SQLITE** | `470` | `44.7%` | SQLite EXPLAIN on Seeded DB |
| **POSTGRES** | `353` | `33.6%` | sqlglot Postgres Dialect |
| **MONGODB** | `228` | `21.7%` | MongoDB Whitelist & Fields |

## 4. Intent & Category Distribution

| Category / Router Intent | Count | Description |
| :--- | :--- | :--- |
| **`query`** | `386` | SELECT, filtering, joins, aggregations, GROUP BY, MongoDB find/aggregate |
| **`dangerous`** | `93` | Refusals for DROP DATABASE, mass deletes, stacked queries, truncate |
| **`relationship`** | `88` | Foreign key generation, safe delete simulation, relationship diagram |
| **`write_insert`** | `83` | Single and multi-row document/record insertions |
| **`write_update`** | `83` | Targeted updates strictly constrained by WHERE clauses |
| **`write_delete`** | `78` | Targeted record deletions strictly constrained by WHERE clauses |
| **`export`** | `60` | CSV and JSON export actions |
| **`ambiguous`** | `60` | Clarifying questions for underspecified requests |
| **`user_mgmt`** | `46` | User role creation, permissions, privileges (or unsupported on SQLite) |
| **`fake_data`** | `40` | Synthetic data generation specs with locales and column rules |
| **`nonexistent_table`** | `34` | Did-you-mean suggestions and clarification for missing tables |

## 5. Phrasing Style Distribution

| Request Style | Count | Example |
| :--- | :--- | :--- |
| **Casual** | `74` | `"gimme customers where status is active"` |
| **Typo-filled** | `54` | `"selct name from customrs wher id=10"` |
| **Short** | `158` | `"count orders"`, `"first 5 users"` |
| **Long / Formal** | `20` | `"Please generate a SQL query to retrieve all records..."` |
| **Standard** | `745` | Standard natural language engineering requests |

## 6. System Prompt & Prompt Format

Fine-tuning uses a 3-line concise system prompt:
```text
You are DevDB Assistant, an expert AI database engineer.
Convert user database requests into structured JSON actions matching DevDB specifications (query, write, relationship, fake_data, create_user, export, clarify, refusal).
Respond with valid JSON only, without markdown code fences or surrounding text.
```
The database schema is passed inside the user turn, keeping inference lean and context-efficient.
