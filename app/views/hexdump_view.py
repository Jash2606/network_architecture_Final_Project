"""Frames -> something a person can read: hexdumps and field-by-field annotation.

"If you cannot annotate your own bytes, the spec is not finished." annotate()
walks a frame octet by octet and names every field, so the tool can.
"""
from __future__ import annotations

import struct
import threading
from typing import List, TextIO, Tuple

from ..models.bhttp import (
    END_STREAM, HEADER_SIZE, LITERAL_NAME, STATIC_TABLE, Frame, FrameType, method_name,
    reason_phrase,
)
from ..network.bhttp_codec import (
    MalformedFrame, PayloadReader, decode_goaway, decode_request, decode_response, pack_header,
)

Row = Tuple[int, bytes, str, str]   # offset, octets, field, meaning


def hexdump(data: bytes, prefix: str = "") -> str:
    """Classic 'hexdump -C' layout: offset, 16 octets in two groups, ASCII."""
    lines = []
    for offset in range(0, len(data), 16):
        row = data[offset:offset + 16]
        left = " ".join(f"{b:02x}" for b in row[:8])
        right = " ".join(f"{b:02x}" for b in row[8:])
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        lines.append(f"{prefix}{offset:08x}  {left:<23}  {right:<23}  |{text}|")
    return "\n".join(lines)


def frame_bytes(frame: Frame) -> bytes:
    return pack_header(frame.type, frame.flags, frame.stream_id, frame.length) + frame.payload


def flag_names(frame: Frame) -> str:
    names = ["END_STREAM"] if frame.flags & END_STREAM else []
    if frame.flags & ~END_STREAM & 0xFF:
        names.append(f"0x{frame.flags & ~END_STREAM & 0xFF:02x}")
    return "|".join(names) or "-"


def summary(frame: Frame) -> str:
    """One line: what this frame is and what it says."""
    what = ""
    try:
        if frame.skipped:
            what = "  (skipped: unknown type)" if frame.type_name.startswith("UNKNOWN") \
                else "  (skipped: too large)"
        elif frame.type == FrameType.REQUEST:
            method, path, _ = decode_request(frame.payload)
            what = f"  {method_name(method)} {path}"
        elif frame.type == FrameType.RESPONSE:
            status, headers = decode_response(frame.payload)
            what = f"  {status} {reason_phrase(status)}, {len(headers)} headers"
        elif frame.type == FrameType.GOAWAY:
            last, code, reason = decode_goaway(frame.payload)
            what = f"  last stream {last}, code {code}: {reason}"
    except MalformedFrame as exc:
        what = f"  (malformed: {exc})"
    return (f"{frame.type_name} stream={frame.stream_id} flags={flag_names(frame)} "
            f"length={frame.length}{what}")


# ------------------------------------------------------------------ annotate
def annotate(frame: Frame) -> List[Row]:
    header = pack_header(frame.type, frame.flags, frame.stream_id, frame.length)
    rows: List[Row] = [
        (0, header[0:1], "sync", "0xBF, every frame starts with it"),
        (1, header[1:2], "type", frame.type_name),
        (2, header[2:3], "flags", flag_names(frame)),
        (3, header[3:5], "stream id", str(frame.stream_id)),
        (5, header[5:8], "length", f"{frame.length} payload octets follow"),
    ]
    if frame.skipped:
        rows.append((HEADER_SIZE, b"", "payload", f"{frame.length} octets, read and discarded"))
        return rows
    reader = PayloadReader(frame.payload)

    def field(count: int, name: str, meaning) -> bytes:
        start = HEADER_SIZE + reader.pos
        raw = reader.take(count)
        rows.append((start, raw, name, meaning(raw) if callable(meaning) else meaning))
        return raw

    def u16(name: str, meaning=lambda raw: str(struct.unpack("!H", raw)[0])) -> int:
        return struct.unpack("!H", field(2, name, meaning))[0]

    def string(name: str) -> None:
        length = u16(f"{name} length")
        field(length, name, lambda raw: repr(raw.decode("utf-8", "replace")))

    try:
        if frame.type == FrameType.REQUEST:
            field(1, "method", lambda raw: method_name(raw[0]))
            string("path")
            _annotate_headers(reader, field, string)
        elif frame.type == FrameType.RESPONSE:
            u16("status", _status_text)
            _annotate_headers(reader, field, string)
        elif frame.type == FrameType.GOAWAY:
            u16("last stream")
            u16("code")
            field(len(frame.payload) - reader.pos, "reason",
                  lambda raw: repr(raw.decode("utf-8", "replace")))
        elif frame.payload:
            field(len(frame.payload), "body", f"{len(frame.payload)} octets of content")
    except MalformedFrame as exc:
        rows.append((HEADER_SIZE + reader.pos, frame.payload[reader.pos:], "MALFORMED", str(exc)))
    return rows


def _status_text(raw: bytes) -> str:
    status = struct.unpack("!H", raw)[0]
    return f"{status} {reason_phrase(status)}"


def _annotate_headers(reader: PayloadReader, field, string) -> None:
    while not reader.at_end:
        index = field(1, "header", lambda raw: (
            "literal name follows" if raw[0] == LITERAL_NAME else
            f"#{raw[0]} = {STATIC_TABLE[raw[0] - 1]}" if raw[0] <= len(STATIC_TABLE) else
            f"#{raw[0]} unassigned: skip"))[0]
        if index == LITERAL_NAME:
            string("  name")
        string("  value")


def render_annotation(frame: Frame, prefix: str = "", max_octets: int = 8) -> str:
    lines = []
    for offset, raw, name, meaning in annotate(frame):
        pieces = [(offset + i, raw[i:i + max_octets]) for i in range(0, len(raw), max_octets)]
        pieces = pieces or [(offset, b"")]
        if len(pieces) > 3:          # long strings and bodies: show the ends only
            pieces = pieces[:2] + [(-1, b"")] + pieces[-1:]
        for n, (at, chunk) in enumerate(pieces):
            if at < 0:
                lines.append(f"{prefix}      ...")
                continue
            octets = " ".join(f"{b:02x}" for b in chunk)
            label = f"{name:<14} {meaning}" if n == 0 else ""
            lines.append(f"{prefix}{at:04x}  {octets:<24} {label}".rstrip())
    return "\n".join(lines)


# -------------------------------------------------------------------- tracer
class FrameTracer:
    """Writes every frame to a text stream as it is sent ('>') or received ('<')."""

    def __init__(self, stream: TextIO, annotate: bool = False, limit: int = 0) -> None:
        self.stream = stream
        self.annotate = annotate
        self.limit = limit             # 0 = dump every octet
        self._lock = threading.Lock()  # bserve traces from many threads

    def info(self, text: str) -> None:
        with self._lock:
            print(f"* {text}", file=self.stream, flush=True)

    def frame(self, direction: str, frame: Frame, tag: str = "") -> None:
        prefix = f"{tag}{direction} "
        if self.annotate:
            body = render_annotation(frame, prefix)
        else:
            data = frame_bytes(frame)
            if self.limit and len(data) > self.limit:
                cut = len(data) - self.limit
                body = hexdump(data[:self.limit], prefix) + f"\n{prefix}... {cut} more octets"
            else:
                body = hexdump(data, prefix)
        with self._lock:
            print(f"{prefix}{summary(frame)}\n{body}", file=self.stream, flush=True)
