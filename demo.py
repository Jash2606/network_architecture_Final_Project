#!/usr/bin/env python3
"""See the whole project work with one command:

    python demo.py

It starts bserve on a free port, runs bcurl the way you would type it, checks
the server against every rule in the spec, and stops the server again.
Nothing has to be started first.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from typing import List, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from app.config import BServeConfig                          # noqa: E402
from app.controllers.file_controller import FileController   # noqa: E402
from app.models.file_store import FileStore                  # noqa: E402
from app.network.bhttp_server import BServe                  # noqa: E402
from app.views.bhttp_view import BHttpView                   # noqa: E402
from tools.check_server import print_results, run_checks     # noqa: E402

WWW = os.path.join(ROOT, "www")
FRAME_LINE = re.compile(r"^[<>] [A-Z]")        # summary lines of bcurl -v, not the hexdump rows


class LogCapture(logging.Handler):
    """Keeps what the server logs, so the demo can prove what the server did."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def bcurl(*args: str) -> Tuple[int, str, str]:
    run = subprocess.run([sys.executable, os.path.join(ROOT, "bcurl"), *args],
                         capture_output=True, timeout=60)
    return run.returncode, run.stdout.decode("utf-8", "replace"), run.stderr.decode("utf-8", "replace")


def main() -> int:
    capture = LogCapture()
    server_log = logging.getLogger("app.network.bhttp_server")
    server_log.setLevel(logging.INFO)
    server_log.addHandler(capture)
    server_log.propagate = False

    config = BServeConfig(root=WWW, host="127.0.0.1", port=0)
    view = BHttpView(data_frame_size=config.data_frame_size)
    server = BServe(FileController(FileStore(WWW), view), config, view)
    server.start_in_thread()
    base = f"127.0.0.1:{server.port}"
    print(f"Started bserve on port {server.port}, serving the www folder.\n")

    steps: List[Tuple[str, bool]] = []

    def step(title: str, command: str, passed: bool, *lines: str) -> None:
        number = len(steps) + 1
        print(f"{number}. {title}")
        print(f"   $ {command}")
        for line in lines:
            print(f"   {line}")
        print(f"   [{'PASS' if passed else 'FAIL'}]\n")
        steps.append((title, passed))

    try:
        # 1. a plain fetch
        with open(os.path.join(WWW, "index.html"), "rb") as f:
            page = f.read().decode("utf-8")
        code, out, _ = bcurl(f"{base}/index.html")
        step("Fetch a file", f"python bcurl {base}/index.html", code == 0 and out == page,
             f"received index.html ({len(out.encode())} bytes), exit code {code}")

        # 2. the frames behind it
        code, out, err = bcurl("-v", f"{base}/hello.txt")
        frames = [line for line in err.splitlines() if FRAME_LINE.match(line)]
        step("Look at the frames", f"python bcurl -v {base}/hello.txt",
             code == 0 and len(frames) == 3, *frames, f"body: {out.strip()}")

        # 3. a missing file must exit non-zero
        code, out, _ = bcurl(f"{base}/missing.html")
        step("Ask for a file that is not there", f"python bcurl {base}/missing.html", code == 4,
             out.strip(), f"exit code {code} (non-zero, as the brief asks)")

        # 4. several files, one connection
        before = server.tcp.accepted
        code, out, _ = bcurl(f"{base}/hello.txt", f"{base}/docs/")
        handshakes = server.tcp.accepted - before
        step("Fetch two files over one connection",
             f"python bcurl {base}/hello.txt {base}/docs/",
             code == 0 and handshakes == 1 and "Plain text" in out and "docs" in out,
             f"2 files received, the server saw {handshakes} TCP connection")

        # 5. frames nobody knows are skipped
        capture.lines.clear()
        code, out, _ = bcurl("--grease", f"{base}/hello.txt")
        skipped = [line for line in capture.lines if "skipped frame type" in line]
        step("Send a frame of unknown type first", f"python bcurl --grease {base}/hello.txt",
             code == 0 and bool(skipped),
             "server: " + (skipped[0].split(": ", 1)[1] if skipped else "(nothing skipped)"),
             f"and it still answered: {out.strip()}")

        # 6. every rule in the spec
        print("6. Check the server against every rule in the spec")
        print(f"   $ python tools/check_server.py {base}")
        results = run_checks("127.0.0.1", server.port)
        print_results(results, indent="   ")
        passed_checks = sum(r.passed for r in results)
        all_checks = passed_checks == len(results)
        print(f"   [{'PASS' if all_checks else 'FAIL'}] {passed_checks} of {len(results)} checks\n")
        steps.append(("Check the server against the spec", all_checks))
    finally:
        server.shutdown()

    passed = sum(ok for _, ok in steps)
    if passed == len(steps):
        print(f"Everything passed: {passed} of {len(steps)} steps. bserve stopped.")
        return 0
    print(f"{passed} of {len(steps)} steps passed. Failed: "
          + ", ".join(title for title, ok in steps if not ok))
    return 1


if __name__ == "__main__":
    sys.exit(main())
