#!/usr/bin/env python3
"""Check any BHTTP/1 server against the rules in spec/BHTTP-1.md.

    python tools/check_server.py localhost:9000
    python tools/check_server.py 192.168.1.20:9000 --path /index.html

Each check opens its own connection and sends raw frames, so this works against
anyone's server, not just bserve. That is the point: a client that only works
against its own server is an implementation, not a protocol.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import uuid
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models.bhttp import END_STREAM, FrameType, Method, header_value  # noqa: E402
from app.network.bhttp_codec import (  # noqa: E402
    FrameReader, FramingError, PeerIdle, decode_goaway, decode_response,
    encode_frame, encode_header_block, encode_request, encode_string,
)

TIMEOUT = 5.0


class CheckFailed(Exception):
    pass


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


@dataclass
class Answer:
    status: int = 0
    headers: list = field(default_factory=list)
    body: bytes = b""
    data_frames: int = 0
    stream_id: int = -1
    goaway: Optional[Tuple[int, int, str]] = None     # (last stream, code, reason)


class Connection:
    """One raw connection to the server under test."""

    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.create_connection((host, port), timeout=TIMEOUT)
        self.reader = FrameReader(self.sock)

    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, *exc) -> None:
        self.sock.close()

    def send(self, *frames: bytes) -> None:
        self.sock.sendall(b"".join(frames))

    def answer(self) -> Answer:
        """Read frames until one response is complete, or a GOAWAY arrives."""
        answer = Answer()
        while True:
            frame = self.reader.read_frame(TIMEOUT, TIMEOUT)
            if frame is None:
                raise CheckFailed("the server closed the connection")
            if frame.skipped:
                continue
            if frame.type == FrameType.GOAWAY:
                answer.goaway = decode_goaway(frame.payload)
                return answer
            if frame.type == FrameType.RESPONSE:
                answer.status, answer.headers = decode_response(frame.payload)
                answer.stream_id = frame.stream_id
            elif frame.type == FrameType.DATA:
                answer.body += frame.payload
                answer.data_frames += 1
            if frame.end_stream:
                return answer

    def closes(self) -> bool:
        """Does the server hang up within the timeout?"""
        try:
            while self.reader.read_frame(TIMEOUT, TIMEOUT) is not None:
                pass
            return True
        except PeerIdle:
            return False
        except (OSError, FramingError):
            return True


def request(path: str, stream: int = 1, method: int = Method.GET,
            flags: int = END_STREAM, headers=(("host", "check"),)) -> bytes:
    return encode_frame(FrameType.REQUEST, flags, stream, encode_request(method, path, headers))


@dataclass
class Target:
    host: str
    port: int
    path: str                       # a file that exists on the server
    missing: str                    # a file that surely does not

    def connect(self) -> Connection:
        return Connection(self.host, self.port)


# ------------------------------------------------------------------- checks
def check_get(t: Target) -> None:
    with t.connect() as c:
        c.send(request(t.path))
        a = c.answer()
        expect(a.status == 200, f"expected 200, got {a.status or a.goaway}")
        expect(a.stream_id == 1, f"answer came on stream {a.stream_id}, not 1")
        length = header_value(a.headers, "content-length")
        if length is not None:
            expect(int(length) == len(a.body), f"content-length {length}, body {len(a.body)} bytes")


def check_missing(t: Target) -> None:
    with t.connect() as c:
        c.send(request(t.missing))
        a = c.answer()
        expect(a.status == 404, f"expected 404, got {a.status or a.goaway}")


def check_keep_alive(t: Target) -> None:
    with t.connect() as c:
        for stream in (1, 2, 3):
            c.send(request(t.path, stream))
            a = c.answer()
            expect(a.status == 200, f"request {stream}: got {a.status or a.goaway}")
            expect(a.stream_id == stream, f"request {stream} answered on stream {a.stream_id}")


def check_pipelining(t: Target) -> None:
    with t.connect() as c:
        c.send(request(t.path, 1), request(t.missing, 2), request(t.path, 3))
        got = [(a.stream_id, a.status) for a in (c.answer(), c.answer(), c.answer())]
        expect(got == [(1, 200), (2, 404), (3, 200)], f"answers were {got}")


def check_unknown_type(t: Target) -> None:
    with t.connect() as c:
        c.send(encode_frame(0xF7, 0, 0, b"abc"), request(t.path))
        a = c.answer()
        expect(a.status == 200, f"after an unknown frame: got {a.status or a.goaway}")


def check_unknown_flags(t: Target) -> None:
    with t.connect() as c:
        c.send(request(t.path, flags=END_STREAM | 0x80))
        a = c.answer()
        expect(a.status == 200, f"with flag 0x80 set: got {a.status or a.goaway}")


def check_unknown_header(t: Target) -> None:
    payload = (bytes([Method.GET]) + encode_string(t.path.encode()) +
               bytes([0x63]) + encode_string(b"zz") + encode_header_block([("host", "check")]))
    with t.connect() as c:
        c.send(encode_frame(FrameType.REQUEST, END_STREAM, 1, payload))
        a = c.answer()
        expect(a.status == 200, f"with header #99: got {a.status or a.goaway}")


def check_literal_header(t: Target) -> None:
    with t.connect() as c:
        c.send(request(t.path, headers=[("host", "check"), ("x-checker", "1")]))
        a = c.answer()
        expect(a.status == 200, f"with a literal header: got {a.status or a.goaway}")


def check_malformed(t: Target) -> None:
    broken = encode_frame(FrameType.REQUEST, END_STREAM, 1, b"\x01\x00\x09/ind")  # path cut short
    with t.connect() as c:
        c.send(broken, request(t.path, 2))
        first, second = c.answer(), c.answer()
        expect(first.status == 400, f"malformed frame: got {first.status or first.goaway}")
        expect(second.status == 200, f"next request on the same connection: got {second.status or second.goaway}")


def check_stream_zero(t: Target) -> None:
    with t.connect() as c:
        c.send(request(t.path, stream=0))
        a = c.answer()
        expect(a.status == 400, f"request on stream 0: got {a.status or a.goaway}")


def check_head(t: Target) -> None:
    with t.connect() as c:
        c.send(request(t.path, method=Method.HEAD))
        a = c.answer()
        expect(a.status == 200, f"HEAD: got {a.status or a.goaway}")
        expect(a.data_frames == 0, f"HEAD answer carried {a.data_frames} DATA frame(s)")


def check_method(t: Target) -> None:
    with t.connect() as c:
        c.send(request(t.path, method=Method.POST))
        a = c.answer()
        expect(a.status == 405, f"POST: got {a.status or a.goaway}")
        expect(header_value(a.headers, "allow") is not None, "405 without an allow header")


def check_traversal(t: Target) -> None:
    with t.connect() as c:
        c.send(request("/../secret.txt"))
        a = c.answer()
        expect(a.status == 400, f"/../secret.txt: got {a.status or a.goaway}")


def check_bad_sync(t: Target) -> None:
    with t.connect() as c:
        c.send(b"GET / HTTP/1.1\r\nHost: check\r\n\r\n")
        a = c.answer()
        expect(a.goaway is not None, f"expected GOAWAY, got status {a.status}")
        expect(a.goaway[1] == 400, f"GOAWAY code {a.goaway[1]}, expected 400")
        expect(c.closes(), "the server kept the connection open after GOAWAY")


CHECKS: List[Tuple[str, str, Callable[[Target], None]]] = [
    ("GET an existing file gives 200", "4", check_get),
    ("A missing file gives 404", "4", check_missing),
    ("Three requests share one connection", "4", check_keep_alive),
    ("Requests sent all at once are answered in order", "4", check_pipelining),
    ("A frame of unknown type is skipped", "1", check_unknown_type),
    ("Unknown flag bits are ignored", "1", check_unknown_flags),
    ("An unknown header number is skipped", "3", check_unknown_header),
    ("A literal header name is accepted", "3", check_literal_header),
    ("A malformed frame gives 400, connection stays open", "4", check_malformed),
    ("A request on stream 0 gives 400", "4", check_stream_zero),
    ("HEAD gives headers and no body", "4", check_head),
    ("POST gives 405 with an allow header", "4", check_method),
    ("A path above the root gives 400", "4", check_traversal),
    ("Bytes that are not a frame give GOAWAY 400, then close", "1, 4", check_bad_sync),
]


@dataclass
class Result:
    name: str
    section: str
    passed: bool
    problem: str = ""


def run_checks(host: str, port: int, path: str = "/index.html") -> List[Result]:
    target = Target(host, port, path, f"/no-such-file-{uuid.uuid4().hex[:8]}.html")
    results = []
    for name, section, check in CHECKS:
        try:
            check(target)
            results.append(Result(name, section, True))
        except Exception as exc:        # a broken server must never crash the checker
            problem = str(exc) if isinstance(exc, CheckFailed) else f"{type(exc).__name__}: {exc}"
            results.append(Result(name, section, False, problem))
    return results


def print_results(results: List[Result], indent: str = "  ") -> None:
    for r in results:
        print(f"{indent}[{'PASS' if r.passed else 'FAIL'}] {r.name:<55} spec {r.section}")
        if not r.passed:
            print(f"{indent}       -> {r.problem}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Check a BHTTP/1 server against the spec.")
    parser.add_argument("server", help="host:port, for example localhost:9000")
    parser.add_argument("--path", default="/index.html", help="a file that exists on the server")
    args = parser.parse_args(argv)
    host, _, port = args.server.rpartition(":")
    if not host or not port.isdigit():
        parser.error("server must look like host:port")
    host = host.strip("[]")
    try:
        socket.create_connection((host, int(port)), timeout=TIMEOUT).close()
    except OSError as exc:
        print(f"cannot connect to {args.server}: {exc}")
        return 2

    print(f"Checking the BHTTP/1 server at {args.server}")
    print("Every check uses its own connection and only the rules in spec/BHTTP-1.md.\n")
    results = run_checks(host, int(port), args.path)
    print_results(results)
    passed = sum(r.passed for r in results)
    print(f"\n{passed} of {len(results)} checks passed.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
