# DevDB 🛠️
> **An Intelligent Natural Language & Safety-Guarded Assistant for Development Databases**  
> *100% Local & Offline: Powered by local Ollama (`llama3.2`) or in-memory mock with SQLite & PostgreSQL — Zero cloud API dependencies, Zero token charges!*

---

## 📌 Architecture Overview

DevDB provides an interactive command-line environment for developers to query, inspect, migrate, populate, and manage development databases using plain English while enforcing strict local development guardrails.

```
                     ┌────────────────────────────────────────┐
                     │          Developer Plain English       │
                     └───────────────────┬────────────────────┘
                                         │
                                         ▼
                     ┌────────────────────────────────────────┐
                     │      DevDB Interactive CLI & REPL      │
                     │    (--verbose, history, undo last)     │
                     └───────────────────┬────────────────────┘
                                         │
                 ┌───────────────────────┴───────────────────────┐
                 │                                               │
                 ▼                                               ▼
   ┌───────────────────────────┐                   ┌───────────────────────────┐
   │ Prompt Engine (V1 - V4)   │                   │   Safety Guardrail Engine │
   │ • V1: Baseline            │                   │ • Local-only URLs         │
   │ • V2: Persona + Schema    │                   │ • Block DROP DB/TRUNCATE  │
   │ • V3: Few-Shot + CoT      │                   │ • Require Table for DROP  │
   │ • V4: ReAct Loop (Thought │                   │ • Block Multi-statement   │
   │       -> Action -> Obs)   │                   │ • Referential Cascade     │
   └─────────────┬─────────────┘                   └─────────────┬─────────────┘
                 │                                               │
                 └───────────────────────┬───────────────────────┘
                                         │
                                         ▼
                     ┌────────────────────────────────────────┐
                     │         Execution & Self-Correction    │
                     │  • Instant SELECT query preview & run  │
                     │  • Transactional write confirmation    │
                     │  • ReAct Self-Correction (Max 2 tries) │
                     │  • Fake Data & Dependency Tracker      │
                     └───────────────────┬────────────────────┘
                                         │
                        ┌────────────────┴────────────────┐
                        │                                 │
                        ▼                                 ▼
         ┌─────────────────────────────┐   ┌─────────────────────────────┐
         │       SQLite (dev.db)       │   │  PostgreSQL (Docker Compose)│
         └─────────────────────────────┘   └─────────────────────────────┘
```

---

## 🧠 4 Prompting Techniques Implemented

DevDB demonstrates four distinct prompting engineering techniques that can be evaluated side-by-side using the `--version` flag:

| Technique | File | Description | When to Use |
| :--- | :--- | :--- | :--- |
| **V1: Baseline** | `prompts/v1.txt` | Minimal zero-shot prompt with zero schema context or persona. | Baseline benchmarking. |
| **V2: Persona + Schema** | `prompts/v2.txt` | Injects dialect, complete table/column schema, and Senior DBA persona. | Standard SQL queries where schema fidelity is required. |
| **V3: CoT + Few-Shot** | `prompts/v3.txt` | Chain-of-Thought reasoning object (`tables_involved`, `keys`, `dependency_order`, `risks`) + 8 curated few-shot demonstrations per dialect (`prompts/fewshot/`). | Complex multi-table joins, aggregations, and edge cases. |
| **V4: ReAct Self-Correction** | `prompts/v4.txt` | **Reason + Act feedback loop**: If execution fails with a database error, DevDB sends back the failed SQL, database error message, and schema to the model. Retries at most 2 times, displaying **Thought -> Action -> Observation** steps in terminal panels. | Resilient query execution and automatic schema self-healing. |

---

## 🔄 ReAct Self-Correction Loop (Phase 7)

When an executed query or write fails:
1. **Error Qualification**: DevDB inspects the raw error. Retries occur **only** for:
   - Syntax errors (`near "FORM": syntax error`)
   - Unknown column references (`no such column: user_name` -> auto-corrected to `name`)
   - Unknown table references (`no such table: client` -> auto-corrected to `customers`)
   - Data type mismatches and ambiguous column names
