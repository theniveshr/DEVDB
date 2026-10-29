"""Local LLM client for DevDB using local Ollama and swappable providers.

Zero external cloud APIs required (no OpenAI or Gemini API keys needed).
Runs completely offline using native local Ollama on http://localhost:11434.
"""

import os
import re
import json
import time
import warnings
from typing import Any, Dict, Optional, List
import requests

from devdb.config import LLM_PROVIDER, LLM_MODEL, OLLAMA_URL, OLLAMA_BASE_URL


def strip_markdown_code_fences(text: str) -> str:
    """Strip markdown code fences (```json ... ``` or ``` ... ```) from text."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    
    # If the text has an internal fenced block, inspect if it contains JSON
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
    if m:
        candidate = m.group(1).strip()
        if "{" in candidate and "}" in candidate:
            return candidate
            
    return cleaned.strip()


def extract_first_json_block(text: str) -> str:
    """Extract the first balanced {...} JSON block from text, ignoring braces inside strings."""
    start = text.find("{")
    if start == -1:
        return text.strip()

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        char = text[i]

        if escape:
            escape = False
            continue

        if char == "\\":
            if in_string:
                escape = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if not in_string:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

    # If unmatched braces, fallback to everything from first '{'
    return text[start:].strip()


class OllamaProvider:
    """
    Ollama LLM provider calling http://localhost:11434/api/chat with requests library.
    Stream=False, 120s timeout, temperature 0.1, num_ctx 4096, and JSON mode ("format": "json").
    """

    def __init__(
        self,
        model: Optional[str] = None,
        url: Optional[str] = None,
        timeout: int = 120,
        temperature: float = 0.1,
        num_ctx: int = 4096,
    ):
        self.model = model or os.getenv("LLM_MODEL", "llama3.2") or LLM_MODEL or "llama3.2"
        raw_url = url or os.getenv("OLLAMA_URL", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")) or OLLAMA_URL
        self.url = raw_url.rstrip("/")
        self.timeout = timeout
        self.temperature = temperature
        self.num_ctx = num_ctx
        self.last_estimated_tokens: int = 0

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count (approximately 1 token per 4 characters)."""
        return max(1, len(text) // 4)

    def _check_token_length(self, system_prompt: Optional[str], user_prompt: str) -> int:
        """Warn if prompt + schema is longer than ~3,000 tokens."""
        combined = (system_prompt or "") + (user_prompt or "")
        est = self._estimate_tokens(combined)
        self.last_estimated_tokens = est
        if est > 3000:
            warnings.warn(
                f"Prompt + schema is longer than about 3,000 tokens (~{est} tokens). "
                f"This approaches Ollama's num_ctx limit ({self.num_ctx}) and may risk silent truncation.",
                UserWarning,
                stacklevel=3,
            )
        return est

    def call_api(
        self,
        system_prompt: Optional[str],
        user_prompt: str,
        json_mode: bool = False,
        temperature: Optional[float] = None,
    ) -> str:
        """
        Send a chat completion request to Ollama /api/chat.
        Friendly error messages:
        - Ollama not running -> contains 'start Ollama'
        - Model missing -> contains 'run: ollama pull <model>'
        - Timeout -> contains 'timed out after 120s'
        """
        self._check_token_length(system_prompt, user_prompt)
        chat_url = f"{self.url}/api/chat"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature if temperature is not None else self.temperature,
                "num_ctx": self.num_ctx,
            },
        }
        if json_mode:
            payload["format"] = "json"

        try:
            resp = requests.post(chat_url, json=payload, timeout=self.timeout)
        except requests.exceptions.ConnectionError as ce:
            raise ConnectionError(
                f"Could not connect to Ollama at {self.url}. Please start Ollama ('ollama serve' or open the Ollama desktop app)."
            ) from ce
        except requests.exceptions.Timeout as te:
            raise TimeoutError(
                f"Request to Ollama timed out after {self.timeout}s (model: '{self.model}')."
            ) from te
        except Exception as e:
            raise RuntimeError(f"Failed to communicate with Ollama: {e}") from e

        # Model missing check (Ollama returns 404 or body with model not found)
        if resp.status_code == 404 or "not found" in resp.text.lower():
            raise RuntimeError(
                f"Model '{self.model}' is not installed in Ollama. Please run: ollama pull {self.model}"
            )

        if not resp.ok:
            raise RuntimeError(
                f"Ollama API returned HTTP {resp.status_code}: {resp.text}"
            )

        try:
            data = resp.json()
        except Exception as je:
            raise ValueError(f"Failed to decode Ollama response as JSON: {je}. Raw: {resp.text}") from je

        return data.get("message", {}).get("content", "")

    def ask(self, system_prompt: Optional[str], user_prompt: str) -> str:
        return self.call_api(system_prompt=system_prompt, user_prompt=user_prompt, json_mode=False)

    def ask_json_raw(self, system_prompt: Optional[str], user_prompt: str) -> str:
        return self.call_api(system_prompt=system_prompt, user_prompt=user_prompt, json_mode=True)


