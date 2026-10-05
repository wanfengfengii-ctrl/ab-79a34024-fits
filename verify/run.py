"""One-shot verification entry point.

Waits for the API to become healthy, then runs the unit tests, a
byte-compile build check and the FITS API smoke checks.  The process exit
code is 0 only if every step passes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("API_BASE_URL", "http://api:8000").rstrip("/")


def wait_for_health(timeout: float = 120.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{API}/health", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1.0)
    return False


def run_step(name: str, argv: list[str]) -> bool:
    print(f"\n=== {name} ===", flush=True)
    proc = subprocess.run(argv, cwd=ROOT)
    ok = proc.returncode == 0
    print(f"--- {name}: {'PASS' if ok else 'FAIL'} (exit {proc.returncode})",
          flush=True)
    return ok


def main() -> int:
    print(f"waiting for API health at {API} ...", flush=True)
    if not wait_for_health():
        print("API did not become healthy in time", flush=True)
        return 1
    print("API is healthy", flush=True)

    results = [
        run_step("unit tests", [sys.executable, "-m", "pytest", "tests", "-q"]),
        run_step("build (byte-compile)",
                 [sys.executable, "-m", "compileall", "-q", "app", "verify", "tests"]),
        run_step("FITS API smoke", [sys.executable, "-m", "verify.smoke"]),
    ]
    ok = all(results)
    print(f"\nVERIFY RESULT: {'PASS' if ok else 'FAIL'}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
