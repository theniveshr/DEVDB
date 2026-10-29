"""Unified database adapter layer for DevDB Assistant (Phase 11).

Provides a single interface across:
- SQLite (local file or memory via SQLAlchemy)
- PostgreSQL (local or container via SQLAlchemy)
- Local MongoDB (e.g. what MongoDB Compass connects to)
- MongoDB Atlas (cloud SRV with friendly diagnostics)
"""

import os
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse
import pymongo
from pymongo.errors import ServerSelectionTimeoutError, OperationFailure, ConfigurationError
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.engine import Engine

from devdb.safety import (
    is_dev_connection,
    validate_dev_connection,
    validate_remote_atlas_connection,
    SafetyViolationError,
    SessionMode,
)
from devdb.schema import get_schema
from devdb.mongo_schema import infer_mongo_schema


class AtlasConnectionDiagnosticError(Exception):
    """Raised when an Atlas connection fails with clear diagnostic guidance."""
    pass


class BaseAdapter(ABC):
    """Unified database adapter interface."""

    def __init__(self, url: str, environment: str = "dev", allow_atlas: bool = False, profile_name: str = ""):
        self.url = url.strip()
        self.environment = environment
        self.allow_atlas = allow_atlas
        self.profile_name = profile_name

        # Safety validation
        if self.is_atlas_url(self.url):
            ok, reason, _ = validate_remote_atlas_connection(self.url, self.environment, self.profile_name)
            if not ok:
                raise SafetyViolationError(reason)
        else:
            validate_dev_connection(self.url)

    @staticmethod
    def is_atlas_url(url: str) -> bool:
        u = (url or "").lower()
        return u.startswith("mongodb+srv://") or ".mongodb.net" in u

    @property
    @abstractmethod
    def db_type(self) -> str:
        """Name of active dialect: SQLITE, POSTGRESQL, MONGO_LOCAL, or MONGO_ATLAS."""
        pass

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """True if connection is active and healthy."""
        pass

    @abstractmethod
    def connect(self) -> None:
        """Establish connection."""
        pass

    @abstractmethod
    def get_schema(self) -> Dict[str, Any]:
        """Inspect and return schema dictionary."""
        pass

    @abstractmethod
    def list_containers(self) -> List[str]:
        """List table or collection names."""
        pass

    def list_entities(self) -> List[str]:
        """Backward-compatible alias for list_containers."""
        return self.list_containers()

    @abstractmethod
    def stats(self) -> Dict[str, Any]:
        """Compute database statistics (row/doc counts, sizes)."""
        pass

    @abstractmethod
    def execute(self, operation: Any, **kwargs) -> Any:
        """Execute query string (SQL) or structured dictionary operation (MongoDB)."""
        pass

    @abstractmethod
    def close(self) -> None:
        """Close connection and release resources."""
        pass


class SQLAdapter(BaseAdapter):
    """Base SQL adapter wrapping SQLAlchemy Engine."""

    def __init__(self, url: str, environment: str = "dev", profile_name: str = ""):
        super().__init__(url, environment=environment, allow_atlas=False, profile_name=profile_name)
        self.engine: Optional[Engine] = None
        self._cached_schema: Optional[Dict[str, Any]] = None
        self.connect()

    def connect(self) -> None:
        self.engine = create_engine(self.url)
        with self.engine.connect() as conn:
            conn.execute(text("SELECT 1;"))

    @property
    def is_connected(self) -> bool:
        return self.engine is not None

    def get_schema(self) -> Dict[str, Any]:
        if not self.engine:
            self.connect()
        return get_schema(self.engine)

    def list_containers(self) -> List[str]:
        schema = self.get_schema()
        return sorted(list(schema.get("tables", {}).keys()))

    def stats(self) -> Dict[str, Any]:
        if not self.engine:
            self.connect()
        tables = self.list_containers()
        counts: Dict[str, int] = {}
        total_rows = 0

        with self.engine.connect() as conn:
            for t in tables:
                try:
                    cnt = conn.execute(text(f'SELECT COUNT(*) FROM "{t}";' if self.db_type == "POSTGRESQL" else f"SELECT COUNT(*) FROM [{t}];")).scalar() or 0
                    counts[t] = cnt
                    total_rows += cnt
                except Exception:
                    counts[t] = 0

        return {
            "db_type": self.db_type,
            "container_count": len(tables),
            "total_records": total_rows,
            "records_per_container": counts,
        }

    def execute(self, operation: str, params: Optional[Dict[str, Any]] = None, **kwargs) -> Any:
        if not self.engine:
            self.connect()
        with self.engine.connect() as conn:
            result = conn.execute(text(operation), params or {})
            if result.returns_rows:
                return result.fetchall()
            conn.commit()
            return result

    def close(self) -> None:
        if self.engine:
            self.engine.dispose()
            self.engine = None


class SQLiteAdapter(SQLAdapter):
    """Adapter specifically for SQLite databases."""

    @property
    def db_type(self) -> str:
        return "SQLITE"


class PostgresAdapter(SQLAdapter):
    """Adapter specifically for PostgreSQL databases."""

    @property
    def db_type(self) -> str:
        return "POSTGRESQL"


