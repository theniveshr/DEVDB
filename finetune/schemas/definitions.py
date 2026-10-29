"""20 Realistic Database Schema Definitions for DevDB Fine-Tuning.

Includes:
- 14 SQL schemas (8 with foreign keys, 6 without foreign keys)
- 6 MongoDB collection sets with document structures and types
"""

from typing import Dict, Any, List

# =============================================================================
# 14 SQL SCHEMAS
# =============================================================================

SQL_SCHEMAS: Dict[str, Dict[str, Any]] = {
    # 1. E-Commerce (WITH FKs)
    "ecommerce_sql": {
        "db_type": "sqlite",
        "has_foreign_keys": True,
        "description": "Online retail store with products, shoppers, purchase orders, purchase items, and store reviews",
        "tables": {
            "shoppers": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "full_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "email_address", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "billing_city", "type": "TEXT", "primary_key": False, "nullable": True},
                    {"name": "joined_at", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": [{"name": "idx_shopper_email", "column_names": ["email_address"], "unique": True}]
            },
            "inventory_items": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "product_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "item_category", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "retail_price", "type": "REAL", "primary_key": False, "nullable": False},
                    {"name": "available_stock", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "purchase_orders": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "shopper_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "order_timestamp", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                    {"name": "fulfillment_status", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "invoice_total", "type": "REAL", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_orders_shopper", "constrained_columns": ["shopper_id"], "referred_table": "shoppers", "referred_columns": ["id"]}
                ],
                "indexes": [{"name": "idx_orders_shopper", "column_names": ["shopper_id"], "unique": False}]
            },
            "purchase_lines": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "purchase_order_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "item_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "item_quantity", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "sale_price", "type": "REAL", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_lines_order", "constrained_columns": ["purchase_order_id"], "referred_table": "purchase_orders", "referred_columns": ["id"]},
                    {"name": "fk_lines_item", "constrained_columns": ["item_id"], "referred_table": "inventory_items", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "store_reviews": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "item_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "shopper_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "star_rating", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "feedback_text", "type": "TEXT", "primary_key": False, "nullable": True},
                ],
                "foreign_keys": [
                    {"name": "fk_reviews_item", "constrained_columns": ["item_id"], "referred_table": "inventory_items", "referred_columns": ["id"]},
                    {"name": "fk_reviews_shopper", "constrained_columns": ["shopper_id"], "referred_table": "shoppers", "referred_columns": ["id"]}
                ],
                "indexes": []
            }
        }
    },

    # 2. School Management (WITH FKs)
    "school_sql": {
        "db_type": "sqlite",
        "has_foreign_keys": True,
        "description": "Academic school system with students, teachers, courses, enrollments, and classrooms",
        "tables": {
            "students": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "full_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "grade_level", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "gpa", "type": "REAL", "primary_key": False, "nullable": True},
                    {"name": "enrollment_date", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "teachers": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "subject", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "email", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "hire_date", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "courses": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "course_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "teacher_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "credits", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_course_teacher", "constrained_columns": ["teacher_id"], "referred_table": "teachers", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "enrollments": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "student_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "course_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "letter_grade", "type": "TEXT", "primary_key": False, "nullable": True},
                ],
                "foreign_keys": [
                    {"name": "fk_enroll_student", "constrained_columns": ["student_id"], "referred_table": "students", "referred_columns": ["id"]},
                    {"name": "fk_enroll_course", "constrained_columns": ["course_id"], "referred_table": "courses", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "classrooms": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "room_number", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "building", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "capacity", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            }
        }
    },

    # 3. Hospital Care (WITH FKs)
    "hospital_sql": {
        "db_type": "postgres",
        "has_foreign_keys": True,
        "description": "Hospital patient records, doctors, departments, appointments, and prescriptions",
        "tables": {
            "departments": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "dept_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "floor", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "doctors": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "doctor_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "specialty", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "dept_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_doc_dept", "constrained_columns": ["dept_id"], "referred_table": "departments", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "patients": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "patient_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "blood_type", "type": "TEXT", "primary_key": False, "nullable": True},
                    {"name": "birth_date", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                    {"name": "phone", "type": "TEXT", "primary_key": False, "nullable": True},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "appointments": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "patient_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "doctor_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "appointment_date", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                    {"name": "diagnosis", "type": "TEXT", "primary_key": False, "nullable": True},
                ],
                "foreign_keys": [
                    {"name": "fk_appt_patient", "constrained_columns": ["patient_id"], "referred_table": "patients", "referred_columns": ["id"]},
                    {"name": "fk_appt_doctor", "constrained_columns": ["doctor_id"], "referred_table": "doctors", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "medical_records": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "patient_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "condition", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "treatment", "type": "TEXT", "primary_key": False, "nullable": True},
                    {"name": "recorded_at", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_med_patient", "constrained_columns": ["patient_id"], "referred_table": "patients", "referred_columns": ["id"]}
                ],
                "indexes": []
            }
        }
    },

    # 4. Library Management (WITH FKs)
    "library_sql": {
        "db_type": "sqlite",
        "has_foreign_keys": True,
        "description": "Public library catalog with books, authors, members, loans, and branches",
        "tables": {
            "authors": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "author_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "nationality", "type": "TEXT", "primary_key": False, "nullable": True},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "books": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "title", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "isbn", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "author_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "published_year", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_books_author", "constrained_columns": ["author_id"], "referred_table": "authors", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "members": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "member_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "card_number", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "membership_date", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "loans": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "book_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "member_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "loan_date", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                    {"name": "return_date", "type": "TIMESTAMP", "primary_key": False, "nullable": True},
                ],
                "foreign_keys": [
                    {"name": "fk_loans_book", "constrained_columns": ["book_id"], "referred_table": "books", "referred_columns": ["id"]},
                    {"name": "fk_loans_member", "constrained_columns": ["member_id"], "referred_table": "members", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "branches": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "branch_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "city", "type": "TEXT", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            }
        }
    },

    # 5. Banking & Transactions (WITH FKs)
    "banking_sql": {
        "db_type": "postgres",
        "has_foreign_keys": True,
        "description": "Financial core system with bank customers, accounts, cards, and transactions",
        "tables": {
            "customers": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "customer_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "tax_id", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "email", "type": "TEXT", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "accounts": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "customer_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "account_type", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "balance", "type": "REAL", "primary_key": False, "nullable": False},
                    {"name": "currency", "type": "TEXT", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_acct_cust", "constrained_columns": ["customer_id"], "referred_table": "customers", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "transactions": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "account_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "amount", "type": "REAL", "primary_key": False, "nullable": False},
                    {"name": "trans_type", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "timestamp", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_trans_acct", "constrained_columns": ["account_id"], "referred_table": "accounts", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "cards": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "account_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "card_number", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "expiration_date", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "is_active", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_cards_acct", "constrained_columns": ["account_id"], "referred_table": "accounts", "referred_columns": ["id"]}
                ],
                "indexes": []
            }
        }
    },

    # 6. Hotel Booking (WITH FKs)
    "hotel_sql": {
        "db_type": "sqlite",
        "has_foreign_keys": True,
        "description": "Hotel property management with guests, rooms, reservations, payments, and amenities",
        "tables": {
            "guests": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "guest_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "email", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "country", "type": "TEXT", "primary_key": False, "nullable": True},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "rooms": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "room_number", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "room_type", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "nightly_rate", "type": "REAL", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "bookings": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "guest_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "room_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "check_in", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                    {"name": "check_out", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_book_guest", "constrained_columns": ["guest_id"], "referred_table": "guests", "referred_columns": ["id"]},
                    {"name": "fk_book_room", "constrained_columns": ["room_id"], "referred_table": "rooms", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "payments": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "booking_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "amount", "type": "REAL", "primary_key": False, "nullable": False},
                    {"name": "payment_method", "type": "TEXT", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_pay_book", "constrained_columns": ["booking_id"], "referred_table": "bookings", "referred_columns": ["id"]}
                ],
                "indexes": []
            }
        }
    },

    # 7. HR & Payroll (WITH FKs)
    "hr_sql": {
        "db_type": "postgres",
        "has_foreign_keys": True,
        "description": "Human resources database with employees, departments, salaries, and performance reviews",
        "tables": {
            "departments": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "department_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "budget", "type": "REAL", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "employees": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "first_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "last_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "dept_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "job_title", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "salary", "type": "REAL", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_emp_dept", "constrained_columns": ["dept_id"], "referred_table": "departments", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "reviews": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "employee_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "review_period", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "score", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_rev_emp", "constrained_columns": ["employee_id"], "referred_table": "employees", "referred_columns": ["id"]}
                ],
                "indexes": []
            }
        }
    },

    # 8. Food Delivery (WITH FKs)
    "food_delivery_sql": {
        "db_type": "sqlite",
        "has_foreign_keys": True,
        "description": "Food on-demand delivery with restaurants, menu items, delivery orders, and couriers",
        "tables": {
            "restaurants": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "restaurant_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "cuisine", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "rating", "type": "REAL", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "menu_items": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "restaurant_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "item_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "price", "type": "REAL", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_menu_rest", "constrained_columns": ["restaurant_id"], "referred_table": "restaurants", "referred_columns": ["id"]}
                ],
                "indexes": []
            },
            "orders": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "restaurant_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "delivery_address", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "status", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "order_total", "type": "REAL", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [
                    {"name": "fk_deliv_rest", "constrained_columns": ["restaurant_id"], "referred_table": "restaurants", "referred_columns": ["id"]}
                ],
                "indexes": []
            }
        }
    },

    # 9. Real Estate (WITHOUT FKs)
    "real_estate_sql": {
        "db_type": "sqlite",
        "has_foreign_keys": False,
        "description": "Property listing network with properties, agents, buyers, tours (unconstrained)",
        "tables": {
            "properties": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "title", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "city", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "price", "type": "REAL", "primary_key": False, "nullable": False},
                    {"name": "bedrooms", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "agent_id", "type": "INTEGER", "primary_key": False, "nullable": True},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "agents": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "agent_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "phone", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "license_number", "type": "TEXT", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "tours": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "property_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "client_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "tour_date", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            }
        }
    },

    # 10. Airline Reservation (WITHOUT FKs)
    "airline_sql": {
        "db_type": "postgres",
        "has_foreign_keys": False,
        "description": "Commercial airline schedules, passengers, flights, and airports without relational constraints",
        "tables": {
            "flights": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "flight_number", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "origin", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "destination", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "departure_time", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                    {"name": "seat_capacity", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "passengers": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "passenger_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "passport_number", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "nationality", "type": "TEXT", "primary_key": False, "nullable": True},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "tickets": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "flight_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "passenger_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "seat_number", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "fare", "type": "REAL", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            }
        }
    },

    # 11. Fitness Center (WITHOUT FKs)
    "fitness_sql": {
        "db_type": "sqlite",
        "has_foreign_keys": False,
        "description": "Gym and fitness center tracking members, trainers, sessions, and workouts",
        "tables": {
            "members": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "member_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "plan", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "joined_date", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "trainers": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "trainer_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "specialty", "type": "TEXT", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "gym_classes": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "class_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "trainer_id", "type": "INTEGER", "primary_key": False, "nullable": True},
                    {"name": "capacity", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            }
        }
    },

    # 12. Warehouse Inventory (WITHOUT FKs)
    "inventory_sql": {
        "db_type": "postgres",
        "has_foreign_keys": False,
        "description": "Logistics inventory tracking parts, suppliers, stock quantities, and transfers",
        "tables": {
            "warehouse_items": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "sku", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "item_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "quantity", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "warehouse_zone", "type": "TEXT", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "suppliers": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "supplier_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "contact_email", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "country", "type": "TEXT", "primary_key": False, "nullable": True},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "transfers": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "item_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "from_zone", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "to_zone", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "quantity", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            }
        }
    },

    # 13. Ride Sharing (WITHOUT FKs)
    "ride_sharing_sql": {
        "db_type": "sqlite",
        "has_foreign_keys": False,
        "description": "Mobility platform with riders, drivers, vehicles, and trip logs",
        "tables": {
            "riders": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "rider_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "rating", "type": "REAL", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "drivers": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "driver_name", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "license_plate", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "rating", "type": "REAL", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "trips": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "rider_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "driver_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "distance_km", "type": "REAL", "primary_key": False, "nullable": False},
                    {"name": "fare", "type": "REAL", "primary_key": False, "nullable": False},
                    {"name": "trip_status", "type": "TEXT", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            }
        }
    },

    # 14. Media Streaming (WITHOUT FKs)
    "streaming_sql": {
        "db_type": "postgres",
        "has_foreign_keys": False,
        "description": "Video-on-demand platform cataloging users, titles, streaming logs, and subscriptions",
        "tables": {
            "users": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "username", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "plan_tier", "type": "TEXT", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "movies": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "title", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "genre", "type": "TEXT", "primary_key": False, "nullable": False},
                    {"name": "duration_minutes", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "release_year", "type": "INTEGER", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            },
            "watch_history": {
                "columns": [
                    {"name": "id", "type": "INTEGER", "primary_key": True, "nullable": False},
                    {"name": "user_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "movie_id", "type": "INTEGER", "primary_key": False, "nullable": False},
                    {"name": "progress_percent", "type": "REAL", "primary_key": False, "nullable": False},
                    {"name": "watched_at", "type": "TIMESTAMP", "primary_key": False, "nullable": False},
                ],
                "foreign_keys": [],
                "indexes": []
            }
        }
    }
}


