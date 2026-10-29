"""Tests for DevDB Fine-Tuning Dataset Generator, Validator, and Splitter (Phase 12B)."""

import json
import pytest
from pathlib import Path

from finetune.schemas.definitions import get_all_schemas, SQL_SCHEMAS, MONGO_SCHEMAS
from finetune.dataset.validate import DatasetValidator, normalize_text
from finetune.dataset.split import (
    check_for_eval_leaks,
    estimate_tokens,
    SHORT_SYSTEM_PROMPT,
    TRAIN_FILE,
    VAL_FILE,
    STATS_FILE,
    VALIDATED_DATASET_FILE,
)

BASE_DIR = Path(__file__).resolve().parent.parent
DEFINITIONS_DIR = BASE_DIR / "finetune" / "schemas" / "definitions"
DBS_DIR = BASE_DIR / "finetune" / "schemas" / "dbs"


def test_schema_definitions_count():
    """Verify all 20 schemas are defined (14 SQL, 6 MongoDB)."""
    schemas = get_all_schemas()
    assert len(schemas) == 20
    assert len(SQL_SCHEMAS) == 14
    assert len(MONGO_SCHEMAS) == 6

    # Verify JSON files in definitions directory
    assert DEFINITIONS_DIR.exists()
    json_files = list(DEFINITIONS_DIR.glob("*.json"))
    assert len(json_files) == 20


def test_seeded_sqlite_databases():
    """Verify all 14 SQL schemas have seeded SQLite databases with tables and rows."""
    import sqlite3

    assert DBS_DIR.exists()
    db_files = list(DBS_DIR.glob("*.db"))
    assert len(db_files) == 14

    for db_file in db_files:
        conn = sqlite3.connect(str(db_file))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        tables = cur.fetchall()
        assert len(tables) > 0, f"No tables found in {db_file.name}"

        # Verify rows exist in at least one table
        t_name = tables[0][0]
        cur.execute(f"SELECT COUNT(*) FROM {t_name};")
        count = cur.fetchone()[0]
        assert count > 0, f"Database {db_file.name} table {t_name} is empty"
        conn.close()


def test_validator_safety_rules():
    """Verify validator flags stacked statements and unbounded writes."""
    validator = DatasetValidator()
    try:
        # Stacked query test
        bad_stacked = {
            "schema_name": "school_sql",
            "db_type": "sqlite",
            "user_request": "Show students",
            "expected_response": {
                "action": "query",
                "sql": "SELECT 1; DROP TABLE students;",
            }
        }
        safe, msg = validator.validate_safety_rules(bad_stacked)
        assert not safe
        assert "Stacked SQL" in msg

        # Unbounded delete test
        bad_delete = {
            "schema_name": "school_sql",
            "db_type": "sqlite",
            "user_request": "Delete students",
            "expected_response": {
                "action": "write",
                "sql": "DELETE FROM students;",
            }
        }
        safe, msg = validator.validate_safety_rules(bad_delete)
        assert not safe
        assert "WHERE clause" in msg

        # Unbounded update test
        bad_update = {
            "schema_name": "school_sql",
            "db_type": "sqlite",
            "user_request": "Update students",
            "expected_response": {
                "action": "write",
                "sql": "UPDATE students SET full_name='test';",
            }
        }
        safe, msg = validator.validate_safety_rules(bad_update)
        assert not safe
        assert "WHERE clause" in msg

        # DROP DATABASE without refusal
        bad_drop = {
            "schema_name": "school_sql",
            "db_type": "sqlite",
            "user_request": "DROP DATABASE devdb",
            "expected_response": {
                "action": "write",
                "sql": "DROP DATABASE devdb;",
            }
        }
        safe, msg = validator.validate_safety_rules(bad_drop)
        assert not safe
        assert "DROP DATABASE" in msg

        # Safe refusal is allowed
        good_refusal = {
            "schema_name": "school_sql",
            "db_type": "sqlite",
            "user_request": "DROP DATABASE devdb",
            "expected_response": {
                "action": "refusal",
                "sql": "",
                "explanation": "Refused destructive action."
            }
        }
        safe, msg = validator.validate_safety_rules(good_refusal)
        assert safe
    finally:
        validator.close()


