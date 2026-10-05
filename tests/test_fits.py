"""Unit tests for the strict FITS parser and pixel extraction."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.fits import (
    FitsError,
    extract_pixels,
    format_decimal,
    parse_fits,
    validate_window,
)
from verify.fitsbuild import END_CARD, assemble, build_fits, card


class TestParseValid:
    def test_minimal_file(self):
        img = parse_fits(build_fits(4, 3, 16))
        assert (img.naxis1, img.naxis2, img.bitpix) == (4, 3, 16)
        assert img.blank is None
        assert img.bscale == 1 and img.bzero == 0
        assert img.data_offset == 2880
        assert img.data_size == 24

    def test_optional_calibration_cards(self):
        img = parse_fits(
            build_fits(
                2,
                2,
                32,
                extra_cards=[
                    card("BSCALE", "2.5"),
                    card("BZERO", "-1.25"),
                    card("BLANK", "-1"),
                ],
            )
        )
        assert img.bscale == Decimal("2.5")
        assert img.bzero == Decimal("-1.25")
        assert img.blank == -1

    def test_fortran_d_exponent_accepted(self):
        img = parse_fits(build_fits(1, 1, 16, extra_cards=[card("BSCALE", "1.0D+2")]))
        assert img.bscale == Decimal("100")

    def test_commentary_cards_ignored(self):
        img = parse_fits(
            build_fits(
                1,
                1,
                16,
                extra_cards=[card("COMMENT"), card("HISTORY"), card("BUNIT", "'adu'")],
            )
        )
        assert img.naxis1 == 1

    def test_multi_block_header(self):
        extras = [card("COMMENT") for _ in range(40)]  # forces a second block
        img = parse_fits(build_fits(1, 1, 16, extra_cards=extras))
        assert img.data_offset == 2 * 2880


class TestParseStructuralErrors:
    def test_empty_file(self):
        with pytest.raises(FitsError, match="empty"):
            parse_fits(b"")

    def test_size_not_multiple_of_2880(self):
        with pytest.raises(FitsError, match="multiple of 2880"):
            parse_fits(build_fits(2, 2, 16) + b"x")

    def test_missing_end_card(self):
        cards = [card("SIMPLE", True), card("BITPIX", 16), card("NAXIS", 2),
                 card("NAXIS1", 2), card("NAXIS2", 2)]
        header = b"".join(cards)
        header += b" " * ((-len(header)) % 2880)
        with pytest.raises(FitsError, match="no END card"):
            parse_fits(header + b" " * 2880)  # printable, but no END anywhere

    def test_non_printable_header_byte(self):
        bad = bytearray(build_fits(2, 2, 16))
        bad[100] = 0x07
        with pytest.raises(FitsError, match="non-printable"):
            parse_fits(bytes(bad))

    def test_content_after_end_card(self):
        cards = [card("SIMPLE", True), card("BITPIX", 16), card("NAXIS", 2),
                 card("NAXIS1", 2), card("NAXIS2", 2), END_CARD,
                 card("SNEAKY", 1)]
        with pytest.raises(FitsError, match="after END"):
            parse_fits(assemble(cards, b"\0" * 8))

    def test_first_card_not_simple(self):
        cards = [card("XTENSION", "'IMAGE'"), card("BITPIX", 16),
                 card("NAXIS", 2), card("NAXIS1", 2), card("NAXIS2", 2), END_CARD]
        with pytest.raises(FitsError, match="SIMPLE"):
            parse_fits(assemble(cards, b"\0" * 8))

    def test_simple_false(self):
        cards = [card("SIMPLE", False), card("BITPIX", 16), card("NAXIS", 2),
                 card("NAXIS1", 2), card("NAXIS2", 2), END_CARD]
        with pytest.raises(FitsError, match="SIMPLE"):
            parse_fits(assemble(cards, b"\0" * 8))

    def test_keyword_out_of_order(self):
        cards = [card("SIMPLE", True), card("NAXIS", 2), card("BITPIX", 16),
                 card("NAXIS1", 2), card("NAXIS2", 2), END_CARD]
        with pytest.raises(FitsError, match="BITPIX"):
            parse_fits(assemble(cards, b"\0" * 8))

    def test_missing_value_indicator(self):
        raw = b"BITPIX    16".ljust(80, b" ")
        cards = [card("SIMPLE", True), raw, card("NAXIS", 2),
                 card("NAXIS1", 2), card("NAXIS2", 2), END_CARD]
        with pytest.raises(FitsError, match="value indicator"):
            parse_fits(assemble(cards, b"\0" * 8))

    def test_lowercase_keyword_rejected(self):
        cards = [card("SIMPLE", True), card("BITPIX", 16), card("NAXIS", 2),
                 card("NAXIS1", 2), card("NAXIS2", 2), card("bscale", "1.0"),
                 END_CARD]
        with pytest.raises(FitsError, match="invalid keyword"):
            parse_fits(assemble(cards, b"\0" * 8))

    def test_duplicate_keyword_rejected(self):
        with pytest.raises(FitsError, match="duplicate keyword BZERO"):
            parse_fits(
                build_fits(1, 1, 16, extra_cards=[card("BZERO", "1"), card("BZERO", "2")])
            )

    @pytest.mark.parametrize("bitpix", [8, -32, -64, 64])
    def test_unsupported_bitpix(self, bitpix):
        cards = [card("SIMPLE", True), card("BITPIX", bitpix), card("NAXIS", 2),
                 card("NAXIS1", 2), card("NAXIS2", 2), END_CARD]
        with pytest.raises(FitsError, match="BITPIX"):
            parse_fits(assemble(cards, b"\0" * 8))

    @pytest.mark.parametrize("naxis", [0, 1, 3, 4])
    def test_unsupported_naxis(self, naxis):
        cards = [card("SIMPLE", True), card("BITPIX", 16), card("NAXIS", naxis),
                 card("NAXIS1", 2), card("NAXIS2", 2), END_CARD]
        with pytest.raises(FitsError, match="NAXIS"):
            parse_fits(assemble(cards, b"\0" * 8))

    @pytest.mark.parametrize("n1,n2", [(0, 2), (2, 0), (-1, 2), (2, -3)])
    def test_bad_axis_lengths(self, n1, n2):
        cards = [card("SIMPLE", True), card("BITPIX", 16), card("NAXIS", 2),
                 card("NAXIS1", n1), card("NAXIS2", n2), END_CARD]
        with pytest.raises(FitsError, match="axis lengths"):
            parse_fits(assemble(cards, b"\0" * 8))

    def test_non_integer_axis(self):
        cards = [card("SIMPLE", True), card("BITPIX", 16), card("NAXIS", 2),
                 card("NAXIS1", "2.5"), card("NAXIS2", 2), END_CARD]
        with pytest.raises(FitsError, match="integer"):
            parse_fits(assemble(cards, b"\0" * 8))

    def test_pcount_must_be_zero(self):
        with pytest.raises(FitsError, match="PCOUNT"):
            parse_fits(build_fits(1, 1, 16, extra_cards=[card("PCOUNT", 1)]))

    def test_gcount_must_be_one(self):
        with pytest.raises(FitsError, match="GCOUNT"):
            parse_fits(build_fits(1, 1, 16, extra_cards=[card("GCOUNT", 2)]))

    def test_blank_out_of_range(self):
        with pytest.raises(FitsError, match="BLANK"):
            parse_fits(build_fits(1, 1, 16, extra_cards=[card("BLANK", "40000")]))

    @pytest.mark.parametrize("token", ["NaN", "Inf", "-Infinity", "abc", ""])
    def test_non_finite_or_invalid_bscale(self, token):
        with pytest.raises(FitsError, match="BSCALE"):
            parse_fits(
                build_fits(1, 1, 16, extra_cards=[card("BSCALE", field=f"{token} / c")])
            )

    def test_truncated_data(self):
        data = build_fits(20, 20, 16)  # needs 800 data bytes -> 2 blocks total
        with pytest.raises(FitsError, match="truncated data"):
            parse_fits(data[:2880])

    def test_trailing_content_rejected(self):
        with pytest.raises(FitsError, match="trailing content"):
            parse_fits(build_fits(2, 2, 16, trailing=b"\0" * 2880))


class TestWindowValidation:
    def test_ok(self):
        validate_window(0, 0, 1, 1, 10, 10)
        validate_window(9, 9, 1, 1, 10, 10)
        validate_window(0, 0, 100, 100, 100, 100)

    @pytest.mark.parametrize(
        "x,y,w,h",
        [(-1, 0, 1, 1), (0, -1, 1, 1), (0, 0, 0, 1), (0, 0, 1, 0),
         (0, 0, -2, 1), (0, 0, 101, 100), (10, 0, 1, 1), (0, 10, 1, 1),
         (9, 9, 2, 1), (0, 0, 11, 11)],
    )
    def test_rejected(self, x, y, w, h):
        with pytest.raises(FitsError):
            validate_window(x, y, w, h, 10, 10)


class TestFormatDecimal:
    @pytest.mark.parametrize(
        "text,expected",
        [("0", "0"), ("-0", "0"), ("0.000", "0"), ("1.2300", "1.23"),
         ("100", "100"), ("1E+2", "100"), ("0.10", "0.1"), ("-2.500", "-2.5"),
         ("1E-3", "0.001"), ("1.5E+4", "15000"), ("9.99E-5", "0.0000999"),
         ("12345678901234567890123456789", "12345678901234567890123456789"),
         ("-0.5", "-0.5"), ("10.0", "10")],
    )
    def test_format(self, text, expected):
        assert format_decimal(Decimal(text)) == expected


class TestExtractPixels:
    def test_row_order_and_blank(self):
        pixels = list(range(12))
        pixels[5] = -1
        data = build_fits(4, 3, 16, pixels=pixels, extra_cards=[card("BLANK", "-1")])
        img = parse_fits(data)
        rows = extract_pixels(data, img, 1, 1, 3, 2)
        assert rows == [[None, "6", "7"], ["9", "10", "11"]]

    def test_scaling_is_exact_decimal(self):
        data = build_fits(3, 1, 16, pixels=[1, 2, 3],
                          extra_cards=[card("BSCALE", "0.1"), card("BZERO", "0.2")])
        img = parse_fits(data)
        assert extract_pixels(data, img, 0, 0, 3, 1) == [["0.3", "0.4", "0.5"]]

    def test_trailing_zeros_trimmed(self):
        data = build_fits(2, 1, 16, pixels=[5, -3],
                          extra_cards=[card("BSCALE", "1.200")])
        img = parse_fits(data)
        assert extract_pixels(data, img, 0, 0, 2, 1) == [["6", "-3.6"]]

    def test_big_endian_32bit_exact(self):
        data = build_fits(2, 1, 32, pixels=[2147483647, -2147483648],
                          extra_cards=[card("BSCALE", "3"), card("BZERO", "-1")])
        img = parse_fits(data)
        assert extract_pixels(data, img, 0, 0, 2, 1) == [
            ["6442450940", "-6442450945"]
        ]

    def test_blank_not_scaled(self):
        data = build_fits(2, 1, 16, pixels=[-1, 4],
                          extra_cards=[card("BLANK", "-1"), card("BSCALE", "10")])
        img = parse_fits(data)
        assert extract_pixels(data, img, 0, 0, 2, 1) == [[None, "40"]]