2. **Strict Guardrail**: **Safety blocks, permission denials, and user cancellations are NEVER retried.**
3. **Step-by-Step Visualization**:
   - 🔍 **Observation Panel**: Shows the raw and beginner-friendly error description.
   - 💡 **Thought Panel**: Visualizes the LLM's reflection and root-cause diagnosis.
   - ⚡ **Action Panel**: Displays the corrected SQL with risk evaluation.
   - ✅ **Success / Retry**: Succeeded queries are rendered immediately; writes prompt confirmation.

```text
┌────────────────── Observation: Database Error (Attempt 1) ──────────────────┐
│ Execution Failed on Attempt 1                                               │
│ Failed SQL: SELECT user_name FROM customers;                                │
│ ❌ Unknown Column (Not Found)                                               │
│ The database table does not have a column named user_name.                  │
│ 💡 Check the schema above or run 'schema' to see exact column names.        │
└─────────────────────────────────────────────────────────────────────────────┘
┌───────────────────── DevDB ReAct Step (Attempt 1/2) ────────────────────────┐
│ 🧠 Thought: Column 'user_name' does not exist in 'customers' table.         │
│             Replaced non-existent column 'user_name' with 'name' from schema│
│ ⚡ Action:  QUERY                                                           │
│ Corrected SQL: SELECT name FROM customers;                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 📸 Screenshots & Visual Walkthrough

> *Screenshots captured from live interactive DevDB sessions:*

### 1. Interactive REPL Banner & Schema Inspection
![DevDB Interactive REPL](file:///c:/Users/Admin/Desktop/dev/docs/screenshots/repl_banner.png)
*(Placeholder: Run `python -m devdb.cli` to view the Rich REPL banner and schema view)*

### 2. ReAct Self-Correction Loop in Action
![ReAct Self-Correction Loop](file:///c:/Users/Admin/Desktop/dev/docs/screenshots/react_loop.png)
*(Placeholder: Run `SELECT user_name FROM customers;` to trigger Thought -> Action -> Observation panels)*

### 3. Session History Inspection (`history`)
![Session History Table](file:///c:/Users/Admin/Desktop/dev/docs/screenshots/history_table.png)
*(Placeholder: Type `history` in REPL or run `python -m devdb.cli history` to inspect logs/session.jsonl)*

### 4. Undo Last Write Operation (`undo last`)
![Undo Last Operation](file:///c:/Users/Admin/Desktop/dev/docs/screenshots/undo_last.png)
*(Placeholder: Type `undo` to safely revert fake data, user creations, or single row inserts)*

### 5. ASCII Relationship Map & Rich Tree
![ASCII Relationship Map](file:///c:/Users/Admin/Desktop/dev/docs/screenshots/relationship_map.png)
*(Placeholder: Type `show_relationships --mermaid` to render terminal maps and export relationships.md)*

### 6. Safe PostgreSQL User Management
![PostgreSQL User Management](file:///c:/Users/Admin/Desktop/dev/docs/screenshots/user_management.png)
*(Placeholder: Type `create user report_user read-only orders` to view single-use credentials)*

---

## ⚡ CLI Flags & Commands Reference

### REPL Commands (Inside `devdb>`)
- `connect <url>`: Connect to local SQLite (`sqlite:///dev.db`) or PostgreSQL.
- `schema`: Print formatted Rich schema table with columns, types, PKs, FKs, and indexes.
- `show_relationships [--mermaid]`: Draw ASCII relationship chain, Rich tree, and export Mermaid diagram.
- `validate_relationships`: Referential integrity health check (orphan detection, unindexed FKs).
- `add_relationship <child.fk> <parent.pk>`: Add 1:N, 1:1, or N:M relationship with safe transactional migration.
- `safe_delete_check <table> <id>`: Calculate cascading delete impact before running a `DELETE`.
- `create_user <username> [privileges] [tables]`: Create PostgreSQL user with safe password (printed once).
- `list_users`: List PostgreSQL users and table-level permissions.
- `drop_user <username>`: Drop a PostgreSQL user safely.
- `history [limit]`: View recent interaction history recorded in `logs/session.jsonl`.
- `undo` / `undo last`: Revert the last reversible write operation (fake data batch, user creation, or INSERT).
- `verbose`: Toggle verbose reasoning breakdown on/off.
- `exit` / `quit`: Exit DevDB.

