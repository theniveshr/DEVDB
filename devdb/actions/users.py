"""User and permission management action for DevDB (PostgreSQL only).

Supports:
1. create_user: Create user/role with password, grant initial privileges (read-only, read-write, or table-specific)
2. grant: Grant privileges on specific tables or public schema
3. revoke: Revoke privileges
4. list_users: Query pg_roles and information_schema.role_table_grants, render Rich table
5. drop_user: Drop user/role safely (reassigning/dropping owned objects)

Safety Rules:
- SQLite and non-PostgreSQL databases receive polite notice:
  "SQLite is a serverless, file-based database and does not support database users, passwords, or GRANT/REVOKE permissions. Access control is managed at the filesystem level."
- Safe Identifier Quoting: Code constructs all SQL itself with double-quoted identifiers. Raw SQL from LLM is NEVER executed for user operations.
- Validation: Usernames strictly validated (^[a-zA-Z0-9_]+$, 1-63 chars). Table names must exist in active schema.
- Guardrails: Creating superusers or granting blanket ALL requires typing confirmation keyword: "CONFIRM_SUPERUSER" or "CONFIRM_GRANT_ALL".
- Strong Random Passwords: Generated via secrets module if none given, printed ONCE in highlighted panel, never saved in logs.
- Preview then confirmation: Preview panel with masked password, then [y/N].
"""

import re
import secrets
import string
from typing import Any, Dict, List, Optional, Set, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from sqlalchemy import text

console = Console()

SQLITE_UNSUPPORTED_MESSAGE = (
    "SQLite is a serverless, file-based database and does not support database users, "
    "passwords, or GRANT/REVOKE permissions. Access control is managed at the filesystem level."
)

ALLOWED_PRIVILEGES: Set[str] = {
    "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "ALL", "SUPERUSER"
}

CONFIRMATION_SUPERUSER_KEYWORD = "CONFIRM_SUPERUSER"
CONFIRMATION_GRANT_ALL_KEYWORD = "CONFIRM_GRANT_ALL"


class UserManagementError(ValueError):
    """Exception raised for user management validation or execution errors."""
    pass


# ==============================================================================
# 1. VALIDATION AND SAFE IDENTIFIER QUOTING
# ==============================================================================

def validate_username(username: str) -> str:
    """
    Validate that username consists only of letters, digits, and underscores,
    and adheres to PostgreSQL identifier length limits (1-63 chars).
    """
    clean = (username or "").strip()
    if not clean:
        raise UserManagementError("Username cannot be empty.")
    if not re.match(r"^[a-zA-Z0-9_]{1,63}$", clean):
        raise UserManagementError(
            f"Invalid username '{username}'. Usernames must contain only letters, numbers, "
            f"and underscores (1 to 63 characters max)."
        )
    return clean


def quote_identifier(ident: str) -> str:
    """Safely quote a SQL identifier using PostgreSQL double quotes."""
    clean = ident.replace('"', '""')
    return f'"{clean}"'


def quote_literal(val: str) -> str:
    """Safely quote a SQL literal value using single quotes."""
    clean = val.replace("'", "''")
    return f"'{clean}'"


def mask_password_in_sql(sql: str) -> str:
    """Mask any password literal in SQL statements so it is safe to log."""
    return re.sub(r"PASSWORD\s+'[^']*'", "PASSWORD '[REDACTED]'", sql, flags=re.IGNORECASE)


def generate_strong_password(length: int = 16) -> str:
    """
    Generate a cryptographically secure random password containing uppercase,
    lowercase, digits, and safe special characters.
    """
    specials = "!@#$%^&*_-+"
    alphabet = string.ascii_letters + string.digits + specials

    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pwd)
            and any(c.isupper() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in specials for c in pwd)
        ):
            return pwd


