"""End-to-end API tests through FastAPI's TestClient."""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient

from app.main import app
from verify.fitsbuild import build_fits, card

client = TestClient(app)


def post_cutout(body: bytes, params: dict, content_type: str = "application/fits"):
    return client.post(
        "/api/fits/cutout",
        params=params,
        content=body,
        headers={"Content-Type": content_type},
    )


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_cutout_success_16bit():
    pixels = list(range(80))  # 10x8
    pixels[3 * 10 + 2] = 32767  # BLANK inside the window
    body = build_fits(
        10,
        8,
        16,
        pixels=pixels,
        extra_cards=[card("BSCALE", "0.1"), card("BZERO", "-10"),
                     card("BLANK", "32767")],
    )
    resp = post_cutout(body, {"x": 2, "y": 3, "width": 4, "height": 2})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["width"] == 4 and payload["height"] == 2
    assert payload["sha256"] == hashlib.sha256(body).hexdigest()
    assert payload["pixels"] == [
        [None, "-6.7", "-6.6", "-6.5"],
        ["-5.8", "-5.7", "-5.6", "-5.5"],
    ]


def test_cutout_success_32bit_full_window():
    body = build_fits(3, 2, 32, pixels=[-1, 0, 1, 2, 3, 4])
    resp = post_cutout(body, {"x": 0, "y": 0, "width": 3, "height": 2})
    assert resp.status_code == 200
    assert resp.json()["pixels"] == [["-1", "0", "1"], ["2", "3", "4"]]


def test_wrong_content_type():
    resp = post_cutout(build_fits(1, 1, 16), {"x": 0, "y": 0, "width": 1, "height": 1},
                       content_type="application/octet-stream")
    assert resp.status_code == 415
    assert "detail" in resp.json()


def test_body_too_large():
    resp = post_cutout(b"\0" * (16 * 1024 * 1024 + 1),
                       {"x": 0, "y": 0, "width": 1, "height": 1})
    assert resp.status_code == 413


def test_corrupt_fits():
    resp = post_cutout(b"not a fits file".ljust(2880, b" "),
                       {"x": 0, "y": 0, "width": 1, "height": 1})
    assert resp.status_code == 400
    assert "detail" in resp.json()


def test_window_out_of_bounds():
    resp = post_cutout(build_fits(10, 10, 16),
                       {"x": 8, "y": 0, "width": 4, "height": 1})
    assert resp.status_code == 400


def test_window_too_many_pixels():
    resp = post_cutout(build_fits(200, 200, 16),
                       {"x": 0, "y": 0, "width": 101, "height": 100})
    assert resp.status_code == 400


def test_negative_origin():
    resp = post_cutout(build_fits(10, 10, 16),
                       {"x": -1, "y": 0, "width": 1, "height": 1})
    assert resp.status_code == 400


def test_non_integer_param():
    resp = post_cutout(build_fits(10, 10, 16),
                       {"x": "a", "y": 0, "width": 1, "height": 1})
    assert resp.status_code == 422


def test_missing_param():
    resp = client.post("/api/fits/cutout", content=build_fits(1, 1, 16),
                       headers={"Content-Type": "application/fits"})
    assert resp.status_code == 422
