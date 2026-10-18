"""Test integrasi: order -> pembayaran -> lunas, plus jalur gagalnya.

Test di sini memakai sumber transaksi palsu, jadi tidak menyentuh jaringan
dan tidak memerlukan token asli.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from qris_gateway.config import Settings
from qris_gateway.db import ORDER_EXPIRED, ORDER_OPEN, ORDER_PAID, Database
from qris_gateway.events import EventBus
from qris_gateway.poller import WATERMARK_KEY, Poller
from qris_gateway.qris import describe, validate_crc
from qris_gateway.service import OrderService

from .conftest import FakeSource


@pytest.fixture
def service(settings: Settings, db: Database) -> OrderService:
    return OrderService(settings, db)


@pytest.fixture
def poller(settings: Settings, db: Database, fake_source: FakeSource, bus: EventBus) -> Poller:
    return Poller(settings=settings, db=db, source=fake_source, bus=bus)


class TestOrderCreation:
    def test_membuat_order_dengan_nominal_unik(self, service: OrderService) -> None:
        created = service.create_order(10_000)
        assert created.order.amount == 10_001
        assert created.order.status == ORDER_OPEN
        assert created.qr_png.startswith(b"\x89PNG")

    def test_qris_yang_dihasilkan_valid(self, service: OrderService) -> None:
        created = service.create_order(25_000)
        info = describe(created.order.qris)
        assert info["valid_crc"] is True
        assert info["mode"] == "dinamis"
        assert info["amount"] == str(created.order.amount)

    def test_referensi_order_tertanam(self, service: OrderService) -> None:
        created = service.create_order(15_000)
        from qris_gateway.qris import TAG_ADDITIONAL_DATA, as_dict, parse_tlv

        extra = as_dict(parse_tlv(created.order.qris))[TAG_ADDITIONAL_DATA]
        assert as_dict(parse_tlv(extra))["01"] == created.order.id

    def test_order_berurutan_tidak_kolisi(self, service: OrderService) -> None:
        amounts = {service.create_order(9_000).order.amount for _ in range(20)}
        assert len(amounts) == 20

    def test_order_id_kustom_dihormati(self, service: OrderService) -> None:
        created = service.create_order(1_000, order_id="INV-KUSTOM-1")
        assert created.order.id == "INV-KUSTOM-1"

    @pytest.mark.parametrize("bad", [0, -5])
    def test_nominal_tidak_valid_ditolak(self, service: OrderService, bad: int) -> None:
        from qris_gateway.service import ServiceError

        with pytest.raises(ServiceError):
            service.create_order(bad)

    def test_qris_statis_bernominal_ditolak(self, settings: Settings, tmp_path) -> None:
        """QRIS statis yang sudah punya tag 54 tidak boleh dipakai."""
        from qris_gateway.db import Database as DB
        from qris_gateway.qris import make_dynamic
        from qris_gateway.service import ServiceError

        already_dynamic = make_dynamic(settings.qris_static, 5_000)
        bad_settings = Settings(
            shopee_token="x", api_key="y", qris_static=already_dynamic,
            database=str(tmp_path / "x.db"),
        )
        db = DB(bad_settings.database)
        try:
            with pytest.raises(ServiceError, match="nominal"):
                OrderService(bad_settings, db)
        finally:
            db.close()


class TestPollerMatching:
    async def test_pembayaran_menandai_order_lunas(
        self, poller: Poller, service: OrderService, fake_source: FakeSource
    ) -> None:
        created = service.create_order(50_000)
        fake_source.add_txn("TXN-1", created.order.amount)

        await poller.tick()

        order = service.get_order(created.order.id)
        assert order is not None
        assert order.status == ORDER_PAID
        assert order.txn_id == "TXN-1"
        assert order.issuer == "OVO"

    async def test_event_paid_disiarkan(
        self, poller: Poller, service: OrderService, fake_source: FakeSource, bus: EventBus
    ) -> None:
        created = service.create_order(20_000)
        q = bus.subscribe(created.order.id)
        fake_source.add_txn("TXN-2", created.order.amount)

        await poller.tick()

        assert not q.empty()
        event = q.get_nowait()
        assert event.type == "order.paid"
        assert event.data["amount"] == created.order.amount

    async def test_nominal_tidak_cocok_tidak_menandai_lunas(
        self, poller: Poller, service: OrderService, fake_source: FakeSource
    ) -> None:
        created = service.create_order(30_000)
        fake_source.add_txn("TXN-LAIN", created.order.amount + 500)

        await poller.tick()

        assert service.get_order(created.order.id).status == ORDER_OPEN  # type: ignore[union-attr]

    async def test_transaksi_gagal_diabaikan(
        self, poller: Poller, service: OrderService, fake_source: FakeSource
    ) -> None:
        created = service.create_order(40_000)
        fake_source.add_txn("TXN-GAGAL", created.order.amount, status=2)

        await poller.tick()

        assert service.get_order(created.order.id).status == ORDER_OPEN  # type: ignore[union-attr]

    async def test_dua_order_nominal_sama_tidak_tertukar(
        self, poller: Poller, service: OrderService, fake_source: FakeSource
    ) -> None:
        a = service.create_order(7_000)
        b = service.create_order(7_000)
        assert a.order.amount != b.order.amount

        fake_source.add_txn("TXN-B", b.order.amount)
        await poller.tick()

        assert service.get_order(b.order.id).status == ORDER_PAID  # type: ignore[union-attr]
        assert service.get_order(a.order.id).status == ORDER_OPEN  # type: ignore[union-attr]

    async def test_transaksi_sama_tidak_diklaim_dua_kali(
        self, poller: Poller, service: OrderService, fake_source: FakeSource, db: Database
    ) -> None:
        created = service.create_order(12_000)
        fake_source.add_txn("TXN-DUP", created.order.amount)

        await poller.tick()
        await poller.tick()   # siklus kedua, transaksi yang sama masih ada

        assert db.stats()["claims_total"] == 1


class TestOrphans:
    async def test_pembayaran_tanpa_order_dicatat(
        self, poller: Poller, fake_source: FakeSource, db: Database
    ) -> None:
        fake_source.add_txn("TXN-YATIM", 88_888)
        await poller.tick()

        orphans = db.list_orphans()
        assert len(orphans) == 1
        assert orphans[0].txn_id == "TXN-YATIM"
        assert orphans[0].amount == 88_888

    async def test_orphan_tidak_dicatat_dua_kali(
        self, poller: Poller, fake_source: FakeSource, db: Database
    ) -> None:
        fake_source.add_txn("TXN-YATIM", 77_777)
        await poller.tick()
        await poller.tick()
        assert len(db.list_orphans()) == 1

    async def test_orphan_disiarkan_sebagai_event(
        self, poller: Poller, fake_source: FakeSource, bus: EventBus
    ) -> None:
        q = bus.subscribe(None)
        fake_source.add_txn("TXN-YATIM", 66_666)
        await poller.tick()

        assert not q.empty()
        assert q.get_nowait().type == "order.orphan"


class TestWatermark:
    async def test_watermark_maju_setelah_ada_transaksi(
        self, poller: Poller, service: OrderService, fake_source: FakeSource, db: Database
    ) -> None:
        before = db.get_state_int(WATERMARK_KEY, 0)
        created = service.create_order(11_000)
        fake_source.add_txn("TXN-W", created.order.amount, create_time=int(time.time()))
        await poller.tick()
        assert db.get_state_int(WATERMARK_KEY, 0) >= before

    async def test_watermark_bertahan_setelah_restart(
        self, settings: Settings, db: Database, fake_source: FakeSource, bus: EventBus
    ) -> None:
        poller = Poller(settings=settings, db=db, source=fake_source, bus=bus)
        fake_source.add_txn("TXN-W2", 5_555)
        await poller.tick()
        mark = db.get_state_int(WATERMARK_KEY, 0)
        assert mark > 0
        # Poller baru membaca watermark yang sama dari database
        poller2 = Poller(settings=settings, db=db, source=fake_source, bus=bus)
        await poller2.tick()
        assert db.get_state_int(WATERMARK_KEY, 0) >= mark


class TestEfisiensi:
    async def test_satu_siklus_satu_panggilan_berapa_pun_order(
        self, poller: Poller, service: OrderService, fake_source: FakeSource
    ) -> None:
        """Inti perbaikan efisiensi: biaya poll tidak tumbuh dengan jumlah order."""
        for _ in range(30):
            service.create_order(1_000)
        fake_source.calls.clear()

        await poller.tick()

        assert len(fake_source.calls) == 1

    async def test_issuer_hanya_diambil_saat_klaim_berhasil(
        self, poller: Poller, service: OrderService, fake_source: FakeSource
    ) -> None:
        created = service.create_order(3_000)
        # Isi daftar dengan banyak transaksi yang tidak cocok
        for i in range(20):
            fake_source.add_txn(f"NOISE-{i}", 999_000 + i)
        fake_source.add_txn("TXN-REAL", created.order.amount)

        issuer_calls: list[str] = []
        original = fake_source.get_issuer

        async def spy(txn_id: str) -> str | None:
            issuer_calls.append(txn_id)
            return await original(txn_id)

        fake_source.get_issuer = spy  # type: ignore[method-assign]
        await poller.tick()

        assert issuer_calls == ["TXN-REAL"]


class TestExpiry:
    async def test_order_lewat_ttl_ditandai_kedaluwarsa(
        self, settings: Settings, db: Database, fake_source: FakeSource, bus: EventBus
    ) -> None:
        service = OrderService(settings, db)
        created = service.create_order(1_000)
        # Paksa order menjadi tua
        with db._lock:  # noqa: SLF001
            db._conn.execute(
                "UPDATE orders SET expires_at = ? WHERE id = ?",
                (int(time.time()) - 10, created.order.id),
            )
            db._conn.commit()

        poller = Poller(settings=settings, db=db, source=fake_source, bus=bus)
        await poller.tick()

        assert service.get_order(created.order.id).status == ORDER_EXPIRED  # type: ignore[union-attr]


class TestKetahanan:
    async def test_error_sementara_tidak_merusak_state(
        self, poller: Poller, service: OrderService, fake_source: FakeSource
    ) -> None:
        created = service.create_order(60_000)
        fake_source.raise_on_list = RuntimeError("jaringan putus")

        with pytest.raises(RuntimeError):
            await poller.tick()

        # Order tetap utuh dan masih bisa dibayar setelah pulih
        assert service.get_order(created.order.id).status == ORDER_OPEN  # type: ignore[union-attr]
        fake_source.add_txn("TXN-PULIH", created.order.amount)
        await poller.tick()
        assert service.get_order(created.order.id).status == ORDER_PAID  # type: ignore[union-attr]

    async def test_loop_latar_berjalan_dan_berhenti(
        self, settings: Settings, db: Database, fake_source: FakeSource, bus: EventBus
    ) -> None:
        poller = Poller(settings=settings, db=db, source=fake_source, bus=bus)
        poller.start()
        assert poller.running
        await asyncio.sleep(0.25)
        await poller.stop()
        assert not poller.running
        assert poller.stats.cycles >= 1

    async def test_health_melaporkan_status(
        self, poller: Poller, fake_source: FakeSource
    ) -> None:
        await poller.tick()
        h = poller.health()
        assert h["cycles"] == 1
        assert h["token_alive"] == "1"
