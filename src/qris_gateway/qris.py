"""EMVCo QRIS payload engine: TLV parse/build, CRC16-CCITT, dynamic conversion.

QRIS memakai skema TLV (Tag-Length-Value) sesuai standar EMVCo MPM.
Modul ini murni domain logic: tidak ada I/O, tidak ada network, dan
mudah diuji. Semua fungsi deterministik.

Referensi tag yang dipakai:
    00  Payload Format Indicator        (selalu "01")
    01  Point of Initiation Method      ("11" statis, "12" dinamis)
    26  Merchant Account Information    (NMID domestik)
    51  Merchant Account Information    (JALIN/QRIS)
    52  Merchant Category Code
    53  Transaction Currency            ("360" = IDR)
    54  Transaction Amount              <-- disuntik saat jadi dinamis
    58  Country Code
    59  Merchant Name
    60  Merchant City
    61  Postal Code
    62  Additional Data Field Template  (subtag 01 = Bill Number)
    63  CRC16-CCITT
"""

from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Konstanta tag
# ---------------------------------------------------------------------------

TAG_PAYLOAD_FORMAT = "00"
TAG_POINT_OF_INITIATION = "01"
TAG_MERCHANT_ACCOUNT_DOMESTIC = "26"
TAG_MERCHANT_ACCOUNT_JALIN = "51"
TAG_MERCHANT_CATEGORY_CODE = "52"
TAG_CURRENCY = "53"
TAG_AMOUNT = "54"
TAG_COUNTRY = "58"
TAG_MERCHANT_NAME = "59"
TAG_MERCHANT_CITY = "60"
TAG_POSTAL_CODE = "61"
TAG_ADDITIONAL_DATA = "62"
TAG_CRC = "63"

POI_STATIC = "11"
POI_DYNAMIC = "12"

CURRENCY_IDR = "360"

# Subtag di dalam tag 62
SUBTAG_BILL_NUMBER = "01"
SUBTAG_REFERENCE_LABEL = "05"

_CRC_LENGTH = 4
_MAX_LENGTH = 99


class QRISError(ValueError):
    """Payload QRIS tidak valid."""


@dataclass(frozen=True, slots=True)
class TLV:
    """Satu elemen Tag-Length-Value."""

    tag: str
    value: str

    @property
    def length(self) -> int:
        return len(self.value)

    def encode(self) -> str:
        if len(self.tag) != 2 or not self.tag.isdigit():
            raise QRISError(f"tag harus 2 digit numerik: {self.tag!r}")
        if self.length > _MAX_LENGTH:
            raise QRISError(
                f"nilai tag {self.tag} panjangnya {self.length}, maksimum {_MAX_LENGTH}"
            )
        return f"{self.tag}{self.length:02d}{self.value}"


# ---------------------------------------------------------------------------
# Parse
# ---------------------------------------------------------------------------


def parse_tlv(payload: str) -> list[TLV]:
    """Urai payload TLV menjadi daftar :class:`TLV`.

    Raises:
        QRISError: bila struktur rusak, ada sisa byte, atau tag duplikat.
    """
    if not payload:
        raise QRISError("payload kosong")

    fields: list[TLV] = []
    seen: set[str] = set()
    i = 0
    n = len(payload)

    while i < n:
        if i + 4 > n:
            raise QRISError(f"header TLV terpotong di offset {i}")

        tag = payload[i : i + 2]
        raw_len = payload[i + 2 : i + 4]
        if not raw_len.isdigit():
            raise QRISError(f"panjang bukan angka di offset {i}: {raw_len!r}")

        length = int(raw_len)
        start = i + 4
        end = start + length
        if end > n:
            raise QRISError(
                f"nilai tag {tag} menuntut {length} byte, tersisa {n - start}"
            )

        if tag in seen:
            raise QRISError(f"tag duplikat: {tag}")
        seen.add(tag)

        fields.append(TLV(tag, payload[start:end]))
        i = end

    return fields


def build_tlv(fields: list[TLV]) -> str:
    """Rangkai daftar :class:`TLV` kembali menjadi string payload."""
    return "".join(f.encode() for f in fields)


def as_dict(fields: list[TLV]) -> dict[str, str]:
    """Konversi ke dict tag -> value (untuk inspeksi cepat)."""
    return {f.tag: f.value for f in fields}


# ---------------------------------------------------------------------------
# CRC16-CCITT (poly 0x1021, init 0xFFFF) -- varian CCITT-FALSE
# ---------------------------------------------------------------------------


def crc16_ccitt(data: str) -> str:
    """Hitung CRC16-CCITT dan kembalikan 4 karakter hex uppercase."""
    crc = 0xFFFF
    for ch in data:
        crc ^= ord(ch) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return f"{crc:04X}"


def compute_crc(payload_without_crc: str) -> str:
    """Hitung CRC untuk payload yang belum punya tag 63.

    Tag 63 dan panjangnya ikut dihitung, jadi keduanya ditambahkan dulu
    sebelum CRC dihitung (perilaku standar EMVCo).
    """
    return crc16_ccitt(payload_without_crc + TAG_CRC + "04")


