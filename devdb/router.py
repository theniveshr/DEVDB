"""Natural language intent router for DevDB Assistant (Phase 11).

Classifies user requests into 14 distinct intents:
explore, query, insert, fake_data, update, delete, schema,
relationships, users, data_io, copy, connection, help, unknown.
"""

import re
import json
import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from devdb.prompt_loader import load_prompt
from devdb.llm import LLMClient


VALID_INTENTS = {
    "explore",
    "query",
    "insert",
    "fake_data",
    "update",
    "delete",
    "schema",
    "relationships",
    "users",
    "data_io",
    "copy",
    "connection",
    "help",
    "unknown",
}


@dataclass
class IntentClassification:
    intent: str
    target_entity: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    clarification: str = ""
    reasoning: str = ""

    @property
    def suggested_clarification(self) -> Optional[str]:
        return self.clarification or None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent": self.intent,
            "target_entity": self.target_entity,
            "params": self.params,
            "clarification": self.clarification,
            "reasoning": self.reasoning,
        }


def get_closest_entities(target: str, existing: List[str], n: int = 3) -> List[str]:
    """Find closest matching table or collection names."""
    if not target or not existing:
        return []
    matches = difflib.get_close_matches(target.lower(), [e.lower() for e in existing], n=n, cutoff=0.4)
    # Map back to original casing
    lookup = {e.lower(): e for e in existing}
    return [lookup[m] for m in matches if m in lookup]