class GeminiProvider:
    """Google Gemini LLM provider."""

    def __init__(self, model: Optional[str] = None):
        self.model = model or os.getenv("LLM_MODEL", "gemini-1.5-flash") or LLM_MODEL or "gemini-1.5-flash"

    def call_api(
        self,
        system_prompt: Optional[str],
        user_prompt: str,
        json_mode: bool = False,
        temperature: float = 0.1,
    ) -> str:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set in environment or .env file.\n"
                "Tip: To run with Ollama locally, set LLM_PROVIDER=ollama."
            )
        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError("google-generativeai package is required for Gemini provider. Run: pip install google-generativeai")

        genai.configure(api_key=api_key)
        model_kwargs: Dict[str, Any] = {}
        if system_prompt:
            model_kwargs["system_instruction"] = system_prompt

        gen_model = genai.GenerativeModel(self.model, **model_kwargs)
        generation_config_dict: Dict[str, Any] = {"temperature": temperature}
        if json_mode:
            generation_config_dict["response_mime_type"] = "application/json"
        generation_config = genai.types.GenerationConfig(**generation_config_dict)

        response = gen_model.generate_content(user_prompt, generation_config=generation_config)
        return response.text or ""


class LLMClient:
    """
    LLM Client with swappable providers (ollama, gemini, mock) and small-model JSON safety:
    - Code fence stripping
    - First {...} block extraction
    - Single retry on malformed JSON with 'Return ONLY valid JSON, no extra text.'
    - Retry counter tracking
    """
    total_json_retries: int = 0

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.provider = (provider or os.getenv("LLM_PROVIDER", "ollama") or LLM_PROVIDER).lower()
        self.model = model or os.getenv("LLM_MODEL", "llama3.2") or LLM_MODEL or "llama3.2"
        self.base_url = (base_url or os.getenv("OLLAMA_URL", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")) or OLLAMA_URL).rstrip("/")
        self.json_retry_count: int = 0

        if self.provider == "ollama":
            self.provider_instance = OllamaProvider(model=self.model, url=self.base_url)
        elif self.provider == "gemini":
            self.provider_instance = GeminiProvider(model=self.model)
        else:
            self.provider_instance = None

    def _is_rate_limit_error(self, err: Exception) -> bool:
        """Check if an exception is a rate limit, quota, or busy server error."""
        err_str = str(err).lower()
        return any(term in err_str for term in [
            "429",
            "busy",
            "rate limit",
            "ratelimit",
            "too many requests",
            "resource_exhausted",
        ])


    @classmethod
    def reset_total_retries(cls) -> None:
        """Reset the global count of JSON retries."""
        cls.total_json_retries = 0

    def reset_retry_count(self) -> None:
        """Reset the client instance's JSON retry counter."""
        self.json_retry_count = 0

    def ask(self, system_prompt: str, user_prompt: str) -> str:
        """Send a request to the configured LLM provider and return raw text."""
        if self.provider == "mock":
            return self._mock_ask(system_prompt, user_prompt)

        if self.provider_instance:
            return self.provider_instance.call_api(system_prompt, user_prompt, json_mode=False)

        raise ValueError(f"Unsupported LLM provider: {self.provider}")

    def _clean_json_text(self, text: str) -> str:
        """Extract and clean raw JSON content from possible Markdown code fences."""
        return strip_markdown_code_fences(text)

    def ask_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """
        Enforce structured JSON output using provider's native JSON mode,
        with small-model JSON safety:
        1. Strip markdown code fences.
        2. Extract first {...} block.
        3. If parsing fails, retry ONCE with 'Return ONLY valid JSON, no extra text.'
        4. Count how often a retry was needed.
        """
        if self.provider == "mock":
            return self._mock_ask_json(system_prompt, user_prompt)

        # 1. First attempt with native JSON mode
        raw_text = ""
        if self.provider_instance:
            raw_text = self.provider_instance.call_api(system_prompt, user_prompt, json_mode=True)
        else:
            raw_text = self.ask(system_prompt, user_prompt)

        cleaned = strip_markdown_code_fences(raw_text)
        block = extract_first_json_block(cleaned)

        try:
            return json.loads(block)
        except (json.JSONDecodeError, ValueError):
            pass

        # 2. Parsing failed! Increment retry counter and retry ONCE
        self.json_retry_count += 1
        LLMClient.total_json_retries += 1

        retry_instruction = "Return ONLY valid JSON, no extra text."
        retry_prompt = f"{user_prompt}\n\n{retry_instruction}"

        try:
            if self.provider_instance:
                retry_raw = self.provider_instance.call_api(system_prompt, retry_prompt, json_mode=True)
            else:
                retry_raw = self.ask(system_prompt, retry_prompt)
        except Exception as e:
            raise ValueError(
                f"Failed to communicate with provider during JSON retry.\n"
                f"Initial raw response: {raw_text}\n"
                f"Error: {e}"
            ) from e

        cleaned_retry = strip_markdown_code_fences(retry_raw)
        block_retry = extract_first_json_block(cleaned_retry)

        try:
            return json.loads(block_retry)
        except (json.JSONDecodeError, ValueError) as err:
            raise ValueError(
                f"Failed to parse model output as JSON after retry.\n"
                f"Initial output: {raw_text}\n"
                f"Retry output: {retry_raw}\n"
                f"Error: {err}"
            ) from err

    def _mock_ask(self, system_prompt: str, user_prompt: str) -> str:
        """Offline mock response for text generation."""
        return json.dumps({
            "action": "query",
            "sql": "SELECT * FROM customers LIMIT 10;",
            "explanation": "Mock response for offline testing."
        })

    def _mock_ask_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """Offline mock responder for demonstrations and unit tests."""
        lower_prompt = user_prompt.lower()
        lower_sys = (system_prompt or "").lower()
        is_postgres = "postgres" in lower_sys or "postgres" in lower_prompt

        # ReAct self-correction request
        if "react self-correction protocol" in lower_sys or "correction attempt" in lower_prompt:
            m_sql = re.search(r"Failed SQL:\s*(.+?)(?:\n|$)", user_prompt, re.IGNORECASE)
            failed_sql = m_sql.group(1).strip() if m_sql else ""
            m_err = re.search(r"Database Error:\s*(.+?)(?:\n|$)", user_prompt, re.IGNORECASE)
            db_err = m_err.group(1).strip() if m_err else ""

            corrected_sql = failed_sql
            error_cause = "Non-existent column reference in SQL"
            fix_applied = "Adjusted column and table references to match schema"

            if "user_name" in failed_sql.lower() or "user_name" in db_err.lower() or "user_name" in lower_prompt:
                corrected_sql = re.sub(r"\buser_name\b", "name", failed_sql, flags=re.IGNORECASE)
                error_cause = "Column 'user_name' does not exist in 'customers' table"
                fix_applied = "Replaced non-existent column 'user_name' with 'name' from schema"
            elif "client" in failed_sql.lower() or "client" in db_err.lower() or "client" in lower_prompt:
                corrected_sql = re.sub(r"\bclient\b", "customers", failed_sql, flags=re.IGNORECASE)
                error_cause = "Table 'client' does not exist in database"
                fix_applied = "Replaced 'client' with 'customers' matching active schema"
            elif "order_status" in failed_sql.lower() or "order_status" in lower_prompt:
                corrected_sql = re.sub(r"\border_status\b", "status", failed_sql, flags=re.IGNORECASE)
                error_cause = "Column 'order_status' does not exist in 'orders' table"
                fix_applied = "Replaced 'order_status' with 'status'"
            elif not corrected_sql or "syntax error" in lower_prompt or "syntax error" in db_err.lower():
                corrected_sql = "SELECT id, name, email FROM customers LIMIT 10;"
                error_cause = "Syntax error in SQL query"
                fix_applied = "Reconstructed valid dialect-compliant SELECT statement"

            action = "write" if any(w in corrected_sql.lower() for w in ["insert", "update", "delete", "alter"]) else "query"
            thought = (
                f"Diagnosed database error: {error_cause}. "
                f"Cross-referencing with active schema confirms the actual field definition. "
                f"Correction strategy: {fix_applied}."
            )

            return {
                "thought": thought,
                "reasoning": {
                    "tables_involved": ["customers"],
                    "keys": ["customers.id"],
                    "dependency_order": "Single table query",
                    "risks": "None (Read-only query)" if action == "query" else "Medium",
                    "error_cause": error_cause,
                    "fix_applied": fix_applied,
                },
                "action": action,
                "sql": corrected_sql,
                "explanation": f"Self-corrected SQL: {fix_applied}."
            }

        # Fake data specification request
        if "synthetic test data architect" in lower_sys or "faker" in lower_sys or "fake data spec" in lower_sys:
            if "customer" in lower_prompt:
                rows_match = re.search(r"(\d+)\s+fake", lower_prompt)
                count = int(rows_match.group(1)) if rows_match else 50
                locale = "en_IN" if ("indian" in lower_prompt or "in" in lower_prompt) else "en_US"
                return {
                    "table": "customers",
                    "rows": count,
                    "locale": locale,
                    "columns": {
                        "name": "faker:name",
                        "email": "faker:email",
                        "created_at": "faker:date_between:-2y:today"
                    }
                }
            elif "order_item" in lower_prompt:
                return {
                    "table": "order_items",
                    "rows": 20,
                    "locale": "en_US",
                    "columns": {
                        "order_id": "fk:orders.id",
                        "product_id": "fk:products.id",
                        "quantity": "int_range:1:10"
                    }
                }
            elif "order" in lower_prompt:
                rows_match = re.search(r"(\d+)\s+fake", lower_prompt)
                count = int(rows_match.group(1)) if rows_match else 200
                return {
                    "table": "orders",
                    "rows": count,
                    "locale": "en_US",
                    "columns": {
                        "customer_id": "fk:customers.id",
                        "order_date": "faker:date_between:-1y:today",
                        "status": "choice:pending,processing,shipped,delivered,cancelled",
                        "total_amount": "faker:price"
                    }
                }
            elif "product" in lower_prompt:
                return {
                    "table": "products",
                    "rows": 20,
                    "locale": "en_US",
                    "columns": {
                        "name": "faker:word",
                        "price": "faker:price",
                        "stock": "int_range:1:500"
                    }
                }
            else:
                return {
                    "table": "customers",
                    "rows": 50,
                    "locale": "en_US",
                    "columns": {
                        "name": "faker:name",
                        "email": "faker:email"
                    }
                }

        if "drop" in lower_prompt and "database" in lower_prompt:
            return {
                "action": "write",
                "sql": "DROP DATABASE devdb;",
                "explanation": "Attempt to drop the development database."
            }
        elif "relationship" in lower_prompt or "foreign key" in lower_prompt or "link orders" in lower_prompt:
            is_postgres = "postgres" in lower_sys or "postgres" in lower_prompt
            if "orphan" in lower_prompt:
                return {
                    "action": "query",
                    "sql": "SELECT o.id, o.customer_id, o.order_date, o.total_amount FROM orders o LEFT JOIN customers c ON o.customer_id = c.id WHERE o.customer_id IS NOT NULL AND c.id IS NULL;",
                    "explanation": "Query finding orphan orders that point to non-existent customer records."
                }
            elif "one-to-one" in lower_prompt or "one to one" in lower_prompt or "1:1" in lower_prompt:
                if is_postgres:
                    sql = "ALTER TABLE user_profiles ADD CONSTRAINT fk_user_profiles_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;\nALTER TABLE user_profiles ADD CONSTRAINT uq_user_profiles_user_id UNIQUE (user_id);"
                else:
                    sql = "PRAGMA foreign_keys = OFF;\nBEGIN TRANSACTION;\nCREATE TABLE user_profiles_new (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    user_id INTEGER NOT NULL UNIQUE,\n    bio TEXT,\n    avatar_url TEXT,\n    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE\n);\nINSERT INTO user_profiles_new (id, user_id, bio, avatar_url) SELECT id, user_id, bio, avatar_url FROM user_profiles;\nDROP TABLE user_profiles;\nALTER TABLE user_profiles_new RENAME TO user_profiles;\nCREATE UNIQUE INDEX ix_user_profiles_user_id ON user_profiles(user_id);\nCOMMIT;\nPRAGMA foreign_keys = ON;"
                return {
                    "action": "relationship",
                    "sql": sql,
                    "explanation": "Creates a One-to-One relationship between user_profiles and users."
                }
            elif "many-to-many" in lower_prompt or "many to many" in lower_prompt or "join table" in lower_prompt:
                if is_postgres:
                    sql = "CREATE TABLE product_categories (\n    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,\n    category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,\n    PRIMARY KEY (product_id, category_id)\n);\nCREATE INDEX idx_product_categories_product ON product_categories(product_id);\nCREATE INDEX idx_product_categories_category ON product_categories(category_id);"
                else:
                    sql = "CREATE TABLE product_categories (\n    product_id INTEGER NOT NULL,\n    category_id INTEGER NOT NULL,\n    PRIMARY KEY (product_id, category_id),\n    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE,\n    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE\n);\nCREATE INDEX idx_product_categories_product ON product_categories(product_id);\nCREATE INDEX idx_product_categories_category ON product_categories(category_id);"
                return {
                    "action": "relationship",
                    "sql": sql,
                    "explanation": "Creates a Many-to-Many junction table."
                }
            else:
                if is_postgres:
                    sql = "ALTER TABLE orders ADD CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT;"
                else:
                    sql = "PRAGMA foreign_keys = OFF;\nBEGIN TRANSACTION;\nCREATE TABLE orders_new (\n    id INTEGER PRIMARY KEY AUTOINCREMENT,\n    customer_id INTEGER NOT NULL,\n    order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,\n    status TEXT NOT NULL DEFAULT 'pending',\n    total_amount REAL NOT NULL,\n    FOREIGN KEY (customer_id) REFERENCES customers(id)\n);\nINSERT INTO orders_new SELECT id, customer_id, order_date, status, total_amount FROM orders;\nDROP TABLE orders;\nALTER TABLE orders_new RENAME TO orders;\nCREATE INDEX ix_orders_customer_id ON orders(customer_id);\nCOMMIT;\nPRAGMA foreign_keys = ON;"
                return {
                    "action": "relationship",
                    "sql": sql,
                    "explanation": "Adds foreign key relationship linking orders to customers on customer_id."
                }
        elif "fake" in lower_prompt:
            return {
                "action": "fake_data",
                "sql": "",
                "explanation": "Generate realistic synthetic test data using Faker specification."
            }
        elif re.search(r"\b(?:create\s+(?:a\s+)?(?:[\w\-]+\s+)?(?:user|role)|grant\b|revoke\b|drop\s+(?:user|role)|(?:list|show)\s+(?:all\s+)?users)\b", lower_prompt):
            if not is_postgres:
                return {
                    "action": "create_user",
                    "operation": "unsupported",
                    "sql": "",
                    "explanation": "SQLite is a serverless, file-based database and does not support database users, passwords, or GRANT/REVOKE permissions. Access control is managed at the filesystem level."
                }
            if "list" in lower_prompt or "show" in lower_prompt:
                return {
                    "action": "create_user",
                    "operation": "list",
                    "explanation": "List all PostgreSQL database users and granted privileges."
                }
            elif "drop" in lower_prompt:
                m = re.search(r"drop\s+(?:user|role)\s+([a-zA-Z0-9_]+)", lower_prompt)
                u = m.group(1) if m else "report_user"
                return {
                    "action": "create_user",
                    "operation": "drop_user",
                    "username": u,
                    "explanation": f"Drop user {u}."
                }
            elif "revoke" in lower_prompt:
                return {
                    "action": "create_user",
                    "operation": "revoke",
                    "username": "report_user",
                    "privileges": ["ALL"],
                    "tables": ["orders"],
                    "explanation": "Revoke privileges on orders from report_user."
                }
            elif "grant" in lower_prompt:
                u_target = "app_dev" if "app_dev" in lower_prompt else "report_user"
                return {
                    "action": "create_user",
                    "operation": "grant",
                    "username": u_target,
                    "privileges": ["SELECT", "INSERT", "UPDATE", "DELETE"] if "write" in lower_prompt else ["SELECT"],
                    "tables": ["ALL"] if "all" in lower_prompt else ["orders"],
                    "explanation": f"Grant privileges to {u_target}."
                }
            else:
                m = re.search(r"(?:user|role)\s+([a-zA-Z0-9_]+)", lower_prompt)
                u = m.group(1) if m else "report_user"
                is_ro = "read-only" in lower_prompt or "read_only" in lower_prompt or "readonly" in lower_prompt or "read" in lower_prompt
                tbls = ["orders"] if "orders" in lower_prompt else (["customers"] if "customers" in lower_prompt else ["ALL"])
                return {
                    "action": "create_user",
                    "operation": "create_user",
                    "username": u,
                    "privileges": ["SELECT"] if is_ro else ["SELECT", "INSERT", "UPDATE", "DELETE"],
                    "tables": tbls,
                    "explanation": f"Create user {u} with specified privileges."
                }
        elif "insert" in lower_prompt or "add" in lower_prompt:
            return {
                "action": "write",
                "sql": "INSERT INTO customers (name, email) VALUES ('Demo Customer', 'demo@example.com');",
                "explanation": "Insert a new record into customers table."
            }
        elif "update" in lower_prompt:
            return {
                "action": "write",
                "sql": "UPDATE customers SET name = 'Updated Name' WHERE id = 1;",
                "explanation": "Update customer name for customer #1."
            }
        else:
            return {
                "action": "query",
                "sql": "SELECT id, name, email, created_at FROM customers LIMIT 10;",
                "explanation": "Query all customer records from customers table."
            }
