"""Seed sample data into MongoDB devdb database.

Creates:
- customers: 10 customer documents with Indian & Western names, emails, cities.
- products: 10 product documents with categories, stock, prices.
- orders: 12 order documents referencing customers, with 2 intentional orphan orders.
No secondary indexes are created, allowing relationship validation to test missing index warnings.
"""

import sys
from datetime import datetime, timezone
import pymongo
from bson import ObjectId


def seed_mongo(mongo_url: str = "mongodb://localhost:27017/devdb") -> None:
    """Populate devdb database with sample collections."""
    client = pymongo.MongoClient(mongo_url, serverSelectionTimeoutMS=3000)
    db = client.get_default_database()
    if db is None or db.name == "admin":
        db = client["devdb"]

    # Drop existing collections to ensure fresh reproducible state
    for coll_name in ("customers", "products", "orders", "order_items"):
        if coll_name in db.list_collection_names():
            db[coll_name].drop()

    # 1. Seed Customers (10 documents)
    customer_ids = [
        ObjectId(), ObjectId(), ObjectId(), ObjectId(), ObjectId(),
        ObjectId(), ObjectId(), ObjectId(), ObjectId(), ObjectId()
    ]

    customers_data = [
        {
            "_id": customer_ids[0],
            "name": "Aarav Sharma",
            "email": "aarav.sharma@example.in",
            "city": "Chennai",
            "phone": "+91-98765-43210",
            "tier": "gold",
            "created_at": datetime(2026, 1, 15, tzinfo=timezone.utc),
        },
        {
            "_id": customer_ids[1],
            "name": "Priya Patel",
            "email": "priya.patel@example.in",
            "city": "Ahmedabad",
            "phone": "+91-98765-43211",
            "tier": "silver",
            "created_at": datetime(2026, 2, 10, tzinfo=timezone.utc),
        },
        {
            "_id": customer_ids[2],
            "name": "Rohan Gupta",
            "email": "rohan.gupta@example.in",
            "city": "Chennai",
            "phone": "+91-98765-43212",
            "tier": "platinum",
            "created_at": datetime(2026, 3, 5, tzinfo=timezone.utc),
        },
        {
            "_id": customer_ids[3],
            "name": "Ananya Rao",
            "email": "ananya.rao@example.in",
            "city": "Bengaluru",
            "phone": "+91-98765-43213",
            "tier": "gold",
            "created_at": datetime(2026, 3, 20, tzinfo=timezone.utc),
        },
        {
            "_id": customer_ids[4],
            "name": "Vikram Singh",
            "email": "vikram.singh@example.in",
            "city": "Delhi",
            "phone": "+91-98765-43214",
            "tier": "silver",
            "created_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
        },
        {
            "_id": customer_ids[5],
            "name": "Kavita Reddy",
            "email": "kavita.reddy@example.in",
            "city": "Hyderabad",
            "phone": "+91-98765-43215",
            "tier": "platinum",
            "created_at": datetime(2026, 4, 15, tzinfo=timezone.utc),
        },
        {
            "_id": customer_ids[6],
            "name": "Rahul Nair",
            "email": "rahul.nair@example.in",
            "city": "Kochi",
            "phone": "+91-98765-43216",
            "tier": "bronze",
            "created_at": datetime(2026, 5, 2, tzinfo=timezone.utc),
        },
        {
            "_id": customer_ids[7],
            "name": "Sneha Joshi",
            "email": "sneha.joshi@example.in",
            "city": "Pune",
            "phone": "+91-98765-43217",
            "tier": "gold",
            "created_at": datetime(2026, 5, 18, tzinfo=timezone.utc),
        },
        {
            "_id": customer_ids[8],
            "name": "Amit Banerjee",
            "email": "amit.banerjee@example.in",
            "city": "Kolkata",
            "phone": "+91-98765-43218",
            "tier": "silver",
            "created_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        },
        {
            "_id": customer_ids[9],
            "name": "Sanjay Kumar",
            "email": "sanjay.kumar@example.in",
            "city": "Chennai",
            "phone": "+91-98765-43219",
            "tier": "gold",
            "created_at": datetime(2026, 6, 12, tzinfo=timezone.utc),
        },
    ]
    db.customers.insert_many(customers_data)

    # 2. Seed Products (10 documents)
    products_data = [
        {"name": "Mechanical Keyboard", "category": "Electronics", "price": 4999.0, "stock": 45},
        {"name": "Ergonomic Chair", "category": "Furniture", "price": 14999.0, "stock": 20},
        {"name": "4K Ultra Monitor", "category": "Electronics", "price": 28999.0, "stock": 15},
        {"name": "Wireless Mouse", "category": "Electronics", "price": 1299.0, "stock": 100},
        {"name": "USB-C Hub Multiport", "category": "Accessories", "price": 2499.0, "stock": 60},
        {"name": "Noise Cancelling Headphones", "category": "Audio", "price": 19999.0, "stock": 30},
        {"name": "Desk Mat XXL", "category": "Accessories", "price": 899.0, "stock": 120},
        {"name": "Laptop Stand Aluminum", "category": "Accessories", "price": 1899.0, "stock": 50},
        {"name": "Webcam 1080p HD", "category": "Electronics", "price": 3499.0, "stock": 40},
        {"name": "Cable Management Tray", "category": "Furniture", "price": 999.0, "stock": 75},
    ]
    db.products.insert_many(products_data)

    # 3. Seed Orders (12 documents: 10 valid references + 2 intentional orphans)
    orders_data = [
        {
            "order_number": "ORD-1001",
            "customer_id": str(customer_ids[0]),  # Aarav (Chennai)
            "status": "completed",
            "total_amount": 6298.0,
            "order_date": datetime(2026, 5, 10, tzinfo=timezone.utc),
            "items": [{"item": "Mechanical Keyboard", "qty": 1, "price": 4999.0}, {"item": "Wireless Mouse", "qty": 1, "price": 1299.0}],
        },
        {
            "order_number": "ORD-1002",
            "customer_id": str(customer_ids[1]),  # Priya
            "status": "pending",
            "total_amount": 14999.0,
            "order_date": datetime(2026, 6, 1, tzinfo=timezone.utc),
            "items": [{"item": "Ergonomic Chair", "qty": 1, "price": 14999.0}],
        },
        {
            "order_number": "ORD-1003",
            "customer_id": str(customer_ids[2]),  # Rohan (Chennai)
            "status": "completed",
            "total_amount": 28999.0,
            "order_date": datetime(2026, 6, 5, tzinfo=timezone.utc),
            "items": [{"item": "4K Ultra Monitor", "qty": 1, "price": 28999.0}],
        },
        {
            "order_number": "ORD-1004",
            "customer_id": str(customer_ids[3]),  # Ananya
            "status": "shipped",
            "total_amount": 4398.0,
            "order_date": datetime(2026, 6, 12, tzinfo=timezone.utc),
            "items": [{"item": "USB-C Hub Multiport", "qty": 1, "price": 2499.0}, {"item": "Laptop Stand Aluminum", "qty": 1, "price": 1899.0}],
        },
        {
            "order_number": "ORD-1005",
            "customer_id": str(customer_ids[4]),  # Vikram
            "status": "completed",
            "total_amount": 19999.0,
            "order_date": datetime(2026, 6, 15, tzinfo=timezone.utc),
            "items": [{"item": "Noise Cancelling Headphones", "qty": 1, "price": 19999.0}],
        },
        {
            "order_number": "ORD-1006",
            "customer_id": str(customer_ids[0]),  # Aarav again
            "status": "completed",
            "total_amount": 899.0,
            "order_date": datetime(2026, 6, 18, tzinfo=timezone.utc),
            "items": [{"item": "Desk Mat XXL", "qty": 1, "price": 899.0}],
        },
        {
            "order_number": "ORD-1007",
            "customer_id": str(customer_ids[5]),  # Kavita
            "status": "pending",
            "total_amount": 3499.0,
            "order_date": datetime(2026, 6, 20, tzinfo=timezone.utc),
            "items": [{"item": "Webcam 1080p HD", "qty": 1, "price": 3499.0}],
        },
        {
            "order_number": "ORD-1008",
            "customer_id": str(customer_ids[6]),  # Rahul
            "status": "cancelled",
            "total_amount": 999.0,
            "order_date": datetime(2026, 6, 22, tzinfo=timezone.utc),
            "items": [{"item": "Cable Management Tray", "qty": 1, "price": 999.0}],
        },
        {
            "order_number": "ORD-1009",
            "customer_id": str(customer_ids[7]),  # Sneha
            "status": "completed",
            "total_amount": 6298.0,
            "order_date": datetime(2026, 6, 25, tzinfo=timezone.utc),
            "items": [{"item": "Mechanical Keyboard", "qty": 1, "price": 4999.0}, {"item": "Wireless Mouse", "qty": 1, "price": 1299.0}],
        },
        {
            "order_number": "ORD-1010",
            "customer_id": str(customer_ids[9]),  # Sanjay (Chennai)
            "status": "shipped",
            "total_amount": 28999.0,
            "order_date": datetime(2026, 6, 28, tzinfo=timezone.utc),
            "items": [{"item": "4K Ultra Monitor", "qty": 1, "price": 28999.0}],
        },
        # Intentional Orphan Orders (pointing to non-existent customer IDs)
        {
            "order_number": "ORD-ORPHAN-01",
            "customer_id": "507f1f77bcf86cd799439011",  # Non-existent ID 1
            "status": "pending",
            "total_amount": 4999.0,
            "order_date": datetime(2026, 6, 29, tzinfo=timezone.utc),
            "items": [{"item": "Mechanical Keyboard", "qty": 1, "price": 4999.0}],
        },
        {
            "order_number": "ORD-ORPHAN-02",
            "customer_id": "507f1f77bcf86cd799439022",  # Non-existent ID 2
            "status": "pending",
            "total_amount": 1299.0,
            "order_date": datetime(2026, 6, 30, tzinfo=timezone.utc),
            "items": [{"item": "Wireless Mouse", "qty": 1, "price": 1299.0}],
        },
    ]
    db.orders.insert_many(orders_data)

    print(f"MongoDB seeded successfully in '{db.name}':")
    print(f" - customers: {db.customers.count_documents({})} documents")
    print(f" - products:  {db.products.count_documents({})} documents")
    print(f" - orders:    {db.orders.count_documents({})} documents (including 2 intentional orphans)")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "mongodb://localhost:27017/devdb"
    seed_mongo(url)
