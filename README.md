# QRIS Gateway

Gateway verifikasi pembayaran QRIS yang **read-only** terhadap akun merchant.
Dia membuat QRIS dinamis dari QRIS statis Anda, memantau mutasi, lalu
menandai order sebagai lunas secara real-time.

Ditulis ulang dari nol dalam Python. Fokus: terstruktur, efisien, dan stabil.

> **Status**: read-only. Tidak ada operasi yang mengubah dana. Tidak ada
> pembuatan pembayaran, refund, atau penarikan. Yang dilakukan hanya membaca
> mutasi dan mencocokkannya ke order.

---

## Disclaimer

Proyek ini **tidak resmi**. Tidak berafiliasi dengan, tidak didukung oleh, dan
tidak disetujui oleh PT AirPay International Indonesia, ShopeePay, maupun Sea
Group dalam kapasitas apa pun. Nama "Shopee" dan "ShopeePay" adalah merek milik
pemiliknya masing-masing.

Cara kerja proyek ini adalah membaca data dari portal mitra ShopeePay memakai
sesi akun merchant milik Anda sendiri, serupa dengan cara kerja ekstensi browser
atau skrip otomasi pihak ketiga.

**Yang perlu Anda pahami sebelum memakai:**

* Proyek ini **tidak** memakai API publik resmi. Endpoint yang dipakai bersifat
  internal dan dapat berubah sewaktu-waktu tanpa pemberitahuan.
* Syarat Layanan ShopeePay melarang rekayasa balik layanan mereka. Anda
  bertanggung jawab penuh atas kepatuhan terhadap ketentuan yang berlaku.
* Tidak ada jaminan dalam bentuk apa pun. Tidak ada SLA, tidak ada dukungan.
* Kredensial Anda hanya tersimpan di berkas `.env` di server Anda sendiri dan
  tidak pernah dikirim ke mana pun selain ke server resmi ShopeePay.

Untuk penggunaan produksi komersial, gunakan API merchant resmi dengan
perjanjian komersial. Lihat bagian Batasan di akhir dokumen ini.

---

## Kenapa versi ini berbeda

Implementasi umum dari pendekatan ini biasanya melakukan polling dari sisi
klien, mencocokkan pembayaran berdasarkan nominal saja, dan menyimpan
state di memori. Tiga hal itu sumber dari hampir semua masalahnya.

Versi ini memperbaiki akarnya:

| Masalah umum | Penyelesaian di sini |
|---|---|
| Dua pembeli bayar nominal sama, salah klaim | Nominal dibuat unik per order, dijamin indeks unik database |
| State hilang saat restart | Semua state di SQLite, termasuk watermark poller |
| N pembeli = N poller | Satu poller di server, hasil disiarkan lewat SSE |
| Latensi 10-15 detik | Di bawah interval poll (default 3 detik) |
| Satu transaksi bisa diklaim dua kali | `claims.txn_id` PRIMARY KEY, jaminan struktural |
| Pembayaran telat hilang diam-diam | Dicatat sebagai orphan, bukan dibuang |
| QR dikirim ke server pihak ketiga | Dirender lokal di server sendiri |
| N+1 request ke API | Satu panggilan per siklus, detail hanya saat klaim berhasil |

---

## Arsitektur

```
                    POST /orders
                         |
                         v
              +----------------------+
              |  Allocator nominal   |  base + suffix unik
              |  (indeks unik DB)    |
              +----------+-----------+
                         |
                         v
              +----------------------+
              |  TLV inject + CRC16  |  QRIS statis -> dinamis
              |  (qris.py)           |
              +----------+-----------+
                         |
              QR ditampilkan ke pelanggan
                         |
                         v
   +------------------------------------------+
   |  SATU POLLER (poller.py)                 |
   |  loop tiap 3 detik:                      |
   |    tarik mutasi sejak watermark          |
   |    cocokkan ke order terbuka             |
   |    klaim atomik + siarkan event          |
   +--------------------+---------------------+
                        |
                        v
              +----------------------+
              |  EventBus (SSE)      |  push ke halaman checkout
              +----------------------+
                        |
                        v
              +----------------------+
              |  Rekonsiliasi        |  orphan, telat bayar
              +----------------------+
```

---

## Struktur berkas

