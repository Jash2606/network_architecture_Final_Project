"""BHTTP/1 as data: frame types, flags, methods, the static header table, and
the messages that travel inside frames.

The byte layout is in network/bhttp_codec.py. spec/BHTTP-1.md is the normative
description of both; if this file and the spec disagree, the spec wins.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable, List, Optional, Tuple

# ---- frame header: 8 octets = sync(8) type(8) flags(8) stream(16) length(24)
SYNC = 0xBF                      # first octet of every frame ("B"inary "F"rame)
HEADER_SIZE = 8
MAX_PAYLOAD = 0xFF_FFFF          # 24-bit length: 16 MiB - 1
MAX_STREAM_ID = 0xFFFF           # 16-bit stream id; 0 means "the connection"
MAX_CONTROL_PAYLOAD = 0xFFFF     # REQUEST/RESPONSE/GOAWAY bigger than this: refuse
GREASE_TYPES = range(0xF0, 0x100)  # never assigned; sent only to test the skip rule


class FrameType(IntEnum):
    REQUEST = 0x01
    RESPONSE = 0x02
    DATA = 0x03
    GOAWAY = 0x04


END_STREAM = 0x01                # flag: last frame of this request or response


class Method(IntEnum):
    GET = 0x01
    HEAD = 0x02
    POST = 0x03
    PUT = 0x04
    DELETE = 0x05


def method_name(code: int) -> str:
    try:
        return Method(code).name
    except ValueError:
        return f"METHOD-0x{code:02X}"


# ---- status codes are HTTP's own numbers, so nobody has to learn a new list
REASONS = {
    200: "OK",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    304: "Not Modified",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    408: "Request Timeout",
    413: "Content Too Large",
    500: "Internal Server Error",
    501: "Not Implemented",
    503: "Service Unavailable",
}


def reason_phrase(status: int) -> str:
    return REASONS.get(status, "Unknown")


# ---- the header table: the ten names our programs actually send. Index 0 means
# "literal name follows"; indices 11..255 are unassigned and MUST be skipped.
STATIC_TABLE: Tuple[str, ...] = (
    "host",            # 1   client
    "user-agent",      # 2   client
    "accept",          # 3   client
    "server",          # 4   server
    "date",            # 5   server
    "content-type",    # 6   server
    "content-length",  # 7   server
    "last-modified",   # 8   server
    "etag",            # 9   server
    "allow",           # 10  server, on 405
)
STATIC_INDEX = {name: index for index, name in enumerate(STATIC_TABLE, start=1)}
LITERAL_NAME = 0x00

HeaderList = List[Tuple[str, str]]


def header_value(headers: HeaderList, name: str) -> Optional[str]:
    for n, v in headers:
        if n == name:
            return v
    return None


@dataclass
class Frame:
    """One frame as it came off (or will go onto) the wire."""

    type: int
    flags: int
    stream_id: int
    payload: bytes = b""
    length: int = -1                 # payload length from the header
    skipped: bool = False            # payload was discarded unread (unknown type / too big)

    def __post_init__(self) -> None:
        if self.length < 0:
            self.length = len(self.payload)

    @property
    def end_stream(self) -> bool:
        return bool(self.flags & END_STREAM)

    @property
    def type_name(self) -> str:
        try:
            return FrameType(self.type).name
        except ValueError:
            return f"UNKNOWN-0x{self.type:02X}"


@dataclass
class BRequest:
    method: int
    path: str
    headers: HeaderList = field(default_factory=list)
    stream_id: int = 0


@dataclass
class BResponse:
    status: int
    headers: HeaderList = field(default_factory=list)
    body: Iterable[bytes] = ()       # chunks, produced lazily so files can stream
    send_body: bool = True           # False for HEAD: headers only
