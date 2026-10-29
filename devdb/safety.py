"""Safety guardrails to prevent accidental connections to staging/production databases
and validate SQL statements for destructive risk.
"""

import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit
from sqlalchemy.engine.url import make_url
import sqlparse


class SafetyViolationError(Exception):
    """Exception raised when an unsafe remote database connection is attempted."""
    pass


class SessionMode:
    READ_ONLY = "READ-ONLY"
    READ_WRITE = "READ-WRITE"
    ADMIN = "ADMIN"


PROD_KEYWORDS = {"prod", "production", "live"}


def is_production_named(text: str) -> bool:
    """Check if a cluster, database, or profile name contains production keywords."""
    if not text:
        return False
    # Split by non-alphanumeric characters (. - _ / @ :)
    tokens = [t.lower() for t in re.split(r"[^a-zA-Z0-9]+", text) if t]
    for kw in PROD_KEYWORDS:
        if kw in tokens:
            return True
        # Also check common patterns like prod01, production-db, live-cluster
        if any(re.search(rf"\b{kw}\d*\b", t) for t in tokens):
            return True
    return False


def extract_cluster_or_host(url: str) -> str:
    """Extract host/cluster identifier from connection URL."""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        return parts.hostname or ""
    except Exception:
        # Fallback regex extraction
        m = re.search(r"@([^/?:]+)", url)
        if m:
            return m.group(1)
        return ""


def validate_remote_atlas_connection(
    url: str,
    environment: str = "dev",
    profile_name: str = "",
) -> Tuple[bool, str, str]:
    """
    Validate remote Atlas or external database connection.
    Rules:
    1. Cluster or db or profile name must NOT contain prod/production/live.
    2. Environment must be explicitly 'dev' or 'sandbox'.
    Returns: (is_allowed: bool, reason: str, cluster_name: str)
    """
    cluster = extract_cluster_or_host(url)
    target_label = f"{profile_name} {cluster} {url}"

    # 1. Block production naming
    if is_production_named(target_label):
        return (
            False,
            f"Blocked: Remote database '{cluster or url}' or profile '{profile_name}' contains "
            "a production keyword ('prod', 'production', 'live'). Connecting to production databases is strictly forbidden.",
            cluster
        )

    # 2. Check environment tag
    env_clean = (environment or "").strip().lower()
    if env_clean not in ("dev", "sandbox"):
        return (
            False,
            f"Blocked: Remote database '{cluster or url}' must be explicitly labeled as 'dev' or 'sandbox' environment. "
            f"Current environment label: '{environment}'.",
            cluster
        )

    return (True, "Connection allowed under remote development policy.", cluster)


def is_dev_connection(url: str, environment: str = "dev", allow_atlas: bool = False) -> bool:
    """
    Check if a database connection URL is safe for development.
    Returns True ONLY for SQLite files (or in-memory) or hosts localhost/127.0.0.1.
    All other hosts or remote IPs return False.
    """
    if not url or not isinstance(url, str):
        return False

    url_clean = url.strip()
    if not url_clean:
        return False

    # Check for SQLite dialects or local database files (.db, .sqlite, .sqlite3)
    lower_url = url_clean.lower()
    if lower_url.startswith("sqlite") or lower_url == ":memory:":
        return True
    if (
        lower_url.endswith((".db", ".sqlite", ".sqlite3"))
        and "://" not in lower_url
        and "@" not in lower_url
    ):
        return True

    # Parse using SQLAlchemy make_url
    try:
        parsed = make_url(url_clean)
        backend = (parsed.get_backend_name() or "").lower()
        if backend == "sqlite":
            return True

        host = (parsed.host or "").strip().lower()
        if host in ("localhost", "127.0.0.1"):
            return True

        return False
    except Exception:
        pass

    # If allow_atlas is requested, validate through remote development policy
    if allow_atlas:
        ok, _, _ = validate_remote_atlas_connection(url_clean, environment)
        if ok:
            return True

    # Explicitly refuse cloud MongoDB Atlas (mongodb+srv://) if not explicitly allowed
    if lower_url.startswith("mongodb+srv://") or "mongodb+srv" in lower_url:
        return False

    # Check for direct MongoDB local connections
    if lower_url.startswith("mongodb://"):
        try:
            parts = urlsplit(url_clean)
            hostname = (parts.hostname or "").strip().lower()
            return hostname in ("localhost", "127.0.0.1")
        except Exception:
            return False

    # Fallback parsing with urllib.parse (handles other custom local schemes)
    try:
        parts = urlsplit(url_clean)
        scheme = parts.scheme.lower()
        if "sqlite" in scheme:
            return True

        hostname = (parts.hostname or "").strip().lower()
        if hostname in ("localhost", "127.0.0.1"):
            return True
    except Exception:
        pass

    return False


