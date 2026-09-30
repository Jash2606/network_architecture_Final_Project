"""BHTTP/1 on the wire: the frame header, the payload layouts, and a reader that
takes exactly one frame off a socket -- never a byte of the next one.

    0        1        2        3        4        5        6        7
    +--------+--------+--------+--------+--------+--------+--------+--------+
    |  sync  |  type  | flags  |    stream id    |          length          |
    |  0xBF  |   8    |   8    |       16        |            24            |
    +--------+--------+--------+--------+--------+--------+--------+--------+

All integers are big-endian. The whole header is one 64-bit word.
"""
from __future__ import annotations

import socket
import struct
import time
from typing import Iterable, Optional, Tuple

from ..models.bhttp import (
    HEADER_SIZE, LITERAL_NAME, MAX_CONTROL_PAYLOAD, MAX_PAYLOAD, MAX_STREAM_ID,
    STATIC_INDEX, STATIC_TABLE, SYNC, Frame, FrameType, HeaderList,
)

_WORD = struct.Struct("!Q")
_U16 = struct.Struct("!H")
_KNOWN_TYPES = {t.value for t in FrameType}
_TOKEN_CHARS = set("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyz")


class FramingError(Exception):
    """The byte stream can no longer be trusted: nobody knows where the next
    frame starts. The only way on is GOAWAY and close."""

    status = 400


class FrameTimeout(FramingError):
    """A frame started but did not finish in time."""

    status = 408


class MalformedFrame(ValueError):
    """One frame's payload is wrong. Its length was fine, so the frame has been
    consumed completely; answer 400 and carry on with the next frame."""


class PeerIdle(Exception):
    """No frame started within the idle timeout."""


# --------------------------------------------------------------------- header
def pack_header(frame_type: int, flags: int, stream_id: int, length: int) -> bytes:
    if not 0 <= frame_type <= 0xFF or not 0 <= flags <= 0xFF:
        raise ValueError("type and flags are one octet each")
    if not 0 <= stream_id <= MAX_STREAM_ID:
        raise ValueError(f"stream id {stream_id} does not fit in 16 bits")
    if not 0 <= length <= MAX_PAYLOAD:
        raise ValueError(f"payload of {length} bytes does not fit in 24 bits")
    return _WORD.pack(SYNC << 56 | frame_type << 48 | flags << 40 | stream_id << 24 | length)


def unpack_header(header: bytes) -> Tuple[int, int, int, int]:
    """-> (type, flags, stream_id, length). Raises FramingError on a bad sync octet."""
    (word,) = _WORD.unpack(header)
    sync = word >> 56
    if sync != SYNC:
        raise FramingError(f"bad sync octet 0x{sync:02X} (expected 0x{SYNC:02X}): not a BHTTP/1 frame")
    return (word >> 48) & 0xFF, (word >> 40) & 0xFF, (word >> 24) & 0xFFFF, word & 0xFF_FFFF


def encode_frame(frame_type: int, flags: int, stream_id: int, payload: bytes = b"") -> bytes:
    return pack_header(frame_type, flags, stream_id, len(payload)) + payload


def frame_from_bytes(data: bytes) -> Frame:
    """Split one encoded frame back into a Frame (for tracing what we send)."""
    frame_type, flags, stream_id, length = unpack_header(data[:HEADER_SIZE])
    return Frame(frame_type, flags, stream_id, bytes(data[HEADER_SIZE:HEADER_SIZE + length]))


# ------------------------------------------------------------- payload pieces
def encode_string(data: bytes) -> bytes:
    if len(data) > 0xFFFF:
        raise ValueError("strings are at most 65535 octets")
    return _U16.pack(len(data)) + data


class PayloadReader:
    """A cursor over one payload. Running off the end is MalformedFrame, not a
    crash, and can never read into the next frame: the payload is all we have."""

    def __init__(self, payload: bytes) -> None:
        self.data = payload
        self.pos = 0

    @property
    def at_end(self) -> bool:
        return self.pos >= len(self.data)

    def take(self, count: int) -> bytes:
        if self.pos + count > len(self.data):
            raise MalformedFrame(f"payload ends early: wanted {count} octets at offset "
                                 f"{self.pos}, only {len(self.data) - self.pos} left")
        chunk = self.data[self.pos:self.pos + count]
        self.pos += count
        return chunk

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return _U16.unpack(self.take(2))[0]

    def string(self) -> bytes:
        return self.take(self.u16())

    def rest(self) -> bytes:
        return self.take(len(self.data) - self.pos)


def _text(raw: bytes, what: str) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MalformedFrame(f"{what} is not valid UTF-8") from None
    if any(ch in text for ch in "\x00\r\n"):
        raise MalformedFrame(f"{what} contains NUL, CR or LF")
    return text


