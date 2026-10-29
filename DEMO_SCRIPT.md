# DevDB 5-Minute Live Demonstration Script

This script provides an exact step-by-step transcript for a 5-minute technical walkthrough of **DevDB**. It showcases natural language database administration, schema-grounded prompting, deterministic safety guardrails, PostgreSQL user management, relationship migrations, and the ReAct self-correction auto-healing loop.

---

## Prerequisites & Environment Check

Before the live presentation, verify that the environment is initialized:
```powershell
# 1. Activate virtual environment
.\.venv\Scripts\Activate.ps1

# 2. Verify local Ollama is running
curl.exe -s http://localhost:11434/api/tags

# 3. Clean local dev SQLite database
Remove-Item -Force dev.db -ErrorAction SilentlyContinue
```

---

## 5-Minute Demonstration Timeline

```mermaid
timeline
    title 5-Minute Live Demo Flow
    0:00 - 0:45 : Connect & Schema Grounding : SQLite & PostgreSQL
    0:45 - 1:45 : Synthetic Fake Data : Indian Locale & Topology
    1:45 - 2:45 : Relationship Management : SQLite Migration & Mermaid ER
    2:45 - 3:30 : PostgreSQL User Management : Safe Identifier Quoting & Guardrails
    3:30 - 4:15 : Deterministic Safety Block : DROP DATABASE Intercepted
    4:15 - 5:00 : ReAct Self-Correction & Undo : Auto-Healing & Session Rollback
```

---

## Step-by-Step Terminal Execution

### Step 1: Launch Interactive Session & Connect (0:00 - 0:45)
**Command**:
```bash
python -m devdb.cli
```
**In DevDB Prompt**:
```text
devdb> connect dev.db
```
**Expected Output**:
```text
╭─────────────────────────────────────────────────────────────────────────────╮
│ Connected to sqlite:///dev.db                                               │
│ Active Dialect: sqlite | Dev Safety Mode: ACTIVE (Writes & DDL permitted)   │
╰─────────────────────────────────────────────────────────────────────────────╯
Schema loaded: 4 tables (customers, products, orders, order_items)
```
**Speaker Script**:
> *"Welcome! DevDB is an AI database administrator built specifically for developer environments. Notice that as soon as we connect, DevDB inspects the live schema and confirms safety boundaries. All operations run through local Ollama—zero cloud API keys required."*

---

### Step 2: Generate Synthetic Fake Data with Locale (0:45 - 1:45)
**In DevDB Prompt**:
```text
devdb> add 50 fake Indian customers
```
**Expected Output**:
```text
╭─ Fake Data Generation Preview ───────────────────────────────────────────────╮
│ Target Table: customers                                                      │
│ Row Count: 50 | Locale: en_IN (Subcontinental)                               │
│ Primary Key: id (AUTOINCREMENT - preserved)                                  │
╰──────────────────────────────────────────────────────────────────────────────╯
Sample Rows:
1. Aarav Sharma (aarav.sharma@example.com)
2. Neha Patel (neha.patel@example.com)
3. Rohan Gupta (rohan.gupta@example.com)

Execute insertion inside transaction? [y/N]: y
✓ Successfully inserted 50 rows into 'customers'. Session tracked for rollback.
```
**Speaker Script**:
> *"Notice two things here: first, the LLM inferred the `en_IN` locale, generating authentic subcontinental names rather than generic defaults. Second, DevDB produced a declarative JSON spec rather than raw rows, validated column constraints, previewed the data, and recorded primary keys into our session tracker."*

---

### Step 3: Link Tables & Visualize Relationships (1:45 - 2:45)
**In DevDB Prompt**:
```text
devdb> link orders to customers using customer_id
```
**Expected Output**:
```text
╭─ Relationship Migration Preview (SQLite) ───────────────────────────────────╮
│ Dialect Notice: SQLite does not support ALTER TABLE ADD FOREIGN KEY.        │
│ DevDB will execute a safe 7-step transactional table rebuild.               │
╰──────────────────────────────────────────────────────────────────────────────╯
PRAGMA foreign_keys = OFF;
BEGIN TRANSACTION;
CREATE TABLE orders_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    ...
);
INSERT INTO orders_new SELECT * FROM orders;
DROP TABLE orders;
ALTER TABLE orders_new RENAME TO orders;
CREATE INDEX ix_orders_customer_id ON orders(customer_id);
COMMIT;
PRAGMA foreign_keys = ON;

Execute migration? [y/N]: y
✓ Relationship established: orders.customer_id ──> customers.id
```
**Next Command**:
```text
devdb> show relationships --mermaid
```
**Expected Output**:
```text
ASCII Relationship Chain:
customers (1) ──< orders (N) ──< order_items (N) >── (1) products

✓ Mermaid ER diagram exported to relationships.md
```
**Speaker Script**:
> *"In SQLite, adding a foreign key is notoriously tricky because `ALTER TABLE ADD FOREIGN KEY` is unsupported. DevDB detects the SQLite dialect and safely constructs the 7-step table-recreation migration inside an atomic transaction. With `--mermaid`, it automatically updates documentation."*