def parse_privileges(raw_privs: Any) -> List[str]:
    """Normalize and validate privilege names."""
    if isinstance(raw_privs, str):
        raw_list = [p.strip() for p in raw_privs.split(",") if p.strip()]
    elif isinstance(raw_privs, (list, tuple, set)):
        raw_list = [str(p).strip() for p in raw_privs if str(p).strip()]
    else:
        raw_list = ["SELECT"]

    normalized: List[str] = []
    for item in raw_list:
        up = item.upper().replace("-", "_")
        if up in ("READ_ONLY", "READONLY", "READ", "RO"):
            if "SELECT" not in normalized:
                normalized.append("SELECT")
        elif up in ("READ_WRITE", "READWRITE", "WRITE", "RW"):
            for p in ["SELECT", "INSERT", "UPDATE", "DELETE"]:
                if p not in normalized:
                    normalized.append(p)
        elif up in ALLOWED_PRIVILEGES:
            if up not in normalized:
                normalized.append(up)
        else:
            raise UserManagementError(f"Unsupported privilege '{item}'. Allowed: {', '.join(sorted(ALLOWED_PRIVILEGES))}")

    return normalized or ["SELECT"]


def parse_tables(raw_tables: Any, schema: Optional[Dict[str, Any]] = None) -> List[str]:
    """Validate table names against active schema."""
    if isinstance(raw_tables, str):
        clean = raw_tables.strip()
        if clean.upper() in ("ALL", "*", "PUBLIC", "ALL TABLES", "ENTIRE DATABASE"):
            return ["ALL"]
        tbl_list = [t.strip().strip("'\"") for t in clean.split(",") if t.strip()]
    elif isinstance(raw_tables, (list, tuple, set)):
        tbl_list = [str(t).strip().strip("'\"") for t in raw_tables if str(t).strip()]
    else:
        tbl_list = []

    if not tbl_list:
        return ["ALL"]

    if any(t.upper() in ("ALL", "*", "PUBLIC", "ALL TABLES") for t in tbl_list):
        return ["ALL"]

    if schema and "tables" in schema:
        known_tables = {t.lower(): t for t in schema["tables"].keys()}
        resolved: List[str] = []
        for t in tbl_list:
            t_low = t.lower()
            if t_low not in known_tables:
                available = ", ".join(schema["tables"].keys())
                raise UserManagementError(f"Unknown table '{t}' does not exist in schema. Available tables: {available}")
            resolved.append(known_tables[t_low])
        return resolved

    return tbl_list


# ==============================================================================
# 2. SQL GENERATION (Code builds all SQL with safe identifier quoting)
# ==============================================================================

