"""Test untuk logika pencocokan.

Ini bagian paling kritis: keputusan di sini menentukan apakah uang
dianggap sudah masuk atau belum.
"""

from __future__ import annotations

import time

from qris_gateway.db import ORDER_EXPIRED, ORDER_OPEN, ORDER_PAID, Order, Transaction
from qris_gateway.matcher import (
    CLOCK_SKEW,
    MAX_AGE,
    MatchReason,
    classify_orphan,
    find_match,
    is_candidate,
)

NOW = 1_800_000_000


def order(
    *,
    amount: int = 10_001,
    status: str = ORDER_OPEN,
    created_at: int = NOW - 60,
    oid: str = "ORD-1",
) -> Order:
    return Order(
        id=oid,
        amount=amount,
        base_amount=amount - 1,
        status=status,
        qris="x",
        created_at=created_at,
        expires_at=created_at + 900,
        paid_at=None,
        txn_id=None,
        issuer=None,
    )


def txn(
    *, amount: int = 10_001, status: int = 3, create_time: int | None = NOW - 10,
    tid: str = "T1",
) -> Transaction:
    return Transaction(tid, amount, status, create_time if create_time is not None else 0)


class TestCandidate:
    def test_transaksi_sukses_adalah_kandidat(self) -> None:
        assert is_candidate(txn()) is True

    def test_transaksi_gagal_bukan_kandidat(self) -> None:
        assert is_candidate(txn(status=2)) is False

    def test_nominal_nol_bukan_kandidat(self) -> None:
        assert is_candidate(txn(amount=0)) is False


class TestFindMatch:
    def test_cocok_sempurna(self) -> None:
        r = find_match(txn(), order=order(), now=NOW)
        assert r.matched is True
        assert r.reason is MatchReason.MATCHED

    def test_nominal_berbeda_tidak_cocok(self) -> None:
        r = find_match(txn(amount=10_002), order=order(amount=10_001), now=NOW)
        # order dengan nominal itu tidak ada -> dianggap tanpa order
        assert r.matched is False

    def test_status_bukan_sukses_tidak_cocok(self) -> None:
        r = find_match(txn(status=1), order=order(), now=NOW)
        assert r.matched is False
        assert r.reason is MatchReason.NOT_SUCCESS

    def test_order_tidak_ada(self) -> None:
        r = find_match(txn(), order=None, now=NOW)
        assert r.matched is False
        assert r.reason is MatchReason.NO_OPEN_ORDER

    def test_order_kedaluwarsa_tidak_cocok(self) -> None:
        r = find_match(txn(), order=order(status=ORDER_EXPIRED), now=NOW)
        assert r.matched is False
        assert r.reason is MatchReason.ORDER_EXPIRED

    def test_order_sudah_lunas_tidak_cocok(self) -> None:
        r = find_match(txn(), order=order(status=ORDER_PAID), now=NOW)
        assert r.matched is False

    def test_nominal_nol_ditolak(self) -> None:
        r = find_match(txn(amount=0), order=order(amount=0), now=NOW)
        assert r.reason is MatchReason.AMOUNT_ZERO


class TestTimingWindow:
    def test_transaksi_dalam_toleransi_masih_cocok(self) -> None:
        """Jam server bisa bergeser; transaksi sedikit lebih awal tetap sah."""
        r = find_match(
            txn(create_time=NOW - 60 - CLOCK_SKEW + 5), order=order(), now=NOW
        )
        assert r.matched is True

    def test_transaksi_terlalu_awal_tidak_cocok(self) -> None:
        r = find_match(
            txn(create_time=NOW - 60 - CLOCK_SKEW - 60), order=order(), now=NOW
        )
        assert r.matched is False
        assert r.reason is MatchReason.TOO_EARLY

    def test_transaksi_sangat_tua_tidak_mengklaim_order_baru(self) -> None:
        """Pembayaran lama tidak boleh mengklaim order baru karena nominal sama."""
        r = find_match(
            txn(create_time=NOW - MAX_AGE - 100),
            order=order(created_at=NOW - 10),
            now=NOW,
        )
        assert r.matched is False

    def test_transaksi_tepat_setelah_order_cocok(self) -> None:
        r = find_match(txn(create_time=NOW - 1), order=order(created_at=NOW - 5), now=NOW)
        assert r.matched is True


class TestDoubleClaim:
    def test_sudah_diklaim_tidak_cocok_lagi(self) -> None:
        r = find_match(txn(), order=order(), already_claimed=True, now=NOW)
        assert r.matched is False


class TestClassifyOrphan:
    def test_cocok_ditandai_matched(self) -> None:
        assert classify_orphan(txn(), order=order(), now=NOW) == "matched"

    def test_tanpa_order_diberi_alasan_jelas(self) -> None:
        note = classify_orphan(txn(), order=None, now=NOW)
        assert note == MatchReason.NO_OPEN_ORDER.value
        assert "nominal" in note

    def test_status_gagal_diberi_alasan_jelas(self) -> None:
        assert classify_orphan(txn(status=2), order=None, now=NOW) == "status_bukan_sukses"


class TestAlurNyata:
    """Skenario end-to-end pada level logika: bayar, telat, dan tabrakan."""

    def test_skenario_pembayaran_normal(self) -> None:
        o = order(amount=50_001, created_at=NOW - 30)
        t = txn(amount=50_001, create_time=NOW - 10)
        assert find_match(t, order=o, now=NOW).matched is True

    def test_skenario_telat_bayar_jadi_orphan(self) -> None:
        """QR kedaluwarsa, pelanggan tetap membayar: harus terdeteksi, bukan hilang."""
        o = order(amount=50_002, status=ORDER_EXPIRED, created_at=NOW - 1200)
        t = txn(amount=50_002, create_time=NOW - 100)
        r = find_match(t, order=o, now=NOW)
        assert r.matched is False
        assert r.reason is MatchReason.ORDER_EXPIRED

    def test_skenario_kolisi_nominal_tidak_mungkin(self) -> None:
        """Karena nominal unik, satu nominal hanya punya satu order terbuka."""
        o_a = order(amount=30_001, oid="A")
        o_b = order(amount=30_002, oid="B")
        t = txn(amount=30_001, create_time=NOW - 5)
        assert find_match(t, order=o_a, now=NOW).matched is True
        assert find_match(t, order=o_b, now=NOW).matched is False