# =============================================================================
# 6 MONGODB SCHEMAS
# =============================================================================

MONGO_SCHEMAS: Dict[str, Dict[str, Any]] = {
    # 15. Social Network
    "social_network_mongo": {
        "db_type": "mongodb",
        "description": "User profiles, dynamic social posts, nested comments, and tags",
        "tables": {
            "users": {
                "doc_count": 50,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "username", "type": "string"},
                    {"name": "email", "type": "string"},
                    {"name": "bio", "type": "string"},
                    {"name": "follower_count", "type": "int"},
                    {"name": "interests", "type": "array[string]"},
                ]
            },
            "posts": {
                "doc_count": 120,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "author_id", "type": "ObjectId"},
                    {"name": "content", "type": "string"},
                    {"name": "likes_count", "type": "int"},
                    {"name": "tags", "type": "array[string]"},
                    {"name": "comments", "type": "array[object]", "is_embedded": True},
                    {"name": "created_at", "type": "date"},
                ]
            }
        }
    },

    # 16. IoT Telemetry
    "iot_telemetry_mongo": {
        "db_type": "mongodb",
        "description": "Smart sensor network with devices, telemetry time-series points, and battery health",
        "tables": {
            "devices": {
                "doc_count": 30,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "device_uuid", "type": "string"},
                    {"name": "model", "type": "string"},
                    {"name": "firmware_version", "type": "string"},
                    {"name": "status", "type": "string"},
                ]
            },
            "telemetry_events": {
                "doc_count": 500,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "device_uuid", "type": "string"},
                    {"name": "temperature_c", "type": "double"},
                    {"name": "humidity_pct", "type": "double"},
                    {"name": "battery_level", "type": "int"},
                    {"name": "timestamp", "type": "date"},
                ]
            }
        }
    },

    # 17. Gaming Leaderboards
    "gaming_leaderboard_mongo": {
        "db_type": "mongodb",
        "description": "Multiplayer game statistics, character loadouts, matches, and ranks",
        "tables": {
            "players": {
                "doc_count": 80,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "gamer_tag", "type": "string"},
                    {"name": "level", "type": "int"},
                    {"name": "mmr_rating", "type": "int"},
                    {"name": "unlocked_skins", "type": "array[string]"},
                ]
            },
            "matches": {
                "doc_count": 250,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "match_mode", "type": "string"},
                    {"name": "duration_sec", "type": "int"},
                    {"name": "winner_id", "type": "ObjectId"},
                    {"name": "player_scores", "type": "array[object]", "is_embedded": True},
                ]
            }
        }
    },

    # 18. Headless CMS
    "content_cms_mongo": {
        "db_type": "mongodb",
        "description": "Content publishing platform with articles, revisions, taxonomies, and authors",
        "tables": {
            "articles": {
                "doc_count": 60,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "title", "type": "string"},
                    {"name": "slug", "type": "string"},
                    {"name": "status", "type": "string"},
                    {"name": "view_count", "type": "int"},
                    {"name": "categories", "type": "array[string]"},
                    {"name": "published_at", "type": "date"},
                ]
            },
            "authors": {
                "doc_count": 15,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "author_name", "type": "string"},
                    {"name": "twitter_handle", "type": "string"},
                    {"name": "article_count", "type": "int"},
                ]
            }
        }
    },

    # 19. Logistics & Waybills
    "logistics_tracking_mongo": {
        "db_type": "mongodb",
        "description": "Supply chain shipment milestones, transit checkpoints, and courier assignments",
        "tables": {
            "shipments": {
                "doc_count": 100,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "tracking_code", "type": "string"},
                    {"name": "origin_hub", "type": "string"},
                    {"name": "destination_hub", "type": "string"},
                    {"name": "current_status", "type": "string"},
                    {"name": "checkpoints", "type": "array[object]", "is_embedded": True},
                    {"name": "estimated_delivery", "type": "date"},
                ]
            },
            "couriers": {
                "doc_count": 25,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "courier_name", "type": "string"},
                    {"name": "active_vehicle", "type": "string"},
                    {"name": "assigned_packages", "type": "int"},
                ]
            }
        }
    },

    # 20. Customer Helpdesk
    "customer_support_mongo": {
        "db_type": "mongodb",
        "description": "Customer service tickets, priority escalation, agent responses, and ratings",
        "tables": {
            "tickets": {
                "doc_count": 90,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "subject", "type": "string"},
                    {"name": "priority", "type": "string"},
                    {"name": "status", "type": "string"},
                    {"name": "customer_email", "type": "string"},
                    {"name": "messages", "type": "array[object]", "is_embedded": True},
                    {"name": "created_at", "type": "date"},
                ]
            },
            "agents": {
                "doc_count": 12,
                "columns": [
                    {"name": "_id", "type": "ObjectId", "primary_key": True},
                    {"name": "agent_name", "type": "string"},
                    {"name": "tier", "type": "string"},
                    {"name": "active_tickets", "type": "int"},
                ]
            }
        }
    }
}


def get_all_schemas() -> Dict[str, Dict[str, Any]]:
    """Return dictionary of all 20 schemas."""
    merged = {}
    merged.update(SQL_SCHEMAS)
    merged.update(MONGO_SCHEMAS)
    return merged
