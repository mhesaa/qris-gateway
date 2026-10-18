"""Bus event in-process untuk mendorong notifikasi ke klien (SSE).

Menggantikan pola polling dari sisi klien: halaman checkout cukup
berlangganan, dan begitu poller menemukan kecocokan, event langsung
dikirim. Satu poller di server melayani berapa pun jumlah pembeli.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

_QUEUE_MAX = 32
_HEARTBEAT = 15.0


@dataclass(slots=True, frozen=True)
class Event:
    """Satu event yang dikirim ke klien."""

    type: str          # order.paid | order.expired | order.orphan | heartbeat
    order_id: str
    data: dict[str, Any]

    def to_sse(self) -> str:
        import json

        return f"event: {self.type}\ndata: {json.dumps(self.data)}\n\n"


class EventBus:
    """Pub/sub sederhana berbasis ``asyncio.Queue`` per pelanggan.

    Antrean dibuat terbatas agar pelanggan yang lambat tidak menghabiskan
    memori; bila penuh, event tertua dibuang karena status terbaru selalu
    lebih penting daripada riwayat.
    """

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[Event]]] = defaultdict(set)
        self._global: set[asyncio.Queue[Event]] = set()

    def subscribe(self, order_id: str | None = None) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=_QUEUE_MAX)
        if order_id is None:
            self._global.add(q)
        else:
            self._subs[order_id].add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event], order_id: str | None = None) -> None:
        if order_id is None:
            self._global.discard(q)
        else:
            bucket = self._subs.get(order_id)
            if bucket:
                bucket.discard(q)
                if not bucket:
                    self._subs.pop(order_id, None)

    def publish(self, event: Event) -> int:
        """Kirim event ke semua pelanggan yang relevan; kembalikan jumlahnya."""
        targets = set(self._subs.get(event.order_id, ())) | self._global
        delivered = 0
        for q in targets:
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                try:                       # buang yang tertua, masukkan yang baru
                    q.get_nowait()
                    q.put_nowait(event)
                    delivered += 1
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    log.warning("Antrean pelanggan penuh, event dibuang")
        return delivered

    async def stream(
        self, order_id: str | None = None
    ) -> AsyncIterator[Event]:
        """Async generator event untuk satu pelanggan, dengan heartbeat."""
        q = self.subscribe(order_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=_HEARTBEAT)
                    yield event
                except asyncio.TimeoutError:
                    yield Event("heartbeat", order_id or "*", {})
        finally:
            self.unsubscribe(q, order_id)

    def subscriber_count(self, order_id: str | None = None) -> int:
        if order_id is None:
            return len(self._global)
        return len(self._subs.get(order_id, ()))