def test_validator_sql_parsing():
    """Verify SQL syntax checks via SQLite EXPLAIN and PostgreSQL sqlglot."""
    validator = DatasetValidator()
    try:
        # SQLite valid explain
        valid, msg = validator.validate_sql("school_sql", "sqlite", "SELECT * FROM students LIMIT 10;")
        assert valid

        # SQLite invalid syntax
        invalid, msg = validator.validate_sql("school_sql", "sqlite", "SELECT FROM WHERE students;;;")
        assert not invalid

        # Postgres valid sqlglot
        valid, msg = validator.validate_sql("banking_sql", "postgres", "SELECT * FROM accounts WHERE balance > 1000;")
        assert valid

        # Postgres invalid sqlglot
        invalid, msg = validator.validate_sql("banking_sql", "postgres", "SELCT * FRM accounts WHERE balance >> 1000;")
        assert not invalid
    finally:
        validator.close()


def test_validator_mongo_whitelist():
    """Verify Mongo operations are checked against whitelist and required fields."""
    validator = DatasetValidator()
    try:
        # Whitelisted find
        valid, _ = validator.validate_mongo("social_network_mongo", {
            "action": "query",
            "operation": "find",
            "collection": "profiles",
            "query": {}
        })
        assert valid

        # Invalid operation
        invalid, msg = validator.validate_mongo("social_network_mongo", {
            "action": "query",
            "operation": "unsupported_shell_command",
            "collection": "profiles"
        })
        assert not invalid
        assert "whitelist" in msg

        # Missing required query for find
        missing, msg = validator.validate_mongo("social_network_mongo", {
            "action": "query",
            "operation": "find",
            "collection": "profiles"
        })
        assert not missing
        assert "requires 'collection' and 'query'" in msg
    finally:
        validator.close()


def test_anti_leak_fails_loudly_on_synthetic_leak():
    """Verify anti-leak checker fails loudly when an evaluation request is leaked."""
    synthetic_eval = [
        {"id": "TEST_01", "request": "Add 25 fake customers to the database"}
    ]
    leaked_candidates = [
        {"user_request": "add 25 fake customers to the database", "expected_response": {}}
    ]

    with pytest.raises(ValueError, match="CRITICAL: Found 1 test set leaks in dataset!"):
        check_for_eval_leaks(leaked_candidates, synthetic_eval, similarity_threshold=0.75)


def test_validated_dataset_count_and_ranges():
    """Verify validated dataset has between 800 and 1,200 valid examples."""
    assert VALIDATED_DATASET_FILE.exists()
    with open(VALIDATED_DATASET_FILE, "r", encoding="utf-8") as f:
        items = json.load(f)

    assert 800 <= len(items) <= 1200, f"Expected 800-1200 items, got {len(items)}"


def test_train_and_val_jsonl_splits_and_format():
    """Verify train.jsonl (85%) and val.jsonl (15%) match expected schema and 3-line system prompt."""
    assert TRAIN_FILE.exists()
    assert VAL_FILE.exists()

    train_lines = [line.strip() for line in open(TRAIN_FILE, encoding="utf-8") if line.strip()]
    val_lines = [line.strip() for line in open(VAL_FILE, encoding="utf-8") if line.strip()]

    total = len(train_lines) + len(val_lines)
    assert 800 <= total <= 1200

    train_ratio = len(train_lines) / total
    assert 0.83 <= train_ratio <= 0.87, f"Train split ratio {train_ratio:.2f} not close to 0.85"

    # Verify first 10 records in train and val
    for line in train_lines[:10] + val_lines[:10]:
        data = json.loads(line)
        assert "messages" in data
        msgs = data["messages"]
        assert len(msgs) == 3

        sys_msg, user_msg, asst_msg = msgs[0], msgs[1], msgs[2]
        assert sys_msg["role"] == "system"
        assert sys_msg["content"] == SHORT_SYSTEM_PROMPT
        assert len(sys_msg["content"].splitlines()) == 3

        assert user_msg["role"] == "user"
        assert "Database Schema:" in user_msg["content"]
        assert "User Request:" in user_msg["content"]

        assert asst_msg["role"] == "assistant"
        asst_json = json.loads(asst_msg["content"])
        assert "action" in asst_json
        assert "thought" in asst_json
        assert "explanation" in asst_json


