"""Poller tunggal dengan penanda waktu (watermark) yang tahan restart.

Ini pengganti pola polling berlapis pada implementasi lama:

* lama: tiap halaman checkout menembak endpoint cek, yang masing-masing
  menembak ShopeePay. N pembeli = N x M request per interval.
* baru: satu loop di server menembak ShopeePay sekali per interval, lalu
  menyiarkan hasilnya ke semua pelanggan lewat :class:`EventBus`.

Watermark (``max(createTime)`` yang sudah diproses) disimpan di database,
sehingga restart server tidak menyebabkan transaksi terlewat maupun
diproses ulang.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from .config import Settings
from .db import Database, Transaction, to_transactions
from .events import Event, EventBus
from .matcher import find_match, is_candidate
from .shopee import ShopeeError, TokenInvalid, TransactionSource

log = logging.getLogger(__name__)

WATERMARK_KEY = "poll_watermark"
TOKEN_ALIVE_KEY = "token_alive"


@dataclass(slots=True)
class PollerStats:
    cycles: int = 0
    errors: int = 0
    looked_at: int = 0
    matched: int = 0
    orphans: int = 0
    last_error: str = ""
    last_cycle_at: int = 0


class Poller:
    """Loop latar belakang yang memantau mutasi dan mencocokkannya ke order."""

    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        source: TransactionSource,
        bus: EventBus,
    ) -> None:
        self._s = settings
        self._db = db
        self._source = source
        self._bus = bus
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.stats = PollerStats()
        self._backoff = settings.poll_interval

    # -- lifecycle ------------------------------------------------------ #

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="qris-poller")
        log.info("Poller dimulai (interval %.1fs)", self._s.poll_interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("Poller dihentikan")

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    # -- loop ----------------------------------------------------------- #

    async def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.tick()
                self._backoff = self._s.poll_interval
            except asyncio.CancelledError:
                raise
            except TokenInvalid as exc:
                self.stats.errors += 1
                self.stats.last_error = f"token: {exc}"
                self._db.set_state(TOKEN_ALIVE_KEY, "0")
                log.error("Token ditolak: %s", exc)
                self._backoff = min(self._backoff * 2, self._s.poll_max_backoff)
            except (ShopeeError, Exception) as exc:  # noqa: BLE001
                self.stats.errors += 1
                self.stats.last_error = str(exc)
                log.warning("Siklus poll gagal: %s", exc)
                self._backoff = min(self._backoff * 2, self._s.poll_max_backoff)

            elapsed = time.monotonic() - started
            delay = max(0.0, self._backoff - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    # -- satu siklus ---------------------------------------------------- #

    async def tick(self) -> None:
        """Satu putaran: kedaluwarsakan order, tarik mutasi baru, cocokkan."""
        now = int(time.time())
        self._db.expire_stale_orders(now)

        watermark = self._db.get_state_int(WATERMARK_KEY, now - self._s.poll_lookback)
        # Jangan pernah meminta rentang waktu kosong atau terbalik.
        start = min(watermark, now)

        rows, _cursor = await self._source.list_transactions(
            start, now, self._s.poll_page_size
        )
        self._db.set_state(TOKEN_ALIVE_KEY, "1")

        txns = to_transactions(rows)
        self.stats.cycles += 1
        self.stats.last_cycle_at = now
        self.stats.looked_at += len(txns)

        highest = watermark
        for txn in sorted(txns, key=lambda t: t.create_time):
            await self._process(txn, now)
            highest = max(highest, txn.create_time)

        if highest > watermark:
            # Maju mundur 1 detik untuk berjaga terhadap batas rentang inklusif.
            self._db.set_state(WATERMARK_KEY, max(watermark, highest - 1))

    async def _process(self, txn: Transaction, now: int) -> None:
        """Proses satu transaksi: klaim bila cocok, catat orphan bila tidak."""
        if self._db.is_txn_seen(txn.txn_id):
            return
        self._db.mark_txn_seen(txn.txn_id, txn.create_time)

        if not is_candidate(txn):
            return

        # Penting: yang diperiksa adalah apakah transaksi ini sudah pernah
        # DIKLAIM, bukan apakah sudah pernah DILIHAT. Transaksi baru saja
        # ditandai "seen" di atas, jadi memakai is_txn_seen di sini akan
        # selalu True dan membuat tidak ada pembayaran yang pernah cocok.
        order = self._db.find_open_order_by_amount(txn.amount)
        result = find_match(
            txn,
            order=order,
            already_claimed=self._db.is_txn_claimed(txn.txn_id),
            now=now,
        )

        if not result.matched or result.order is None:
            # Nominal valid tapi tidak ada order terbuka: kandidat orphan.
            if result.reason.value == "tidak_ada_order_terbuka_dengan_nominal_ini":
                if self._db.record_orphan(txn, result.note, now=now):
                    self.stats.orphans += 1
                    log.warning(
                        "Pembayaran tanpa order: %s sebesar %s", txn.txn_id, txn.amount
                    )
                    self._bus.publish(
                        Event(
                            "order.orphan",
                            "*",
                            {
                                "txn_id": txn.txn_id,
                                "amount": txn.amount,
                                "create_time": txn.create_time,
                                "note": result.note,
                            },
                        )
                    )
            return

        claimed = self._db.claim(result.order.id, txn)
        if not claimed:
            return

        # Issuer bersifat kosmetik; ambil hanya setelah klaim berhasil.
        issuer: str | None = None
        try:
            issuer = await self._source.get_issuer(txn.txn_id)
            if issuer:
                with self._db._lock:  # noqa: SLF001 -- pembaruan atribut kosmetik
                    self._db._conn.execute(
                        "UPDATE orders SET issuer = ? WHERE id = ?",
                        (issuer, result.order.id),
                    )
                    self._db._conn.commit()
        except Exception as exc:  # noqa: BLE001
            log.debug("Gagal ambil issuer untuk %s: %s", txn.txn_id, exc)

        self.stats.matched += 1
        order_now = self._db.get_order(result.order.id)
        log.info(
            "LUNAS order %s sebesar %s via %s",
            result.order.id, txn.amount, issuer or "QRIS",
        )
        self._bus.publish(
            Event(
                "order.paid",
                result.order.id,
                {
                    "order_id": result.order.id,
                    "paid": True,
                    "amount": txn.amount,
                    "txn_id": txn.txn_id,
                    "issuer": issuer,
                    "paid_at": order_now.paid_at if order_now else now,
                },
            )
        )

    # -- kesehatan ------------------------------------------------------ #

    def health(self) -> dict[str, object]:
        return {
            "running": self.running,
            "cycles": self.stats.cycles,
            "errors": self.stats.errors,
            "looked_at": self.stats.looked_at,
            "matched": self.stats.matched,
            "orphans": self.stats.orphans,
            "last_error": self.stats.last_error,
            "last_cycle_at": self.stats.last_cycle_at,
            "watermark": self._db.get_state_int(WATERMARK_KEY, 0),
            "token_alive": self._db.get_state(TOKEN_ALIVE_KEY, "unknown"),
            "backoff": round(self._backoff, 1),
        }
