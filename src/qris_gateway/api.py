"""Lapisan HTTP (FastAPI).

API tetap tipis: seluruh aturan aplikasi ada di :mod:`qris_gateway.service`,
seluruh persistensi di :mod:`qris_gateway.db`, seluruh pencocokan di
:mod:`qris_gateway.matcher`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from .config import ConfigError, Settings
from .db import Database
from .events import EventBus
from .shopee import ShopeePartnerClient
from .service import OrderService, ServiceError, render_qr_png

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Skema request / response
# ---------------------------------------------------------------------------


class CreateOrderRequest(BaseModel):
    amount: int = Field(..., ge=1, le=9_999_000, description="Harga dasar, tanpa kode unik")
    order_id: str | None = Field(
        default=None, max_length=64, description="Opsional, untuk idempotensi"
    )


class UpdateTokenRequest(BaseModel):
    token: str = Field(..., min_length=8, description="Token sesi partner portal")


class MockPayRequest(BaseModel):
    amount: int = Field(..., ge=1, description="Nominal pembayaran palsu")
    issuer: str | None = Field(default=None, description="Nama issuer palsu")


# ---------------------------------------------------------------------------
# State aplikasi
# ---------------------------------------------------------------------------


class AppState:
    """Wadah dependensi runtime."""

    def __init__(self) -> None:
        self.settings: Settings
        self.db: Database
        self.bus: EventBus
        self.source: ShopeePartnerClient
        self.service: OrderService
        self.poller: Any = None


state = AppState()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from .poller import Poller

    settings = Settings.load()
    log.info("Konfigurasi: %s", settings.redacted())

    state.settings = settings
    state.db = Database(settings.database)
    state.bus = EventBus()
    state.service = OrderService(settings, state.db)

    if settings.ready:
        if settings.mock_mode:
            from .mock import MockSource

            state.source = MockSource()  # type: ignore[assignment]
            log.warning(
                "MOCK_MODE aktif: sumber transaksi palsu. "
                "JANGAN dipakai di produksi."
            )
        else:
            state.source = ShopeePartnerClient(settings)
        state.poller = Poller(
            settings=settings, db=state.db, source=state.source, bus=state.bus
        )
        state.poller.start()
    else:
        log.warning(
            "Poller TIDAK dijalankan. Variabel belum diisi: %s",
            ", ".join(settings.unset),
        )
        state.source = None  # type: ignore[assignment]

    try:
        yield
    finally:
        if state.poller:
            await state.poller.stop()
        if state.source:
            await state.source.aclose()
        state.db.close()


# ---------------------------------------------------------------------------
# Autentikasi
# ---------------------------------------------------------------------------


async def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    api_key: str | None = Query(default=None),
) -> None:
    """Penjaga sederhana untuk endpoint administratif."""
    settings: Settings = state.settings
    if not settings.require_api_key:
        return
    supplied = x_api_key or api_key or ""
    if not supplied or supplied != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key tidak valid atau tidak ada",
        )


# ---------------------------------------------------------------------------
# Aplikasi
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    app = FastAPI(
        title="QRIS Gateway",
        version="1.0.0",
        description=(
            "Gateway verifikasi pembayaran QRIS berbasis partner portal. "
            "Read-only: tidak ada operasi yang mengubah dana."
        ),
        lifespan=lifespan,
    )

    # -- kesehatan ------------------------------------------------------ #

    @app.get("/health", tags=["umum"])
    async def health() -> dict[str, Any]:
        settings = state.settings
        return {
            "ok": True,
            "ready": settings.ready,
            "unset": list(settings.unset),
            "read_only": settings.read_only,
            "poller": state.poller.health() if state.poller else {"running": False},
        }

    @app.get("/stats", tags=["umum"], dependencies=[Depends(require_api_key)])
    async def stats() -> dict[str, Any]:
        return {"ok": True, "data": state.service.stats()}

    # -- QRIS ----------------------------------------------------------- #

    @app.get("/qris/inspect", tags=["qris"], dependencies=[Depends(require_api_key)])
    async def inspect_qris() -> dict[str, Any]:
        from .qris import describe

        return {"ok": True, "data": describe(state.settings.qris_static)}

    # -- order ---------------------------------------------------------- #

    @app.post(
        "/orders",
        tags=["order"],
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_api_key)],
    )
    async def create_order(body: CreateOrderRequest) -> dict[str, Any]:
        try:
            created = state.service.create_order(body.amount, order_id=body.order_id)
        except ServiceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        order = created.order
        return {
            "ok": True,
            "data": {
                **order.to_dict(),
                "qr_png_url": f"/orders/{order.id}/qr.png",
                "stream_url": f"/orders/{order.id}/stream",
                "ttl": order.expires_at - order.created_at,
            },
        }

    @app.get("/orders", tags=["order"], dependencies=[Depends(require_api_key)])
    async def list_orders(
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        orders = state.service.list_orders(status=status_filter, limit=limit)
        return {"ok": True, "count": len(orders), "data": [o.to_dict() for o in orders]}

    @app.get("/orders/{order_id}", tags=["order"], dependencies=[Depends(require_api_key)])
    async def get_order(order_id: str) -> dict[str, Any]:
        order = state.service.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order tidak ditemukan")
        return {"ok": True, "data": order.to_dict()}

    @app.get("/orders/{order_id}/qr.png", tags=["order"])
    async def order_qr(order_id: str) -> Response:
        """Gambar QR. Publik: dipindai pelanggan, tidak memuat rahasia."""
        png = state.service.qr_png(order_id)
        if png is None:
            raise HTTPException(status_code=404, detail="Order tidak ditemukan")
        return Response(
            content=png,
            media_type="image/png",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/orders/{order_id}/qris.txt", tags=["order"])
    async def order_qris_payload(order_id: str) -> Response:
        """Payload QRIS mentah, untuk debugging atau render sendiri."""
        order = state.service.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order tidak ditemukan")
        return Response(content=order.qris, media_type="text/plain")

    @app.get("/orders/{order_id}/stream", tags=["order"])
    async def order_stream(order_id: str, request: Request) -> StreamingResponse:
        """Server-Sent Events: dorong status ke halaman checkout."""

        async def gen() -> AsyncIterator[str]:
            order = state.service.get_order(order_id)
            if order is None:
                yield "event: error\ndata: {\"detail\":\"order tidak ditemukan\"}\n\n"
                return
            # Kirim status terkini lebih dulu agar klien tahu posisinya.
            yield f"event: order.state\ndata: {_json(order.to_dict())}\n\n"
            if not order.is_open:
                return

            async for event in state.bus.stream(order_id):
                if await request.is_disconnected():
                    break
                yield event.to_sse()
                if event.type in {"order.paid", "order.expired"}:
                    break

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # -- orphans -------------------------------------------------------- #

    @app.get("/orphans", tags=["orphan"], dependencies=[Depends(require_api_key)])
    async def list_orphans(
        resolved: int | None = Query(default=0, ge=0, le=1),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        rows = state.db.list_orphans(resolved=resolved, limit=limit)
        return {
            "ok": True,
            "count": len(rows),
            "data": [
                {
                    "txn_id": o.txn_id,
                    "amount": o.amount,
                    "create_time": o.create_time,
                    "note": o.note,
                    "created_at": o.created_at,
                    "resolved": bool(o.resolved),
                }
                for o in rows
            ],
        }

    @app.post(
        "/orphans/{txn_id}/resolve",
        tags=["orphan"],
        dependencies=[Depends(require_api_key)],
    )
    async def resolve_orphan(txn_id: str) -> dict[str, Any]:
        if not state.db.resolve_orphan(txn_id):
            raise HTTPException(status_code=404, detail="Orphan tidak ditemukan")
        return {"ok": True, "data": {"txn_id": txn_id, "resolved": True}}

    # -- admin ---------------------------------------------------------- #

    @app.post("/admin/token", tags=["admin"], dependencies=[Depends(require_api_key)])
    async def update_token(body: UpdateTokenRequest) -> dict[str, Any]:
        if not state.source:
            raise HTTPException(
                status_code=409,
                detail="Poller tidak aktif; lengkapi konfigurasi lalu mulai ulang",
            )
        state.source.update_token(body.token)
        return {"ok": True, "data": {"message": "Token diperbarui"}}

    # -- mock (hanya aktif saat MOCK_MODE) ------------------------------ #

    @app.post("/mock/pay", tags=["mock"], dependencies=[Depends(require_api_key)])
    async def mock_pay(body: MockPayRequest) -> dict[str, Any]:
        """Suntikkan pembayaran palsu, untuk menguji alur tanpa token asli."""
        from .mock import MockSource

        if not state.settings.mock_mode or not isinstance(state.source, MockSource):
            raise HTTPException(
                status_code=404, detail="MOCK_MODE tidak aktif pada server ini"
            )
        txn = state.source.simulate_payment(body.amount, issuer=body.issuer)
        return {"ok": True, "data": txn}

    @app.get("/mock/state", tags=["mock"], dependencies=[Depends(require_api_key)])
    async def mock_state() -> dict[str, Any]:
        from .mock import MockSource

        if not state.settings.mock_mode or not isinstance(state.source, MockSource):
            raise HTTPException(status_code=404, detail="MOCK_MODE tidak aktif")
        return {
            "ok": True,
            "data": {
                "payments_injected": state.source.payments_injected,
                "records": state.source.pending,
            },
        }

    return app


def _json(value: object) -> str:
    import json

    return json.dumps(value)


app = create_app()
