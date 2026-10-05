"""Strict, minimal FITS primary-HDU reader for the cutout service.

Only a single primary HDU holding a two-dimensional image with BITPIX 16 or
32 is accepted.  The parser enforces the structural rules that quality
control depends on:

* 80-character header cards, printable ASCII only
* SIMPLE as the first card and the mandatory keyword order
  SIMPLE / BITPIX / NAXIS / NAXIS1 / NAXIS2
* an END card terminating the header, blank remainder of the final block
* 2880-byte alignment of header and data units
* positive axis lengths and an exact data length
* no trailing content after the primary HDU (single-HDU files only)
* finite BSCALE / BZERO calibration constants

Pixel scaling is applied with exact decimal arithmetic so that quality
control conclusions are never polluted by binary floating-point error.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from decimal import Decimal, localcontext

BLOCK_SIZE = 2880
CARD_SIZE = 80
SUPPORTED_BITPIX = (16, 32)
MAX_WINDOW_PIXELS = 10_000
# High enough that raw*BSCALE+BZERO stays exact for any calibration constant
# that fits in an 80-column card.
CALC_PRECISION = 10_000

_INT_RE = re.compile(r"^[+-]?\d+$")
_REAL_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eEdD][+-]?\d+)?$")
_KEYWORD_RE = re.compile(r"^[A-Z0-9_-]+$")
_COMMENTARY_KEYWORDS = frozenset({"", "COMMENT", "HISTORY", "HIERARCH"})


class FitsError(ValueError):
    """Structural or calibration defect in a FITS file (maps to HTTP 400)."""


@dataclass
class Card:
    keyword: str
    value_field: bytes
    has_value: bool


@dataclass
class FitsImage:
    naxis1: int
    naxis2: int
    bitpix: int
    blank: int | None
    bscale: Decimal
    bzero: Decimal
    data_offset: int
    data_size: int


def _parse_card(raw: bytes) -> Card:
    keyword_field = raw[:8].decode("ascii")
    keyword = keyword_field.rstrip()
    if keyword == "":
        return Card("", b"", False)
    if keyword_field[0] == " ":
        raise FitsError(f"keyword not left-justified: {keyword_field!r}")
    if not _KEYWORD_RE.match(keyword):
        raise FitsError(f"invalid keyword: {keyword_field!r}")
    if keyword in _COMMENTARY_KEYWORDS:
        return Card(keyword, b"", False)
    if raw[8:10] != b"= ":
        raise FitsError(f"keyword {keyword} lacks the '= ' value indicator")
    return Card(keyword, raw[10:], True)


def _value_token(card: Card) -> str:
    """Return the value token of a card, stopping at an unquoted '/'."""
    field = card.value_field.decode("ascii")
    token: list[str] = []
    in_string = False
    i = 0
    while i < len(field):
        ch = field[i]
        if in_string:
            if ch == "'":
                if i + 1 < len(field) and field[i + 1] == "'":
                    i += 2
                    continue
                in_string = False
        elif ch == "'":
            in_string = True
        elif ch == "/":
            break
        else:
            token.append(ch)
        i += 1
    return "".join(token).strip()


def _integer(card: Card) -> int:
    token = _value_token(card)
    if not _INT_RE.match(token):
        raise FitsError(f"{card.keyword}: expected an integer, got {token!r}")
    return int(token)


def _decimal(card: Card) -> Decimal:
    token = _value_token(card)
    if not _REAL_RE.match(token):
        raise FitsError(
            f"{card.keyword}: expected a finite decimal, got {token!r}"
        )
    value = Decimal(token.replace("D", "E").replace("d", "e"))
    if not value.is_finite():
        raise FitsError(f"{card.keyword}: non-finite calibration value {token!r}")
    return value


def _logical(card: Card) -> bool:
    token = _value_token(card)
    if token == "T":
        return True
    if token == "F":
        return False
    raise FitsError(f"{card.keyword}: expected logical T or F, got {token!r}")


def _require(cards: list[Card], index: int, keyword: str) -> Card:
    if len(cards) <= index or cards[index].keyword != keyword:
        found = cards[index].keyword if len(cards) > index else "<none>"
        raise FitsError(
            f"mandatory keyword {keyword} missing at card {index + 1} "
            f"(found {found})"
        )
    if not cards[index].has_value:
        raise FitsError(f"mandatory keyword {keyword} has no value")
    return cards[index]


def parse_fits(data: bytes) -> FitsImage:
    """Validate the file structure and return the image metadata."""
    if not data:
        raise FitsError("empty file")
    if len(data) % BLOCK_SIZE != 0:
        raise FitsError(
            f"file size {len(data)} is not a multiple of {BLOCK_SIZE} bytes"
        )

    cards: list[Card] = []
    end_found = False
    header_size = 0
    while header_size < len(data) and not end_found:
        block = data[header_size : header_size + BLOCK_SIZE]
        for i in range(0, BLOCK_SIZE, CARD_SIZE):
            raw = block[i : i + CARD_SIZE]
            for byte in raw:
                if not 0x20 <= byte <= 0x7E:
                    raise FitsError("header card contains non-printable bytes")
            if raw[:8] == b"END     ":
                if raw[8:] != b" " * (CARD_SIZE - 8):
                    raise FitsError("END card carries unexpected content")
                remainder = block[i + CARD_SIZE :]
                if remainder != b" " * len(remainder):
                    raise FitsError("header block after END is not blank")
                end_found = True
                break
            cards.append(_parse_card(raw))
        header_size += BLOCK_SIZE
    if not end_found:
        raise FitsError("header has no END card")

    simple = _require(cards, 0, "SIMPLE")
    if not _logical(simple):
        raise FitsError("SIMPLE is not T: not a standard primary HDU")
    bitpix = _integer(_require(cards, 1, "BITPIX"))
    if bitpix not in SUPPORTED_BITPIX:
        raise FitsError(
            f"unsupported BITPIX={bitpix}: only 16 and 32 are accepted"
        )
    naxis = _integer(_require(cards, 2, "NAXIS"))
    if naxis != 2:
        raise FitsError(
            f"unsupported NAXIS={naxis}: only two-dimensional images are accepted"
        )
    naxis1 = _integer(_require(cards, 3, "NAXIS1"))
    naxis2 = _integer(_require(cards, 4, "NAXIS2"))
    if naxis1 < 1 or naxis2 < 1:
        raise FitsError(f"axis lengths must be positive, got {naxis1}x{naxis2}")

    seen: set[str] = set()
    for card in cards:
        if card.keyword in _COMMENTARY_KEYWORDS:
            continue
        if card.keyword in seen:
            raise FitsError(f"duplicate keyword {card.keyword}")
        seen.add(card.keyword)

    blank: int | None = None
    bscale = Decimal(1)
    bzero = Decimal(0)
    for card in cards[5:]:
        if card.keyword == "BLANK":
            blank = _integer(card)
        elif card.keyword == "BSCALE":
            bscale = _decimal(card)
        elif card.keyword == "BZERO":
            bzero = _decimal(card)
        elif card.keyword == "PCOUNT":
            if _integer(card) != 0:
                raise FitsError("PCOUNT must be 0 for a primary image HDU")
        elif card.keyword == "GCOUNT":
            if _integer(card) != 1:
                raise FitsError("GCOUNT must be 1 for a primary image HDU")

    if blank is not None:
        bits = SUPPORTED_BITPIX[SUPPORTED_BITPIX.index(bitpix)]
        lo, hi = -(2 ** (bits - 1)), 2 ** (bits - 1) - 1
        if not lo <= blank <= hi:
            raise FitsError(f"BLANK={blank} lies outside the BITPIX={bitpix} range")

    bytes_per_pixel = bitpix // 8
    data_size = naxis1 * naxis2 * bytes_per_pixel
    padded_size = -(-data_size // BLOCK_SIZE) * BLOCK_SIZE
    expected_total = header_size + padded_size
    if len(data) < expected_total:
        raise FitsError(
            f"truncated data: expected {expected_total} bytes, got {len(data)}"
        )
    if len(data) > expected_total:
        raise FitsError(
            "unexpected trailing content after the primary HDU: "
            "only single-HDU files are accepted"
        )

    return FitsImage(
        naxis1=naxis1,
        naxis2=naxis2,
        bitpix=bitpix,
        blank=blank,
        bscale=bscale,
        bzero=bzero,
        data_offset=header_size,
        data_size=data_size,
    )


def validate_window(
    x: int, y: int, width: int, height: int, naxis1: int, naxis2: int
) -> None:
    """Validate the cutout window against the image bounds."""
    if x < 0 or y < 0:
        raise FitsError("x and y must be zero-based non-negative integers")
    if width < 1 or height < 1:
        raise FitsError("width and height must be positive")
    if width * height > MAX_WINDOW_PIXELS:
        raise FitsError(
            f"window of {width}x{height} exceeds the "
            f"{MAX_WINDOW_PIXELS} pixel limit"
        )
    if x + width > naxis1 or y + height > naxis2:
        raise FitsError(
            f"window (x={x}, y={y}, {width}x{height}) exceeds the "
            f"image bounds {naxis1}x{naxis2}"
        )


def format_decimal(value: Decimal) -> str:
    """Format a Decimal without exponent and without trailing zeros."""
    if value.is_zero():
        return "0"
    sign, digits, exponent = value.as_tuple()
    digit_list = list(digits)
    while len(digit_list) > 1 and digit_list[-1] == 0:
        digit_list.pop()
        exponent += 1
    text = "".join(str(d) for d in digit_list)
    if exponent >= 0:
        out = text + "0" * exponent
    else:
        point = len(text) + exponent
        if point > 0:
            out = text[:point] + "." + text[point:]
        else:
            out = "0." + "0" * (-point) + text
    return ("-" if sign else "") + out


def extract_pixels(
    data: bytes, image: FitsImage, x: int, y: int, width: int, height: int
) -> list[list[str | None]]:
    """Extract the window as rows of decimal strings (None for BLANK)."""
    bytes_per_pixel = image.bitpix // 8
    code = "h" if image.bitpix == 16 else "i"
    rows: list[list[str | None]] = []
    with localcontext() as ctx:
        ctx.prec = CALC_PRECISION
        for row_index in range(y, y + height):
            offset = image.data_offset + (row_index * image.naxis1 + x) * bytes_per_pixel
            raw_row = struct.unpack_from(f">{width}{code}", data, offset)
            row: list[str | None] = []
            for raw in raw_row:
                if image.blank is not None and raw == image.blank:
                    row.append(None)
                else:
                    scaled = Decimal(raw) * image.bscale + image.bzero
                    row.append(format_decimal(scaled))
            rows.append(row)
    return rows
