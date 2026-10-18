"""Sumber transaksi palsu untuk pengembangan dan demo.

Aktif hanya bila ``MOCK_MODE=true``. Berguna untuk:

* mengembangkan dan menguji alur lengkap tanpa token ShopeePay;
* demo ke pihak lain tanpa menyentuh akun merchant asli;
* mereproduksi skenario sulit (telat bayar, klaim ganda) secara deterministik.

Modul ini tidak pernah dipakai saat ``MOCK_MODE`` tidak diaktifkan.
"""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class MockSource:
    """Memenuhi protokol ``TransactionSource`` tanpa menyentuh jaringan."""

    def __init__(self) -> None:
        self._txns: list[dict[str, Any]] = []
        self._issuer_default = "MOCK-OVO"
        self.payments_injected = 0

    # -- TransactionSource ---------------------------------------------- #

    async def list_transactions(
        self, start_time: int, end_time: int, page_size: int, cursor: str = ""
    ) -> tuple[list[dict[str, Any]], str]:
        rows = [
            t
            for t in self._txns
            if start_time <= int(t.get("createTime", 0)) <= end_time
        ]
        return rows[:page_size], ""

    async def get_issuer(self, txn_id: str) -> str | None:
        return self._issuer_default

    async def aclose(self) -> None:
        return None

    # -- kendali untuk pengujian ---------------------------------------- #

    def simulate_payment(
        self,
        amount: int,
        *,
        issuer: str | None = None,
        txn_id: str | None = None,
        status: int = 3,
        create_time: int | None = None,
    ) -> dict[str, Any]:
        """Suntikkan satu transaksi palsu ke daftar mutasi."""
        if issuer:
            self._issuer_default = issuer
        txn = {
            "transactionId": txn_id or f"MOCK-{int(time.time() * 1000)}",
            "displayTransactionId": txn_id or f"MOCK-{int(time.time() * 1000)}",
            "amount": f"{amount:,}".replace(",", "."),
            "status": status,
            "createTime": create_time if create_time is not None else int(time.time()),
        }
        self._txns.append(txn)
        self.payments_injected += 1
        log.info("[MOCK] Pembayaran disimulasikan: %s sebesar %s", txn["transactionId"], amount)
        return txn

    def reset(self) -> None:
        self._txns.clear()
        self.payments_injected = 0

    @property
    def pending(self) -> int:
        return len(self._txns)
