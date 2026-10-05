"""HTTP smoke checks for a running FITS cutout API.

Exercises the happy path (16/32-bit, BLANK, BSCALE/BZERO, sha256) and the
main 4xx failure modes.  Exits non-zero if any check fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from verify.fitsbuild import END_CARD, assemble, build_fits, card

API = os.environ.get("API_BASE_URL", "http://127.0.0.1:8080").rstrip("/")

_checks = 0
_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if condition:
        print(f"  ok   {name}", flush=True)
    else:
        _failures.append(name)
        print(f"  FAIL {name} {detail}", flush=True)


def get(path: str):
    try:
        with urllib.request.urlopen(API + path, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except OSError:
        return None, None


def post_cutout(body: bytes, params: dict, content_type: str = "application/fits"):
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{API}/api/fits/cutout?{query}",
        data=body,
        method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, None
    except OSError as exc:
        return None, str(exc)


def expect_4xx(name: str, status, payload, want: int | None = None) -> None:
    ok = status is not None and 400 <= status < 500
    if want is not None:
        ok = status == want
    has_detail = isinstance(payload, dict) and "detail" in payload
    check(name, ok and has_detail, f"(status={status}, body={payload!r})")


def main() -> int:
    print(f"smoke checks against {API}", flush=True)

    status, payload = get("/health")
    check("health endpoint", status == 200 and payload.get("status") == "ok",
          f"(status={status})")

    # -- happy path: 16-bit with BLANK / BSCALE / BZERO --------------------
    pixels = list(range(80))  # 10x8
    pixels[3 * 10 + 2] = 32767
    body = build_fits(
        10, 8, 16, pixels=pixels,
        extra_cards=[card("BSCALE", "0.1"), card("BZERO", "-10"),
                     card("BLANK", "32767")],
    )
    status, payload = post_cutout(body, {"x": 2, "y": 3, "width": 4, "height": 2})
    check("16-bit cutout status", status == 200, f"(status={status})")
    if status == 200:
        check("16-bit cutout dims",
              payload.get("width") == 4 and payload.get("height") == 2)
        check("16-bit cutout sha256",
              payload.get("sha256") == hashlib.sha256(body).hexdigest())
        check("16-bit cutout pixels",
              payload.get("pixels") == [[None, "-6.7", "-6.6", "-6.5"],
                                        ["-5.8", "-5.7", "-5.6", "-5.5"]],
              f"(got {payload.get('pixels')!r})")

    # -- happy path: 32-bit, trailing-zero trimming, full window -----------
    body32 = build_fits(3, 2, 32, pixels=[5, -3, 0, 123456, -1, 7],
                        extra_cards=[card("BSCALE", "1.200"), card("BZERO", "0")])
    status, payload = post_cutout(body32, {"x": 0, "y": 0, "width": 3, "height": 2})
    check("32-bit cutout", status == 200
          and payload.get("pixels") == [["6", "-3.6", "0"],
                                        ["148147.2", "-1.2", "8.4"]],
          f"(status={status}, got={None if payload is None else payload.get('pixels')!r})")

    # -- window at the pixel limit is accepted -----------------------------
    big = build_fits(120, 100, 16)
    status, _ = post_cutout(big, {"x": 0, "y": 0, "width": 100, "height": 100})
    check("10000-pixel window accepted", status == 200, f"(status={status})")

    # -- window violations --------------------------------------------------
    for name, params in [
        ("window out of bounds x", {"x": 10, "y": 0, "width": 1, "height": 1}),
        ("window out of bounds y", {"x": 0, "y": 8, "width": 1, "height": 1}),
        ("window too many pixels", {"x": 0, "y": 0, "width": 101, "height": 100}),
        ("negative x", {"x": -1, "y": 0, "width": 1, "height": 1}),
        ("zero width", {"x": 0, "y": 0, "width": 0, "height": 1}),
    ]:
        status, payload = post_cutout(body, params)
        expect_4xx(name, status, payload, 400)

    status, payload = post_cutout(body, {"x": "a", "y": 0, "width": 1, "height": 1})
    expect_4xx("non-integer parameter", status, payload)

    # -- transport-level violations -----------------------------------------
    status, payload = post_cutout(body, {"x": 0, "y": 0, "width": 1, "height": 1},
                                  content_type="application/octet-stream")
    expect_4xx("wrong content type", status, payload, 415)

    status, payload = post_cutout(b"\0" * (16 * 1024 * 1024 + 1),
                                  {"x": 0, "y": 0, "width": 1, "height": 1})
    expect_4xx("body over 16 MiB", status, payload, 413)

    # -- structural violations ----------------------------------------------
    header_only = assemble([card("SIMPLE", True), card("BITPIX", 16),
                            card("NAXIS", 2), card("NAXIS1", 2), card("NAXIS2", 2)])
    cases = [
        ("missing END card", header_only),
        ("BITPIX=8 rejected", build_fits(2, 2, 8)),
        ("trailing garbage", build_fits(2, 2, 16, trailing=b"\0" * 2880)),
        ("truncated data", build_fits(20, 20, 16)[:2880]),
        ("non-finite BSCALE",
         build_fits(1, 1, 16, extra_cards=[card("BSCALE", field="NaN / bad")])),
        ("duplicate keyword",
         build_fits(1, 1, 16, extra_cards=[card("BZERO", "1"), card("BZERO", "2")])),
        ("NAXIS=3 rejected",
         assemble([card("SIMPLE", True), card("BITPIX", 16), card("NAXIS", 3),
                   card("NAXIS1", 2), card("NAXIS2", 2), card("NAXIS3", 1),
                   END_CARD], b"\0" * 8)),
    ]
    for name, bad_body in cases:
        status, payload = post_cutout(bad_body, {"x": 0, "y": 0, "width": 1, "height": 1})
        expect_4xx(name, status, payload, 400)

    print(f"\nsmoke: {_checks - len(_failures)}/{_checks} checks passed", flush=True)
    if _failures:
        print("failed: " + ", ".join(_failures), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
