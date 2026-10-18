"""Test untuk lapisan database.

Fokus: dua jaminan yang tidak boleh pernah bocor.

* Anti-kolisi  -- tidak ada dua order terbuka dengan nominal sama.
* Anti-klaim-ganda -- satu transaksi hanya bisa diklaim sekali, permanen,
  termasuk setelah server restart.
"""

from __future__ import annotations

import time

import pytest

from qris_gateway.db import (
    ORDER_EXPIRED,
    ORDER_OPEN,
    ORDER_PAID,
    AmountExhausted,
    Database,
    Transaction,
)


def make_builder(prefix: str = "QRIS"):
    """Builder QRIS palsu yang deterministik, cukup untuk uji persistensi."""
    def build(amount: int, reference: str) -> str:
        return f"{prefix}:{amount}:{reference}"
    return build


def txn(txn_id: str, amount: int, create_time: int | None = None) -> Transaction:
    return Transaction(txn_id, amount, 3, create_time or int(time.time()))


class TestAllocation:
    def test_nominal_pertama_dapat_suffix_minimum(self, db: Database) -> None:
        order = db.allocate_and_create_order(
            order_id="A", base_amount=10_000, qris_builder=make_builder(), ttl=900
        )
        assert order.amount == 10_001
        assert order.base_amount == 10_000
        assert order.status == ORDER_OPEN

    def test_order_kedua_dapat_suffix_berbeda(self, db: Database) -> None:
        a = db.allocate_and_create_order(
            order_id="A", base_amount=10_000, qris_builder=make_builder(), ttl=900
        )
        b = db.allocate_and_create_order(
            order_id="B", base_amount=10_000, qris_builder=make_builder(), ttl=900
        )
        assert a.amount != b.amount
        assert {a.amount, b.amount} == {10_001, 10_002}

    def test_banyak_order_tidak_pernah_kolisi(self, db: Database) -> None:
        """Inti jaminan anti-kolisi: 50 order pada harga sama, semua unik."""
        amounts = []
        for i in range(50):
            o = db.allocate_and_create_order(
                order_id=f"O{i}",
                base_amount=25_000,
                qris_builder=make_builder(),
                ttl=900,
                suffix_max=999,
            )
            amounts.append(o.amount)
        assert len(amounts) == len(set(amounts))

    def test_rentang_habis_menaikkan_error(self, db: Database) -> None:
        for i in range(3):
            db.allocate_and_create_order(
                order_id=f"O{i}",
                base_amount=5_000,
                qris_builder=make_builder(),
                ttl=900,
                suffix_min=1,
                suffix_max=3,
            )
        with pytest.raises(AmountExhausted):
            db.allocate_and_create_order(
                order_id="X",
                base_amount=5_000,
                qris_builder=make_builder(),
                ttl=900,
                suffix_min=1,
                suffix_max=3,
            )

    def test_nominal_dibebaskan_setelah_order_kedaluwarsa(self, db: Database) -> None:
        past = int(time.time()) - 10_000
        first = db.allocate_and_create_order(
            order_id="A",
            base_amount=1_000,
            qris_builder=make_builder(),
            ttl=1,
            suffix_min=1,
            suffix_max=1,
            now=past,
        )
        assert first.amount == 1_001
        # Rentang penuh saat order masih terbuka
        with pytest.raises(AmountExhausted):
            db.allocate_and_create_order(
                order_id="B",
                base_amount=1_000,
                qris_builder=make_builder(),
                ttl=900,
                suffix_min=1,
                suffix_max=1,
            )
        # Setelah kedaluwarsa, nominal yang sama bisa dipakai lagi
        db.expire_stale_orders()
        again = db.allocate_and_create_order(
            order_id="C",
            base_amount=1_000,
            qris_builder=make_builder(),
            ttl=900,
            suffix_min=1,
            suffix_max=1,
        )
        assert again.amount == 1_001

    def test_order_id_duplikat_ditolak(self, db: Database) -> None:
        db.allocate_and_create_order(
            order_id="SAMA", base_amount=1_000, qris_builder=make_builder(), ttl=900
        )
        with pytest.raises(AmountExhausted):
            db.allocate_and_create_order(
                order_id="SAMA",
                base_amount=1_000,
                qris_builder=make_builder(),
                ttl=900,
                suffix_min=1,
                suffix_max=1,
            )


