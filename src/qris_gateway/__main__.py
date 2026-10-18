"""Titik masuk CLI: ``qris-gateway`` atau ``python -m qris_gateway``."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="qris-gateway",
        description="Gateway verifikasi pembayaran QRIS (read-only).",
    )
    parser.add_argument("--host", help="Alamat bind (default dari .env)")
    parser.add_argument("--port", type=int, help="Port (default dari .env)")
    parser.add_argument("--reload", action="store_true", help="Auto-reload (pengembangan)")
    parser.add_argument("--log-level", default="info", help="Level log uvicorn")
    parser.add_argument(
        "--check", action="store_true", help="Periksa konfigurasi lalu keluar"
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.check:
        return _check()


    try:
        import uvicorn
    except ImportError:
        print("uvicorn belum terpasang. Jalankan: pip install -e .", file=sys.stderr)
        return 2

    settings = Settings.load()
    host = args.host or settings.host
    port = args.port or settings.port

    if not settings.ready:
        print(
            "Peringatan: variabel berikut belum diisi: "
            + ", ".join(settings.unset),
            file=sys.stderr,
        )
        print("Server tetap dijalankan, tetapi poller tidak akan aktif.", file=sys.stderr)

    uvicorn.run(
        "qris_gateway.api:app",
        host=host,
        port=port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


def _check() -> int:
    """Validasi konfigurasi dan QRIS statis tanpa menjalankan server."""
    try:
        settings = Settings.load()
    except ConfigError as exc:
        print(f"GAGAL: {exc}", file=sys.stderr)
        return 1

    print("Konfigurasi:")
    for key, value in settings.redacted().items():
        print(f"  {key:20} = {value}")

    if not settings.ready:
        print("\nStatus: BELUM LENGKAP. Isi dulu: " + ", ".join(settings.unset))
        return 1

    from .db import Database
    from .service import OrderService, ServiceError

    db = Database(settings.database)
    try:
        OrderService(settings, db)
    except ServiceError as exc:
        print(f"\nGAGAL: {exc}", file=sys.stderr)
        return 1
    finally:
        db.close()

    print("\nStatus: OK. QRIS statis valid dan konfigurasi lengkap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
