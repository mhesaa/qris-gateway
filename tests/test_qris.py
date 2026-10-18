"""Test untuk mesin QRIS (TLV, CRC, konversi dinamis)."""

from __future__ import annotations

import pytest

from qris_gateway.qris import (
    POI_DYNAMIC,
    POI_STATIC,
    QRISError,
    TAG_ADDITIONAL_DATA,
    TAG_AMOUNT,
    TAG_CRC,
    TAG_POINT_OF_INITIATION,
    TLV,
    as_dict,
    build_tlv,
    compute_crc,
    crc16_ccitt,
    describe,
    make_dynamic,
    parse_tlv,
    validate_crc,
)

# Vektor uji CRC: payload sintetis dengan CRC yang dihitung sekali lalu
# dibekukan. Sengaja TIDAK memakai data merchant mana pun.
FROZEN = (
    "00020101021126630016ID.CO.SHOPEE.WWW0118936009140000000000021000"
    "000000000303UMI51440014ID.CO.QRIS.WWW0215ID10200000000000303UMI52"
    "04581253033605802ID5908TOKO UJI6007BANDUNG61054011563046377"
)


class TestParse:
    def test_parse_valid(self, static_qris: str) -> None:
        fields = parse_tlv(static_qris)
        tags = [f.tag for f in fields]
        assert tags[0] == "00"
        assert tags[-1] == TAG_CRC
        assert len(tags) == len(set(tags)), "tidak boleh ada tag duplikat"

    def test_parse_kembali_ke_string_sama(self, static_qris: str) -> None:
        assert build_tlv(parse_tlv(static_qris)) == static_qris

    @pytest.mark.parametrize(
        "bad,alasan",
        [
            ("", "kosong"),
            ("00", "header terpotong"),
            ("0010abc", "panjang melebihi sisa"),
            ("00xx", "panjang bukan angka"),
            ("000201" + "59xx", "panjang tag kedua bukan angka"),
            ("000201" + "5910abc", "nilai tag kedua terpotong"),
        ],
    )
    def test_tolak_payload_cacat(self, bad: str, alasan: str) -> None:
        with pytest.raises(QRISError):
            parse_tlv(bad)

    def test_tolak_tag_duplikat(self) -> None:
        with pytest.raises(QRISError, match="duplikat"):
            parse_tlv("000201" + "000201")

    def test_tlv_encode_menolak_nilai_terlalu_panjang(self) -> None:
        with pytest.raises(QRISError):
            TLV("59", "X" * 100).encode()


class TestCRC:
    def test_crc_vektor_diketahui(self) -> None:
        # "123456789" -> CRC16/CCITT-FALSE yang sudah diketahui
        assert crc16_ccitt("123456789") == "29B1"

    def test_crc_payload_diketahui(self) -> None:
        assert validate_crc(FROZEN)
        assert compute_crc(FROZEN[:-8]) == FROZEN[-4:]

    def test_crc_menolak_payload_diubah(self) -> None:
        tampered = FROZEN[:-5] + ("0" if FROZEN[-5] != "0" else "1") + FROZEN[-4:]
        assert not validate_crc(tampered)

    def test_crc_tidak_valid_pada_payload_pendek(self) -> None:
        assert not validate_crc("000201")
        assert not validate_crc("")