```
src/qris_gateway/
    config.py      Konfigurasi dan validasi environment
    qris.py        Mesin EMVCo: TLV, CRC16, konversi dinamis
    db.py          SQLite: skema, allocator, klaim, orphan
    matcher.py     Logika pencocokan (murni, tanpa I/O)
    shopee.py      Klien partner-web (read-only)
    poller.py      Loop latar belakang + watermark
    events.py      Pub/sub untuk SSE
    service.py     Orkestrasi order + render QR
    api.py         Rute HTTP (FastAPI)
    mock.py        Sumber transaksi palsu (hanya untuk pengembangan)
tests/
    104 test, semuanya jalan tanpa jaringan
```

Pemisahannya disengaja: `matcher.py` dan `qris.py` murni fungsi tanpa I/O,
sehingga bisa diuji sampai habis tanpa mock. `db.py` memegang jaminan
integritas di level skema, bukan di level kode aplikasi.

---

## Instalasi

```bash
cd qris-gateway
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env
```

Isi `.env`:

| Variabel | Isi |
|---|---|
| `SHOPEE_TOKEN` | Token sesi partner portal (diawali `B:`) |
| `API_KEY` | Kunci acak untuk melindungi gateway ini |
| `QRIS_STATIC` | QRIS statis merchant Anda, tanpa nominal |

Cara mengambil token: login ke `https://partner.shopee.co.id`, buka Developer
Tools (F12), tab Network, filter Fetch/XHR, refresh halaman transaksi, cari
request `get-transaction-list`, lihat Request Payload, ambil
`data.metadata.token`.

Cara membuat API key:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## Menjalankan

```bash
.venv/bin/uvicorn qris_gateway.api:app --host 127.0.0.1 --port 8088
```

Dokumentasi API otomatis tersedia di `http://127.0.0.1:8088/docs`.

---

## Coba tanpa token (mode mock)

Untuk mengembangkan atau mendemokan alur tanpa menyentuh akun merchant asli:

```bash
# di .env
MOCK_MODE=true
SHOPEE_TOKEN=
```

Lalu suntikkan pembayaran palsu:

```bash
# 1. buat order
curl -X POST localhost:8088/orders \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"amount":50000}'

# 2. simulasikan pembayaran sebesar nominal yang dikembalikan
curl -X POST localhost:8088/mock/pay \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"amount":50001,"issuer":"DANA"}'

# 3. lihat status dan event SSE
curl -N localhost:8088/orders/<ORDER_ID>/stream
```

Mode mock dimatikan secara default dan endpoint `/mock/*` mengembalikan 404
saat tidak aktif.

---

## Endpoint

| Metode | Path | Auth | Keterangan |
|---|---|---|---|
| GET | `/health` | - | Status server dan poller |
| GET | `/stats` | ya | Statistik order dan klaim |
| GET | `/qris/inspect` | ya | Ringkasan QRIS statis yang dikonfigurasi |
| POST | `/orders` | ya | Buat order + QRIS dinamis |
| GET | `/orders` | ya | Daftar order |
| GET | `/orders/{id}` | ya | Status satu order |
| GET | `/orders/{id}/qr.png` | - | Gambar QR (publik, dipindai pelanggan) |
| GET | `/orders/{id}/qris.txt` | - | Payload QRIS mentah |
| GET | `/orders/{id}/stream` | - | SSE, push status real-time |
| GET | `/orphans` | ya | Pembayaran tanpa order |
| POST | `/orphans/{txn_id}/resolve` | ya | Tandai orphan selesai ditangani |
| POST | `/admin/token` | ya | Perbarui token sesi |
| POST | `/mock/pay` | ya | Suntik pembayaran palsu (hanya MOCK_MODE) |

Autentikasi lewat header `X-API-Key` atau query `?api_key=`.

---

## Jaminan integritas

Dua jaminan ini ditegakkan oleh skema database, bukan oleh kode aplikasi,
sehingga tidak bisa dilanggar oleh bug maupun restart:

**Anti-kolisi nominal.** Indeks unik parsial pada `orders(amount)` untuk
baris berstatus `open`. Dua order terbuka tidak mungkin punya nominal sama,
sehingga nominal bisa dipakai sebagai kunci pencocokan yang andal.

```sql
CREATE UNIQUE INDEX idx_orders_amount_open
    ON orders(amount) WHERE status = 'open';
```

