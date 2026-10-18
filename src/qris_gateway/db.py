"""Lapisan persistensi SQLite.

Dua jaminan penting dipasang di level skema, bukan di level kode, sehingga
tidak bisa dilanggar oleh bug aplikasi maupun restart server:

1. **Anti-kolisi nominal** -- indeks unik parsial pada ``orders(amount)``
   untuk baris berstatus ``open``. Database menolak dua order terbuka dengan
   nominal sama, sehingga nominal bisa dipakai sebagai kunci pencocokan.
2. **Anti-klaim-ganda** -- ``claims.txn_id`` adalah PRIMARY KEY. Satu
   transaksi hanya bisa diklaim sekali, permanen, lintas restart.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

ORDER_OPEN = "open"
ORDER_PAID = "paid"
ORDER_EXPIRED = "expired"
ORDER_CANCELLED = "cancelled"

TX_SUCCESS = 3  # kode status transaksi ShopeePay: 3 = sukses

SCHEMA_VERSION = 1


class DBError(RuntimeError):
    """Kesalahan tak terduga pada lapisan database."""


class AmountExhausted(DBError):
    """Tidak ada nominal unik yang tersedia pada rentang suffix."""


@dataclass(slots=True, frozen=True)
class Order:
    id: str
    amount: int
    base_amount: int
    status: str
    qris: str
    created_at: int
    expires_at: int
    paid_at: int | None
    txn_id: str | None
    issuer: str | None

    @property
    def is_open(self) -> bool:
        return self.status == ORDER_OPEN

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "amount": self.amount,
            "base_amount": self.base_amount,
            "suffix": self.amount - self.base_amount,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "paid_at": self.paid_at,
            "txn_id": self.txn_id,
            "issuer": self.issuer,
        }


@dataclass(slots=True, frozen=True)
class Transaction:
    txn_id: str
    amount: int
    status: int
    create_time: int

    @property
    def is_success(self) -> bool:
        return self.status == TX_SUCCESS


@dataclass(slots=True, frozen=True)
class Orphan:
    txn_id: str
    amount: int
    create_time: int
    note: str
    created_at: int
    resolved: int


# ---------------------------------------------------------------------------
# Skema
# ---------------------------------------------------------------------------

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id          TEXT    PRIMARY KEY,
    amount      INTEGER NOT NULL,
    base_amount INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    qris        TEXT    NOT NULL,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    paid_at     INTEGER,
    txn_id      TEXT,
    issuer      TEXT,
    CHECK (amount > 0),
    CHECK (status IN ('open','paid','expired','cancelled'))
);

-- Jaminan anti-kolisi: satu nominal hanya boleh dipakai satu order terbuka.
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_amount_open
    ON orders(amount) WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at DESC);

CREATE TABLE IF NOT EXISTS claims (
    txn_id     TEXT    PRIMARY KEY,
    order_id   TEXT    NOT NULL REFERENCES orders(id),
    claimed_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orphans (
    txn_id      TEXT    PRIMARY KEY,
    amount      INTEGER NOT NULL,
    create_time INTEGER NOT NULL,
    note        TEXT    NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL,
    resolved    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_orphans_resolved ON orphans(resolved, created_at DESC);

CREATE TABLE IF NOT EXISTS seen_txns (
    txn_id      TEXT    PRIMARY KEY,
    create_time INTEGER NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


class Database:
    """Pembungkus SQLite yang aman dipakai dari banyak thread.

    Operasi SQLite lokal berlangsung dalam hitungan mikrodetik, jadi
    pemanggilan sinkron dari konteks async dapat diterima dan jauh lebih
    sederhana daripada mengelola koneksi async.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            self._conn.commit()

    # -- lifecycle ------------------------------------------------------ #

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- meta / state --------------------------------------------------- #

    def set_state(self, key: str, value: str | int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self._conn.commit()

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def get_state_int(self, key: str, default: int) -> int:
        raw = self.get_state(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default

    # -- orders --------------------------------------------------------- #

    def allocate_and_create_order(
        self,
        *,
        order_id: str,
        base_amount: int,
        qris_builder,
        ttl: int,
        suffix_min: int = 1,
        suffix_max: int = 999,
        now: int | None = None,
    ) -> Order:
        """Cari nominal unik lalu buat order, secara atomik.

        Nominal dicoba dari ``base_amount + suffix_min`` naik sampai
        ``base_amount + suffix_max``. Karena ada indeks unik parsial,
        tabrakan ditolak database dan loop lanjut ke kandidat berikutnya,
        sehingga aman walau ada permintaan bersamaan.

        Args:
            qris_builder: callable ``(amount, reference) -> str`` yang
                menghasilkan payload QRIS untuk nominal tersebut.

        Raises:
            AmountExhausted: bila seluruh rentang suffix sedang terpakai.
        """
        now = int(time.time()) if now is None else now
        expires_at = now + ttl

        with self._lock:
            for suffix in range(suffix_min, suffix_max + 1):
                amount = base_amount + suffix
                qris = qris_builder(amount, order_id)
                try:
                    self._conn.execute("BEGIN IMMEDIATE")
                    self._conn.execute(
                        "INSERT INTO orders (id, amount, base_amount, status, qris,"
                        " created_at, expires_at) VALUES (?,?,?,?,?,?,?)",
                        (order_id, amount, base_amount, ORDER_OPEN, qris, now, expires_at),
                    )
                    self._conn.commit()
                except sqlite3.IntegrityError:
                    self._conn.rollback()
                    continue
                except Exception:
                    self._conn.rollback()
                    raise

                return self.get_order(order_id)  # type: ignore[return-value]

        raise AmountExhausted(
            f"semua nominal pada rentang +{suffix_min}..+{suffix_max} sedang terpakai"
        )

    def get_order(self, order_id: str) -> Order | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
        return _to_order(row) if row else None

    def find_open_order_by_amount(self, amount: int) -> Order | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM orders WHERE amount = ? AND status = ?",
                (amount, ORDER_OPEN),
            ).fetchone()
        return _to_order(row) if row else None

    def list_orders(
        self, *, status: str | None = None, limit: int = 50
    ) -> list[Order]:
        sql = "SELECT * FROM orders"
        params: list[object] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_to_order(r) for r in rows]

    def expire_stale_orders(self, now: int | None = None) -> int:
        """Tandai order terbuka yang sudah lewat TTL sebagai kedaluwarsa."""
        now = int(time.time()) if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "UPDATE orders SET status = ? WHERE status = ? AND expires_at <= ?",
                (ORDER_EXPIRED, ORDER_OPEN, now),
            )
            self._conn.commit()
            return cur.rowcount

    # -- klaim ---------------------------------------------------------- #

    def claim(self, order_id: str, txn: Transaction, issuer: str | None = None) -> bool:
        """Klaim transaksi untuk sebuah order, secara atomik dan idempoten.

        Returns:
            True bila klaim berhasil (pertama kali).
            False bila transaksi ini sudah pernah diklaim order lain.
        """
        now = int(time.time())
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO claims (txn_id, order_id, claimed_at) VALUES (?,?,?)",
                    (txn.txn_id, order_id, now),
                )
                self._conn.execute(
                    "UPDATE orders SET status = ?, paid_at = ?, txn_id = ?, issuer = ?"
                    " WHERE id = ?",
                    (ORDER_PAID, now, txn.txn_id, issuer, order_id),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False
            except Exception:
                self._conn.rollback()
                raise
        return True

    def is_txn_seen(self, txn_id: str) -> bool:
        """Sudah pernah dilihat di daftar mutasi (untuk majukan watermark)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seen_txns WHERE txn_id = ?", (txn_id,)
            ).fetchone()
        return row is not None

    def is_txn_claimed(self, txn_id: str) -> bool:
        """Sudah pernah diklaim oleh sebuah order.

        Berbeda dari :meth:`is_txn_seen`: ``seen`` menandai bahwa transaksi
        pernah muncul di daftar mutasi, sedangkan ``claimed`` menandai bahwa
        transaksi itu sudah dipakai untuk melunasi order. Pemeriksaan
        anti-klaim-ganda harus memakai yang kedua.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM claims WHERE txn_id = ?", (txn_id,)
            ).fetchone()
        return row is not None

    def mark_txn_seen(self, txn_id: str, create_time: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen_txns (txn_id, create_time) VALUES (?,?)",
                (txn_id, create_time),
            )
            self._conn.commit()

    # -- orphans -------------------------------------------------------- #

    def record_orphan(
        self, txn: Transaction, note: str, *, now: int | None = None
    ) -> bool:
        """Catat pembayaran yang tidak cocok dengan order manapun.

        Returns:
            True bila ini catatan baru, False bila sudah pernah dicatat.
        """
        now = int(time.time()) if now is None else now
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO orphans"
                " (txn_id, amount, create_time, note, created_at) VALUES (?,?,?,?,?)",
                (txn.txn_id, txn.amount, txn.create_time, note, now),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def list_orphans(self, *, resolved: int | None = 0, limit: int = 100) -> list[Orphan]:
        sql = "SELECT * FROM orphans"
        params: list[object] = []
        if resolved is not None:
            sql += " WHERE resolved = ?"
            params.append(resolved)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [
            Orphan(
                txn_id=r["txn_id"],
                amount=r["amount"],
                create_time=r["create_time"],
                note=r["note"],
                created_at=r["created_at"],
                resolved=r["resolved"],
            )
            for r in rows
        ]

    def resolve_orphan(self, txn_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE orphans SET resolved = 1 WHERE txn_id = ?", (txn_id,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    # -- statistik ------------------------------------------------------ #

    def stats(self) -> dict[str, object]:
        with self._lock:
            def scalar(sql: str, *params: object) -> int:
                row = self._conn.execute(sql, params).fetchone()
                return int(row[0]) if row else 0

            return {
                "orders_open": scalar(
                    "SELECT COUNT(*) FROM orders WHERE status = ?", ORDER_OPEN
                ),
                "orders_paid": scalar(
                    "SELECT COUNT(*) FROM orders WHERE status = ?", ORDER_PAID
                ),
                "orders_expired": scalar(
                    "SELECT COUNT(*) FROM orders WHERE status = ?", ORDER_EXPIRED
                ),
                "claims_total": scalar("SELECT COUNT(*) FROM claims"),
                "orphans_open": scalar(
                    "SELECT COUNT(*) FROM orphans WHERE resolved = 0"
                ),
                "claimed_value": scalar(
                    "SELECT COALESCE(SUM(amount),0) FROM orders WHERE status = ?",
                    ORDER_PAID,
                ),
            }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _to_order(row: sqlite3.Row) -> Order:
    return Order(
        id=row["id"],
        amount=row["amount"],
        base_amount=row["base_amount"],
        status=row["status"],
        qris=row["qris"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        paid_at=row["paid_at"],
        txn_id=row["txn_id"],
        issuer=row["issuer"],
    )


def to_transactions(rows: Iterable[dict]) -> Sequence[Transaction]:
    """Normalisasi baris mentah dari ShopeePay menjadi :class:`Transaction`."""
    out: list[Transaction] = []
    for r in rows:
        txn_id = str(r.get("transactionId") or r.get("displayTransactionId") or "")
        if not txn_id:
            continue
        raw_amount = str(r.get("amount") or "0").replace(".", "").replace(",", "")
        try:
            amount = int(raw_amount)
        except ValueError:
            continue
        try:
            status = int(r.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        try:
            create_time = int(r.get("createTime") or 0)
        except (TypeError, ValueError):
            create_time = 0
        out.append(Transaction(txn_id, amount, status, create_time))
    return out
