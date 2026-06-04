from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_PATH = Path(os.getenv("DATA_PATH", BASE_DIR / "data" / "customers.json"))
DB_PATH = Path(os.getenv("DB_PATH", BASE_DIR / "runtime" / "support_agent.db"))


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                tier TEXT NOT NULL,
                account_status TEXT NOT NULL,
                return_count_90d INTEGER NOT NULL DEFAULT 0,
                risk_notes TEXT
            );

            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer_id TEXT NOT NULL,
                status TEXT NOT NULL,
                purchased_at TEXT NOT NULL,
                delivered_at TEXT,
                total REAL NOT NULL,
                currency TEXT NOT NULL,
                refund_status TEXT NOT NULL,
                carrier_status TEXT,
                FOREIGN KEY(customer_id) REFERENCES customers(id)
            );

            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                sku TEXT NOT NULL,
                name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price REAL NOT NULL,
                category TEXT NOT NULL,
                final_sale INTEGER NOT NULL DEFAULT 0,
                digital INTEGER NOT NULL DEFAULT 0,
                license_redeemed INTEGER NOT NULL DEFAULT 0,
                consumable INTEGER NOT NULL DEFAULT 0,
                gift_card INTEGER NOT NULL DEFAULT 0,
                tags TEXT NOT NULL DEFAULT '[]',
                FOREIGN KEY(order_id) REFERENCES orders(id)
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                customer_id TEXT NOT NULL,
                message TEXT NOT NULL,
                decision TEXT NOT NULL,
                response TEXT NOT NULL,
                logs_json TEXT NOT NULL
            );
            """
        )

        existing = conn.execute("SELECT COUNT(*) AS count FROM customers").fetchone()["count"]
        if existing == 0:
            seed_database(conn)


def seed_database(conn: sqlite3.Connection) -> None:
    payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    for customer in payload["customers"]:
        conn.execute(
            """
            INSERT INTO customers
                (id, name, email, tier, account_status, return_count_90d, risk_notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                customer["id"],
                customer["name"],
                customer["email"],
                customer["tier"],
                customer["account_status"],
                customer.get("return_count_90d", 0),
                customer.get("risk_notes"),
            ),
        )
        for order in customer["orders"]:
            conn.execute(
                """
                INSERT INTO orders
                    (id, customer_id, status, purchased_at, delivered_at, total, currency, refund_status, carrier_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order["id"],
                    customer["id"],
                    order["status"],
                    order["purchased_at"],
                    order.get("delivered_at"),
                    order["total"],
                    order.get("currency", "USD"),
                    order.get("refund_status", "none"),
                    order.get("carrier_status"),
                ),
            )
            for item in order["items"]:
                conn.execute(
                    """
                    INSERT INTO order_items
                        (order_id, sku, name, quantity, unit_price, category, final_sale, digital,
                         license_redeemed, consumable, gift_card, tags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order["id"],
                        item["sku"],
                        item["name"],
                        item["quantity"],
                        item["unit_price"],
                        item["category"],
                        int(item.get("final_sale", False)),
                        int(item.get("digital", False)),
                        int(item.get("license_redeemed", False)),
                        int(item.get("consumable", False)),
                        int(item.get("gift_card", False)),
                        json.dumps(item.get("tags", [])),
                    ),
                )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def get_customer(customer_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        return row_to_dict(conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone())


def list_customers() -> list[dict[str, Any]]:
    with connect() as conn:
        customers = [dict(row) for row in conn.execute("SELECT * FROM customers ORDER BY id").fetchall()]
        for customer in customers:
            customer["orders"] = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT id, status, purchased_at, delivered_at, total, currency, refund_status, carrier_status
                    FROM orders
                    WHERE customer_id = ?
                    ORDER BY purchased_at DESC
                    """,
                    (customer["id"],),
                ).fetchall()
            ]
        return customers


def get_customer_orders(customer_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM orders WHERE customer_id = ? ORDER BY purchased_at DESC", (customer_id,)
            ).fetchall()
        ]


def get_order(order_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        order = row_to_dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
        if not order:
            return None
        items = [
            dict(row)
            for row in conn.execute(
                """
                SELECT sku, name, quantity, unit_price, category, final_sale, digital,
                       license_redeemed, consumable, gift_card, tags
                FROM order_items
                WHERE order_id = ?
                """,
                (order_id,),
            ).fetchall()
        ]
        for item in items:
            item["final_sale"] = bool(item["final_sale"])
            item["digital"] = bool(item["digital"])
            item["license_redeemed"] = bool(item["license_redeemed"])
            item["consumable"] = bool(item["consumable"])
            item["gift_card"] = bool(item["gift_card"])
            item["tags"] = json.loads(item["tags"])
        order["items"] = items
        return order


def save_decision(
    session_id: str,
    created_at: str,
    customer_id: str,
    message: str,
    decision: str,
    response: str,
    logs: list[dict[str, Any]],
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO decisions
                (session_id, created_at, customer_id, message, decision, response, logs_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, created_at, customer_id, message, decision, response, json.dumps(logs)),
        )


def list_decisions(limit: int = 25) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, created_at, customer_id, message, decision, response, logs_json
            FROM decisions
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                **dict(row),
                "logs": json.loads(row["logs_json"]),
            }
            for row in rows
        ]

