"""Database abstraction, schema inspection, and execution module.

Supports:
- Local MongoDB (Native Windows, no Docker required!)
- Local SQLite (file / in-memory)
- Local PostgreSQL (when configured)
"""

import ast
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from src.config import validate_dev_database_url


class MongoDBManager:
    """Manages local MongoDB connections, collection inspection, and MQL execution."""

    def __init__(self, database_url: str):
        import pymongo

        validate_dev_database_url(database_url)
        self.database_url = database_url
        self.client = pymongo.MongoClient(database_url, serverSelectionTimeoutMS=3000)

        # Parse db name from path, e.g. mongodb://localhost:27017/devdb
        parsed = urlparse(database_url)
        path = (parsed.path or "").lstrip("/")
        db_name = path.split("?")[0] if path else "devdb"
        self.db_name = db_name or "devdb"
        self.db = self.client[self.db_name]

    @property
    def dialect_name(self) -> str:
        return "mongodb"

    def get_tables(self) -> List[str]:
        """Return all collection names in the active MongoDB database."""
        try:
            return sorted([c for c in self.db.list_collection_names() if not c.startswith("system.")])
        except Exception:
            return []

    def get_table_columns(self, table_name: str) -> List[Dict[str, Any]]:
        """Infer fields and types from sample documents in the collection."""
        coll = self.db[table_name]
        sample_docs = list(coll.find().limit(50))
        if not sample_docs:
            return [{"name": "_id", "type": "ObjectId", "nullable": False}]

        field_types: Dict[str, str] = {}
        for doc in sample_docs:
            for k, v in doc.items():
                if k not in field_types:
                    field_types[k] = type(v).__name__

        cols = []
        for k, t in field_types.items():
            cols.append({
                "name": k,
                "type": t,
                "nullable": k != "_id",
            })
        return cols

    def get_foreign_keys(self, table_name: str) -> List[Dict[str, Any]]:
        """Detect cross-collection references based on field naming conventions (e.g. user_id -> users._id)."""
        cols = [c["name"] for c in self.get_table_columns(table_name)]
        all_colls = set(self.get_tables())
        fks = []

        for col in cols:
            if col == "_id":
                continue

            target_table = None
            if col.endswith("_id"):
                prefix = col[:-3]
                for candidate in [prefix, f"{prefix}s", f"{prefix}es"]:
                    if candidate in all_colls:
                        target_table = candidate
                        break
            elif col.endswith("Id"):
                prefix = col[:-2]
                for candidate in [prefix, f"{prefix}s", f"{prefix}es"]:
                    if candidate in all_colls:
                        target_table = candidate
                        break

            if target_table:
                fks.append({
                    "constrained_columns": [col],
                    "referred_table": target_table,
                    "referred_columns": ["_id"],
                })

        return fks

    def get_primary_keys(self, table_name: str) -> List[str]:
        return ["_id"]

    def get_schema_summary(self) -> str:
        """Generate human-readable schema of collections, field types, and references."""
        colls = self.get_tables()
        if not colls:
            return "MongoDB database is empty. No collections found."

        lines = []
        for coll in colls:
            lines.append(f"Collection: {coll}")
            lines.append("Fields:")
            cols = self.get_table_columns(coll)
            for c in cols:
                pk_str = " (PRIMARY KEY)" if c["name"] == "_id" else ""
                lines.append(f"  - {c['name']}: {c['type']}{pk_str}")

            fks = self.get_foreign_keys(coll)
            if fks:
                lines.append("References:")
                for fk in fks:
                    local = ", ".join(fk["constrained_columns"])
                    ref = fk["referred_table"]
                    lines.append(f"  - {local} -> {ref}._id")
            lines.append("")

        return "\n".join(lines).strip()

    def _parse_command_args(self, args_str: str) -> List[Any]:
        """Safely parse arguments inside a db.collection.method(...) call."""
        s = args_str.strip()
        if not s:
            return []

        # Attempt standard JSON array parse
        try:
            return json.loads(f"[{s}]")
        except Exception:
            pass

        # Relax quotes on unquoted object keys: {status: 'active'} -> {"status": 'active'}
        relaxed = re.sub(r'([{,]\s*)([a-zA-Z0-9_$]+)\s*:', r'\1"\2":', s)
        relaxed = relaxed.replace("'", '"')
        try:
            return json.loads(f"[{relaxed}]")
        except Exception:
            pass

        # Python literal_eval fallback
        try:
            return list(ast.literal_eval(f"({s},)"))
        except Exception:
            # Return raw string argument if complex
            return [s]

    def execute_query(self, query_str: str) -> Tuple[List[str], List[Tuple]]:
        """Parse and execute a MongoDB shell query or command."""
        clean = query_str.strip().rstrip(";")

        # Pattern: db.<collection>.<method>(<args>)
        match = re.match(r"^db\.([a-zA-Z0-9_]+)\.([a-zA-Z0-9_]+)\s*\((.*)\)$", clean, re.DOTALL)
        if not match:
            # Fallback: check if raw collection JSON command
            try:
                cmd_obj = json.loads(clean)
                coll_name = cmd_obj.get("collection")
                action = cmd_obj.get("action", "find")
                args = cmd_obj.get("args", [])
                return self._dispatch_mongo_method(coll_name, action, args)
            except Exception:
                raise ValueError(
                    f"Invalid MongoDB query format: '{clean}'.\n"
                    "Expected syntax: db.<collection>.<method>(<arguments>), e.g.:\n"
                    "  db.users.find({\"role\": \"admin\"})\n"
                    "  db.orders.aggregate([{\"$match\": {\"status\": \"completed\"}}])"
                )

        coll_name = match.group(1)
        method_name = match.group(2)
        raw_args = match.group(3)
        parsed_args = self._parse_command_args(raw_args)

        return self._dispatch_mongo_method(coll_name, method_name, parsed_args)

    def _dispatch_mongo_method(self, coll_name: str, method_name: str, args: List[Any]) -> Tuple[List[str], List[Tuple]]:
        """Execute parsed MongoDB method on target collection."""
        coll = self.db[coll_name]
        norm_method = method_name.lower().replace("_", "")

        if norm_method == "find":
            filter_doc = args[0] if len(args) > 0 else {}
            proj = args[1] if len(args) > 1 else None
            cursor = coll.find(filter_doc, proj).limit(100)
            return self._format_docs_as_table(list(cursor))

        elif norm_method == "aggregate":
            pipeline = args[0] if len(args) > 0 else []
            cursor = coll.aggregate(pipeline)
            return self._format_docs_as_table(list(cursor))

        elif norm_method in ("count", "countdocuments"):
            filter_doc = args[0] if len(args) > 0 else {}
            count = coll.count_documents(filter_doc)
            return ["Collection", "Document Count"], [(coll_name, count)]

        elif norm_method in ("insertone", "insert"):
            doc = args[0] if args else {}
            res = coll.insert_one(doc)
            return ["Status", "Inserted _id"], [("Success", str(res.inserted_id))]

        elif norm_method in ("insertmany"):
            docs = args[0] if args else []
            res = coll.insert_many(docs)
            return ["Status", "Inserted Count"], [("Success", len(res.inserted_ids))]

        elif norm_method in ("updateone"):
            filter_doc = args[0] if len(args) > 0 else {}
            update_doc = args[1] if len(args) > 1 else {}
            res = coll.update_one(filter_doc, update_doc)
            return ["Status", "Matched", "Modified"], [("Success", res.matched_count, res.modified_count)]

        elif norm_method in ("updatemany"):
            filter_doc = args[0] if len(args) > 0 else {}
            update_doc = args[1] if len(args) > 1 else {}
            res = coll.update_many(filter_doc, update_doc)
            return ["Status", "Matched", "Modified"], [("Success", res.matched_count, res.modified_count)]

        elif norm_method in ("deleteone"):
            filter_doc = args[0] if len(args) > 0 else {}
            res = coll.delete_one(filter_doc)
            return ["Status", "Deleted Count"], [("Success", res.deleted_count)]

        elif norm_method in ("deletemany", "delete"):
            filter_doc = args[0] if len(args) > 0 else {}
            res = coll.delete_many(filter_doc)
            return ["Status", "Deleted Count"], [("Success", res.deleted_count)]

        elif norm_method == "drop":
            coll.drop()
            return ["Status", "Action"], [("Success", f"Collection {coll_name} dropped")]

        else:
            raise ValueError(f"Unsupported MongoDB operation '{method_name}'.")

    def _format_docs_as_table(self, docs: List[Dict[str, Any]]) -> Tuple[List[str], List[Tuple]]:
        """Format a list of MongoDB documents into headers and rows for Rich table display."""
        if not docs:
            return ["Result"], [("No documents found.",)]

        cols: List[str] = []
        for d in docs:
            for k in d.keys():
                if k not in cols:
                    cols.append(k)

        rows: List[Tuple] = []
        for d in docs:
            row = []
            for col in cols:
                val = d.get(col)
                if val is None:
                    row.append(None)
                elif isinstance(val, (dict, list)):
                    row.append(json.dumps(val, default=str))
                elif isinstance(val, datetime):
                    row.append(val.strftime("%Y-%m-%d %H:%M:%S"))
                else:
                    row.append(str(val))
            rows.append(tuple(row))

        return cols, rows

    def init_sample_database(self) -> None:
        """Initialize MongoDB with sample e-commerce collections and documents."""
        # Clean existing sample collections
        for name in ["users", "products", "orders", "order_items", "user_profiles"]:
            self.db[name].drop()

        users_data = [
            {"_id": 1, "name": "Alice Johnson", "email": "alice@example.com", "role": "admin", "created_at": datetime(2026, 1, 10, 10, 0)},
            {"_id": 2, "name": "Bob Smith", "email": "bob@example.com", "role": "customer", "created_at": datetime(2026, 2, 14, 11, 30)},
            {"_id": 3, "name": "Charlie Brown", "email": "charlie@example.com", "role": "customer", "created_at": datetime(2026, 3, 1, 9, 15)},
            {"_id": 4, "name": "Diana Prince", "email": "diana@example.com", "role": "customer", "created_at": datetime(2026, 3, 20, 14, 45)},
        ]
        self.db["users"].insert_many(users_data)

        products_data = [
            {"_id": 1, "name": "Mechanical Keyboard", "category": "Electronics", "price": 129.99, "stock_quantity": 45},
            {"_id": 2, "name": "Wireless Ergonomic Mouse", "category": "Electronics", "price": 59.50, "stock_quantity": 80},
            {"_id": 3, "name": "Standing Desk Mat", "category": "Office", "price": 42.00, "stock_quantity": 25},
            {"_id": 4, "name": "Noise Cancelling Headphones", "category": "Electronics", "price": 249.99, "stock_quantity": 15},
            {"_id": 5, "name": "Stainless Steel Water Bottle", "category": "Accessories", "price": 19.99, "stock_quantity": 120},
        ]
        self.db["products"].insert_many(products_data)

        orders_data = [
            {"_id": 1, "user_id": 1, "status": "completed", "total_amount": 189.49, "order_date": datetime(2026, 2, 1, 12, 0)},
            {"_id": 2, "user_id": 2, "status": "completed", "total_amount": 249.99, "order_date": datetime(2026, 2, 20, 15, 30)},
            {"_id": 3, "user_id": 1, "status": "shipped", "total_amount": 42.00, "order_date": datetime(2026, 3, 5, 8, 45)},
            {"_id": 4, "user_id": 3, "status": "pending", "total_amount": 129.99, "order_date": datetime(2026, 3, 22, 16, 10)},
        ]
        self.db["orders"].insert_many(orders_data)

        order_items_data = [
            {"_id": 1, "order_id": 1, "product_id": 1, "quantity": 1, "unit_price": 129.99},
            {"_id": 2, "order_id": 1, "product_id": 2, "quantity": 1, "unit_price": 59.50},
            {"_id": 3, "order_id": 2, "product_id": 4, "quantity": 1, "unit_price": 249.99},
            {"_id": 4, "order_id": 3, "product_id": 3, "quantity": 1, "unit_price": 42.00},
            {"_id": 5, "order_id": 4, "product_id": 1, "quantity": 1, "unit_price": 129.99},
        ]
        self.db["order_items"].insert_many(order_items_data)

        user_profiles_data = [
            {"_id": 1, "user_id": 1, "phone": "+1-555-0101", "address": "123 Tech Blvd, Austin TX", "bio": "Lead system admin"},
            {"_id": 2, "user_id": 2, "phone": "+1-555-0102", "address": "456 Market St, Seattle WA", "bio": "Frequent shopper"},
        ]
        self.db["user_profiles"].insert_many(user_profiles_data)