def build_user_management_sql(
    spec: Dict[str, Any],
    schema: Optional[Dict[str, Any]] = None,
) -> Tuple[List[str], Optional[str], str, bool]:
    """
    Build safe PostgreSQL user management statements from structured specification.
    Returns: (statements, generated_password, explanation, requires_explicit_confirmation)
    """
    op = (spec.get("operation") or spec.get("action") or "create_user").lower().strip()
    username = validate_username(spec.get("username", "")) if op != "list" else ""
    privileges = parse_privileges(spec.get("privileges", ["SELECT"])) if op in ("create_user", "grant", "revoke") else []
    tables = parse_tables(spec.get("tables", []), schema=schema) if op in ("create_user", "grant", "revoke") else []

    is_superuser = bool(spec.get("is_superuser", False)) or "SUPERUSER" in privileges
    generated_pwd: Optional[str] = None
    statements: List[str] = []
    requires_confirmation = False

    # Guardrail: Check for Superuser or Blanket ALL on whole database
    if is_superuser or (op == "grant" and "ALL" in privileges and tables == ["ALL"]):
        requires_confirmation = True

    quoted_user = quote_identifier(username) if username else ""

    if op in ("create_user", "create"):
        # Password handling
        raw_pwd = spec.get("password")
        if not raw_pwd:
            generated_pwd = generate_strong_password()
            pwd_to_use = generated_pwd
        else:
            pwd_to_use = str(raw_pwd).strip()

        super_clause = " SUPERUSER" if is_superuser else ""
        create_stmt = f"CREATE USER {quoted_user} WITH PASSWORD {quote_literal(pwd_to_use)}{super_clause};"
        statements.append(create_stmt)
        statements.append(f"GRANT USAGE ON SCHEMA public TO {quoted_user};")

        # Initial privileges
        privs_to_grant = [p for p in privileges if p != "SUPERUSER"]
        if privs_to_grant:
            privs_str = ", ".join(privs_to_grant)
            if tables == ["ALL"]:
                statements.append(f"GRANT {privs_str} ON ALL TABLES IN SCHEMA public TO {quoted_user};")
                statements.append(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT {privs_str} ON TABLES TO {quoted_user};")
            else:
                for tbl in tables:
                    statements.append(f"GRANT {privs_str} ON TABLE {quote_identifier(tbl)} TO {quoted_user};")

        table_desc = "all tables" if tables == ["ALL"] else ", ".join(tables)
        explanation = (
            f"Creates PostgreSQL user '{username}' with a strong password, grants schema access, "
            f"and sets initial privileges ({', '.join(privileges)}) on {table_desc}."
        )

    elif op == "grant":
        privs_to_grant = [p for p in privileges if p != "SUPERUSER"]
        if not privs_to_grant:
            privs_to_grant = ["SELECT"]

        privs_str = ", ".join(privs_to_grant)
        statements.append(f"GRANT USAGE ON SCHEMA public TO {quoted_user};")

        if tables == ["ALL"]:
            statements.append(f"GRANT {privs_str} ON ALL TABLES IN SCHEMA public TO {quoted_user};")
            statements.append(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT {privs_str} ON TABLES TO {quoted_user};")
        else:
            for tbl in tables:
                statements.append(f"GRANT {privs_str} ON TABLE {quote_identifier(tbl)} TO {quoted_user};")

        table_desc = "all tables" if tables == ["ALL"] else ", ".join(tables)
        explanation = f"Grants privileges ({privs_str}) on {table_desc} to user '{username}'."

    elif op == "revoke":
        privs_str = ", ".join(privileges)
        if tables == ["ALL"]:
            statements.append(f"REVOKE {privs_str} ON ALL TABLES IN SCHEMA public FROM {quoted_user};")
        else:
            for tbl in tables:
                statements.append(f"REVOKE {privs_str} ON TABLE {quote_identifier(tbl)} FROM {quoted_user};")

        table_desc = "all tables" if tables == ["ALL"] else ", ".join(tables)
        explanation = f"Revokes privileges ({privs_str}) on {table_desc} from user '{username}'."

    elif op in ("drop_user", "drop"):
        statements.append(f"REASSIGN OWNED BY {quoted_user} TO CURRENT_USER;")
        statements.append(f"DROP OWNED BY {quoted_user};")
        statements.append(f"DROP ROLE {quoted_user};")
        explanation = f"Safely reassigns and drops objects owned by '{username}', then drops the user account."

    elif op in ("list", "list_users"):
        explanation = "Lists database users and their granted privileges from system catalogs."

    else:
        raise UserManagementError(f"Unknown user management operation '{op}'. Supported: create_user, grant, revoke, list, drop_user.")

    return statements, generated_pwd, explanation, requires_confirmation


# ==============================================================================
# 3. LIST USERS AND PERMISSIONS
# ==============================================================================

def list_pg_users(engine: Any) -> List[Dict[str, Any]]:
    """Query PostgreSQL roles and information_schema table grants."""
    query = """
    SELECT r.rolname AS username,
           r.rolsuper AS is_superuser,
           r.rolcanlogin AS can_login,
           COALESCE(
               string_agg(DISTINCT t.table_name || ' (' || t.privilege_type || ')', ', '),
               '(no table grants)'
           ) AS privileges
    FROM pg_roles r
    LEFT JOIN information_schema.role_table_grants t 
      ON r.rolname = t.grantee AND t.table_schema = 'public'
    WHERE r.rolname NOT LIKE 'pg_%'
    GROUP BY r.rolname, r.rolsuper, r.rolcanlogin
    ORDER BY r.rolname;
    """
    with engine.connect() as conn:
        res = conn.execute(text(query)).fetchall()
        users = []
        for row in res:
            users.append({
                "username": row[0],
                "is_superuser": bool(row[1]),
                "can_login": bool(row[2]),
                "privileges": row[3] or "(no table grants)",
            })
        return users


def render_users_table(users: List[Dict[str, Any]]) -> Table:
    """Render a Rich Table of database users and granted permissions."""
    table = Table(
        title="PostgreSQL Database Users & Privileges",
        show_lines=True,
        header_style="bold magenta",
        title_style="bold cyan"
    )
    table.add_column("Username", style="bold white", no_wrap=True)
    table.add_column("Can Login", justify="center")
    table.add_column("Superuser", justify="center")
    table.add_column("Public Table Privileges", style="cyan")

    for u in users:
        login_str = "[green]YES[/green]" if u["can_login"] else "[dim]NO[/dim]"
        super_str = "[bold red]YES[/bold red]" if u["is_superuser"] else "[dim]NO[/dim]"
        priv_str = u["privileges"]
        if priv_str == "(no table grants)":
            priv_str = "[dim](no table grants)[/dim]"
        table.add_row(u["username"], login_str, super_str, priv_str)

    return table


# ==============================================================================
# 4. NATURAL LANGUAGE SPEC PARSER
# ==============================================================================

def parse_user_request_nl(user_request: str, schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Parse a plain English request into structured JSON specification:
    {"operation": "...", "username": "...", "privileges": [...], "tables": [...]}
    Fast pattern extraction with fallback.
    """
    req_lower = user_request.lower().strip()

    # List users
    if any(k in req_lower for k in ["list user", "list users", "show user", "show users", "list all users", "get users"]):
        return {"operation": "list"}

    # Drop user
    drop_m = re.search(r"drop\s+(?:user|role)\s+([a-zA-Z0-9_]+)", user_request, re.IGNORECASE)
    if drop_m:
        return {"operation": "drop_user", "username": drop_m.group(1)}

    # Revoke
    rev_m = re.search(
        r"revoke\s+([\w,\s]+)\s+(?:on|for)\s+([\w,\s]+)\s+from\s+([a-zA-Z0-9_]+)",
        user_request,
        re.IGNORECASE
    )
    if rev_m:
        privs_raw = rev_m.group(1).strip()
        tables_raw = rev_m.group(2).strip()
        user_name = rev_m.group(3).strip()
        return {
            "operation": "revoke",
            "username": user_name,
            "privileges": parse_privileges([p.strip() for p in privs_raw.split(",") if p.strip()]),
            "tables": [t.strip() for t in tables_raw.split(",") if t.strip()]
        }

    # Grant
    grant_m = re.search(
        r"grant\s+(read-only|read-write|read_only|read_write|[\w,\s]+)\s+(?:on|for)\s+([\w,\s]+)\s+to\s+([a-zA-Z0-9_]+)",
        user_request,
        re.IGNORECASE
    )
    if grant_m:
        privs_raw = grant_m.group(1).strip()
        tables_raw = grant_m.group(2).strip()
        user_name = grant_m.group(3).strip()
        return {
            "operation": "grant",
            "username": user_name,
            "privileges": parse_privileges([privs_raw]),
            "tables": [t.strip() for t in tables_raw.split(",") if t.strip()]
        }

    # Create user: e.g. "create a read-only user report_user for orders"
    create_m = re.search(
        r"create\s+(?:a\s+)?(read-only|read-write|read_only|read_write|superuser)?\s*(?:user|role)\s+([a-zA-Z0-9_]+)(?:\s+(?:for|on|with\s+access\s+to)\s+([\w,\s]+))?",
        user_request,
        re.IGNORECASE
    )
    if create_m:
        role_type = (create_m.group(1) or "read-only").lower()
        user_name = create_m.group(2).strip()
        tables_raw = create_m.group(3)
        tables = [t.strip() for t in tables_raw.split(",") if t.strip()] if tables_raw else ["ALL"]
        is_super = "superuser" in role_type

        return {
            "operation": "create_user",
            "username": user_name,
            "privileges": parse_privileges([role_type]),
            "tables": tables,
            "is_superuser": is_super
        }

    # Fallback generic extraction
    words = user_request.split()
    target_user = "new_user"
    for w in words:
        if re.match(r"^[a-zA-Z0-9_]{3,30}$", w) and w.lower() not in ("create", "user", "role", "read", "only", "write", "for", "orders", "customers"):
            target_user = w
            break

    return {
        "operation": "create_user",
        "username": target_user,
        "privileges": ["SELECT"],
        "tables": ["ALL"]
    }


# ==============================================================================
# 5. EXECUTION & LOGGING
# ==============================================================================

def execute_user_statements(statements: List[str], engine: Any) -> bool:
    """Execute SQL statements inside a database transaction."""
    with engine.begin() as conn:
        for stmt in statements:
            clean = stmt.strip()
            if clean:
                conn.execute(text(clean))
    return True
