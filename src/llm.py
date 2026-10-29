"""Swappable LLM interface supporting OpenAI, Gemini, Ollama, and Mock modes."""

import os
import re
from typing import Optional
from src.config import get_llm_provider, get_llm_model
from devdb.llm import OllamaProvider, GeminiProvider


class LLMClient:
    """Unified wrapper around different LLM providers."""

    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None):
        self.provider = (provider or get_llm_provider()).lower()
        self.model = model or get_llm_model()

    def generate(self, user_prompt: str, system_prompt: Optional[str] = None, temperature: float = 0.0) -> str:
        """Generate text completion from the configured LLM provider."""
        if self.provider == "ollama":
            return self._generate_ollama(user_prompt, system_prompt, temperature)
        elif self.provider == "openai":
            return self._generate_openai(user_prompt, system_prompt, temperature)
        elif self.provider == "gemini":
            return self._generate_gemini(user_prompt, system_prompt, temperature)
        elif self.provider == "mock":
            return self._generate_mock(user_prompt, system_prompt)
        else:
            raise ValueError(
                f"Unsupported LLM provider '{self.provider}'. "
                "Supported options in .env: 'ollama', 'mock', 'openai', 'gemini'."
            )

    def _generate_ollama(self, user_prompt: str, system_prompt: Optional[str], temperature: float) -> str:
        """Call local Ollama server natively without external cloud APIs."""
        import json
        import urllib.request
        import urllib.error

        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        chat_url = f"{base_url}/api/chat"

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }

        req = urllib.request.Request(
            chat_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("message", {}).get("content", "")
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"Could not connect to local Ollama server at '{base_url}'.\n"
                f"Make sure Ollama is installed and running (`ollama serve`).\n"
                f"Details: {e}"
            )
        except Exception as e:
            raise RuntimeError(f"Ollama generation error: {e}")

    def _generate_openai(self, user_prompt: str, system_prompt: Optional[str], temperature: float) -> str:
        """Call OpenAI-compatible API (supports OpenAI, Groq, Ollama)."""
        import openai

        api_key = os.getenv("OPENAI_API_KEY")
        base_url = os.getenv("OPENAI_BASE_URL")

        # For local Ollama, API key can be a dummy string
        if not api_key and self.provider == "ollama":
            api_key = "ollama"

        if not api_key:
            raise ValueError(
                "OPENAI_API_KEY is not set in environment or .env file.\n"
                "Tip: To run without an API key, set LLM_PROVIDER=mock in your .env file."
            )

        client = openai.OpenAI(api_key=api_key, base_url=base_url)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
        )
        return response.choices[0].message.content or ""

    def _generate_gemini(self, user_prompt: str, system_prompt: Optional[str], temperature: float) -> str:
        """Call Google Gemini API."""
        import google.generativeai as genai

        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set in environment or .env file.\n"
                "Tip: To run without an API key, set LLM_PROVIDER=mock in your .env file."
            )

        genai.configure(api_key=api_key)
        
        # Configure model with system instruction if supported
        model_kwargs = {}
        if system_prompt:
            model_kwargs["system_instruction"] = system_prompt

        model = genai.GenerativeModel(self.model, **model_kwargs)
        generation_config = genai.types.GenerationConfig(temperature=temperature)

        response = model.generate_content(user_prompt, generation_config=generation_config)
        return response.text or ""

    def _generate_mock(self, user_prompt: str, system_prompt: Optional[str]) -> str:
        """Intelligent offline mock responder for demonstrations and unit tests."""
        lower_prompt = user_prompt.lower()

        # Check if this is a fake data spec request
        if "faker" in lower_prompt or "json specification" in lower_prompt:
            if "orders" in lower_prompt and "order_items" not in lower_prompt:
                return """```json
{
  "table": "orders",
  "columns": {
    "id": {"rule": "auto_increment"},
    "user_id": {"rule": "foreign_key", "references": "users.id"},
    "status": {"rule": "faker", "provider": "random_element", "params": {"elements": ["pending", "completed", "shipped"]}},
    "total_amount": {"rule": "faker", "provider": "pyfloat", "params": {"left_digits": 2, "right_digits": 2, "positive": true}},
    "order_date": {"rule": "faker", "provider": "date_time_this_year"}
  }
}
```"""
            elif "order_items" in lower_prompt:
                return """```json
{
  "table": "order_items",
  "columns": {
    "id": {"rule": "auto_increment"},
    "order_id": {"rule": "foreign_key", "references": "orders.id"},
    "product_id": {"rule": "foreign_key", "references": "products.id"},
    "quantity": {"rule": "faker", "provider": "random_int", "params": {"min": 1, "max": 5}},
    "unit_price": {"rule": "faker", "provider": "pyfloat", "params": {"left_digits": 2, "right_digits": 2, "positive": true}}
  }
}
```"""
            else:
                return """```json
{
  "table": "users",
  "columns": {
    "id": {"rule": "auto_increment"},
    "name": {"rule": "faker", "provider": "name"},
    "email": {"rule": "faker", "provider": "safe_email"},
    "role": {"rule": "faker", "provider": "random_element", "params": {"elements": ["customer", "moderator", "admin"]}},
    "created_at": {"rule": "faker", "provider": "date_time_this_year"}
  }
}
```"""

        full_text = f"{system_prompt or ''} {user_prompt}".lower()
        is_mongo = "mongodb" in full_text
        is_v3 = "think step by step" in full_text or "reasoning" in full_text or "principal architect" in full_text or "<thinking>" in full_text
        is_v1 = "you are a sql generator" in full_text or "write the sql query:" in full_text or "you are a database query generator" in full_text or "convert this request to sql" in full_text

        # Check if this is a user management request
        user_query_matches = re.findall(r"(?:user query|user request):\s*([^\n\r]+)", lower_prompt)
        q_target = user_query_matches[-1] if user_query_matches else (lower_prompt.splitlines()[-1] if lower_prompt else "")
        if any(kw in q_target for kw in ["create user", "create a user", "grant ", "revoke "]):
            if is_mongo:
                return """```javascript
db.createUser({
  user: "test_analyst",
  pwd: "DevPass123!",
  roles: [{ role: "read", db: "devdb" }]
})
```"""
            return """```sql
CREATE USER test_analyst WITH PASSWORD 'DevPass123!';
GRANT SELECT ON ALL TABLES IN SCHEMA public TO test_analyst;
```"""

        # Generate sample queries based on dialect and keywords
        if is_mongo:
            if "top" in lower_prompt or "spent" in lower_prompt:
                query = """db.orders.aggregate([
  { "$group": { "_id": "$user_id", "total_spent": { "$sum": "$total_amount" } } },
  { "$sort": { "total_spent": -1 } },
  { "$limit": 3 },
  { "$lookup": { "from": "users", "localField": "_id", "foreignField": "_id", "as": "user" } },
  { "$unwind": "$user" },
  { "$project": { "_id": 0, "name": "$user.name", "total_spent": 1 } }
])"""
            elif "orphan" in lower_prompt:
                query = """db.orders.aggregate([
  { "$lookup": { "from": "users", "localField": "user_id", "foreignField": "_id", "as": "matched" } },
  { "$match": { "matched": { "$size": 0 } } }
])"""
            elif "delete" in lower_prompt:
                query = """db.orders.deleteMany({ "status": "cancelled" })"""
            else:
                query = """db.users.find({}, { "name": 1, "email": 1, "role": 1 })"""
            code_lang = "javascript"
        else:
            target_check = q_target if q_target else lower_prompt
            if "top" in target_check or "spent" in target_check:
                query = """SELECT u.name, SUM(o.total_amount) AS total_spent
FROM users u
JOIN orders o ON u.id = o.user_id
GROUP BY u.id, u.name
ORDER BY total_spent DESC
LIMIT 3;"""
            elif "orphan" in target_check:
                query = """SELECT o.* FROM orders o
LEFT JOIN users u ON o.user_id = u.id
WHERE u.id IS NULL;"""
            elif "delete" in target_check:
                query = """DELETE FROM orders WHERE status = 'cancelled';"""
            else:
                query = """SELECT u.name, u.email, COUNT(o.id) AS order_count
FROM users u
LEFT JOIN orders o ON u.id = o.user_id
GROUP BY u.id, u.name, u.email;"""
            code_lang = "sql"

        if is_v3:
            return f"""<thinking>
1. Schema Grounding: Verified entities and fields in active schema. No hallucinated attributes.
2. Relationship Paths: Cross-collection references correctly mapped.
3. Dialect Compliance: Formatted natively for {'MongoDB' if is_mongo else 'SQL'}.
4. Operation Classification: READ query.
5. Step-by-Step Logic: Construct aggregation / query pipeline.
</thinking>

```{code_lang}
{query}
```"""
        elif is_v1:
            return f"Here is the query you asked for:\n\n{query}"
        else:
            return f"```{code_lang}\n{query}\n```"


# Global singleton instance for easy import
default_client = LLMClient()


def query_llm(user_prompt: str, system_prompt: Optional[str] = None, provider: Optional[str] = None, model: Optional[str] = None) -> str:
    """Convenience helper function to query LLM."""
    client = LLMClient(provider=provider, model=model) if (provider or model) else default_client
    return client.generate(user_prompt=user_prompt, system_prompt=system_prompt)
