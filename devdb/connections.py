"""Connection profile manager for DevDB Assistant (Phase 11).

Manages saved profiles in ~/.devdb/profiles.json.
Security Guarantee: Passwords are NEVER written to disk.
"""

import os
import re
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit, quote_plus, unquote_plus
from dotenv import load_dotenv

# Load local .env if present
load_dotenv()

PROFILES_DIR = Path.home() / ".devdb"
PROFILES_FILE = PROFILES_DIR / "profiles.json"


@dataclass
class ConnectionProfile:
    name: str
    type: str  # "sqlite", "postgres", "mongo_local", "mongo_atlas"
    url: str  # Password-less connection string
    environment: str = "dev"  # "dev" | "sandbox"
    default_mode: str = "READ-WRITE"  # "READ-ONLY" | "READ-WRITE" | "ADMIN"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConnectionProfile":
        return cls(
            name=data.get("name", "unnamed"),
            type=data.get("type", "sqlite"),
            url=data.get("url", ""),
            environment=data.get("environment", "dev"),
            default_mode=data.get("default_mode", "READ-WRITE"),
        )


def sanitize_url_for_storage(url: str) -> Tuple[str, Optional[str]]:
    """
    Remove password from URL before persisting to disk.
    Returns: (password_less_url, extracted_password)
    """
    if not url:
        return "", None

    # Handle SQLite paths directly
    if url.startswith("sqlite") or url.endswith((".db", ".sqlite", ".sqlite3")):
        return url, None

    if "://" not in url:
        return url, None

    scheme, rest = url.split("://", 1)

    # Authority is up to the first '/' or '?'
    path_start = len(rest)
    for sep in ("/", "?"):
        idx = rest.find(sep)
        if idx != -1 and idx < path_start:
            path_start = idx

    authority = rest[:path_start]
    path_and_query = rest[path_start:]

    if "@" in authority:
        userinfo, hostinfo = authority.rsplit("@", 1)
        if ":" in userinfo:
            user, pwd = userinfo.split(":", 1)
            clean_authority = f"{user}@{hostinfo}"
            clean_url = f"{scheme}://{clean_authority}{path_and_query}"
            return clean_url, pwd

    return url, None


def detect_connection_type(url: str) -> str:
    """Classify URL into sqlite, postgres, mongo_local, or mongo_atlas."""
    u = (url or "").strip().lower()
    if u.startswith("sqlite") or u.endswith((".db", ".sqlite", ".sqlite3")) or u == ":memory:":
        return "sqlite"
    if u.startswith("postgres"):
        return "postgres"
    if u.startswith("mongodb+srv://") or ".mongodb.net" in u:
        return "mongo_atlas"
    if u.startswith("mongodb://"):
        if "localhost" in u or "127.0.0.1" in u:
            return "mongo_local"
        return "mongo_atlas"
    return "sqlite"


def get_default_mode_for_type(conn_type: str) -> str:
    """Atlas defaults to READ-ONLY; local defaults to READ-WRITE."""
    if conn_type == "mongo_atlas":
        return "READ-ONLY"
    return "READ-WRITE"


class ConnectionManager:
    """Manages CRUD operations on saved connection profiles."""

    def __init__(self, profiles_file: Path = PROFILES_FILE):
        self.profiles_file = profiles_file

    @property
    def profiles_path(self) -> Path:
        return self.profiles_file

    def _ensure_dir(self) -> None:
        self.profiles_file.parent.mkdir(parents=True, exist_ok=True)

    def load_profiles(self) -> Dict[str, ConnectionProfile]:
        if not self.profiles_file.exists():
            return {}
        try:
            with open(self.profiles_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return {name: ConnectionProfile.from_dict(p) for name, p in raw.items()}
        except Exception:
            return {}

    def save_profile(
        self,
        name: str,
        url: str,
        environment: str = "dev",
        default_mode: Optional[str] = None,
        conn_type: Optional[str] = None,
        profile_type: Optional[str] = None,
    ) -> ConnectionProfile:
        """
        Save a profile ensuring NO password is written to profiles.json.
        """
        clean_url, _ = sanitize_url_for_storage(url)
        c_type = conn_type or profile_type or detect_connection_type(clean_url)
        mode = default_mode or get_default_mode_for_type(c_type)
        env_label = "sandbox" if environment.lower() == "sandbox" else "dev"

        profile = ConnectionProfile(
            name=name.strip(),
            type=c_type,
            url=clean_url.strip(),
            environment=env_label,
            default_mode=mode,
        )

        self._ensure_dir()
        profiles = self.load_profiles()
        profiles[profile.name] = profile

        # Ensure no passwords leaked in serialized json
        serialized = {}
        for p_name, p_obj in profiles.items():
            safe_dict = p_obj.to_dict()
            safe_dict["url"], _ = sanitize_url_for_storage(safe_dict["url"])
            serialized[p_name] = safe_dict

        with open(self.profiles_file, "w", encoding="utf-8") as f:
            json.dump(serialized, f, indent=2)

        return profile

    def delete_profile(self, name: str) -> bool:
        profiles = self.load_profiles()
        if name in profiles:
            del profiles[name]
            self._ensure_dir()
            with open(self.profiles_file, "w", encoding="utf-8") as f:
                json.dump({k: v.to_dict() for k, v in profiles.items()}, f, indent=2)
            return True
        return False

    def get_profile(self, name: str) -> Optional[ConnectionProfile]:
        profiles = self.load_profiles()
        return profiles.get(name)

    def resolve_password_for_profile(self, profile: ConnectionProfile) -> Optional[str]:
        """
        Look up password in environment variables:
        1. <PROFILE_NAME>_PASSWORD
        2. ATLAS_PASSWORD (if Atlas)
        3. DEVDB_PASSWORD
        """
        name_clean = re.sub(r"[^A-Za-z0-9_]", "_", profile.name).upper()
        candidates = [
            f"{name_clean}_PASSWORD",
            f"DEVDB_{name_clean}_PASSWORD",
        ]
        if profile.type == "mongo_atlas":
            candidates.extend(["ATLAS_PASSWORD", "MONGODB_ATLAS_PASSWORD"])
        elif profile.type == "postgres":
            candidates.extend(["POSTGRES_PASSWORD", "PGPASSWORD"])

        for env_var in candidates:
            val = os.getenv(env_var)
            if val:
                return val
        return None

    def build_active_url(self, profile: ConnectionProfile, user_password: Optional[str] = None) -> str:
        """
        Inject password into URL if needed for connection.
        Password comes from environment or direct input.
        """
        url = profile.url
        pwd = user_password or self.resolve_password_for_profile(profile)

        if not pwd or "@" not in url:
            return url

        # Insert password into user@
        match = re.search(r"://([^@]+)@", url)
        if match:
            user = match.group(1)
            # If user already contains :, password was already in URL
            if ":" not in user:
                encoded_pwd = quote_plus(pwd)
                return url.replace(f"://{user}@", f"://{user}:{encoded_pwd}@")

        return url

    def resolve_url_with_password(self, profile_or_name: Any, user_password: Optional[str] = None) -> str:
        """Resolve full connection URL with credentials injected if found."""
        if isinstance(profile_or_name, str):
            prof = self.get_profile(profile_or_name)
            if not prof:
                return profile_or_name
        else:
            prof = profile_or_name
        return self.build_active_url(prof, user_password=user_password)


# Global singleton instance
default_connection_manager = ConnectionManager()

