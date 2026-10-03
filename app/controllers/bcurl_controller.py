"""What bcurl does: build request frames, send them down one connection, read
the responses, body to stdout, and exit non-zero on 4xx / 5xx.

Exit codes
    0  every response was 1xx-3xx        4  at least one 4xx
    2  bad command line                  5  at least one 5xx
    3  protocol error (bad frames, early close, GOAWAY)
    7  could not connect
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import BinaryIO, List, Optional, Sequence, TextIO, Tuple

from ..models.bhttp import BRequest, FrameType, Method, header_value, reason_phrase
from ..network.bhttp_client import BHttpClient
from ..network.bhttp_codec import FramingError, MalformedFrame, PeerIdle, decode_goaway, decode_response
from ..views.bhttp_view import BHttpView
from ..views.hexdump_view import FrameTracer

OK, USAGE, PROTOCOL, CLIENT_ERROR, SERVER_ERROR, CONNECT = 0, 2, 3, 4, 5, 7
DEFAULT_PORT = 9000
USER_AGENT = "bcurl/1.0"

_TARGET = re.compile(r"(?:bhttp://)?(\[[^\]]+\]|[^:/\[\]]+)(?::(\d{1,5}))?(/.*)?\Z")


class ProtocolError(Exception):
    pass


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    path: str

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


def parse_target(text: str) -> Target:
    """'localhost:9000/index.html', 'bhttp://[::1]:9000/', 'example.org/a.txt'"""
    match = _TARGET.match(text)
    if not match:
        raise ValueError(f"cannot understand URL {text!r}")
    host, port, path = match.groups()
    port_number = int(port) if port else DEFAULT_PORT
    if not 0 < port_number < 65536:
        raise ValueError(f"port {port_number} is out of range")
    return Target(host.strip("[]"), port_number, path or "/")


class BcurlController:
    def __init__(self, view: Optional[BHttpView] = None, out: Optional[BinaryIO] = None,
                 err: Optional[TextIO] = None, tracer: Optional[FrameTracer] = None) -> None:
        self.view = view or BHttpView()
        self.out = out or sys.stdout.buffer
        self.err = err or sys.stderr
        self.tracer = tracer

    def run(self, urls: Sequence[str], method: int = Method.GET,
            extra_headers: Sequence[Tuple[str, str]] = (), pipeline: bool = False,
            grease: bool = False, timeout: float = 30.0) -> int:
        try:
            targets = [parse_target(url) for url in urls]
        except ValueError as exc:
            return self._fail(USAGE, str(exc))
        if len({(t.host, t.port) for t in targets}) > 1:
            return self._fail(USAGE, "all URLs must share one host:port -- bcurl never opens a second connection")

        first = targets[0]
        requests = [BRequest(method, t.path, self._headers(t, extra_headers), stream_id=n)
                    for n, t in enumerate(targets, start=1)]
        try:
            client = BHttpClient(first.host, first.port, timeout, self.tracer)
        except OSError as exc:
            return self._fail(CONNECT, f"cannot connect to {first.authority}: {exc}")

        statuses: List[int] = []
        try:
            if pipeline:                        # every request first, answers come back in order
                for request in requests:
                    self._send(client, request, grease)
            for request in requests:
                if not pipeline:
                    self._send(client, request, grease)
                statuses.append(self._read_response(client, request))
        except (ProtocolError, FramingError, MalformedFrame) as exc:
            return self._fail(PROTOCOL, f"protocol error: {exc}")
        except PeerIdle:
            return self._fail(PROTOCOL, f"no response within {timeout:g}s")
        except OSError as exc:
            return self._fail(PROTOCOL, f"connection failed: {exc}")
        finally:
            client.close()
        self.out.flush()
        if self.tracer:
            self.tracer.info(f"1 connection, {len(requests)} request(s), {len(statuses)} response(s)")
        if any(s >= 500 for s in statuses):
            return SERVER_ERROR
        return CLIENT_ERROR if any(s >= 400 for s in statuses) else OK

    # ------------------------------------------------------------------
    @staticmethod
    def _headers(target: Target, extra: Sequence[Tuple[str, str]]):
        headers = [("host", target.authority), ("user-agent", USER_AGENT), ("accept", "*/*")]
        overridden = {name.lower() for name, _ in extra}
        return [(n, v) for n, v in headers if n not in overridden] + [(n.lower(), v) for n, v in extra]

    def _send(self, client: BHttpClient, request: BRequest, grease: bool) -> None:
        if grease:
            client.send(self.view.grease_frame())
        client.send(self.view.request_frame(request))

    def _read_response(self, client: BHttpClient, request: BRequest) -> int:
        status: Optional[int] = None
        expected: Optional[int] = None
        received = 0
        while True:
            frame = client.receive()
            if frame is None:
                raise ProtocolError("server closed the connection before the response ended")
            if frame.skipped:                   # unknown type: the rule says skip it, so we do
                continue
            if frame.type == FrameType.GOAWAY:
                _, code, reason = decode_goaway(frame.payload)
                raise ProtocolError(f"server sent GOAWAY {code}: {reason}")
            if frame.stream_id != request.stream_id:
                raise ProtocolError(f"expected stream {request.stream_id}, got {frame.stream_id}")
            if frame.type == FrameType.RESPONSE:
                if status is not None:
                    raise ProtocolError("two RESPONSE frames for one request")
                status, headers = decode_response(frame.payload)
                length = header_value(headers, "content-length")
                expected = int(length) if length and length.isdigit() else None
                if self.tracer:
                    self.tracer.info(f"stream {request.stream_id}: {status} {reason_phrase(status)}")
            elif frame.type == FrameType.DATA:
                if status is None:
                    raise ProtocolError("DATA before RESPONSE")
                self.out.write(frame.payload)
                received += len(frame.payload)
            else:
                raise ProtocolError(f"unexpected {frame.type_name} frame from a server")
            if frame.end_stream:
                break
        if request.method != Method.HEAD and expected is not None and received != expected:
            raise ProtocolError(f"content-length said {expected} octets, DATA carried {received}")
        return status

    def _fail(self, code: int, message: str) -> int:
        print(f"bcurl: {message}", file=self.err)
        return code
