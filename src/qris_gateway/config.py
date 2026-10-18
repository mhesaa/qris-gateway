"""Konfigurasi aplikasi, dibaca dari environment / berkas .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # dotenv opsional; kalau tidak ada, environment tetap dipakai
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


class ConfigError(RuntimeError):
    """Konfigurasi wajib tidak lengkap."""


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} harus bilangan bulat, bukan {raw!r}") from exc


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    """Seluruh konfigurasi runtime.

    Rahasia (token, api key, qris statis) tidak pernah dicetak utuh;
    lihat :meth:`redacted` untuk representasi yang aman dilog.
    """

    # --- rahasia -----------------------------------------------------------
    shopee_token: str = ""
    api_key: str = ""
    qris_static: str = ""

    # --- shopee partner-web (read-only) -----------------------------------
    shopee_base_url: str = "https://shopeepay.shopee.co.id"
    shopee_list_path: str = "/merchant/v1/partner-web/get-transaction-list"
    shopee_detail_path: str = "/merchant/v1/partner-web/get-transaction-detail"
    http_timeout: int = 20
    http_retries: int = 3

    # --- poller ------------------------------------------------------------
    poll_interval: float = 3.0
    poll_lookback: int = 120          # detik, saat start pertama kali
    poll_page_size: int = 50
    poll_max_backoff: float = 60.0

    # --- order -------------------------------------------------------------
    amount_suffix_min: int = 1
    amount_suffix_max: int = 999
    order_ttl: int = 900              # detik (15 menit)
    point_of_initiation: str = "12"   # "12" dinamis, "11" pertahankan statis
    embed_reference: bool = True

    # --- database ----------------------------------------------------------
    database: str = "qris.db"

    # --- server ------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8088

    # --- alert -------------------------------------------------------------
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- perilaku ----------------------------------------------------------
    read_only: bool = True            # penjaga: tidak ada operasi yang mengubah dana
    require_api_key: bool = True
    mock_mode: bool = False           # pakai sumber transaksi palsu (hanya untuk dev)

    unset: tuple[str, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------ #

    @classmethod
    def load(cls, env_file: str | Path | None = ".env") -> "Settings":
        """Muat konfigurasi dari .env (bila ada) lalu environment."""
        if load_dotenv and env_file and Path(env_file).exists():
            load_dotenv(env_file, override=False)

        s = cls(
            shopee_token=_env("SHOPEE_TOKEN"),
            api_key=_env("API_KEY"),
            qris_static=_env("QRIS_STATIC"),
            shopee_base_url=_env("SHOPEE_BASE_URL", "https://shopeepay.shopee.co.id"),
            shopee_list_path=_env(
                "SHOPEE_LIST_PATH", "/merchant/v1/partner-web/get-transaction-list"
            ),
            shopee_detail_path=_env(
                "SHOPEE_DETAIL_PATH", "/merchant/v1/partner-web/get-transaction-detail"
            ),
            http_timeout=_env_int("HTTP_TIMEOUT", 20),
            http_retries=_env_int("HTTP_RETRIES", 3),
            poll_interval=float(_env("POLL_INTERVAL", "3")),
            poll_lookback=_env_int("POLL_LOOKBACK", 120),
            poll_page_size=_env_int("POLL_PAGE_SIZE", 50),
            poll_max_backoff=float(_env("POLL_MAX_BACKOFF", "60")),
            amount_suffix_min=_env_int("AMOUNT_SUFFIX_MIN", 1),
            amount_suffix_max=_env_int("AMOUNT_SUFFIX_MAX", 999),
            order_ttl=_env_int("ORDER_TTL", 900),
            point_of_initiation=_env("POINT_OF_INITIATION", "12"),
            embed_reference=_env_bool("EMBED_REFERENCE", True),
            database=_env("DATABASE", "qris.db") or "qris.db",
            host=_env("HOST", "127.0.0.1") or "127.0.0.1",
            port=_env_int("PORT", 8088),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            read_only=_env_bool("READ_ONLY", True),
            require_api_key=_env_bool("REQUIRE_API_KEY", True),
            mock_mode=_env_bool("MOCK_MODE", False),
        )

        # Dalam mode mock, token Shopee tidak diperlukan karena sumber
        # transaksinya palsu; yang tetap wajib adalah API_KEY dan QRIS_STATIC.
        required = [("API_KEY", s.api_key), ("QRIS_STATIC", s.qris_static)]
        if not s.mock_mode:
            required.insert(0, ("SHOPEE_TOKEN", s.shopee_token))
        s.unset = tuple(name for name, val in required if not val)
        return s

    # ------------------------------------------------------------------ #

    @property
    def ready(self) -> bool:
        """True bila semua rahasia wajib sudah terisi."""
        return not self.unset

    def require_ready(self) -> None:
        if not self.ready:
            raise ConfigError(
                "Konfigurasi belum lengkap. Isi dulu di .env: "
                + ", ".join(self.unset)
            )

    def redacted(self) -> dict[str, object]:
        """Representasi aman untuk log: rahasia hanya ditampilkan panjangnya."""
        def mask(v: str) -> str:
            return f"<{len(v)} chars>" if v else "<kosong>"

        return {
            "shopee_token": mask(self.shopee_token),
            "api_key": mask(self.api_key),
            "qris_static": mask(self.qris_static),
            "shopee_base_url": self.shopee_base_url,
            "poll_interval": self.poll_interval,
            "poll_page_size": self.poll_page_size,
            "order_ttl": self.order_ttl,
            "point_of_initiation": self.point_of_initiation,
            "database": self.database,
            "host": self.host,
            "port": self.port,
            "read_only": self.read_only,
            "mock_mode": self.mock_mode,
            "telegram": bool(self.telegram_bot_token and self.telegram_chat_id),
            "ready": self.ready,
            "unset": list(self.unset),
        }