def validate_crc(payload: str) -> bool:
    """Verifikasi CRC payload QRIS lengkap."""
    idx = payload.rfind(f"{TAG_CRC}04")
    if idx < 0 or idx + 8 != len(payload):
        return False
    expected = payload[idx + 4 : idx + 8].upper()
    return compute_crc(payload[:idx]) == expected


# ---------------------------------------------------------------------------
# Konversi statis -> dinamis
# ---------------------------------------------------------------------------


def make_dynamic(
    static_payload: str,
    amount: int,
    *,
    reference: str | None = None,
    point_of_initiation: str = POI_DYNAMIC,
) -> str:
    """Ubah QRIS statis menjadi dinamis dengan menyuntikkan nominal.

    Args:
        static_payload: QRIS statis merchant (dari acquirer).
        amount: nominal dalam rupiah, bilangan bulat positif.
        reference: opsional, nomor referensi/bill number yang ditanam di
            tag 62 subtag 01. Berguna untuk jejak audit; apakah acquirer
            mengembalikannya atau tidak harus diuji terpisah.
        point_of_initiation: "12" (dinamis, default) atau "11" untuk
            mempertahankan perilaku QRIS statis yang disuntik nominal.

    Returns:
        Payload QRIS dinamis lengkap dengan CRC yang benar.

    Raises:
        QRISError: bila nominal tidak valid atau payload sumber rusak.
    """
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise QRISError("nominal harus bilangan bulat")
    if amount <= 0:
        raise QRISError("nominal harus lebih besar dari nol")

    fields = parse_tlv(static_payload)

    # Buang CRC lama dan tag yang akan ditulis ulang.
    rebuilt: list[TLV] = []
    have_amount = False
    have_poi = False

    for f in fields:
        if f.tag == TAG_CRC:
            continue
        if f.tag == TAG_AMOUNT:
            rebuilt.append(TLV(TAG_AMOUNT, str(amount)))
            have_amount = True
            continue
        if f.tag == TAG_POINT_OF_INITIATION:
            rebuilt.append(TLV(TAG_POINT_OF_INITIATION, point_of_initiation))
            have_poi = True
            continue
        if f.tag == TAG_ADDITIONAL_DATA:
            rebuilt.append(_with_reference(f, reference))
            continue
        rebuilt.append(f)

    if not have_amount:
        rebuilt = _insert_after(rebuilt, TAG_CURRENCY, TLV(TAG_AMOUNT, str(amount)))
    if not have_poi:
        rebuilt = _insert_after(
            rebuilt, TAG_PAYLOAD_FORMAT, TLV(TAG_POINT_OF_INITIATION, point_of_initiation)
        )

    body = build_tlv(rebuilt)
    return body + TAG_CRC + "04" + compute_crc(body)


def _with_reference(additional: TLV, reference: str | None) -> TLV:
    """Sisipkan nomor referensi ke dalam tag 62 (Additional Data)."""
    if reference is None:
        return additional

    try:
        subtags = parse_tlv(additional.value)
    except QRISError:
        # Tag 62 rusak; jangan tebak-tebakan, biarkan apa adanya.
        return additional

    reference = reference[:25]  # batas praktis panjang bill number EMVCo
    replaced = False
    out: list[TLV] = []
    for st in subtags:
        if st.tag == SUBTAG_BILL_NUMBER:
            out.append(TLV(SUBTAG_BILL_NUMBER, reference))
            replaced = True
        else:
            out.append(st)
    if not replaced:
        out.insert(0, TLV(SUBTAG_BILL_NUMBER, reference))

    return TLV(TAG_ADDITIONAL_DATA, build_tlv(out))


def _insert_after(fields: list[TLV], tag: str, new_field: TLV) -> list[TLV]:
    """Sisipkan field tepat setelah tag tertentu, atau di akhir."""
    out: list[TLV] = []
    inserted = False
    for f in fields:
        out.append(f)
        if f.tag == tag and not inserted:
            out.append(new_field)
            inserted = True
    if not inserted:
        out.append(new_field)
    return out


# ---------------------------------------------------------------------------
# Inspeksi
# ---------------------------------------------------------------------------


def describe(payload: str) -> dict[str, object]:
    """Ringkasan human-readable dari payload QRIS (untuk debugging)."""
    fields = parse_tlv(payload)
    d = as_dict(fields)
    return {
        "valid_crc": validate_crc(payload),
        "length": len(payload),
        "point_of_initiation": d.get(TAG_POINT_OF_INITIATION),
        "mode": (
            "dinamis"
            if d.get(TAG_POINT_OF_INITIATION) == POI_DYNAMIC
            else "statis"
            if d.get(TAG_POINT_OF_INITIATION) == POI_STATIC
            else "tidak diketahui"
        ),
        "amount": d.get(TAG_AMOUNT),
        "currency": d.get(TAG_CURRENCY),
        "country": d.get(TAG_COUNTRY),
        "merchant_name": d.get(TAG_MERCHANT_NAME),
        "merchant_city": d.get(TAG_MERCHANT_CITY),
        "postal_code": d.get(TAG_POSTAL_CODE),
        "mcc": d.get(TAG_MERCHANT_CATEGORY_CODE),
        "tags": [f.tag for f in fields],
    }
