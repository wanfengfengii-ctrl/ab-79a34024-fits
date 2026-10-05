"""Build synthetic FITS files for unit tests and smoke checks."""

from __future__ import annotations

import struct

BLOCK = 2880
CARD = 80
END_CARD = b"END".ljust(CARD, b" ")

_PIXEL_CODES = {8: "b", 16: "h", 32: "i"}


def card(keyword: str, value=None, field: str | None = None) -> bytes:
    """Return one 80-byte header card.

    ``value`` True/False renders a logical, anything else is rendered with
    str() right-justified in columns 11-30.  ``field`` overrides the
    rendered value field (columns 11-80) verbatim.
    """
    if field is not None:
        body = keyword.ljust(8) + "= " + field
    elif value is None:
        return keyword.ljust(8).ljust(80).encode("ascii")
    elif isinstance(value, bool):
        body = keyword.ljust(8) + "= " + ("T" if value else "F").rjust(20)
    else:
        body = keyword.ljust(8) + "= " + str(value).rjust(20)
    return body.ljust(80).encode("ascii")


def assemble(cards: list[bytes], data: bytes = b"", trailing: bytes = b"") -> bytes:
    """Assemble header cards and a data unit into a padded FITS file."""
    header = b"".join(cards)
    header += b" " * ((-len(header)) % BLOCK)
    data += b"\0" * ((-len(data)) % BLOCK)
    return header + data + trailing


def build_fits(
    naxis1: int,
    naxis2: int,
    bitpix: int,
    pixels: list[int] | None = None,
    extra_cards: list[bytes] | None = None,
    trailing: bytes = b"",
) -> bytes:
    """Build a structurally valid single-HDU FITS file."""
    cards = [
        card("SIMPLE", True),
        card("BITPIX", bitpix),
        card("NAXIS", 2),
        card("NAXIS1", naxis1),
        card("NAXIS2", naxis2),
    ]
    cards.extend(extra_cards or [])
    cards.append(END_CARD)
    if pixels is None:
        pixels = [0] * (naxis1 * naxis2)
    code = _PIXEL_CODES[bitpix]
    data = struct.pack(f">{len(pixels)}{code}", *pixels)
    return assemble(cards, data, trailing)
