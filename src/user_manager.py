"""Database user and role management module (DCL) with dialect awareness."""

import re
from typing import Any, Dict, List, Optional
from src.config import PROMPTS_DIR
from src.db import DatabaseManager
from src.llm import query_llm


class UserManager:
    """Manages database users, roles, and permissions using plain English."""

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def generate_user_management_sql(self, user_query: str, provider: Optional[str] = None) -> Dict[str, Any]:
        """Generate DCL statements from natural language with dialect-specific instructions."""
        prompt_file = PROMPTS_DIR / "user_management.txt"
        if not prompt_file.exists():
            raise FileNotFoundError("user_management.txt not found in prompts/ directory.")

        template = prompt_file.read_text(encoding="utf-8")
        tables = ", ".join(self.db.get_tables()) or "None"
        dialect = self.db.dialect_name

        rendered_prompt = (
            template.replace("{dialect}", dialect)
            .replace("{tables}", tables)
            .replace("{user_query}", user_query)
        )

        response = query_llm(
            user_prompt=rendered_prompt,
            system_prompt="You are an expert database security and access control administrator.",
            provider=provider,
        )

        # Extract SQL or MQL from response
        sql_match = re.search(r"```(?:sql|javascript|js)?\s*(.*?)\s*```", response, flags=re.DOTALL | re.IGNORECASE)
        sql = sql_match.group(1).strip() if sql_match else response.strip()

        is_sqlite = dialect == "sqlite"

        return {
            "sql": sql,
            "raw_response": response,
            "dialect": dialect,
            "is_supported_on_current_db": not is_sqlite,
            "warning": (
                "SQLite is a single-file serverless database that controls access through "
                "operating system file permissions rather than database user accounts. "
                "The generated SQL is standard PostgreSQL DCL provided for your reference."
                if is_sqlite else None
            ),
        }