def test_stats_markdown_content():
    """Verify finetune/dataset/STATS.md exists and contains required breakdown tables."""
    assert STATS_FILE.exists()
    content = STATS_FILE.read_text(encoding="utf-8")

    assert "# DevDB Fine-Tuning Dataset Statistics" in content
    assert "Overview & Dataset Splits" in content
    assert "Token Length Distribution" in content
    assert "Database Type Distribution" in content
    assert "Intent & Category Distribution" in content
    assert "Phrasing Style Distribution" in content
    assert "Eval Test Set Leak Count" in content
    assert "0.0% (Clean)" in content


def test_train_colab_notebook():
    """Verify finetune/train_colab.ipynb exists, is valid JSON, and has all 10 required sections."""
    nb_path = BASE_DIR / "finetune" / "train_colab.ipynb"
    assert nb_path.exists()

    with open(nb_path, "r", encoding="utf-8") as f:
        nb = json.load(f)

    assert nb.get("nbformat") == 4
    cells = nb.get("cells", [])
    assert len(cells) >= 20  # title + 10 markdown + 10 code cells = 21 cells

    # Collect all code sources
    code_sources = ["".join(c.get("source", [])) for c in cells if c.get("cell_type") == "code"]
    md_sources = ["".join(c.get("source", [])) for c in cells if c.get("cell_type") == "markdown"]

    full_code = "\n".join(code_sources)
    full_md = "\n".join(md_sources)

    # 1. Install unsloth, trl, datasets
    assert "pip install" in full_code
    assert "unsloth" in full_code
    assert "trl" in full_code

    # 2. Upload train.jsonl and val.jsonl
    assert "files.upload" in full_code
    assert "train.jsonl" in full_code
    assert "val.jsonl" in full_code

    # 3. Load model in 4-bit, max_seq_length 2048, comment about 1B fallback
    assert "unsloth/Llama-3.2-3B-Instruct" in full_code
    assert "load_in_4bit = True" in full_code
    assert "max_seq_length = 2048" in full_code
    assert "unsloth/Llama-3.2-1B-Instruct" in full_code

    # 4. Chat template llama-3.1
    assert "chat_template = \"llama-3.1\"" in full_code
    assert "formatting_prompts_func" in full_code

    # 5. LoRA: r=16, alpha=16, dropout 0, linear layers, use_gradient_checkpointing unsloth
    assert "r = 16" in full_code
    assert "lora_alpha = 16" in full_code
    assert "lora_dropout = 0" in full_code
    assert "q_proj" in full_code
    assert "gate_proj" in full_code
    assert "use_gradient_checkpointing = \"unsloth\"" in full_code

    # 6. SFTTrainer: 2 epochs, batch 2, grad accum 4, lr 2e-4, eval every 50 steps, train_on_responses_only
    assert "num_train_epochs = 2" in full_code
    assert "per_device_train_batch_size = 2" in full_code
    assert "gradient_accumulation_steps = 4" in full_code
    assert "learning_rate = 2e-4" in full_code
    assert "eval_steps = 50" in full_code
    assert "train_on_responses_only" in full_code

    # 7. Plot loss and save loss_plot.png
    assert "loss_plot.png" in full_code
    assert "eval_loss" in full_code

    # 8. Test 5 validation prompts
    assert "test_cases" in full_code
    assert "FastLanguageModel.for_inference" in full_code

    # 9. Export LoRA & GGUF
    assert "model.save_pretrained" in full_code
    assert "model.save_pretrained_gguf" in full_code
    assert "q4_k_m" in full_code

    # 10. Troubleshooting (OOM, disconnect, overfitting)
    assert "Out of Memory" in full_md or "OOM" in full_md
    assert "Disconnect" in full_md
    assert "Overfitting" in full_md


def test_finetune_guide_markdown():
    """Verify finetune/FINETUNE_GUIDE.md exists and contains complete walkthrough."""
    guide_path = BASE_DIR / "finetune" / "FINETUNE_GUIDE.md"
    assert guide_path.exists()
    content = guide_path.read_text(encoding="utf-8")

    assert "Google Colab Fine-Tuning Guide" in content
    assert "T4 GPU" in content
    assert "20 to 45 minutes" in content
    assert "unsloth/Llama-3.2-3B-Instruct" in content
    assert "Modelfile" in content
    assert "ollama create devdb-llama -f Modelfile" in content
    assert "LLM_MODEL=devdb-llama" in content
