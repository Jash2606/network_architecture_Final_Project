"""The one TCP connection bcurl is allowed to open.

There is deliberately no reconnect logic anywhere in the client: if this
connection dies, the run fails. "Never open a second connection" is enforced
by there being no code that could.
"""
from __future__ import annotations

import socket
from typing import Optional

from ..models.bhttp import Frame
from ..views.hexdump_view import FrameTracer
from .bhttp_codec import FrameReader, frame_from_bytes


class BHttpClient:
    def __init__(self, host: str, port: int, timeout: float = 30.0,
                 tracer: Optional[FrameTracer] = None) -> None:
        self.timeout = timeout
        self.tracer = tracer
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.reader = FrameReader(self.sock)
        if tracer:
            peer = self.sock.getpeername()
            tracer.info(f"connected to {peer[0]} port {peer[1]} (the only connection this run opens)")

    def send(self, wire: bytes) -> None:
        if self.tracer:
            self.tracer.frame(">", frame_from_bytes(wire))
        self.sock.settimeout(self.timeout)
        self.sock.sendall(wire)

    def receive(self) -> Optional[Frame]:
        frame = self.reader.read_frame(idle_timeout=self.timeout, frame_timeout=self.timeout)
        if frame is not None and self.tracer:
            self.tracer.frame("<", frame)
        return frame

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
