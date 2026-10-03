"""BHTTP/1 on one TCP connection: read a frame, act on it, repeat.

Two kinds of "malformed", and they are not the same:
  * a frame whose *payload* is wrong (bad method, bad path, truncated header
    block) has still been consumed exactly -- its length was fine -- so we
    answer 400 on its stream and keep the connection open;
  * a frame whose *header* is wrong (bad sync octet) means we no longer know
    where any frame starts, so we send GOAWAY 400 and close.
Unknown frame types are skipped by FrameReader before we ever see them.
"""
from __future__ import annotations

import logging
import socket
from typing import Optional

from ..config import BServeConfig
from ..controllers.file_controller import FileController
from ..models.bhttp import BRequest, BResponse, FrameType, method_name
from ..views.bhttp_view import BHttpView
from ..views.hexdump_view import FrameTracer
from .bhttp_codec import (
    FrameReader, FramingError, MalformedFrame, PeerIdle, decode_request, frame_from_bytes,
)
from .tcp_server import TcpServer, lingering_close

log = logging.getLogger(__name__)

FLUSH_AT = 64 * 1024     # coalesce small frames into one send, like RESPONSE + first DATA


class BHttpConnection:
    def __init__(self, sock: socket.socket, connection_id: int, controller: FileController,
                 view: BHttpView, config: BServeConfig, tracer: Optional[FrameTracer] = None) -> None:
        self.sock = sock
        self.id = connection_id
        self.controller = controller
        self.view = view
        self.config = config
        self.tracer = tracer
        self.last_stream = 0
        self.served = 0

    def serve(self) -> None:
        reader = FrameReader(self.sock)
        while True:
            try:
                frame = reader.read_frame(self.config.idle_timeout, self.config.frame_timeout)
            except PeerIdle:
                self._goaway(408, f"idle for {self.config.idle_timeout:g}s")
                return self._close("idle timeout")
            except FramingError as exc:
                self._goaway(exc.status, str(exc))
                return self._close(f"framing broken: {exc}")
            except OSError as exc:
                return self._close(f"connection error: {exc}")
            if frame is None:
                return self._close("client closed the connection")
            if self.tracer:
                self.tracer.frame("<", frame, tag=f"#{self.id} ")

            if frame.type == FrameType.REQUEST:
                self.last_stream = frame.stream_id
                response = self._handle_request(frame)
                if not self._send(response, frame.stream_id):
                    return self._close("client went away mid-response")
            elif frame.type == FrameType.GOAWAY:
                return self._close("client sent GOAWAY")
            elif frame.skipped:
                log.info("conn #%d: skipped frame type 0x%02X (%d octets)",
                         self.id, frame.type, frame.length)
            # DATA or RESPONSE from a client carry nothing v1 needs: ignored.

    def _handle_request(self, frame) -> BResponse:
        if frame.skipped:                   # a REQUEST too big to buffer, already discarded
            return self.view.error(400, f"REQUEST frame of {frame.length} octets is too large")
        if frame.stream_id == 0:
            return self.view.error(400, "stream id 0 belongs to the connection, not a request")
        try:
            method, path, headers = decode_request(frame.payload)
        except MalformedFrame as exc:
            return self.view.error(400, f"malformed REQUEST frame: {exc}")
        request = BRequest(method, path, headers, frame.stream_id)
        try:
            response = self.controller.handle(request)
        except Exception:
            log.exception("conn #%d: controller error", self.id)
            response = self.view.error(500, "the server hit a bug")
        log.info("conn #%d: stream %d %s %s -> %d", self.id, frame.stream_id,
                 method_name(method), path, response.status)
        return response

    def _send(self, response: BResponse, stream_id: int) -> bool:
        pending = bytearray()
        try:
            self.sock.settimeout(self.config.idle_timeout)
            for wire in self.view.response_frames(response, stream_id):
                if self.tracer:
                    self.tracer.frame(">", frame_from_bytes(wire), tag=f"#{self.id} ")
                pending += wire
                if len(pending) >= FLUSH_AT:
                    self.sock.sendall(pending)
                    pending.clear()
            self.sock.sendall(pending)
        except OSError as exc:              # includes the file vanishing mid-read
            log.warning("conn #%d: response aborted: %s", self.id, exc)
            return False
        self.served += 1
        return True

    def _goaway(self, code: int, reason: str) -> None:
        wire = self.view.goaway_frame(self.last_stream, code, reason)
        if self.tracer:
            self.tracer.frame(">", frame_from_bytes(wire), tag=f"#{self.id} ")
        try:
            self.sock.settimeout(5)
            self.sock.sendall(wire)
        except OSError:
            pass

    def _close(self, reason: str) -> None:
        lingering_close(self.sock)
        log.info("conn #%d: closed after %d response(s): %s", self.id, self.served, reason)


class BServe:
    def __init__(self, controller: FileController, config: BServeConfig = BServeConfig(),
                 view: Optional[BHttpView] = None, tracer: Optional[FrameTracer] = None) -> None:
        self.controller = controller
        self.config = config
        self.view = view or BHttpView(data_frame_size=config.data_frame_size, grease=config.grease)
        self.tracer = tracer
        self.tcp = TcpServer(config.host, config.port, self._handle, config.max_connections,
                             on_busy=self._busy, name="bserve")

    @property
    def port(self) -> int:
        return self.tcp.port

    def serve_forever(self) -> None:
        self.tcp.serve_forever()

    def start_in_thread(self):
        return self.tcp.start_in_thread()

    def shutdown(self) -> None:
        self.tcp.shutdown()

    def _handle(self, sock: socket.socket, peer, connection_id: int) -> None:
        BHttpConnection(sock, connection_id, self.controller, self.view, self.config, self.tracer).serve()

    def _busy(self, sock: socket.socket) -> None:
        try:
            sock.sendall(self.view.goaway_frame(0, 503, "too many connections"))
        except OSError:
            pass
