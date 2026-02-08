from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Optional


@dataclass(frozen=True)
class Offer:
    id: int
    name: str
    quantity: int
    price: str
    active: bool
    created_at: str
    announce_chat_id: Optional[int]
    announce_message_id: Optional[int]


class OfferStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS offers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    price TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    announce_chat_id INTEGER,
                    announce_message_id INTEGER
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_offers_active ON offers(active)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def add_offer(self, name: str, quantity: int, price: str) -> Offer:
        created_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO offers (name, quantity, price, active, created_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (name, quantity, price, created_at),
            )
            offer_id = int(cur.lastrowid)
        offer = self.get_offer(offer_id)
        if offer is None:
            raise RuntimeError("Failed to load offer after insert")
        return offer

    def get_offer(self, offer_id: int) -> Optional[Offer]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM offers WHERE id = ?",
                (offer_id,),
            ).fetchone()
        return _row_to_offer(row) if row else None

    def list_offers(self, active_only: bool = True) -> Iterable[Offer]:
        query = "SELECT * FROM offers"
        params = ()
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_offer(row) for row in rows]

    def set_active(self, offer_id: int, active: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE offers SET active = ? WHERE id = ?",
                (1 if active else 0, offer_id),
            )
        return cur.rowcount > 0

    def update_quantity(self, offer_id: int, quantity: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE offers SET quantity = ? WHERE id = ?",
                (quantity, offer_id),
            )
        return cur.rowcount > 0

    def update_price(self, offer_id: int, price: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE offers SET price = ? WHERE id = ?",
                (price, offer_id),
            )
        return cur.rowcount > 0

    def attach_announcement(
        self, offer_id: int, chat_id: int, message_id: int
    ) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE offers
                SET announce_chat_id = ?, announce_message_id = ?
                WHERE id = ?
                """,
                (chat_id, message_id, offer_id),
            )
        return cur.rowcount > 0

    def get_setting(self, key: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )


def normalize_price(value: str) -> str:
    try:
        dec = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Price must be a number") from exc
    if dec <= 0:
        raise ValueError("Price must be greater than zero")
    if dec == dec.to_integral():
        return str(dec.quantize(Decimal("1")))
    return f"{dec.quantize(Decimal('0.01')):.2f}".rstrip("0").rstrip(".")


def parse_quantity(value: str) -> int:
    try:
        quantity = int(value)
    except ValueError as exc:
        raise ValueError("Quantity must be a whole number") from exc
    if quantity < 0:
        raise ValueError("Quantity must be zero or greater")
    return quantity


def _row_to_offer(row: sqlite3.Row) -> Offer:
    return Offer(
        id=int(row["id"]),
        name=str(row["name"]),
        quantity=int(row["quantity"]),
        price=str(row["price"]),
        active=bool(row["active"]),
        created_at=str(row["created_at"]),
        announce_chat_id=row["announce_chat_id"],
        announce_message_id=row["announce_message_id"],
    )
