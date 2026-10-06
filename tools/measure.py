#!/usr/bin/env python3
"""Numbers that back up the design: bytes on the wire, and what new connections cost.

    python tools/measure.py                  # starts its own bserve on a free port
    python tools/measure.py --requests 500

"Size" writes the same messages as BHTTP/1 frames and as HTTP/1.1 text.
"Speed" sends the same requests three ways and times them.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from email.utils import formatdate
from typing import List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.config import BServeConfig                          # noqa: E402
from app.controllers.bcurl_controller import USER_AGENT      # noqa: E402
from app.controllers.file_controller import FileController   # noqa: E402
from app.models.bhttp import BRequest, Method                # noqa: E402
from app.models.file_store import FileStore                  # noqa: E402
from app.network.bhttp_server import BServe                  # noqa: E402
from app.views.bhttp_view import SERVER_NAME, BHttpView      # noqa: E402
from tools.check_server import Connection, request           # noqa: E402

WWW = os.path.join(ROOT, "www")
HEADER_TEXT_NAMES = {"etag": "ETag"}   # the one name HTTP/1.1 does not simply title-case


def _title(name: str) -> str:
    return HEADER_TEXT_NAMES.get(name, "-".join(part.capitalize() for part in name.split("-")))


def size_rows() -> List[Tuple[str, int, int]]:
    """(what, BHTTP/1 bytes, HTTP/1.1 bytes) for the same messages."""
    view = BHttpView()
    headers = [("host", "localhost:9000"), ("user-agent", USER_AGENT), ("accept", "*/*")]
    frame = view.request_frame(BRequest(Method.GET, "/index.html", headers, stream_id=1))
    text = "GET /index.html HTTP/1.1\r\n" + "".join(f"{_title(n)}: {v}\r\n" for n, v in headers) + "\r\n"
    rows = [("request: GET /index.html, 3 headers", len(frame), len(text))]

    response = FileController(FileStore(WWW), view).handle(BRequest(Method.GET, "/index.html", []))
    first_frame = next(iter(view.response_frames(response, 1)))
    frames = len(first_frame) + 8                       # + the DATA frame's header
    fields = [("server", SERVER_NAME), ("date", formatdate(usegmt=True)), *response.headers]
    text = "HTTP/1.1 200 OK\r\n" + "".join(f"{_title(n)}: {v}\r\n" for n, v in fields) + "\r\n"
    rows.append((f"response head: 200, {len(fields)} headers", frames, len(text)))

    # one header's cost around its value: index + length prefix, versus 'Content-Length: ' + CRLF
    rows.append(("one header, not counting its value", 1 + 2, len("Content-Length: \r\n")))
    return rows


def _one_at_a_time(host: str, port: int, count: int) -> None:
    with Connection(host, port) as c:
        for stream in range(1, count + 1):
            c.send(request("/hello.txt", stream))
            assert c.answer().status == 200


def _all_at_once(host: str, port: int, count: int) -> None:
    with Connection(host, port) as c:
        c.send(*(request("/hello.txt", stream) for stream in range(1, count + 1)))
        for _ in range(count):
            assert c.answer().status == 200


def _new_connection_each(host: str, port: int, count: int) -> None:
    for _ in range(count):
        with Connection(host, port) as c:
            c.send(request("/hello.txt"))
            assert c.answer().status == 200


def speed_rows(server: BServe, host: str, count: int, repeats: int = 3) -> List[Tuple[str, float, int]]:
    """(how, best seconds, TCP handshakes) for `count` requests."""
    ways = [
        ("one connection, one request at a time", _one_at_a_time),
        ("one connection, all requests at once", _all_at_once),
        ("a new connection for every request", _new_connection_each),
    ]
    rows = []
    for label, run in ways:
        best, handshakes = float("inf"), 0
        for _ in range(repeats):
            before = server.tcp.accepted
            start = time.perf_counter()
            run(host, server.port, count)
            best = min(best, time.perf_counter() - start)
            handshakes = server.tcp.accepted - before
        rows.append((label, best, handshakes))
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Measure BHTTP/1 sizes and connection costs.")
    parser.add_argument("--requests", type=int, default=100)
    args = parser.parse_args(argv)

    print("Size: the same messages as BHTTP/1 frames and as HTTP/1.1 text\n")
    print(f"  {'message':<40} {'BHTTP/1':>8} {'HTTP/1.1':>9} {'smaller by':>11}")
    for label, frames, text in size_rows():
        print(f"  {label:<40} {frames:>6} B {text:>7} B {100 - 100 * frames // text:>10}%")
    print(f"  {'frame header':<40} {8:>6} B   (HTTP/2 uses 9 B)\n")

    config = BServeConfig(root=WWW, host="127.0.0.1", port=0)
    view = BHttpView(data_frame_size=config.data_frame_size)
    server = BServe(FileController(FileStore(WWW), view), config, view)
    server.start_in_thread()
    try:
        print(f"Speed: {args.requests} requests for /hello.txt on this computer (best of 3)\n")
        print(f"  {'how':<40} {'time':>8} {'TCP handshakes':>16}")
        for label, seconds, handshakes in speed_rows(server, "127.0.0.1", args.requests):
            print(f"  {label:<40} {seconds * 1000:>5.0f} ms {handshakes:>16}")
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
