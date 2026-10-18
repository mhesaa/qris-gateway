"""Fixture bersama untuk seluruh test.

QRIS statis untuk pengujian dibangun dari nol, bukan disalin dari merchant
mana pun, sehingga test tidak membawa data orang lain di dalamnya.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from qris_gateway.config import Settings
from qris_gateway.db import Database
from qris_gateway.events import EventBus
from qris_gateway.qris import TAG_CRC, TLV, build_tlv, compute_crc


def build_static_qris(*, merchant: str = "TOKO UJI", city: str = "BANDUNG") -> str:
    """Bangun QRIS statis sintetis yang valid (CRC benar)."""
    fields = [
        TLV("00", "01"),
        TLV("01", "11"),
        TLV("26", "0016ID.CO.SHOPEE.WWW0118936009140000000000021000000000000303UMI"),
        TLV("51", "0014ID.CO.QRIS.WWW0215ID10200000000000303UMI"),
        TLV("52", "5812"),
        TLV("53", "360"),
        TLV("58", "ID"),
        TLV("59", merchant),
        TLV("60", city),
        TLV("61", "40115"),
        TLV("62", "0703A01"),
    ]
    body = build_tlv(fields)
    return body + TAG_CRC + "04" + compute_crc(body)


@pytest.fixture
def static_qris() -> str:
    return build_static_qris()


@pytest.fixture
def settings(tmp_path, static_qris: str) -> Settings:
    return Settings(
        shopee_token="B:token-uji-yang-panjang-sekali",
        api_key="kunci-uji",
        qris_static=static_qris,
        database=str(tmp_path / "test.db"),
        poll_interval=0.05,
        poll_lookback=300,
        order_ttl=900,
        amount_suffix_min=1,
        amount_suffix_max=50,
        read_only=True,
        require_api_key=True,
    )


@pytest.fixture
def db(settings: Settings) -> Database:
    database = Database(settings.database)
    yield database
    database.close()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


class FakeSource:
    """Sumber transaksi palsu yang memenuhi protokol TransactionSource."""

    def __init__(self) -> None:
        self.transactions: list[dict[str, Any]] = []
        self.issuers: dict[str, str] = {"default": "OVO"}
        self.calls: list[tuple[int, int]] = []
        self.fail_next: int = 0
        self.raise_on_list: Exception | None = None
        self.closed = False

    # -- TransactionSource ---------------------------------------------- #

    async def list_transactions(
        self, start_time: int, end_time: int, page_size: int, cursor: str = ""
    ) -> tuple[list[dict[str, Any]], str]:
        self.calls.append((start_time, end_time))
        if self.raise_on_list:
            exc, self.raise_on_list = self.raise_on_list, None
            raise exc
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("kegagalan sementara yang disimulasikan")
        rows = [
            t for t in self.transactions
            if start_time <= int(t.get("createTime", 0)) <= end_time
        ]
        return rows[:page_size], ""

    async def get_issuer(self, txn_id: str) -> str | None:
        return self.issuers.get(txn_id, self.issuers.get("default"))

    async def aclose(self) -> None:
        self.closed = True

    # -- helper --------------------------------------------------------- #

    def add_txn(
        self,
        txn_id: str,
        amount: int,
        *,
        status: int = 3,
        create_time: int | None = None,
    ) -> None:
        self.transactions.append(
            {
                "transactionId": txn_id,
                "displayTransactionId": txn_id,
                "amount": f"{amount:,}".replace(",", "."),
                "status": status,
                "createTime": create_time if create_time is not None else int(time.time()),
            }
        )


@pytest.fixture
def fake_source() -> FakeSource:
    return FakeSource()