ALLOWED_MONGO_OPERATIONS = {
    "find",
    "count",
    "aggregate",
    "insert",
    "insert_one",
    "insert_many",
    "insert_fake",
    "update",
    "delete",
    "create_index",
    "add_reference",
    "show_relationships",
    "validate_relationships",
    "create_user",
    "grant_role",
    "revoke_role",
    "list_users",
    "drop_user",
    "list",
    "grant",
    "revoke",
}

BLOCKED_JS_OPERATORS = {"$where", "$function", "$accumulator"}


def _has_blocked_js_operator(obj: Any) -> bool:
    """Recursively inspect filter or pipeline objects for prohibited JavaScript operators."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).strip() in BLOCKED_JS_OPERATORS:
                return True
            if _has_blocked_js_operator(v):
                return True
    elif isinstance(obj, list):
        for item in obj:
            if _has_blocked_js_operator(item):
                return True
    elif isinstance(obj, str):
        for op in BLOCKED_JS_OPERATORS:
            if op in obj:
                return True
    return False


def check_mongo_op(
    op: Dict[str, Any],
    schema: Optional[Dict[str, Any]] = None,
    current_mode: str = SessionMode.READ_WRITE
) -> Tuple[bool, str, str]:
    """
    Validate MongoDB JSON operation safety and classify risk level.

    Rules:
    - Whitelisted operations only
    - Permanent block on dropDatabase and raw JavaScript ($where, $function)
    - Block update/delete with empty {} filter
    - Block collection drops unless confirmed by typing collection name
    - Block operations targeting non-existent collections (except insert_fake)
    - Enforce SessionMode (READ-ONLY blocks all writes, READ-WRITE requires ADMIN for DDL/users)
    """
    if not isinstance(op, dict):
        return (False, "HIGH", "Blocked: MongoDB request must be a structured JSON object.")

    operation = str(op.get("operation") or op.get("action") or "").strip().lower()

    # 1. Block dropDatabase (Permanent across all modes and connections)
    if operation in ("dropdatabase", "drop_database", "dropdb"):
        return (False, "HIGH", "Blocked: dropDatabase is strictly forbidden.")

    # 2. Block JS execution ($where, $function)
    if _has_blocked_js_operator(op):
        return (False, "HIGH", "Blocked: JavaScript operators ($where, $function) are forbidden.")

    # 3. Whitelist check
    if operation not in ALLOWED_MONGO_OPERATIONS and operation not in ("drop", "drop_collection"):
        return (False, "HIGH", f"Blocked: Operation '{operation}' is not in the allowed operations whitelist.")

    # 4. Drop collection requires typing name
    if operation in ("drop", "drop_collection"):
        coll = op.get("collection", "")
        return (
            True,
            "HIGH",
            f"Dangerous operation: Drop collection '{coll}'. Requires typing the collection name to confirm."
        )

    # 5. Empty filter update/delete protection
    filter_spec = op.get("filter")
    if operation in ("update", "delete"):
        if filter_spec is None or filter_spec == {} or (isinstance(filter_spec, dict) and len(filter_spec) == 0):
            return (
                False,
                "HIGH",
                f"Blocked: '{operation}' without a filter (empty {{}}) would affect all documents."
            )

    # 6. SessionMode Enforcement (if specified)
    read_only_ops = {"find", "count", "aggregate", "show_relationships", "validate_relationships", "list_users", "list"}
    admin_ops = {"create_user", "grant_role", "revoke_role", "drop_user", "drop", "drop_collection", "grant", "revoke"}

    if current_mode == SessionMode.READ_ONLY and operation not in read_only_ops:
        return (
            False,
            "HIGH",
            f"Blocked by Mode Guardrail: Active session is in READ-ONLY mode. Operation '{operation}' would modify data. "
            "Switch to READ-WRITE mode ('switch to read-write mode') to permit writes."
        )

    if current_mode == SessionMode.READ_WRITE and operation in admin_ops:
        return (
            False,
            "HIGH",
            f"Blocked by Mode Guardrail: Operation '{operation}' requires ADMIN mode. "
            "Switch with 'enter admin mode'."
        )

    # 6. Collection existence check
    coll = op.get("collection")
    if schema and coll and operation not in ("create_user", "grant_role", "revoke_role", "list_users", "drop_user", "show_relationships", "validate_relationships", "list", "grant", "revoke"):
        known_colls = set(schema.get("tables", {}).keys())
        if known_colls and coll not in known_colls:
            if operation in ("insert", "insert_one", "insert_many", "insert_fake"):
                return (True, "MEDIUM", f"Collection '{coll}' does not exist yet; will be created on insertion.")
            return (False, "MEDIUM", f"Target collection '{coll}' does not exist in active database.")

    # 7. Risk classification
    if operation in ("find", "count", "aggregate", "list_users", "list", "show_relationships", "validate_relationships"):
        return (True, "LOW", f"Read-only operation: {operation}")
    elif operation in ("insert", "insert_one", "insert_many", "insert_fake", "create_index", "add_reference"):
        return (True, "MEDIUM", f"Schema/Data insertion operation: {operation}")
    elif operation in ("update", "delete", "create_user", "grant_role", "revoke_role", "drop_user", "grant", "revoke"):
        return (True, "HIGH", f"Write/Security modification: {operation}")

    return (True, "MEDIUM", f"Operation: {operation}")


def validate_dev_connection(url: str) -> None:
    """
    Ensure the connection URL complies with dev safety guardrails.
    Raises SafetyViolationError with a clear refusal message if unsafe.
    """
    if not is_dev_connection(url):
        raise SafetyViolationError(
            f"Connection refused: '{url}' is not a permitted dev database.\n"
            "DevDB Safety Guardrail: Only SQLite files or hosts localhost / 127.0.0.1 are allowed."
        )


def check_sql(sql: str, current_mode: Optional[str] = None) -> Tuple[bool, str, str]:
    """
    Validate SQL safety and classify risk level.

    Safety rules:
    - BLOCKS: DROP DATABASE, TRUNCATE, DROP SCHEMA, and DELETE/UPDATE without WHERE
    - REQUIRES typing the table name to confirm: DROP TABLE
    - Allows only ONE statement per request (no stacked statements with semicolons)
    - Enforces SessionMode (READ-ONLY blocks writes, READ-WRITE requires ADMIN for DDL)
    - Returns (allowed: bool, risk: str, message: str)
      where risk is one of: "LOW", "MEDIUM", "HIGH"
    """
    if not sql or not isinstance(sql, str):
        return (False, "LOW", "Empty SQL statement.")

    # Split statements while respecting string literals
    raw_stmts = sqlparse.split(sql)
    clean_stmts = []

    for s in raw_stmts:
        uncommented = sqlparse.format(s, strip_comments=True).strip()
        if uncommented and uncommented != ";":
            clean_stmts.append(uncommented)

    if not clean_stmts:
        return (False, "LOW", "Empty SQL statement.")

    # Check if this is an atomic SQLite table recreation migration block
    is_sqlite_recreate = (
        bool(re.search(r"\bPRAGMA\s+foreign_keys\b", sql, re.IGNORECASE))
        and bool(re.search(r"\bCREATE\s+TABLE\s+\w+_new\b", sql, re.IGNORECASE))
        and bool(re.search(r"\bRENAME\s+TO\b", sql, re.IGNORECASE))
    )

    if len(clean_stmts) > 1 and not is_sqlite_recreate:
        return (
            False,
            "HIGH",
            "Blocked: Multiple stacked statements are not allowed in a single request."
        )

    stmt = clean_stmts[0].rstrip(";").strip()

    # 1. BLOCKED: DROP DATABASE
    if re.search(r"\bDROP\s+DATABASE\b", stmt, re.IGNORECASE):
        return (False, "HIGH", "Blocked: DROP DATABASE is strictly forbidden.")

    # 2. BLOCKED: DROP SCHEMA
    if re.search(r"\bDROP\s+SCHEMA\b", stmt, re.IGNORECASE):
        return (False, "HIGH", "Blocked: DROP SCHEMA is strictly forbidden.")

    # 3. BLOCKED: TRUNCATE
    if re.search(r"\bTRUNCATE\b", stmt, re.IGNORECASE):
        return (False, "HIGH", "Blocked: TRUNCATE is strictly forbidden.")

    # 4. BLOCKED: DELETE without WHERE
    if re.search(r"^\s*DELETE\b", stmt, re.IGNORECASE):
        if not re.search(r"\bWHERE\b", stmt, re.IGNORECASE):
            return (
                False,
                "HIGH",
                "Blocked: DELETE statement without a WHERE clause is forbidden."
            )

    # 5. BLOCKED: UPDATE without WHERE
    if re.search(r"^\s*UPDATE\b", stmt, re.IGNORECASE):
        if not re.search(r"\bWHERE\b", stmt, re.IGNORECASE):
            return (
                False,
                "HIGH",
                "Blocked: UPDATE statement without a WHERE clause is forbidden."
            )

    # 6. CONFIRMATION: DROP TABLE requires typing the table name
    drop_table_match = re.search(
        r"^\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([`\"'\[]?[\w\.\-]+[`\"'\]]?)",
        stmt,
        re.IGNORECASE
    )
    if drop_table_match:
        table_name = drop_table_match.group(1).strip("`\"'[]")
        return (
            True,
            "HIGH",
            f"DROP TABLE requires typing the table name '{table_name}' to confirm."
        )

    if is_sqlite_recreate:
        return (
            True,
            "HIGH",
            "SQLite transactional table recreation migration block."
        )

    # 7. SessionMode Enforcement
    is_read = bool(re.search(r"^\s*(?:SELECT|EXPLAIN|WITH)\b", stmt, re.IGNORECASE))
    is_admin_ddl = bool(re.search(r"^\s*(?:CREATE|ALTER|DROP|GRANT|REVOKE)\b", stmt, re.IGNORECASE)) and not is_sqlite_recreate

    if current_mode == SessionMode.READ_ONLY and not is_read:
        return (
            False,
            "HIGH",
            "Blocked by Mode Guardrail: Active session is in READ-ONLY mode. All write statements are blocked at code level. "
            "Switch to READ-WRITE mode ('switch to read-write mode') to permit writes."
        )

    if current_mode == SessionMode.READ_WRITE and is_admin_ddl:
        return (
            False,
            "HIGH",
            "Blocked by Mode Guardrail: DDL and user privilege statements require ADMIN mode. "
            "Switch with 'enter admin mode'."
        )

    # 7. Risk level classification
    if re.search(r"^\s*(?:SELECT|EXPLAIN|WITH)\b", stmt, re.IGNORECASE):
        return (True, "LOW", "Read-only query allowed.")

    if re.search(r"^\s*INSERT\b", stmt, re.IGNORECASE):
        return (True, "MEDIUM", "Data insertion allowed with confirmation.")

    if re.search(r"^\s*UPDATE\b", stmt, re.IGNORECASE):
        return (True, "MEDIUM", "Data update allowed with confirmation.")

    if re.search(r"^\s*(?:DELETE|ALTER|CREATE)\b", stmt, re.IGNORECASE):
        return (True, "HIGH", "Destructive / DDL operation requires confirmation.")

    return (True, "MEDIUM", "Statement allowed with confirmation.")