### Terminal CLI Commands
```bash
# Start interactive shell
python -m devdb.cli

# Connect directly to a database with flags
python -m devdb.cli connect sqlite:///dev.db --verbose --version v4

# Inspect schema directly
python -m devdb.cli schema --url sqlite:///dev.db

# View recent session history
python -m devdb.cli history --limit 15

# Undo last operation from command line
python -m devdb.cli undo --url sqlite:///dev.db

# Display relationships and export Mermaid diagram
python -m devdb.cli show-relationships --url sqlite:///dev.db --mermaid

# Validate referential integrity
python -m devdb.cli validate-relationships --url sqlite:///dev.db
```

---

## 🚀 Installation & Setup

### 1. Prerequisites
- Python 3.11+
- [Ollama](https://ollama.com/) (running locally on port 11434 with `llama3.2`) **OR** `LLM_PROVIDER=mock`
- Docker Compose (for PostgreSQL 16 testing)

### 2. Clone and Install Dependencies
```bash
cd dev
pip install -r requirements.txt
```

### 3. Configure Environment (`.env`)
```bash
cp .env.example .env
```
Ensure `.env` contains:
```env
LLM_PROVIDER=ollama
LLM_MODEL=llama3.2
OLLAMA_BASE_URL=http://localhost:11434
PROMPT_VERSION=v4
DATABASE_URL=sqlite:///dev.db
```

### 4. Seed Sample Database
```bash
python tests/seed_sample.py
```
Creates `dev.db` with sample tables: `customers`, `products`, `orders`, and `order_items`.

---

## 🎬 Master End-to-End Demo Script

To execute the complete demonstration workflow validating all 7 phases:
```bash
python demo_phase7_full.py
```

### Demonstration Steps Performed:
1. **Connect**: Connects safely to local database (`sqlite:///dev.db` or PostgreSQL).
2. **Fake Data Generation**: Generates 50 synthetic customers with Indian names (`locale: en_IN`).
3. **Link Tables**: Adds foreign key relationship `orders.customer_id -> customers.id`.
4. **Show Relationships**: Displays ASCII chain map, Rich hierarchy tree, and exports `relationships.md`.
5. **Validate Relationships**: Detects orphan rows, unindexed FKs, and referential health.
6. **Create User (PostgreSQL)**: Creates read-only user `report_user` with single-use credentials; politely refused on SQLite.
7. **Destructive Request Refused**: Rejects `DROP DATABASE devdb` through strict safety engine.
8. **ReAct Self-Correction**: Executes `SELECT user_name FROM customers;` -> catches `unknown_column` -> triggers ReAct loop -> self-corrects to `SELECT name FROM customers;` -> displays results.
9. **Undo Last Write**: Invokes `undo last` to cleanly revert the synthetic fake data batch.

---

## 🧪 Running Automated Tests

Run the full automated test suite:
```bash
python -m pytest tests/ -v
```
**Results: 81 passed tests** covering:
- SQL safety guardrails & injection defense (18 tests)
- Synthetic fake data generation & dependency topological sort (10 tests)
- Relationship management, ASCII maps, and Mermaid export (10 tests)
- PostgreSQL user management, quoting, and privilege tests (13 tests)
- ReAct self-correction, beginner-friendly errors, history, and undo (8 tests)
- Schema reflection and MongoDB compatibility (22 tests)

---

## 📊 Phase 8: Running the Evaluation Harness (`eval/`)

DevDB includes a formal evaluation benchmark harness comparing prompt engineering versions (V1 Naive, V2 Schema-Grounded, V3 Chain-of-Thought) across 40 test requests:

### 1. Run Smoke Test (6 diverse requests)
```bash
python eval/run_eval.py --smoke
```

### 2. Run Full Benchmark (40 requests)
```bash
python eval/run_eval.py --versions v1 v2 v3
```
*Outputs raw JSON evaluations to `eval/results/raw_v1.json`, `raw_v2.json`, and `raw_v3.json`.*

### 3. Generate Comparative Reports & Charts
```bash
python eval/report_tables.py
```
*Produces:*
- `eval/results/comparison.csv`: Machine-readable metrics and per-category breakdown
- `eval/results/comparison.md`: Markdown comparison table
- `eval/results/comparison.png`: High-resolution matplotlib comparison bar chart

### 4. Run Demographic Representation & Bias Audit
```bash
python eval/bias_test.py
```
*Generates fake customers across default vs explicit Indian locale conditions, measuring demographic representation saved to `eval/results/bias_test_results.json`.*

---

## 🍃 Phase 10: MongoDB Support (No SQL, Structured JSON Operations)

MongoDB is a document-oriented database with no SQL, no tables, and no database-enforced foreign keys. DevDB handles MongoDB through a structured, safe architecture:

### 1. The Core Design Rule: Structured JSON Operations vs. Raw Strings
- The LLM **never** emits raw JavaScript strings or executable code (zero `eval()`, zero `exec()`).
- The LLM returns a **declarative structured JSON operation** (`find`, `count`, `aggregate`, `update`, `delete`, `create_index`, `insert_fake`, `create_user`, `add_reference`).
- DevDB converts this JSON into safe, parameterized `pymongo` API calls.
- DevDB displays syntax-highlighted `mongosh` syntax previews for developer learning.

### 2. Native Local Connection & Safety Guardrails
- Permitted URLs: `mongodb://localhost:27017/devdb` or `mongodb://127.0.0.1:27017/devdb`.
- **Strictly blocked**: All remote MongoDB hosts, cloud clusters, and `mongodb+srv://` URIs.
- **Forbidden operators**: Raw JavaScript injection (`$where`, `$function`), unconstrained `deleteMany({})`, and `dropDatabase`.

### 3. Dynamic Document Schema Inference
- Samples documents to infer data types, presence percentages, embedded arrays (`list[dict]`), and probable cross-collection references (`orders.customer_id -> customers._id`).

### 4. Referential Integrity & Orphan Detection
- Validates references between collections without database-level foreign key constraints.
- Detects orphaned documents (child documents pointing to non-existent parent `_id`s).
- Flags missing secondary indexes on foreign reference fields to maintain query performance.

### 5. Running the MongoDB Demo
```bash
python demo_mongo.py
```

---

---

## 🌐 Phase 11: Unified Multi-Database CLI (SQLite, PostgreSQL, MongoDB, MongoDB Atlas)

DevDB Assistant acts as a single intelligent CLI managing **SQLite**, **PostgreSQL**, **local MongoDB** (Compass), and **MongoDB Atlas** entirely through natural English, guarded by multi-tier safety controls.

```
devdb[atlas_dev|READ-ONLY]> count documents in customers
devdb[atlas_dev|READ-ONLY]> switch to read-write mode
devdb[atlas_dev|READ-WRITE]> add a customer named Ravi, Chennai
```

### 1. Connecting to MongoDB Atlas (Safely)

Atlas is a remote cloud database. DevDB enforces a strict safety sandbox to ensure LLMs cannot harm production data:

1. **Create an M0 Cluster**: Create a free-tier M0 sandbox cluster in MongoDB Atlas (e.g. `cluster0.abcde.mongodb.net/devdb`).
2. **Network Access**: Add your current IP address (or development subnet) in MongoDB Atlas under **Security -> Network Access**.
3. **Database Access**: Create a dedicated development database user (e.g., `dev_user`).
4. **Environment Password**: Place the password in `.env` or your environment variables (`ATLAS_PASSWORD=...`), or enter it securely via DevDB's masked password prompt. Never commit passwords to disk or git!
5. **Connect & Save Profile**:
   ```bash
   devdb> connect mongodb+srv://dev_user@cluster0.abcde.mongodb.net/devdb
   devdb> save this connection as atlas_dev sandbox
   ```
6. **Atlas Guardrails**:
   - **Environment Label**: Allowed **ONLY** if the profile is explicitly marked `environment = dev` or `sandbox`.
   - **Production Name Refusal**: Atlas connections are **strictly refused** if the cluster or database name contains `prod`, `production`, or `live`.
   - **Typed Confirmation on First Connect**: Requires typing the exact cluster or database name on the first connect.
   - **READ-ONLY Default**: Atlas always opens in `READ-ONLY` mode.
   - **Typed 'yes' Write Banner**: Any write operation on Atlas presents a prominent banner:
     `REMOTE DATABASE: cluster0.abcde.mongodb.net` and requires explicitly typing `yes` (not just `y`).

---

### 2. Using MongoDB Compass Alongside the CLI

MongoDB Compass is a GUI management client for MongoDB:
1. Ensure your local MongoDB is running (`net start MongoDB` or `docker compose up -d`).
2. Open **MongoDB Compass** and connect to `mongodb://localhost:27017` (or `mongodb://dev:dev@localhost:27017`).
3. Connect DevDB Assistant to `mongodb://localhost:27017/devdb`.
4. Run English commands in DevDB:
   - *"add 20 fake customers"*
   - *"create index on email in customers"*
   - *"validate relationships between orders and customers"*
5. Click **Refresh** in MongoDB Compass to verify documents, schema tabs, and newly indexed keys live!

---

### 3. Capability Matrix per Database

Ask DevDB: `"what can you do on this database?"` to inspect supported capabilities:

| Feature | SQLite | PostgreSQL | Local MongoDB | MongoDB Atlas |
| :--- | :---: | :---: | :---: | :---: |
| **Explore** (list, describe, count, sample, stats) | ✅ | ✅ | ✅ | ✅ |
| **Query** (filter, sort, join, aggregate) | ✅ | ✅ | ✅ | ✅ |
| **Insert / Update / Delete** | ✅ | ✅ | ✅ | ✅ (Needs typed 'yes') |
| **Fake Data Generation** | ✅ | ✅ | ✅ | ✅ |
| **Schema & Indexes** | ✅ | ✅ | ✅ | ✅ (ADMIN mode) |
| **Relationships & Orphan Validation** | ✅ | ✅ | ✅ | ✅ |
| **Data I/O** (CSV/JSON export & import preview) | ✅ | ✅ | ✅ | ✅ |
| **Cross-Database Copy** (500-batch with progress) | ✅ | ✅ | ✅ | ✅ |
| **User Management** | ❌ (File-based) | ✅ (Roles/Grants) | ✅ (`createUser`) | ⚠️ (Atlas Admin API / Dashboard) |
| **Reversible Undo / History** | ✅ | ✅ | ✅ | ✅ |

---

## 🛡️ Safety & Guardrail Design Note

DevDB is engineered from the ground up to prevent data loss in developer environments:
1. **Never Against Production**: Connection URIs are verified by `is_dev_connection()` and `validate_remote_atlas_connection()`. Connections to production hosts or URLs containing `prod`/`live` are refused immediately.
2. **Deterministic Interception**: Catastrophic commands (`DROP DATABASE`, `TRUNCATE`, unqualified `DELETE`/`UPDATE`, `$where`, `$function`) are blocked before execution.
3. **Session Modes**:
   - `READ-ONLY`: Hard blocks all write, update, delete, DDL, and insert operations at the code level.
   - `READ-WRITE`: Allows queries and safe filtered data mutations with interactive previews.
   - `ADMIN`: Required for user management, DDL schema alterations, and index drops.
4. **Password-less Storage**: Passwords are never written to `~/.devdb/profiles.json`. Passwords resolve from `.env`, keyring, or secure masked prompt.
5. **100% Local Processing**: All natural language intent classification and query synthesis are processed locally via Ollama (`llama3.2`) or offline fast-path routers.
