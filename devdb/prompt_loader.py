"""Prompt loader module for DevDB prompt library (V1, V2, V3)."""

import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

from devdb.schema import schema_to_text


def _find_prompts_dir() -> Path:
    """Locate the prompts directory relative to this file or current working directory."""
    candidates = [
        Path("prompts"),
        Path(__file__).resolve().parent.parent / "prompts",
        Path(__file__).resolve().parent / "prompts",
    ]
    for p in candidates:
        if p.exists() and p.is_dir():
            return p
    raise FileNotFoundError("Could not locate 'prompts' directory.")


def _format_fewshot_examples(db_type: str, prompts_dir: Path) -> str:
    """Load and format few-shot example pairs for the given database dialect."""
    dialect = db_type.lower()
    if dialect in ("postgresql", "postgres", "psql"):
        filename = "postgres.json"
    elif dialect in ("mongodb", "mongo"):
        filename = "mongo.json"
    else:
        filename = "sqlite.json"

    fewshot_file = prompts_dir / "fewshot" / filename
    if not fewshot_file.exists():
        # Fallback to sqlite.json
        fewshot_file = prompts_dir / "fewshot" / "sqlite.json"

    if not fewshot_file.exists():
        return "(No few-shot examples available for this dialect.)"

    with open(fewshot_file, "r", encoding="utf-8") as f:
        examples = json.load(f)

    formatted = []
    for i, ex in enumerate(examples, start=1):
        req = ex.get("request", "")
        resp = json.dumps(ex.get("response", {}), indent=2)
        formatted.append(f"Example {i}:\nUser: {req}\nResponse:\n{resp}")

    return "\n\n".join(formatted)


def load_prompt(
    version: str,
    db_type: str = "sqlite",
    schema: Optional[Union[Dict[str, Any], str]] = None,
) -> str:
    """
    Load and populate a prompt template from the prompt library.

    Args:
        version: Prompt version ("v1", "v2", "v3", or "mongo_v1", "mongo_v2", "mongo_v3").
        db_type: Database dialect (e.g. "sqlite", "postgresql", "mongodb").
        schema: Database schema as a dictionary (from get_schema) or formatted string.

    Returns:
        The fully populated prompt template as a string.
    """
    prompts_dir = _find_prompts_dir()

    v_norm = version.strip().lower()
    if not v_norm.startswith("v") and not v_norm.startswith("mongo_") and not v_norm.startswith("router_"):
        v_norm = f"v{v_norm}"

    # Auto-resolve to mongo_v*.txt if db_type is mongodb (unless explicitly loading router prompt)
    if db_type.lower() in ("mongodb", "mongo") and not v_norm.startswith("mongo_") and not v_norm.startswith("router_"):
        template_file = prompts_dir / f"mongo_{v_norm}.txt"
        if not template_file.exists():
            template_file = prompts_dir / f"{v_norm}.txt"
    else:
        template_file = prompts_dir / f"{v_norm}.txt"

    if not template_file.exists():
        raise ValueError(
            f"Prompt version '{version}' not found at {template_file}. "
            f"Available versions: v1, v2, v3, mongo_v1, mongo_v2, mongo_v3, router_v1, router_v2, router_v3."
        )

    with open(template_file, "r", encoding="utf-8") as f:
        template = f.read()

    # Convert schema to text if provided as a dict
    if isinstance(schema, dict):
        schema_str = schema_to_text(schema)
    elif schema is not None:
        schema_str = str(schema)
    else:
        schema_str = "(No schema provided)"

    # Format few-shot examples if requested by template
    fewshot_str = ""
    if "{fewshot_examples}" in template:
        fewshot_str = _format_fewshot_examples(db_type, prompts_dir)

    # Safely replace placeholders
    rendered = (
        template.replace("{db_type}", db_type)
        .replace("{dialect}", db_type)
        .replace("{schema}", schema_str)
        .replace("{fewshot_examples}", fewshot_str)
    )

    return rendered