def classify_deterministic(request: str, existing_entities: Optional[List[str]] = None) -> Optional[IntentClassification]:
    """
    Fast-path deterministic classification for common explicit English commands.
    Returns IntentClassification if pattern matches with high confidence, else None.
    """
    req_clean = request.strip()
    r_lower = req_clean.lower()

    # 1. Capabilities / Help
    if any(p in r_lower for p in ["what can you do", "capabilities", "what are your features", "show capabilities", "help me"]):
        return IntentClassification(
            intent="help",
            reasoning="Matched capabilities/help request."
        )

    # 2. Connection: Mode changes
    mode_match = re.search(r"\b(?:switch\s+to|enter)\s+(read-only|read\s+only|read-write|read\s+write|admin)\s+mode\b", r_lower)
    if mode_match:
        raw_m = mode_match.group(1).replace(" ", "-").upper()
        if "READ-ONLY" in raw_m:
            target_m = "READ-ONLY"
        elif "READ-WRITE" in raw_m:
            target_m = "READ-WRITE"
        else:
            target_m = "ADMIN"
        return IntentClassification(
            intent="connection",
            params={"action": "mode", "target_mode": target_m},
            reasoning=f"Matched mode change request to {target_m}."
        )

    # 3. Connection: Save profile
    save_match = re.search(r"\bsave\s+(?:this\s+)?connection\s+as\s+([\w\-]+)(?:\s+(dev|sandbox))?\b", r_lower)
    if save_match:
        p_name = save_match.group(1)
        env = save_match.group(2) or "dev"
        return IntentClassification(
            intent="connection",
            params={"action": "save", "name": p_name, "environment": env},
            reasoning=f"Matched profile save request as '{p_name}'."
        )

    # 4. Connection: List / Switch / Current / Disconnect
    if any(p in r_lower for p in [
        "list my connections", "list my saved connections", "list saved connections",
        "list connections", "show connections", "saved profiles", "list profiles", "saved connections"
    ]):
        return IntentClassification(
            intent="connection",
            params={"action": "list"},
            reasoning="Matched list connections request."
        )

    if any(p in r_lower for p in ["which database am i connected to", "current connection", "connection status", "what database am i on"]):
        return IntentClassification(
            intent="connection",
            params={"action": "current"},
            reasoning="Matched current connection status inquiry."
        )

    if r_lower in ("disconnect", "disconnect database", "close connection"):
        return IntentClassification(
            intent="connection",
            params={"action": "disconnect"},
            reasoning="Matched disconnect request."
        )

    switch_match = re.search(r"\bswitch\s+to\s+([\w\-]+)\b", r_lower)
    if switch_match and "mode" not in r_lower:
        p_target = switch_match.group(1)
        return IntentClassification(
            intent="connection",
            params={"action": "switch", "profile": p_target},
            reasoning=f"Matched profile switch request to '{p_target}'."
        )

    # 5. Cross-database Copy
    copy_match = re.search(
        r"\bcopy\s+([\w\-]+)\s+from\s+([\w\-]+)\s+to\s+([\w\-]+)\b",
        r_lower
    )
    if copy_match:
        entity, src, dst = copy_match.group(1), copy_match.group(2), copy_match.group(3)
        return IntentClassification(
            intent="copy",
            target_entity=entity,
            params={"entity": entity, "source": src, "destination": dst},
            reasoning=f"Matched cross-database copy for entity '{entity}' from '{src}' to '{dst}'."
        )

    # 6. Data I/O: Export
    export_match = re.search(
        r"\bexport\s+([\w\-]+)\s+to\s+([\w\-]+(?:\.csv|\.json)?)\b",
        r_lower
    )
    if export_match:
        entity, dest = export_match.group(1), export_match.group(2)
        fmt = "json" if dest.endswith(".json") else "csv"
        return IntentClassification(
            intent="data_io",
            target_entity=entity,
            params={"action": "export", "entity": entity, "destination": dest, "format": fmt},
            reasoning=f"Matched data export for '{entity}' to '{dest}'."
        )

    # 7. Data I/O: Import
    import_match = re.search(
        r"\bimport\s+([\w\-]+(?:\.csv|\.json)?)\s+into\s+([\w\-]+)\b",
        r_lower
    )
    if import_match:
        src_file, entity = import_match.group(1), import_match.group(2)
        fmt = "json" if src_file.endswith(".json") else "csv"
        return IntentClassification(
            intent="data_io",
            target_entity=entity,
            params={"action": "import", "source": src_file, "entity": entity, "format": fmt},
            reasoning=f"Matched data import from '{src_file}' into '{entity}'."
        )

    # 8. Data I/O: Backup & Restore
    if any(p in r_lower for p in ["backup database", "backup the database", "create a backup", "create backup"]):
        return IntentClassification(
            intent="data_io",
            params={"action": "backup"},
            reasoning="Matched database backup request."
        )
    if any(p in r_lower for p in ["restore database", "restore the database", "restore from backup"]):
        return IntentClassification(
            intent="data_io",
            params={"action": "restore"},
            reasoning="Matched database restore request."
        )

    # 9. Relationships
    if (
        any(p in r_lower for p in ["show relationships", "show foreign keys", "validate relationships", "orphan rows", "relationship map"])
        or r_lower.startswith("link ")
        or "add relationship" in r_lower
        or "safe-delete" in r_lower
        or "safe delete" in r_lower
    ):
        return IntentClassification(
            intent="relationships",
            reasoning="Matched relationships inspection/validation/management request."
        )

    # 9b. Users
    has_users_table = bool(existing_entities and "users" in [e.lower() for e in existing_entities])
    is_explicit_db_user = bool(re.search(
        r"\b(?:create\s+(?:a\s+)?(?:[\w\-]+\s+)?(?:user|role)|grant\b|revoke\b|drop\s+(?:user|role)|(?:list|show)\s+(?:all\s+)?(?:database|db|mongo|postgres|auth)\s+users|list_users|list-users)\b",
        r_lower
    ))
    is_generic_user_list = bool(re.search(r"\b(?:list|show|find|display|get)\s+(?:all\s+)?users\b", r_lower))

    if is_explicit_db_user or (is_generic_user_list and not has_users_table):
        return IntentClassification(
            intent="users",
            reasoning="Matched database user/role management request."
        )

    if is_generic_user_list and has_users_table:
        return IntentClassification(
            intent="query",
            target_entity="users",
            params={"action": "query", "entity": "users"},
            reasoning="Matched query request on existing 'users' collection/table."
        )

    # 9c. Manual Single Record Insert
    insert_match = re.search(r"\b(?:add|insert)\s+(?:a|an|one|new)?\s*([\w\-]+)\s+(?:named|with|into|details)?\b", r_lower)
    if insert_match and not any(k in r_lower for k in ["fake", "user", "role", "relationship", "connection", "profile"]):
        entity = insert_match.group(1)
        return IntentClassification(
            intent="insert",
            target_entity=entity,
            params={"action": "insert", "entity": entity},
            reasoning=f"Matched manual record insertion into '{entity}'."
        )

    # 10. Fake Data Generation
    fake_match = re.search(r"\b(?:generate|add|insert|create)\s+(\d+)\s+fake\s+([\w\-]+)\b", r_lower)
    if fake_match:
        count = int(fake_match.group(1))
        entity = fake_match.group(2)
        return IntentClassification(
            intent="fake_data",
            target_entity=entity,
            params={"count": count, "entity": entity},
            reasoning=f"Matched fake data generation for {count} records into '{entity}'."
        )

    # 11. Explore: List collections / tables
    if re.search(r"\b(?:list|show)\s+(?:all\s+)?(?:collections|tables)\b", r_lower):
        return IntentClassification(
            intent="explore",
            reasoning="Matched list collections/tables exploration request."
        )

    # 12. Explore: Schema, Count, Sample Data, Stats
    schema_match = re.search(r"\b(?:show|describe)\s+(?:the\s+)?schema\s+(?:for|of)\s+([\w\-]+)\b", r_lower)
    if schema_match:
        entity = schema_match.group(1)
        return IntentClassification(
            intent="explore",
            target_entity=entity,
            params={"action": "describe", "entity": entity},
            reasoning=f"Matched schema description for '{entity}'."
        )

    count_match = re.search(
        r"(?:^|\b)(?:count\s+(?:the\s+)?(?:number\s+of\s+)?(?:rows|documents|records)?\s*(?:in|of)?\s*([\w\-]+)|([\w\-]+)\s+count|(?:how\s+many|number\s+of)\s+([\w\-]+))\b",
        r_lower
    )
    if count_match:
        entity = (count_match.group(1) or count_match.group(2) or count_match.group(3) or "").strip()
        stop_entities = {"rows", "documents", "records", "table", "tables", "collection", "collections", "database", "all", "the"}
        if entity and entity not in stop_entities:
            return IntentClassification(
                intent="explore",
                target_entity=entity,
                params={"action": "count", "entity": entity},
                reasoning=f"Matched count request for '{entity}'."
            )

    sample_match = re.search(r"\b(?:show|display)\s+(?:sample\s+)?(\d+)\s+(?:sample\s+)?rows\s+(?:from|in)\s+([\w\-]+)\b", r_lower)
    if sample_match:
        limit = int(sample_match.group(1))
        entity = sample_match.group(2)
        return IntentClassification(
            intent="explore",
            target_entity=entity,
            params={"action": "sample", "entity": entity, "limit": limit},
            reasoning=f"Matched sample data exploration for '{entity}'."
        )

    if any(p in r_lower for p in ["database size", "how much storage", "storage does this database use", "database stats", "db stats"]):
        return IntentClassification(
            intent="explore",
            params={"action": "stats"},
            reasoning="Matched database size/stats exploration request."
        )

    # 13. Update
    update_match = re.search(r"\b(?:update|change)\s+(?:all\s+)?([\w\-]+)\b", r_lower)
    if update_match and "mode" not in r_lower:
        entity = update_match.group(1)
        return IntentClassification(
            intent="update",
            target_entity=entity,
            params={"action": "update", "entity": entity},
            reasoning=f"Matched update request on '{entity}'."
        )

    # 14. Delete
    delete_match = re.search(r"\bdelete\s+(?:from\s+|all\s+)?([\w\-]+)\b", r_lower)
    if delete_match:
        entity = delete_match.group(1)
        return IntentClassification(
            intent="delete",
            target_entity=entity,
            params={"action": "delete", "entity": entity},
            reasoning=f"Matched delete request on '{entity}'."
        )

    # 15. Query: Explicit table/collection query patterns
    # e.g. "show all users", "show all products", "list all orders", "show all records from customers"
    query_match = re.search(
        r"\b(?:show|list|find|display|get)\s+(?:all\s+)?(?:records\s+from\s+|data\s+from\s+)?([\w\-]+)\b",
        r_lower
    )
    if query_match:
        entity = query_match.group(1)
        query_stopwords = {
            "table", "tables", "collection", "collections", "schema", "relationships",
            "database", "connections", "profiles", "history", "capabilities",
            "top", "first", "last", "some", "the", "a", "an", "few", "any", "all"
        }
        if entity not in query_stopwords:
            if entity == "users" and not has_users_table:
                pass
            else:
                if existing_entities:
                    resolved_e = resolve_entity_name(entity, existing_entities)
                    if not resolved_e:
                        return None
                    entity = resolved_e
                return IntentClassification(
                    intent="query",
                    target_entity=entity,
                    params={"action": "query", "entity": entity},
                    reasoning=f"Matched query request on '{entity}'."
                )

    return None