---

### Step 4: PostgreSQL User Management & Guardrails (2:45 - 3:30)
**In DevDB Prompt**:
```text
devdb> connect postgresql://dev:dev@localhost:5432/devdb
devdb> create a read-only user analyst_demo for table orders
```
**Expected Output**:
```text
╭─ Generated Credentials ─────────────────────────────────────────────────────╮
│ Role: analyst_demo                                                          │
│ Generated Password: [bold green]9k#mP$7vXq2L!wZb[/bold green] (Printed ONCE)│
╰─────────────────────────────────────────────────────────────────────────────╯
SQL Preview:
CREATE ROLE "analyst_demo" WITH LOGIN PASSWORD '********';
GRANT SELECT ON "orders" TO "analyst_demo";

Confirm user creation? [y/N]: y
✓ Role 'analyst_demo' created with restricted SELECT privileges.
```
**Speaker Script**:
> *"For PostgreSQL, DevDB handles role and privilege management. The LLM only returns structured intent; Python constructs the SQL with safe double-quoted identifiers. Notice that the strong password was generated by the `secrets` module, displayed once, and never saved in logs."*

---

### Step 5: Deterministic Safety Guardrail Refusal (3:30 - 4:15)
**In DevDB Prompt**:
```text
devdb> DROP DATABASE devdb;
```
**Expected Output**:
```text
╭─ SAFETY VIOLATION BLOCKED ──────────────────────────────────────────────────╮
│ Attempted SQL: DROP DATABASE devdb;                                         │
│ Result: Allowed=False | Risk=CRITICAL                                       │
│ Reason: DROP DATABASE is strictly forbidden on development connections.     │
╰─────────────────────────────────────────────────────────────────────────────╯
✗ Query blocked by DevDB safety engine prior to execution.
```
**Speaker Script**:
> *"Safety in DevDB is deterministic, not prompt-dependent. Even if an LLM is jailbroken or confused, our AST and regex safety filter blocks catastrophic operations like DROP DATABASE, TRUNCATE, and unqualified DELETEs before they ever touch the connection."*

---

### Step 6: ReAct Self-Correction Loop & Undo (4:15 - 5:00)
**In DevDB Prompt**:
```text
devdb> SELECT user_name, email FROM customers LIMIT 5;
```
**Expected Output**:
```text
(sqlite3.OperationalError) no such column: user_name

╭─ ReAct Self-Correction Cycle (Attempt 1/2) ──────────────────────────────────╮
│ Thought: Column 'user_name' does not exist in 'customers'. Cross-referencing │
│          active schema confirms column is 'name'. Correcting query.          │
│ Action:  SELECT name, email FROM customers LIMIT 5;                         │
│ Observation: Query succeeded returning 5 rows.                               │
╰─────────────────────────────────────────────────────────────────────────────╯
┌────┬────────────────┬──────────────────────────┐
│ id │ name           │ email                    │
├────┼────────────────┼──────────────────────────┤
│ 1  │ Aarav Sharma   │ aarav.sharma@example.com │
│ 2  │ Neha Patel     │ neha.patel@example.com   │
...
```
**Next Command**:
```text
devdb> undo last
```
**Expected Output**:
```text
╭─ Undo Operation ─────────────────────────────────────────────────────────────╮
│ Reverting fake data generation from session tracker:                         │
│ Deleted 50 rows from 'customers'.                                            │
╰─────────────────────────────────────────────────────────────────────────────╯
✓ Database restored to previous state.
```
**Speaker Script**:
> *"When a query encounters a schema error—like `user_name` instead of `name`—DevDB initiates a ReAct feedback loop: Thought -> Action -> Observation. It self-diagnosed the error, inspected the schema, healed the query, and executed it seamlessly. Finally, `undo last` reverses our fake data insertion."*

---

## Live Demo Fallback Plan (If Local LLM is Slow or Unavailable)

If Ollama experiences CPU memory latency during a presentation:
1. **Automated Master Script Fallback**:
   Run the pre-tested end-to-end master demonstration script:
   ```powershell
   python demo_phase7_full.py
   ```
   *Executes all 9 phases sequentially with instant rich outputs and pre-validated mock fixtures.*
2. **Mock Mode Switch**:
   Set `LLM_PROVIDER=mock` in `.env` or run:
   ```powershell
   $env:LLM_PROVIDER="mock"; python -m devdb.cli
   ```
   *Allows instantaneous response times with zero latency.*