# --------------------------------------------------------------- header block
def encode_header_block(headers: Iterable[Tuple[str, str]]) -> bytes:
    """Mechanism 1: a name in the static table is one octet, its index.
    Mechanism 2: any other name is 0x00 + a length-prefixed literal.
    Either way the value follows as a length-prefixed string."""
    out = bytearray()
    for name, value in headers:
        name = name.lower()
        index = STATIC_INDEX.get(name)
        if index:
            out.append(index)
        else:
            out.append(LITERAL_NAME)
            out += encode_string(name.encode("ascii"))
        out += encode_string(value.encode("utf-8"))
    return bytes(out)


def decode_header_block(reader: PayloadReader) -> HeaderList:
    headers: HeaderList = []
    while not reader.at_end:
        index = reader.u8()
        if index == LITERAL_NAME:
            name = _text(reader.string(), "header name")
            if not name or not set(name) <= _TOKEN_CHARS:
                raise MalformedFrame(f"literal header name {name!r} is not a lower-case token")
        elif index <= len(STATIC_TABLE):
            name = STATIC_TABLE[index - 1]
        else:
            reader.string()          # unassigned index: skip the field (room for v2's table)
            continue
        headers.append((name, _text(reader.string(), f"value of {name}")))
    return headers


# ------------------------------------------------------------------ payloads
def encode_request(method: int, path: str, headers: Iterable[Tuple[str, str]]) -> bytes:
    return bytes([method]) + encode_string(path.encode("utf-8")) + encode_header_block(headers)


def decode_request(payload: bytes) -> Tuple[int, str, HeaderList]:
    reader = PayloadReader(payload)
    method = reader.u8()
    path = _text(reader.string(), "path")
    if not path.startswith("/"):
        raise MalformedFrame("path must start with '/'")
    return method, path, decode_header_block(reader)


def encode_response(status: int, headers: Iterable[Tuple[str, str]]) -> bytes:
    return _U16.pack(status) + encode_header_block(headers)


def decode_response(payload: bytes) -> Tuple[int, HeaderList]:
    reader = PayloadReader(payload)
    status = reader.u16()
    if not 100 <= status <= 599:
        raise MalformedFrame(f"status {status} is not in 100..599")
    return status, decode_header_block(reader)


def encode_goaway(last_stream: int, code: int, reason: str = "") -> bytes:
    return _U16.pack(last_stream) + _U16.pack(code) + reason.encode("utf-8")


def decode_goaway(payload: bytes) -> Tuple[int, int, str]:
    reader = PayloadReader(payload)
    last_stream, code = reader.u16(), reader.u16()
    return last_stream, code, reader.rest().decode("utf-8", "replace")


# -------------------------------------------------------------------- reader
class FrameReader:
    """Reads whole frames from a socket.

    It asks the socket for exactly the octets it still needs -- 8 for a header,
    then `length` for the payload -- so the next frame's bytes are never taken.
    Unknown frame types, and control frames too big to buffer, are skipped
    cleanly: their payload is read and thrown away, and the frame comes back
    with skipped=True.
    """

    def __init__(self, sock: socket.socket, max_control_payload: int = MAX_CONTROL_PAYLOAD) -> None:
        self.sock = sock
        self.max_control_payload = max_control_payload

    def read_frame(self, idle_timeout: Optional[float] = None,
                   frame_timeout: Optional[float] = None) -> Optional[Frame]:
        """Next frame, or None if the peer closed cleanly between frames."""
        self.sock.settimeout(idle_timeout)
        try:
            first = self.sock.recv(1)
        except socket.timeout:
            raise PeerIdle() from None
        if not first:
            return None
        deadline = time.monotonic() + frame_timeout if frame_timeout else None
        frame_type, flags, stream_id, length = unpack_header(first + self._exactly(HEADER_SIZE - 1, deadline))
        too_big = frame_type != FrameType.DATA and length > self.max_control_payload
        if frame_type not in _KNOWN_TYPES or too_big:
            self._exactly(length, deadline, keep=False)
            return Frame(frame_type, flags, stream_id, b"", length=length, skipped=True)
        return Frame(frame_type, flags, stream_id, self._exactly(length, deadline))

    def _exactly(self, count: int, deadline: Optional[float], keep: bool = True) -> bytes:
        parts = []
        while count:
            if deadline is None:
                self.sock.settimeout(None)
            else:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise FrameTimeout("frame did not arrive in time")
                self.sock.settimeout(left)
            try:
                chunk = self.sock.recv(min(count, 64 * 1024))
            except socket.timeout:
                raise FrameTimeout("frame did not arrive in time") from None
            if not chunk:
                raise FramingError(f"connection closed {count} octets before the end of a frame")
            if keep:
                parts.append(chunk)
            count -= len(chunk)
        return b"".join(parts)
