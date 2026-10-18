"""Lapisan service: orkestrasi order, QR, dan pelaporan.

Memisahkan aturan aplikasi dari transport (HTTP) dan dari infrastruktur
(database, jaringan), sehingga API tetap tipis dan logika bisnis dapat
diuji tanpa menjalankan server.
"""

from __future__ import annotations

import io
import logging
import time
import uuid
from dataclasses import dataclass

from .config import Settings
from .db import AmountExhausted, Database, Order
from .qris import QRISError, describe, make_dynamic, validate_crc
from .qris import parse_tlv

log = logging.getLogger(__name__)

ORDER_PREFIX = "ORD"


class ServiceError(RuntimeError):
    """Kesalahan tingkat aplikasi yang aman ditampilkan ke pemanggil."""


@dataclass(slots=True, frozen=True)
class CreatedOrder:
    order: Order
    qr_png: bytes


class OrderService:
    """Operasi tingkat aplikasi untuk pembuatan dan pembacaan order."""

    def __init__(self, settings: Settings, db: Database) -> None:
        self._s = settings
        self._db = db
        self._validate_static_qris()

    # -- validasi awal -------------------------------------------------- #

    def _validate_static_qris(self) -> None:
        """Pastikan QRIS statis yang dikonfigurasi memang sehat sejak awal."""
        try:
            parse_tlv(self._s.qris_static)
        except QRISError as exc:
            raise ServiceError(f"QRIS_STATIC tidak bisa diurai: {exc}") from exc
        if not validate_crc(self._s.qris_static):
            raise ServiceError(
                "QRIS_STATIC gagal verifikasi CRC. Periksa kembali nilai di .env"
            )
        info = describe(self._s.qris_static)
        if info["amount"]:
            raise ServiceError(
                "QRIS_STATIC sudah mengandung nominal (tag 54). "
                "Gunakan QRIS statis tanpa nominal agar bisa dijadikan dinamis."
            )
        log.info(
            "QRIS statis OK: merchant=%s kota=%s",
            info["merchant_name"], info["merchant_city"],
        )

    # -- pembuatan order ------------------------------------------------ #

    def create_order(self, base_amount: int, *, order_id: str | None = None) -> CreatedOrder:
        """Buat order dengan nominal unik dan QRIS dinamis.

        Args:
            base_amount: harga dasar sebelum suffix unik ditambahkan.
            order_id: opsional, untuk idempotensi dari sisi pemanggil.

        Raises:
            ServiceError: bila nominal tidak valid atau rentang suffix habis.
        """
        if not isinstance(base_amount, int) or isinstance(base_amount, bool):
            raise ServiceError("nominal harus bilangan bulat")
        if base_amount < 1:
            raise ServiceError("nominal minimal 1")
        if base_amount > 9_999_000:
            raise ServiceError("nominal terlalu besar")

        # Sisakan ruang agar base + suffix tidak melewati batas praktis QRIS.
        if base_amount + self._s.amount_suffix_max > 9_999_999:
            raise ServiceError(
                "nominal terlalu besar untuk ditambah kode unik; "
                "kurangi nominal dasarnya"
            )

        oid = order_id or self._new_order_id()

        def build_qris(amount: int, reference: str) -> str:
            return make_dynamic(
                self._s.qris_static,
                amount,
                reference=reference if self._s.embed_reference else None,
                point_of_initiation=self._s.point_of_initiation,
            )

        try:
            order = self._db.allocate_and_create_order(
                order_id=oid,
                base_amount=base_amount,
                qris_builder=build_qris,
                ttl=self._s.order_ttl,
                suffix_min=self._s.amount_suffix_min,
                suffix_max=self._s.amount_suffix_max,
            )
        except AmountExhausted as exc:
            raise ServiceError(
                "Terlalu banyak order aktif dengan harga sama. "
                "Selesaikan atau tunggu order lama kedaluwarsa."
            ) from exc

        return CreatedOrder(order=order, qr_png=render_qr_png(order.qris))

    @staticmethod
    def _new_order_id() -> str:
        stamp = time.strftime("%Y%m%d")
        return f"{ORDER_PREFIX}-{stamp}-{uuid.uuid4().hex[:10].upper()}"

    # -- pembacaan ------------------------------------------------------ #

    def get_order(self, order_id: str) -> Order | None:
        order = self._db.get_order(order_id)
        if order is None:
            return None
        # Kedaluwarsakan tepat waktu saat dibaca, tanpa menunggu siklus poller.
        if order.is_open and order.expires_at <= int(time.time()):
            self._db.expire_stale_orders()
            order = self._db.get_order(order_id)
        return order

    def qr_png(self, order_id: str) -> bytes | None:
        order = self._db.get_order(order_id)
        return render_qr_png(order.qris) if order else None

    def list_orders(self, *, status: str | None = None, limit: int = 50) -> list[Order]:
        return self._db.list_orders(status=status, limit=limit)

    def stats(self) -> dict[str, object]:
        return self._db.stats()


# ---------------------------------------------------------------------------
# Render QR lokal
# ---------------------------------------------------------------------------


def render_qr_png(payload: str, *, box_size: int = 8, border: int = 2) -> bytes:
    """Render QRIS menjadi PNG di server sendiri.

    Sengaja tidak memakai layanan pihak ketiga: payload QRIS memuat NMID
    merchant, sehingga mengirimkannya ke server lain adalah kebocoran data.
    """
    try:
        import qrcode
    except ImportError:  # pragma: no cover
        raise ServiceError(
            "Pustaka 'qrcode' belum terpasang. Jalankan: pip install qrcode[pil]"
        ) from None

    img = qrcode.make(payload)
    # qrcode.make tidak menerima box_size langsung; bikin ulang secara eksplisit.
    import qrcode as _q

    qr = _q.QRCode(
        version=None,
        error_correction=_q.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=border,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
