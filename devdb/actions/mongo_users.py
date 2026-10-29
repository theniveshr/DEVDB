"""MongoDB User and Role Management Action.

Executes native MongoDB security commands:
- createUser, grantRolesToUser, revokeRolesFromUser, usersInfo, dropUser
- Default roles permitted: read, readWrite
- Restricts elevated roles (root, dbOwner, userAdmin)
- Generates strong 16-character passwords and prints once
"""

import re
import secrets
import string
from typing import Any, Dict, List, Optional, Tuple
import pymongo
from rich.table import Table

BLOCKED_ELEVATED_ROLES = {"root", "dbowner", "useradmin"}
DEFAULT_SAFE_ROLES = {"read", "readwrite"}


def generate_mongo_password(length: int = 16) -> str:
    """Generate a high-entropy password containing letters, digits, and safe punctuation."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pwd)
                and any(c.isupper() for c in pwd)
                and any(c.isdigit() for c in pwd)
                and any(c in "!@#$%^&*" for c in pwd)):
            return pwd


def validate_mongo_username(username: str) -> str:
    """Ensure username consists only of alphanumeric characters and underscores."""
    u = username.strip()
    if not u or not re.match(r"^[a-zA-Z0-9_]+$", u):
        raise ValueError(
            f"Invalid MongoDB username '{username}'. Only alphanumeric characters and underscores are allowed."
        )
    return u


def normalize_mongo_roles(raw_roles: Any, db_name: str) -> List[Dict[str, str]]:
    """Convert input role string or list into MongoDB role object list."""
    if not raw_roles:
        return [{"role": "read", "db": db_name}]

    roles_list = []
    if isinstance(raw_roles, str):
        parts = [r.strip() for r in raw_roles.split(",") if r.strip()]
        for p in parts:
            p_lower = p.lower()
            role_name = "readWrite" if p_lower in ("readwrite", "read_write", "write") else "read"
            roles_list.append({"role": role_name, "db": db_name})
    elif isinstance(raw_roles, list):
        for item in raw_roles:
            if isinstance(item, dict):
                roles_list.append(item)
            elif isinstance(item, str):
                role_name = "readWrite" if item.lower() in ("readwrite", "write") else "read"
                roles_list.append({"role": role_name, "db": db_name})

    return roles_list or [{"role": "read", "db": db_name}]


def create_mongo_user(
    db: pymongo.database.Database,
    username: str,
    roles: Optional[Any] = None,
    password: Optional[str] = None,
    allow_elevated: bool = False,
) -> Dict[str, Any]:
    """Create a database user with assigned roles."""
    clean_user = validate_mongo_username(username)
    formatted_roles = normalize_mongo_roles(roles, db.name)

    # Elevated role check
    for r in formatted_roles:
        r_name = r.get("role", "").lower()
        if r_name in BLOCKED_ELEVATED_ROLES and not allow_elevated:
            raise PermissionError(
                f"Role '{r_name}' is an administrative role and requires explicit administrator confirmation."
            )

    pwd = password or generate_mongo_password(16)
    db.command("createUser", clean_user, pwd=pwd, roles=formatted_roles)

    return {
        "success": True,
        "operation": "create_user",
        "username": clean_user,
        "roles": formatted_roles,
        "database": db.name,
        "generated_password": pwd,  # To display ONCE in terminal
    }


def list_mongo_users(db: pymongo.database.Database) -> List[Dict[str, Any]]:
    """Retrieve all users configured for the current database."""
    res = db.command("usersInfo", 1)
    users = res.get("users", [])
    output = []
    for u in users:
        output.append({
            "user": u.get("user"),
            "db": u.get("db"),
            "roles": [f"{r.get('role')}@{r.get('db')}" for r in u.get("roles", [])]
        })
    return output


def grant_mongo_role(
    db: pymongo.database.Database,
    username: str,
    roles: Any,
) -> Dict[str, Any]:
    """Grant additional roles to an existing user."""
    clean_user = validate_mongo_username(username)
    formatted_roles = normalize_mongo_roles(roles, db.name)
    db.command("grantRolesToUser", clean_user, roles=formatted_roles)
    return {
        "success": True,
        "operation": "grant_role",
        "username": clean_user,
        "granted_roles": formatted_roles,
    }


def revoke_mongo_role(
    db: pymongo.database.Database,
    username: str,
    roles: Any,
) -> Dict[str, Any]:
    """Revoke specific roles from a user."""
    clean_user = validate_mongo_username(username)
    formatted_roles = normalize_mongo_roles(roles, db.name)
    db.command("revokeRolesFromUser", clean_user, roles=formatted_roles)
    return {
        "success": True,
        "operation": "revoke_role",
        "username": clean_user,
        "revoked_roles": formatted_roles,
    }


def drop_mongo_user(db: pymongo.database.Database, username: str) -> Dict[str, Any]:
    """Safely drop a database user."""
    clean_user = validate_mongo_username(username)
    db.command("dropUser", clean_user)
    return {
        "success": True,
        "operation": "drop_user",
        "username": clean_user,
    }


def handle_mongo_user_op(db: pymongo.database.Database, op: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch user management operations from CLI."""
    if not isinstance(op, dict):
        op = {"operation": "list_users"}
    action = str(op.get("operation") or op.get("action") or "").lower()

    if action == "create_user":
        return create_mongo_user(
            db=db,
            username=op.get("username", "analyst_ro"),
            roles=op.get("roles") or op.get("privileges", "read"),
            password=op.get("password"),
        )
    elif action in ("list_users", "list"):
        users = list_mongo_users(db)
        return {
            "success": True,
            "operation": "list_users",
            "count": len(users),
            "users": users,
        }
    elif action in ("grant_role", "grant"):
        return grant_mongo_role(
            db=db,
            username=op.get("username"),
            roles=op.get("roles") or op.get("privileges", "read"),
        )
    elif action in ("revoke_role", "revoke"):
        return revoke_mongo_role(
            db=db,
            username=op.get("username"),
            roles=op.get("roles") or op.get("privileges", "read"),
        )
    elif action == "drop_user":
        return drop_mongo_user(
            db=db,
            username=op.get("username"),
        )

    raise ValueError(f"Unknown user management operation: {action}")
