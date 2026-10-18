"""Klien ShopeePay partner-web (READ-ONLY).

Modul ini sengaja hanya mengimplementasikan dua operasi baca:

* ``get-transaction-list``   -- daftar mutasi pada rentang waktu
* ``get-transaction-detail`` -- detail satu transaksi (untuk nama issuer)

Tidak ada, dan tidak boleh ada, operasi yang mengubah dana: tidak ada
pembuatan pembayaran, tidak ada refund, tidak ada penarikan. Penjaga
``READ_ONLY`` di :class:`~qris_gateway.config.Settings` ditegakkan di sini.

Semua sumber transaksi memenuhi protokol :class:`TransactionSource`, sehingga
poller dapat diuji dengan sumber palsu tanpa menyentuh jaringan.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Protocol, runtime_checkable

import httpx

from .config import Settings

log = logging.getLogger(__name__)

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
)

ORIGIN = "https://partner.shopee.co.id"


class ShopeeError(RuntimeError):
    """Kesalahan umum saat berbicara dengan ShopeePay."""


class TokenInvalid(ShopeeError):
    """Token sesi ditolak atau sudah kedaluwarsa."""


@runtime_checkable
class TransactionSource(Protocol):
    """Kontrak minimum yang dibutuhkan poller."""

    async def list_transactions(
        self, start_time: int, end_time: int, page_size: int, cursor: str = ""
    ) -> tuple[list[dict[str, Any]], str]:
        """Kembalikan (daftar transaksi, cursor berikutnya)."""
        ...

    async def get_issuer(self, txn_id: str) -> str | None:
        """Kembalikan nama issuer (OVO, DANA, BCA, ...) atau None."""
        ...

    async def aclose(self) -> None:
        ...


class ShopeePartnerClient:
    """Klien HTTP untuk endpoint partner-web ShopeePay."""

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None) -> None:
        if not settings.read_only:
            raise ShopeeError(
                "Klien ini hanya mendukung mode read-only. "
                "Set READ_ONLY=true atau gunakan klien lain."
            )
        self._s = settings
        self._token = settings.shopee_token
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.http_timeout),
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )

    # -- token ---------------------------------------------------------- #

    def update_token(self, token: str) -> None:
        self._token = token.strip()
        log.info("Token Shopee diperbarui (%d karakter)", len(self._token))

    @property
    def token_present(self) -> bool:
        return bool(self._token)

    # -- internal ------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Origin": ORIGIN,
            "Referer": ORIGIN + "/",
            "User-Agent": random.choice(USER_AGENTS),
        }

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self._s.shopee_base_url.rstrip("/") + path
        last: Exception | None = None

        for attempt in range(1, self._s.http_retries + 1):
            try:
                resp = await self._client.post(url, json=body, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
                code = data.get("code")
                if code not in (0, None):
                    msg = str(data.get("msg") or "kode tidak dikenal")
                    if _looks_like_token_error(data):
                        raise TokenInvalid(msg)
                    raise ShopeeError(f"API menolak (code={code}): {msg}")
                return data
            except TokenInvalid:
                raise
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                if attempt < self._s.http_retries:
                    delay = min(2 ** (attempt - 1), 8) + random.random()
                    log.warning(
                        "Percobaan %d/%d gagal (%s), ulangi dalam %.1fs",
                        attempt, self._s.http_retries, exc, delay,
                    )
                    await asyncio.sleep(delay)

        raise ShopeeError(f"gagal setelah {self._s.http_retries} percobaan: {last}")

    # -- operasi baca --------------------------------------------------- #

    async def list_transactions(
        self, start_time: int, end_time: int, page_size: int, cursor: str = ""
    ) -> tuple[list[dict[str, Any]], str]:
        body = {
            "data": {
                "metadata": {
                    "token": self._token,
                    "language": "id",
                    "timezone": "Asia/Jakarta",
                },
                "pageSize": page_size,
                "filter": {
                    "startTime": start_time,
                    "endTime": end_time,
                    "serviceList": [1, 3],
                },
                "sorter": {"field": "createTime", "order": "descend"},
                "next_position": cursor or "",
            }
        }
        data = await self._post(self._s.shopee_list_path, body)
        payload = data.get("data") or {}
        rows = payload.get("list") or []
        next_cursor = str(payload.get("next_position") or "")
        return rows, next_cursor

    async def get_issuer(self, txn_id: str) -> str | None:
        body = {
            "data": {
                "metadata": {
                    "token": self._token,
                    "language": "id",
                    "timezone": "Asia/Jakarta",
                },
                "order_sn": txn_id,
            }
        }
        data = await self._post(self._s.shopee_detail_path, body)
        payload = data.get("data") or {}
        issuer = payload.get("issuer")
        return str(issuer) if issuer else None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _looks_like_token_error(data: dict[str, Any]) -> bool:
    """Deteksi indikasi token mati dari bentuk respons."""
    blob = f"{data.get('code')} {data.get('msg')} {data.get('error')}".lower()
    return any(k in blob for k in ("token", "200020", "unauthor", "login", "session"))
