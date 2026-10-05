"""HTTP API for the FITS cutout service."""

from __future__ import annotations

import hashlib

from fastapi import FastAPI, HTTPException, Query, Request

from .fits import FitsError, extract_pixels, parse_fits, validate_window

MAX_BODY_BYTES = 16 * 1024 * 1024  # 16 MiB

app = FastAPI(title="FITS cutout service", version="1.0.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/fits/cutout")
async def cutout(
    request: Request,
    x: int = Query(..., description="zero-based column of the window origin"),
    y: int = Query(..., description="zero-based row of the window origin"),
    width: int = Query(..., description="window width in pixels"),
    height: int = Query(..., description="window height in pixels"),
) -> dict:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/fits":
        raise HTTPException(415, "expected Content-Type application/fits")
    body = await _read_body(request)
    try:
        image = parse_fits(body)
        validate_window(x, y, width, height, image.naxis1, image.naxis2)
        pixels = extract_pixels(body, image, x, y, width, height)
    except FitsError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(body).hexdigest(),
        "pixels": pixels,
    }


async def _read_body(request: Request) -> bytes:
    """Read the request body, enforcing the 16 MiB limit.

    On overflow the stream is drained (up to a bound) before raising 413 so
    the connection stays in sync and the client reliably receives the
    response instead of a reset.
    """
    chunks: list[bytes] = []
    total = 0
    overflow = False
    async for chunk in request.stream():
        total += len(chunk)
        if not overflow:
            if total > MAX_BODY_BYTES:
                overflow = True
                chunks.clear()
            else:
                chunks.append(chunk)
        elif total > 2 * MAX_BODY_BYTES:
            # The client keeps sending far beyond the limit; stop draining.
            break
    if overflow:
        raise HTTPException(413, "file exceeds the 16 MiB limit")
    return b"".join(chunks)
