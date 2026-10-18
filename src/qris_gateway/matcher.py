"""Logika pencocokan transaksi ke order.

Modul ini murni: tidak ada I/O, tidak ada state global, tidak ada efek
samping. Semua keputusan pencocokan bisa diuji unit tanpa database dan
tanpa jaringan, yang penting karena di sinilah uang dipertaruhkan.

Aturan pencocokan (semua harus terpenuhi):

1. Transaksi berstatus sukses (``status == 3``).
2. Nominal transaksi sama persis dengan nominal order.
3. Order masih terbuka.
4. Transaksi terjadi setelah order dibuat, dengan toleransi jam server.
5. Transaksi belum pernah diklaim order lain.

Toleransi waktu (``CLOCK_SKEW``) ada karena jam server ShopeePay dan jam
server kita tidak dijamin sinkron. Tanpa toleransi, pembayaran yang datang
beberapa detik lebih awal dari catatan order akan hilang.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .db import Order, Transaction

# Toleransi selisih jam antara server kita dan server ShopeePay (detik).
CLOCK_SKEW = 90

# Jendela maksimum: transaksi lebih tua dari ini tidak akan pernah dicocokkan
# ke order baru, untuk mencegah pembayaran lama mengklaim order baru.
MAX_AGE = 3600


class MatchReason(str, Enum):
    """Alasan sebuah transaksi tidak cocok, untuk catatan orphan."""

    MATCHED = "matched"
    NOT_SUCCESS = "status_bukan_sukses"
    NO_OPEN_ORDER = "tidak_ada_order_terbuka_dengan_nominal_ini"
    ORDER_EXPIRED = "order_sudah_kedaluwarsa"
    TOO_EARLY = "transaksi_lebih_awal_dari_order"
    TOO_OLD = "transaksi_terlalu_lama"
    AMOUNT_ZERO = "nominal_nol_atau_tidak_valid"
    AMOUNT_MISMATCH = "nominal_tidak_sama_dengan_order"


@dataclass(slots=True, frozen=True)
class MatchResult:
    """Hasil pencocokan satu transaksi."""

    matched: bool
    reason: MatchReason
    order: Order | None = None

    @property
    def note(self) -> str:
        return self.reason.value


def is_candidate(txn: Transaction) -> bool:
    """Filter cepat: transaksi sukses dan nominalnya masuk akal."""
    return txn.is_success and txn.amount > 0


def find_match(
    txn: Transaction,
    *,
    order: Order | None,
    already_claimed: bool = False,
    now: int,
) -> MatchResult:
    """Cocokkan satu transaksi ke satu kandidat order.

    Args:
        txn: transaksi dari sumber.
        order: kandidat order dengan nominal sama, atau None.
        already_claimed: True bila ``txn_id`` sudah pernah diklaim.
        now: waktu sekarang (epoch detik), disuntik agar mudah diuji.

    Returns:
        :class:`MatchResult` dengan keputusan dan alasannya.
    """
    if txn.amount <= 0:
        return MatchResult(False, MatchReason.AMOUNT_ZERO)

    if not txn.is_success:
        return MatchResult(False, MatchReason.NOT_SUCCESS)

    if already_claimed:
        # Sudah diklaim order lain; bukan orphan, hanya duplikat.
        return MatchResult(False, MatchReason.NOT_SUCCESS)

    if order is None:
        return MatchResult(False, MatchReason.NO_OPEN_ORDER)

    if not order.is_open:
        return MatchResult(False, MatchReason.ORDER_EXPIRED, order)

    # Pertahanan berlapis: walau pemanggil seharusnya sudah mencari order
    # berdasarkan nominal, fungsi ini tetap memverifikasi sendiri agar aman
    # dipanggil dengan kandidat order apa pun.
    if txn.amount != order.amount:
        return MatchResult(False, MatchReason.AMOUNT_MISMATCH, order)

    # Transaksi tidak boleh lebih tua dari order, dengan toleransi jam.
    if txn.create_time and txn.create_time < order.created_at - CLOCK_SKEW:
        return MatchResult(False, MatchReason.TOO_EARLY, order)

    # Jaring pengaman: transaksi yang sangat tua tidak boleh mengklaim order
    # baru hanya karena nominalnya kebetulan sama.
    if txn.create_time and (now - txn.create_time) > MAX_AGE:
        return MatchResult(False, MatchReason.TOO_OLD, order)

    return MatchResult(True, MatchReason.MATCHED, order)


def classify_orphan(txn: Transaction, *, order: Order | None, now: int) -> str:
    """Beri catatan deskriptif untuk pembayaran yang tidak bisa dicocokkan.

    Orphan bukan sampah: ini uang nyata yang masuk tanpa order yang jelas.
    Catatannya harus cukup untuk tindak lanjut manual.
    """
    result = find_match(txn, order=order, now=now)
    if result.matched:
        return "matched"
    return result.reason.value