class SQLAlchemyDatabaseManager:
    """Manages SQLAlchemy engine, schema reflection, and query execution for SQLite and PostgreSQL."""

    def __init__(self, database_url: str):
        from sqlalchemy import create_engine, inspect, Engine

        validate_dev_database_url(database_url)
        self.database_url = database_url
        self.engine: Engine = create_engine(database_url)

    @property
    def dialect_name(self) -> str:
        return self.engine.dialect.name

    def get_inspector(self):
        from sqlalchemy import inspect
        return inspect(self.engine)

    def get_tables(self) -> List[str]:
        insp = self.get_inspector()
        return insp.get_table_names()

    def get_table_columns(self, table_name: str) -> List[Dict[str, Any]]:
        insp = self.get_inspector()
        return insp.get_columns(table_name)

    def get_foreign_keys(self, table_name: str) -> List[Dict[str, Any]]:
        insp = self.get_inspector()
        return insp.get_foreign_keys(table_name)

    def get_primary_keys(self, table_name: str) -> List[str]:
        insp = self.get_inspector()
        pk_constraint = insp.get_pk_constraint(table_name)
        return pk_constraint.get("constrained_columns", [])

    def get_schema_summary(self) -> str:
        tables = self.get_tables()
        if not tables:
            return "Database is empty. No tables found."

        summary_lines = []
        insp = self.get_inspector()

        for table in tables:
            columns = insp.get_columns(table)
            fks = insp.get_foreign_keys(table)
            pk_constraint = insp.get_pk_constraint(table)
            pk_cols = set(pk_constraint.get("constrained_columns", []))

            summary_lines.append(f"Table: {table}")
            summary_lines.append("Columns:")
            for col in columns:
                col_name = col["name"]
                col_type = str(col["type"])
                attributes = []
                if col_name in pk_cols:
                    attributes.append("PRIMARY KEY")
                if not col.get("nullable", True):
                    attributes.append("NOT NULL")

                attr_str = f" ({', '.join(attributes)})" if attributes else ""
                summary_lines.append(f"  - {col_name}: {col_type}{attr_str}")

            if fks:
                summary_lines.append("Foreign Keys:")
                for fk in fks:
                    local_cols = ", ".join(fk["constrained_columns"])
                    ref_table = fk["referred_table"]
                    ref_cols = ", ".join(fk["referred_columns"])
                    summary_lines.append(f"  - {local_cols} -> {ref_table}({ref_cols})")
            summary_lines.append("")

        return "\n".join(summary_lines).strip()

    def execute_query(self, sql: str) -> Tuple[List[str], List[Tuple]]:
        from sqlalchemy import text

        clean_sql = sql.strip().rstrip(";")
        with self.engine.connect() as conn:
            result = conn.execute(text(clean_sql))
            if result.returns_rows:
                columns = list(result.keys())
                rows = [tuple(row) for row in result.fetchall()]
                return columns, rows
            else:
                conn.commit()
                rowcount = result.rowcount if hasattr(result, "rowcount") else 0
                return ["Status", "Affected Rows"], [("Success", rowcount)]

    def init_sample_database(self) -> None:
        from sqlalchemy import (
            MetaData,
            Table,
            Column,
            Integer,
            String,
            Float,
            DateTime,
            ForeignKey,
            Text,
            text,
        )

        metadata = MetaData()

        users = Table(
            "users",
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("name", String(100), nullable=False),
            Column("email", String(120), unique=True, nullable=False),
            Column("role", String(50), default="customer"),
            Column("created_at", DateTime, default=lambda: datetime.now(timezone.utc)),
        )

        products = Table(
            "products",
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("name", String(150), nullable=False),
            Column("category", String(80), nullable=False),
            Column("price", Float, nullable=False),
            Column("stock_quantity", Integer, default=0),
        )

        orders = Table(
            "orders",
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("user_id", Integer, ForeignKey("users.id"), nullable=False),
            Column("status", String(50), default="pending"),
            Column("total_amount", Float, nullable=False),
            Column("order_date", DateTime, default=lambda: datetime.now(timezone.utc)),
        )

        order_items = Table(
            "order_items",
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("order_id", Integer, ForeignKey("orders.id"), nullable=False),
            Column("product_id", Integer, ForeignKey("products.id"), nullable=False),
            Column("quantity", Integer, nullable=False, default=1),
            Column("unit_price", Float, nullable=False),
        )

        user_profiles = Table(
            "user_profiles",
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("user_id", Integer, ForeignKey("users.id"), unique=True, nullable=False),
            Column("phone", String(30), nullable=True),
            Column("address", Text, nullable=True),
            Column("bio", Text, nullable=True),
        )

        metadata.create_all(self.engine)

        with self.engine.connect() as conn:
            existing_users = conn.execute(text("SELECT COUNT(*) FROM users")).scalar()
            if existing_users == 0:
                conn.execute(
                    users.insert(),
                    [
                        {"name": "Alice Johnson", "email": "alice@example.com", "role": "admin", "created_at": datetime(2026, 1, 10, 10, 0)},
                        {"name": "Bob Smith", "email": "bob@example.com", "role": "customer", "created_at": datetime(2026, 2, 14, 11, 30)},
                        {"name": "Charlie Brown", "email": "charlie@example.com", "role": "customer", "created_at": datetime(2026, 3, 1, 9, 15)},
                        {"name": "Diana Prince", "email": "diana@example.com", "role": "customer", "created_at": datetime(2026, 3, 20, 14, 45)},
                    ],
                )
                conn.execute(
                    products.insert(),
                    [
                        {"name": "Mechanical Keyboard", "category": "Electronics", "price": 129.99, "stock_quantity": 45},
                        {"name": "Wireless Ergonomic Mouse", "category": "Electronics", "price": 59.50, "stock_quantity": 80},
                        {"name": "Standing Desk Mat", "category": "Office", "price": 42.00, "stock_quantity": 25},
                        {"name": "Noise Cancelling Headphones", "category": "Electronics", "price": 249.99, "stock_quantity": 15},
                        {"name": "Stainless Steel Water Bottle", "category": "Accessories", "price": 19.99, "stock_quantity": 120},
                    ],
                )
                conn.execute(
                    orders.insert(),
                    [
                        {"user_id": 1, "status": "completed", "total_amount": 189.49, "order_date": datetime(2026, 2, 1, 12, 0)},
                        {"user_id": 2, "status": "completed", "total_amount": 249.99, "order_date": datetime(2026, 2, 20, 15, 30)},
                        {"user_id": 1, "status": "shipped", "total_amount": 42.00, "order_date": datetime(2026, 3, 5, 8, 45)},
                        {"user_id": 3, "status": "pending", "total_amount": 129.99, "order_date": datetime(2026, 3, 22, 16, 10)},
                    ],
                )
                conn.execute(
                    order_items.insert(),
                    [
                        {"order_id": 1, "product_id": 1, "quantity": 1, "unit_price": 129.99},
                        {"order_id": 1, "product_id": 2, "quantity": 1, "unit_price": 59.50},
                        {"order_id": 2, "product_id": 4, "quantity": 1, "unit_price": 249.99},
                        {"order_id": 3, "product_id": 3, "quantity": 1, "unit_price": 42.00},
                        {"order_id": 4, "product_id": 1, "quantity": 1, "unit_price": 129.99},
                    ],
                )
                conn.execute(
                    user_profiles.insert(),
                    [
                        {"user_id": 1, "phone": "+1-555-0101", "address": "123 Tech Blvd, Austin TX", "bio": "Lead system admin"},
                        {"user_id": 2, "phone": "+1-555-0102", "address": "456 Market St, Seattle WA", "bio": "Frequent shopper"},
                    ],
                )
                conn.commit()


def DatabaseManager(database_url: str):
    """Factory creating appropriate manager based on database connection URL."""
    clean = database_url.strip()
    if clean.startswith("mongodb://") or clean.startswith("mongodb+srv://"):
        return MongoDBManager(database_url)
    return SQLAlchemyDatabaseManager(database_url)