class TestMakeDynamic:
    def test_nominal_tersuntik(self, static_qris: str) -> None:
        dyn = make_dynamic(static_qris, 50000)
        d = as_dict(parse_tlv(dyn))
        assert d[TAG_AMOUNT] == "50000"
        assert validate_crc(dyn)

    def test_poi_berubah_ke_dinamis(self, static_qris: str) -> None:
        dyn = make_dynamic(static_qris, 1000)
        d = as_dict(parse_tlv(dyn))
        assert d[TAG_POINT_OF_INITIATION] == POI_DYNAMIC

    def test_poi_dapat_dipertahankan_statis(self, static_qris: str) -> None:
        dyn = make_dynamic(static_qris, 1000, point_of_initiation=POI_STATIC)
        d = as_dict(parse_tlv(dyn))
        assert d[TAG_POINT_OF_INITIATION] == POI_STATIC
        assert d[TAG_AMOUNT] == "1000"

    def test_idempoten(self, static_qris: str) -> None:
        assert make_dynamic(static_qris, 7777) == make_dynamic(static_qris, 7777)

    def test_nominal_berbeda_menghasilkan_qr_berbeda(self, static_qris: str) -> None:
        assert make_dynamic(static_qris, 1000) != make_dynamic(static_qris, 1001)

    def test_menghasilkan_tepat_satu_tag_54(self, static_qris: str) -> None:
        dyn = make_dynamic(static_qris, 25000)
        assert [f.tag for f in parse_tlv(dyn)].count(TAG_AMOUNT) == 1

    def test_menghasilkan_tepat_satu_tag_63(self, static_qris: str) -> None:
        dyn = make_dynamic(static_qris, 25000)
        assert [f.tag for f in parse_tlv(dyn)].count(TAG_CRC) == 1

    def test_idempotensi_konversi_berulang(self, static_qris: str) -> None:
        once = make_dynamic(static_qris, 5000)
        twice = make_dynamic(once, 5000)
        assert once == twice

    def test_ganti_nominal_pada_qris_dinamis(self, static_qris: str) -> None:
        first = make_dynamic(static_qris, 5000)
        second = make_dynamic(first, 9000)
        assert as_dict(parse_tlv(second))[TAG_AMOUNT] == "9000"
        assert validate_crc(second)

    @pytest.mark.parametrize("bad", [0, -1, -500])
    def test_tolak_nominal_tidak_positif(self, static_qris: str, bad: int) -> None:
        with pytest.raises(QRISError):
            make_dynamic(static_qris, bad)

    def test_tolak_nominal_bukan_bilangan_bulat(self, static_qris: str) -> None:
        with pytest.raises(QRISError):
            make_dynamic(static_qris, "5000")  # type: ignore[arg-type]

    def test_tolak_bool_sebagai_nominal(self, static_qris: str) -> None:
        # bool adalah subclass int; harus ditolak eksplisit
        with pytest.raises(QRISError):
            make_dynamic(static_qris, True)  # type: ignore[arg-type]


class TestReference:
    def test_referensi_tertanam_di_tag_62(self, static_qris: str) -> None:
        dyn = make_dynamic(static_qris, 1000, reference="ORD-123")
        extra = as_dict(parse_tlv(dyn))[TAG_ADDITIONAL_DATA]
        subtags = as_dict(parse_tlv(extra))
        assert subtags["01"] == "ORD-123"

    def test_referensi_dipangkas(self, static_qris: str) -> None:
        dyn = make_dynamic(static_qris, 1000, reference="X" * 60)
        extra = as_dict(parse_tlv(dyn))[TAG_ADDITIONAL_DATA]
        assert len(as_dict(parse_tlv(extra))["01"]) == 25

    def test_tanpa_referensi_tag_62_tidak_berubah(self, static_qris: str) -> None:
        before = as_dict(parse_tlv(static_qris))[TAG_ADDITIONAL_DATA]
        dyn = make_dynamic(static_qris, 1000)
        after = as_dict(parse_tlv(dyn))[TAG_ADDITIONAL_DATA]
        assert before == after

    def test_crc_tetap_valid_dengan_referensi(self, static_qris: str) -> None:
        assert validate_crc(make_dynamic(static_qris, 1000, reference="INV-9"))


class TestDescribe:
    def test_describe_statis(self, static_qris: str) -> None:
        info = describe(static_qris)
        assert info["valid_crc"] is True
        assert info["mode"] == "statis"
        assert info["amount"] is None
        assert info["currency"] == "360"

    def test_describe_dinamis(self, static_qris: str) -> None:
        info = describe(make_dynamic(static_qris, 12345))
        assert info["mode"] == "dinamis"
        assert info["amount"] == "12345"
        assert info["valid_crc"] is True