def resolve_entity_name(entity: str, existing_entities: List[str]) -> Optional[str]:
    """Map singular/plural and case-insensitive table/collection names."""
    if not entity or not existing_entities:
        return None
    e_low = entity.lower().strip()
    for ex in existing_entities:
        if ex.lower() == e_low:
            return ex
        if ex.lower() == f"{e_low}s" or ex.lower() == f"{e_low}es":
            return ex
        if e_low == f"{ex.lower()}s" or e_low == f"{ex.lower()}es":
            return ex
    return None


def classify_intent(
    user_request: str,
    schema: Optional[Dict[str, Any]] = None,
    db_type: str = "sqlite",
    client: Optional[Any] = None,
    version: str = "v3",
    prompt_version: Optional[str] = None,
    llm_client: Optional[Any] = None,
) -> IntentClassification:
    """
    Classify a natural language request using the router prompt and active schema.
    If the requested entity does not exist, classifies as 'unknown' and returns a clarification question.
    """
    existing_entities: List[str] = []
    if schema and isinstance(schema, dict):
        existing_entities = list(schema.get("tables", {}).keys())

    # 1. Deterministic fast-path
    fast = classify_deterministic(user_request, existing_entities=existing_entities)
    if fast:
        if fast.intent in ("connection", "copy", "help"):
            return fast

        # Entity verification and singular/plural resolution
        if fast.target_entity and existing_entities:
            resolved = resolve_entity_name(fast.target_entity, existing_entities)
            if resolved:
                fast.target_entity = resolved
            else:
                if fast.intent not in ("schema", "fake_data", "insert", "connection", "help"):
                    closest = get_closest_entities(fast.target_entity, existing_entities)
                    fast.intent = "unknown"
                    if closest:
                        fast.clarification = (
                            f"Table or collection '{fast.target_entity}' does not exist in the active database. "
                            f"Did you mean: {', '.join(closest)}?"
                        )
                    else:
                        fast.clarification = (
                            f"Table or collection '{fast.target_entity}' was not found in the active database "
                            f"(available: {', '.join(existing_entities[:5])})."
                        )
        return fast

    # 2. Check prepositional entities for typos (e.g. 'for ordrs_archive', 'from custmers')
    prep_match = re.search(r"\b(?:for|from|in|into)\s+([\w\-]+)\b", user_request.lower())
    if prep_match and existing_entities:
        prep_target = prep_match.group(1)
        stop_preps = {
            "table", "collection", "all", "records", "data", "rows", "database",
            "disk", "dev", "sandbox", "read-write", "read-only", "admin"
        }
        if prep_target not in stop_preps and not resolve_entity_name(prep_target, existing_entities):
            closest = get_closest_entities(prep_target, existing_entities)
            if closest:
                return IntentClassification(
                    intent="unknown",
                    target_entity=prep_target,
                    clarification=f"Table or collection '{prep_target}' does not exist in the active database. Did you mean: {', '.join(closest)}?",
                    reasoning=f"Found unknown entity '{prep_target}' with close match to existing table."
                )

    # 2. Router prompt execution
    active_version = prompt_version or version or "v3"
    v_norm = active_version.lower().strip()
    if not v_norm.startswith("router_"):
        v_name = f"router_{v_norm}" if v_norm in ("v1", "v2", "v3") else "router_v3"
    else:
        v_name = v_norm

    llm = llm_client or client or LLMClient()
    try:
        sys_prompt = load_prompt(version=v_name, db_type=db_type, schema=schema)
        parsed = llm.ask_json(system_prompt=sys_prompt, user_prompt=user_request)
    except Exception:
        parsed = {}

    intent = str(parsed.get("intent", "unknown")).strip().lower()
    if intent not in VALID_INTENTS:
        intent = "unknown"

    target_entity = str(parsed.get("target_entity", "")).strip()
    params = parsed.get("params", {})
    if not isinstance(params, dict):
        params = {}
    if not target_entity:
        target_entity = str(params.get("target", "") or params.get("entity", "")).strip()

    clarification = str(parsed.get("clarification", "")).strip()
    reasoning = str(parsed.get("reasoning", "")).strip()

    # 3. Disambiguation & Entity Verification Guardrail
    # If the user targets a specific entity for query/update/delete/describe but entity doesn't exist:
    if target_entity and existing_entities and target_entity not in existing_entities:
        # Ignore creation intents
        if intent not in ("schema", "fake_data", "insert", "connection", "help"):
            closest = get_closest_entities(target_entity, existing_entities)
            intent = "unknown"
            if closest:
                clarification = (
                    f"Table or collection '{target_entity}' does not exist in the active database. "
                    f"Did you mean: {', '.join(closest)}?"
                )
            else:
                clarification = (
                    f"Table or collection '{target_entity}' was not found in the active database "
                    f"(available: {', '.join(existing_entities[:5])})."
                )
    elif not target_entity and existing_entities and intent in ("query", "update", "delete", "unknown"):
        stop_words = {
            "from", "select", "find", "all", "where", "into", "table", "collection",
            "in", "show", "data", "schema", "count", "documents", "rows", "columns",
            "between", "and", "or", "for", "of", "the", "a", "an", "is", "are", "to",
            "by", "with", "as", "at", "get", "set"
        }
        tokens = re.findall(r"\b[a-zA-Z_]\w+\b", user_request.lower())
        for tok in tokens:
            if tok not in existing_entities and tok not in stop_words and not resolve_entity_name(tok, existing_entities):
                closest = get_closest_entities(tok, existing_entities)
                if closest:
                    target_entity = tok
                    intent = "unknown"
                    clarification = (
                        f"Table or collection '{tok}' does not exist in the active database. "
                        f"Did you mean: {', '.join(closest)}?"
                    )
                    break

    return IntentClassification(
        intent=intent,
        target_entity=target_entity,
        params=params,
        clarification=clarification,
        reasoning=reasoning,
    )