**Anti-klaim-ganda.** `claims.txn_id` adalah PRIMARY KEY. Satu transaksi
hanya bisa melunasi satu order, selamanya, lintas restart.

```sql
CREATE TABLE claims (
    txn_id     TEXT PRIMARY KEY,
    order_id   TEXT NOT NULL REFERENCES orders(id),
    ...
);
```

Keduanya punya test khusus yang mensimulasikan restart proses.

---

## Cara kerja pencocokan

Sebuah transaksi cocok dengan order bila **semua** kondisi berikut terpenuhi:

1. Status transaksi sukses (`status == 3`)
2. Nominal sama persis
3. Order masih terbuka
4. Transaksi terjadi setelah order dibuat, dengan toleransi jam 90 detik
5. Transaksi belum pernah diklaim order lain

Toleransi jam (`CLOCK_SKEW`) ada karena jam server tidak dijamin sinkron.
Tanpa itu, pembayaran yang datang beberapa detik lebih awal akan hilang.

Transaksi yang lebih tua dari 60 menit tidak akan pernah mengklaim order baru,
sebagai jaring pengaman agar pembayaran lama tidak cocok ke order yang baru
dibuat karena kebetulan nominalnya sama.

---

## Orphan: pembayaran tanpa order

Kalau pelanggan membayar setelah QR kedaluwarsa, atau nominalnya tidak cocok
dengan order apa pun, uang itu tetap masuk. Sistem ini **tidak membuangnya**.
Transaksi tersebut dicatat di tabel `orphans` beserta alasannya, dan
dikirim sebagai event.

Ini disengaja: uang nyata yang tidak tercatat adalah kegagalan yang jauh
lebih mahal daripada catatan tambahan yang perlu ditindaklanjuti.

```bash
curl -H "X-API-Key: $API_KEY" localhost:8088/orphans
```

---

## Menjalankan test

```bash
.venv/bin/pytest
```

104 test, semuanya berjalan tanpa jaringan dan tanpa token asli. Cakupannya:

* `test_qris.py` (34) - TLV, CRC16 terhadap vektor nyata, konversi dinamis
* `test_matcher.py` (21) - semua jalur pencocokan, termasuk kasus batas waktu
* `test_db.py` - anti-kolisi, anti-klaim-ganda, ketahanan restart
* `test_integration.py` - alur order sampai lunas, orphan, efisiensi poller

---

## Catatan keamanan

* **Rahasia tidak pernah dicetak.** `Settings.redacted()` hanya menampilkan
  panjang nilai, bukan isinya.
* **QR dirender lokal.** Payload QRIS memuat NMID merchant, jadi mengirimnya
  ke layanan pihak ketiga adalah kebocoran data.
* **Default hanya localhost.** Ubah `HOST` ke `0.0.0.0` hanya bila benar-benar
  perlu diakses dari luar, dan pastikan sudah ada pembatas jaringan.
* **Read-only ditegakkan di kode.** `ShopeePartnerClient` menolak konstruksi
  bila `READ_ONLY` dimatikan.

---

## Batasan yang perlu diketahui

Tiga hal ini tidak bisa diselesaikan dengan rekayasa, dan penting dipahami
sebelum memakai sistem ini untuk uang sungguhan:

1. **Token diambil manual.** Tidak ada auto-refresh. Kalau sesi mati, token
   harus diambil ulang dari browser. Poller akan berhenti dan melaporkan
   error, tetapi perbaikannya tetap manual.
2. **Tidak ada refund.** Sistem ini read-only. Sengketa dan pengembalian dana
   harus ditangani manual di luar sistem.
3. **Tidak ada kontrak.** Endpoint yang dipakai bersifat internal, tanpa
   jaminan kestabilan maupun pemberitahuan perubahan. Bila berubah, sistem
   ini berhenti dan tidak ada pihak yang bisa dimintai pertanggungjawaban.

Untuk penggunaan produksi jangka panjang, jalur yang benar adalah API merchant
resmi dengan perjanjian komersial, yang menyediakan `partnerReferenceNo`,
webhook bertanda tangan, dan endpoint pembatalan QR. Modul `service.py`
sengaja dibuat di belakang antarmuka yang sempit supaya provider resmi bisa
ditambahkan di samping implementasi ini tanpa mengubah UI.

---

## Lisensi

MIT
