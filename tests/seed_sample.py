"""Sample database seed script for SQLite and native local MongoDB.

Creates:
- customers / users (indexed by email)
- products
- orders (Intentionally NO foreign key/reference to customers for relationship testing)
- order_items (references orders and products)
"""

import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def seed_sqlite(db_path: str = "dev.db") -> None:
    """Create sample schema and seed test data into SQLite database."""
    target_path = Path(db_path)
    if target_path.exists():
        target_path.unlink()

    conn = sqlite3.connect(str(target_path))
    cursor = conn.cursor()

    # 1. Customers
    cursor.execute("""
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    cursor.execute("CREATE INDEX ix_customers_email ON customers (email);")

    # 2. Products
    cursor.execute("""
        CREATE TABLE products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            stock INTEGER DEFAULT 0
        );
    """)

    # 3. Orders (Intentionally omitting foreign key to customers)
    cursor.execute("""
        CREATE TABLE orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'pending',
            total_amount REAL NOT NULL
        );
    """)
    cursor.execute("CREATE INDEX ix_orders_customer_id ON orders (customer_id);")

    # 4. Order Items (with foreign keys to orders and products)
    cursor.execute("""
        CREATE TABLE order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            quantity INTEGER NOT NULL,
            unit_price REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(id),
            FOREIGN KEY (product_id) REFERENCES products(id)
        );
    """)
    cursor.execute("CREATE INDEX ix_order_items_order_id ON order_items (order_id);")

    # Seed Sample Data
    customers_data = [
        ("Alice Smith", "alice@example.com", "2026-01-15 10:00:00"),
        ("Bob Jones", "bob@example.com", "2026-02-01 11:30:00"),
        ("Charlie Brown", "charlie@example.com", "2026-02-14 09:15:00"),
    ]
    cursor.executemany(
        "INSERT INTO customers (name, email, created_at) VALUES (?, ?, ?);",
        customers_data
    )

    products_data = [
        ("Mechanical Keyboard", 129.99, 45),
        ("Wireless Mouse", 49.99, 80),
        ("4K Ultra-Wide Monitor", 499.00, 15),
        ("USB-C Docking Station", 89.50, 30),
    ]
    cursor.executemany(
        "INSERT INTO products (name, price, stock) VALUES (?, ?, ?);",
        products_data
    )

    orders_data = [
        (1, "2026-03-01 14:20:00", "completed", 179.98),
        (2, "2026-03-02 16:45:00", "shipped", 499.00),
        (1, "2026-03-05 18:10:00", "pending", 89.50),
    ]
    cursor.executemany(
        "INSERT INTO orders (customer_id, order_date, status, total_amount) VALUES (?, ?, ?, ?);",
        orders_data
    )

    order_items_data = [
        (1, 1, 1, 129.99),
        (1, 2, 1, 49.99),
        (2, 3, 1, 499.00),
        (3, 4, 1, 89.50),
    ]
    cursor.executemany(
        "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES (?, ?, ?, ?);",
        order_items_data
    )

    conn.commit()
    conn.close()
    print(f"Sample SQLite database successfully created and seeded at: {target_path.resolve()}")


def seed_mongo(mongo_url: str = "mongodb://localhost:27017/devdb") -> None:
    """Seed sample collections and documents into local native MongoDB (no Docker needed)."""
    import pymongo
    from urllib.parse import urlparse

    parsed = urlparse(mongo_url)
    path = (parsed.path or "").lstrip("/")
    db_name = path.split("?")[0] if path else "devdb"

    client = pymongo.MongoClient(mongo_url, serverSelectionTimeoutMS=2500)
    db = client[db_name]

    # Clean existing collections
    for name in ["users", "customers", "products", "orders", "order_items", "user_profiles"]:
        if name in db.list_collection_names():
            db[name].drop()

    users_data = [
        {"_id": 1, "name": "Alice Johnson", "email": "alice@example.com", "role": "admin", "created_at": datetime(2026, 1, 10, 10, 0)},
        {"_id": 2, "name": "Bob Smith", "email": "bob@example.com", "role": "customer", "created_at": datetime(2026, 2, 14, 11, 30)},
        {"_id": 3, "name": "Charlie Brown", "email": "charlie@example.com", "role": "customer", "created_at": datetime(2026, 3, 1, 9, 15)},
        {"_id": 4, "name": "Diana Prince", "email": "diana@example.com", "role": "customer", "created_at": datetime(2026, 3, 20, 14, 45)},
    ]
    db["users"].insert_many(users_data)
    db["users"].create_index("email", unique=True)

    products_data = [
        {"_id": 1, "name": "Mechanical Keyboard", "category": "Electronics", "price": 129.99, "stock_quantity": 45},
        {"_id": 2, "name": "Wireless Ergonomic Mouse", "category": "Electronics", "price": 59.50, "stock_quantity": 80},
        {"_id": 3, "name": "Standing Desk Mat", "category": "Office", "price": 42.00, "stock_quantity": 25},
        {"_id": 4, "name": "Noise Cancelling Headphones", "category": "Electronics", "price": 249.99, "stock_quantity": 15},
        {"_id": 5, "name": "Stainless Steel Water Bottle", "category": "Accessories", "price": 19.99, "stock_quantity": 120},
    ]
    db["products"].insert_many(products_data)

    orders_data = [
        {"_id": 1, "user_id": 1, "status": "completed", "total_amount": 189.49, "order_date": datetime(2026, 2, 1, 12, 0)},
        {"_id": 2, "user_id": 2, "status": "completed", "total_amount": 249.99, "order_date": datetime(2026, 2, 20, 15, 30)},
        {"_id": 3, "user_id": 1, "status": "pending", "total_amount": 42.00, "order_date": datetime(2026, 3, 5, 9, 0)},
        {"_id": 4, "user_id": 3, "status": "shipped", "total_amount": 129.99, "order_date": datetime(2026, 3, 12, 14, 15)},
    ]
    db["orders"].insert_many(orders_data)
    db["orders"].create_index("user_id")

    order_items_data = [
        {"_id": 1, "order_id": 1, "product_id": 1, "quantity": 1, "unit_price": 129.99},
        {"_id": 2, "order_id": 1, "product_id": 2, "quantity": 1, "unit_price": 59.50},
        {"_id": 3, "order_id": 2, "product_id": 4, "quantity": 1, "unit_price": 249.99},
        {"_id": 4, "order_id": 3, "product_id": 3, "quantity": 1, "unit_price": 42.00},
        {"_id": 5, "order_id": 4, "product_id": 1, "quantity": 1, "unit_price": 129.99},
    ]
    db["order_items"].insert_many(order_items_data)
    db["order_items"].create_index("order_id")

    user_profiles_data = [
        {"_id": 1, "user_id": 1, "phone": "+1-555-0101", "address": "123 Tech Blvd, Austin TX", "bio": "Lead system admin"},
        {"_id": 2, "user_id": 2, "phone": "+1-555-0102", "address": "456 Market St, Seattle WA", "bio": "Frequent shopper"},
    ]
    db["user_profiles"].insert_many(user_profiles_data)
    db["user_profiles"].create_index("user_id", unique=True)

    print(f"Sample MongoDB collections successfully seeded in database '{db_name}' at {mongo_url}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--mongo":
        mongo_url = sys.argv[2] if len(sys.argv) > 2 else "mongodb://localhost:27017/devdb"
        seed_mongo(mongo_url)
    else:
        db_file = sys.argv[1] if len(sys.argv) > 1 else "dev.db"
        seed_sqlite(db_file)
