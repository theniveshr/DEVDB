"""Natural Language to SQL translation module with prompt version management."""

import re
from dataclasses import dataclass
from typing import Optional
from src.config import PROMPTS_DIR
from src.db import DatabaseManager
from src.llm import query_llm


@dataclass
class SQLGenerationResult:
    """Encapsulates the generated SQL, reasoning, and safety classification."""
    sql: str
    thinking: Optional[str]
    operation_type: str  # "READ", "WRITE", or "DESTRUCTIVE"
    is_write: bool
    is_destructive: bool
    prompt_version: str
    raw_response: str


class SQLGenerator:
    """Manages prompt template rendering and SQL extraction."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def load_prompt_template(self, version: str) -> str:
        """Load prompt file from prompts/ folder."""
        clean_version = version.lower().strip()
        filename = f"{clean_version}.txt" if not clean_version.endswith(".txt") else clean_version
        prompt_path = PROMPTS_DIR / filename

        if not prompt_path.exists():
            raise FileNotFoundError(
                f"Prompt template '{filename}' not found under '{PROMPTS_DIR}'. "
                f"Available templates: v1.txt, v2.txt, v3.txt"
            )

        return prompt_path.read_text(encoding="utf-8")

    def classify_sql(self, sql: str) -> tuple[str, bool, bool]:
        """Classify SQL or MongoDB statement into READ, WRITE, or DESTRUCTIVE."""
        clean = re.sub(r"--.*?\n", "", sql)
        clean = re.sub(r"//.*?\n", "", clean)
        clean = re.sub(r"/\*.*?\*/", "", clean, flags=re.DOTALL).strip().upper()

        if not clean:
            return "UNKNOWN", False, False

        # Destructive operations
        destructive_keywords = ["DROP", "DELETE", "TRUNCATE", "ALTER", "DELETEMANY", "DELETEONE"]
        if any(kw in clean for kw in destructive_keywords):
            return "DESTRUCTIVE", True, True

        # Write operations
        write_keywords = ["INSERT", "UPDATE", "REPLACE", "INSERTONE", "INSERTMANY", "UPDATEONE", "UPDATEMANY", "CREATE"]
        if any(kw in clean for kw in write_keywords):
            return "WRITE", True, False

        # Read operations
        read_keywords = ["SELECT", "EXPLAIN", "PRAGMA", "SHOW", "WITH", "FIND", "AGGREGATE", "COUNT", "COUNTDOCUMENTS", "DISTINCT"]
        if any(kw in clean for kw in read_keywords):
            return "READ", False, False

        return "WRITE", True, False

    def extract_sql_and_thinking(self, raw_text: str) -> tuple[str, Optional[str]]:
        """Extract SQL/MongoDB query and optional Chain-of-Thought thinking from raw LLM output."""
        thinking = None
        thinking_match = re.search(r"<thinking>(.*?)</thinking>", raw_text, flags=re.DOTALL | re.IGNORECASE)
        if thinking_match:
            thinking = thinking_match.group(1).strip()

        # Look for markdown block (sql, javascript, js, mongodb, json, or generic)
        block_match = re.search(r"```(?:sql|javascript|js|mongodb|json)?\s*(.*?)\s*```", raw_text, flags=re.DOTALL | re.IGNORECASE)
        if block_match:
            code = block_match.group(1).strip()
            return code, thinking

        # Fallback: remove thinking block and extract lines starting with query keywords
        text_without_thinking = re.sub(r"<thinking>.*?</thinking>", "", raw_text, flags=re.DOTALL | re.IGNORECASE).strip()
        lines = text_without_thinking.splitlines()
        sql_lines = []
        capturing = False

        for line in lines:
            stripped = line.strip()
            upper = stripped.upper()
            if not capturing and (
                any(upper.startswith(kw) for kw in ["SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP", "WITH", "DB."])
            ):
                capturing = True
            if capturing:
                if stripped.startswith(("Here", "Note:", "Hope", "Let me know")):
                    break
                sql_lines.append(line)

        if sql_lines:
            return "\n".join(sql_lines).strip(), thinking

        return text_without_thinking, thinking

    def generate(self, user_query: str, version: str = "v3", provider: Optional[str] = None, model: Optional[str] = None) -> SQLGenerationResult:
        """Render prompt, invoke LLM, parse SQL, and evaluate safety classification."""
        template = self.load_prompt_template(version)
        schema = self.db.get_schema_summary()
        dialect = self.db.dialect_name

        rendered_prompt = (
            template.replace("{schema}", schema)
            .replace("{dialect}", dialect)
            .replace("{user_query}", user_query)
        )

        raw_output = query_llm(
            user_prompt=rendered_prompt,
            system_prompt="You are a professional database AI assistant.",
            provider=provider,
            model=model,
        )

        sql, thinking = self.extract_sql_and_thinking(raw_output)
        op_type, is_write, is_destructive = self.classify_sql(sql)

        return SQLGenerationResult(
            sql=sql,
            thinking=thinking,
            operation_type=op_type,
            is_write=is_write,
            is_destructive=is_destructive,
            prompt_version=version,
            raw_response=raw_output,
        )