class TestClaims:
    def test_klaim_berhasil_sekali(self, db: Database) -> None:
        db.allocate_and_create_order(
            order_id="A", base_amount=1_000, qris_builder=make_builder(), ttl=900
        )
        assert db.claim("A", txn("T1", 1_001)) is True
        order = db.get_order("A")
        assert order is not None
        assert order.status == ORDER_PAID
        assert order.txn_id == "T1"

    def test_klaim_kedua_kali_ditolak(self, db: Database) -> None:
        """Satu transaksi tidak boleh mengklaim dua order berbeda."""
        db.allocate_and_create_order(
            order_id="A", base_amount=1_000, qris_builder=make_builder(), ttl=900
        )
        db.allocate_and_create_order(
            order_id="B", base_amount=1_000, qris_builder=make_builder(), ttl=900
        )
        assert db.claim("A", txn("T-DUP", 1_001)) is True
        assert db.claim("B", txn("T-DUP", 1_002)) is False
        assert db.get_order("B").status == ORDER_OPEN  # type: ignore[union-attr]

    def test_klaim_bertahan_setelah_restart(self, settings) -> None:
        """Jaminan anti-klaim-ganda harus selamat dari restart server."""
        db1 = Database(settings.database)
        try:
            db1.allocate_and_create_order(
                order_id="A", base_amount=1_000, qris_builder=make_builder(), ttl=900
            )
            db1.allocate_and_create_order(
                order_id="B", base_amount=1_000, qris_builder=make_builder(), ttl=900
            )
            assert db1.claim("A", txn("T-RESTART", 1_001)) is True
        finally:
            db1.close()

        db2 = Database(settings.database)          # simulasi proses baru
        try:
            assert db2.claim("B", txn("T-RESTART", 1_002)) is False
            assert db2.get_order("A").status == ORDER_PAID  # type: ignore[union-attr]
        finally:
            db2.close()

    def test_klaim_menyimpan_issuer(self, db: Database) -> None:
        db.allocate_and_create_order(
            order_id="A", base_amount=1_000, qris_builder=make_builder(), ttl=900
        )
        db.claim("A", txn("T1", 1_001), issuer="DANA")
        assert db.get_order("A").issuer == "DANA"  # type: ignore[union-attr]


class TestExpiry:
    def test_expire_menandai_order_lewat_ttl(self, db: Database) -> None:
        past = int(time.time()) - 5_000
        db.allocate_and_create_order(
            order_id="A",
            base_amount=1_000,
            qris_builder=make_builder(),
            ttl=10,
            now=past,
        )
        assert db.expire_stale_orders() == 1
        assert db.get_order("A").status == ORDER_EXPIRED  # type: ignore[union-attr]

    def test_order_masih_hidup_tidak_terkedaluwarsa(self, db: Database) -> None:
        db.allocate_and_create_order(
            order_id="A", base_amount=1_000, qris_builder=make_builder(), ttl=900
        )
        assert db.expire_stale_orders() == 0
        assert db.get_order("A").status == ORDER_OPEN  # type: ignore[union-attr]

    def test_order_kedaluwarsa_tidak_bisa_diklaim(self, db: Database) -> None:
        past = int(time.time()) - 5_000
        db.allocate_and_create_order(
            order_id="A",
            base_amount=1_000,
            qris_builder=make_builder(),
            ttl=10,
            now=past,
        )
        db.expire_stale_orders()
        # Klaim tetap bisa secara teknis di level DB, tapi order tidak lagi open,
        # sehingga matcher (diuji terpisah) yang mencegahnya.
        assert db.find_open_order_by_amount(1_001) is None


class TestOrphans:
    def test_catat_orphan_baru(self, db: Database) -> None:
        assert db.record_orphan(txn("T1", 99_999), "tidak ada order") is True
        assert len(db.list_orphans()) == 1

    def test_orphan_duplikat_tidak_dicatat_dua_kali(self, db: Database) -> None:
        assert db.record_orphan(txn("T1", 99_999), "x") is True
        assert db.record_orphan(txn("T1", 99_999), "x") is False
        assert len(db.list_orphans()) == 1

    def test_orphan_dapat_diselesaikan(self, db: Database) -> None:
        db.record_orphan(txn("T1", 99_999), "x")
        assert db.resolve_orphan("T1") is True
        assert db.list_orphans(resolved=0) == []
        assert len(db.list_orphans(resolved=1)) == 1

    def test_selesaikan_orphan_tidak_ada(self, db: Database) -> None:
        assert db.resolve_orphan("TIDAK-ADA") is False


class TestState:
    def test_state_tersimpan_dan_terbaca(self, db: Database) -> None:
        db.set_state("watermark", 12345)
        assert db.get_state_int("watermark", 0) == 12345

    def test_state_bertahan_setelah_restart(self, settings) -> None:
        db1 = Database(settings.database)
        db1.set_state("watermark", 777)
        db1.close()
        db2 = Database(settings.database)
        try:
            assert db2.get_state_int("watermark", 0) == 777
        finally:
            db2.close()

    def test_default_dipakai_bila_belum_ada(self, db: Database) -> None:
        assert db.get_state_int("belum-ada", 42) == 42
        assert db.get_state("belum-ada") is None


class TestSeenTxns:
    def test_menandai_dan_memeriksa(self, db: Database) -> None:
        assert db.is_txn_seen("T1") is False
        db.mark_txn_seen("T1", 1000)
        assert db.is_txn_seen("T1") is True

    def test_menandai_dua_kali_aman(self, db: Database) -> None:
        db.mark_txn_seen("T1", 1000)
        db.mark_txn_seen("T1", 1001)  # tidak boleh meledak
        assert db.is_txn_seen("T1") is True


class TestStats:
    def test_statistik_awal_kosong(self, db: Database) -> None:
        s = db.stats()
        assert s["orders_open"] == 0
        assert s["orders_paid"] == 0
        assert s["claimed_value"] == 0

    def test_statistik_mencerminkan_aktivitas(self, db: Database) -> None:
        db.allocate_and_create_order(
            order_id="A", base_amount=1_000, qris_builder=make_builder(), ttl=900
        )
        db.claim("A", txn("T1", 1_001))
        s = db.stats()
        assert s["orders_paid"] == 1
        assert s["orders_open"] == 0
        assert s["claimed_value"] == 1_001
        assert s["claims_total"] == 1