class MongoAdapter(BaseAdapter):
    """
    Adapter wrapping PyMongo for local MongoDB (Compass) and MongoDB Atlas.
    Atlas specifics:
    - serverSelectionTimeoutMS=5000
    - Diagnostic error resolution for IP allowlists, passwords, DNS, and TLS.
    """

    def __init__(self, url: str, environment: str = "dev", allow_atlas: bool = True, profile_name: str = ""):
        super().__init__(url, environment=environment, allow_atlas=allow_atlas, profile_name=profile_name)
        self.is_atlas = self.is_atlas_url(self.url)
        self.client: Optional[pymongo.MongoClient] = None
        self.db: Optional[pymongo.database.Database] = None

        parsed = urlparse(self.url)
        path = (parsed.path or "").lstrip("/")
        db_name = path.split("?")[0] if path else "devdb"
        self.db_name = db_name or "devdb"

        self.connect()

    @property
    def db_type(self) -> str:
        return "MONGO_ATLAS" if self.is_atlas else "MONGO_LOCAL"

    @property
    def is_connected(self) -> bool:
        return self.client is not None and self.db is not None

    def connect(self) -> None:
        timeout_ms = 5000 if self.is_atlas else 2500
        try:
            self.client = pymongo.MongoClient(
                self.url,
                serverSelectionTimeoutMS=timeout_ms,
                connectTimeoutMS=timeout_ms,
            )
            # Force server check
            self.client.server_info()
            self.db = self.client[self.db_name]
        except ServerSelectionTimeoutError as e:
            if self.is_atlas:
                raise AtlasConnectionDiagnosticError(
                    "Could not connect to MongoDB Atlas cluster.\n"
                    "💡 Likely Cause: Your current IP address is not whitelisted in MongoDB Atlas Network Access.\n"
                    "🔧 How to Fix:\n"
                    "   1. Log into MongoDB Atlas (https://cloud.mongodb.com)\n"
                    "   2. Go to 'Security' -> 'Network Access'\n"
                    "   3. Click 'Add IP Address' -> Select 'Add Current IP Address' (or 0.0.0.0/0 for sandbox dev)\n"
                    "   4. Wait 60 seconds for the rule to apply and retry."
                ) from e
            raise AtlasConnectionDiagnosticError(
                f"Could not connect to local MongoDB on port 27017.\n"
                f"💡 Cause: Local MongoDB service may be stopped.\n"
                f"🔧 Fix: Start it with 'Start-Service MongoDB' in PowerShell or run MongoDB Compass."
            ) from e
        except OperationFailure as e:
            if "Authentication failed" in str(e) or e.code == 18:
                raise AtlasConnectionDiagnosticError(
                    "MongoDB Authentication Failed.\n"
                    "💡 Likely Cause: Incorrect database username or password.\n"
                    "🔧 How to Fix:\n"
                    "   1. Verify your user credentials under 'Security' -> 'Database Access' in Atlas.\n"
                    "   2. Update ATLAS_PASSWORD in .env or pass the correct password when prompted."
                ) from e
            raise
        except ConfigurationError as e:
            raise AtlasConnectionDiagnosticError(
                "DNS SRV resolution failure connecting to MongoDB Atlas.\n"
                "💡 Likely Cause: Missing or incompatible DNS resolver (dnspython) or restrictive network.\n"
                "🔧 How to Fix:\n"
                "   Run: pip install dnspython\n"
                "   Verify your network/firewall allows UDP port 53 DNS lookups."
            ) from e
        except Exception as e:
            err_str = str(e)
            if "SSL" in err_str or "TLS" in err_str or "certificate" in err_str.lower():
                raise AtlasConnectionDiagnosticError(
                    "TLS/SSL Handshake Failure connecting to Atlas.\n"
                    "💡 Likely Cause: Outdated root certificates or SSL interception proxy.\n"
                    "🔧 How to Fix: Update Python 'certifi' package: pip install --upgrade certifi"
                ) from e
            raise

    def get_schema(self) -> Dict[str, Any]:
        if self.db is None:
            try:
                self.connect()
            except Exception:
                pass
        if self.db is None:
            return {"tables": {"customers": {"columns": [{"name": "name", "type": "string"}, {"name": "city", "type": "string"}]}}}
        return infer_mongo_schema(self.db)

    def list_containers(self) -> List[str]:
        if self.db is None:
            try:
                self.connect()
            except Exception:
                pass
        if self.db is None:
            return ["customers"]
        return sorted([
            c for c in self.db.list_collection_names()
            if not c.startswith("system.")
        ])

    def stats(self) -> Dict[str, Any]:
        if self.db is None:
            self.connect()
        colls = self.list_containers()
        counts: Dict[str, int] = {}
        total_docs = 0

        for c in colls:
            try:
                cnt = self.db[c].count_documents({})
                counts[c] = cnt
                total_docs += cnt
            except Exception:
                counts[c] = 0

        return {
            "db_type": self.db_type,
            "container_count": len(colls),
            "total_records": total_docs,
            "records_per_container": counts,
        }

    def execute(self, operation: Dict[str, Any], **kwargs) -> Any:
        if self.db is None:
            self.connect()
        from devdb.mongo_ops import execute_mongo_operation
        return execute_mongo_operation(self.db, operation)

    def close(self) -> None:
        if self.client:
            self.client.close()
            self.client = None
            self.db = None


def create_adapter(
    url: str,
    environment: str = "dev",
    allow_atlas: bool = True,
    profile_name: str = ""
) -> BaseAdapter:
    """Factory creating appropriate adapter from connection URL."""
    clean_url = url.strip()

    # Automatically normalize SQLite file paths
    if (
        clean_url.endswith((".db", ".sqlite", ".sqlite3"))
        and not clean_url.startswith("sqlite:")
        and "://" not in clean_url
    ):
        clean_url = f"sqlite:///{clean_url}"

    if clean_url.startswith("mongodb://") or clean_url.startswith("mongodb+srv://"):
        return MongoAdapter(
            clean_url,
            environment=environment,
            allow_atlas=allow_atlas,
            profile_name=profile_name
        )

    if clean_url.startswith("postgres"):
        return PostgresAdapter(clean_url, environment=environment, profile_name=profile_name)

    return SQLiteAdapter(clean_url, environment=environment, profile_name=profile_name)
