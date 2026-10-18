"""QRIS Gateway: verifikasi pembayaran QRIS berbasis partner portal.

Paket ini sengaja **read-only** terhadap akun merchant: tidak ada operasi
yang mengubah dana (tidak ada pembuatan pembayaran, refund, atau penarikan).
Yang dilakukan hanya membaca mutasi dan mencocokkannya ke order.
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "config",
    "db",
    "events",
    "matcher",
    "poller",
    "qris",
    "service",
    "shopee",
]
