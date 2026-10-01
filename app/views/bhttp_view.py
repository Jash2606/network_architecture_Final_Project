"""Messages -> BHTTP/1 frames: the server's responses and bcurl's requests.

A response is one RESPONSE frame (status + headers) and then DATA frames; the
last frame of the response carries END_STREAM. That flag, not the connection
closing, is where a response ends.
"""
from __future__ import annotations

import os
import random
from email.utils import formatdate
from typing import Iterable, Iterator, Tuple

from ..models.bhttp import (
    END_STREAM, GREASE_TYPES, BRequest, BResponse, FrameType, reason_phrase,
)
from ..network.bhttp_codec import encode_frame, encode_goaway, encode_request, encode_response

SERVER_NAME = "bserve/1.0"


class BHttpView:
    def __init__(self, server_name: str = SERVER_NAME, data_frame_size: int = 16 * 1024,
                 grease: bool = False) -> None:
        self.server_name = server_name
        self.data_frame_size = data_frame_size
        self.grease = grease

    # ---------------------------------------------------------- server side
    def error(self, status: int, message: str,
              headers: Iterable[Tuple[str, str]] = ()) -> BResponse:
        body = f"{status} {reason_phrase(status)}: {message}\n".encode("utf-8")
        fields = [("content-type", "text/plain; charset=utf-8"),
                  ("content-length", str(len(body))), *headers]
        return BResponse(status, fields, [body])

    def response_frames(self, response: BResponse, stream_id: int) -> Iterator[bytes]:
        headers = [("server", self.server_name), ("date", formatdate(usegmt=True)),
                   *response.headers]
        if self.grease:
            yield self.grease_frame()
        chunks = (c for c in response.body if c) if response.send_body else iter(())
        pending = next(chunks, None)
        if pending is None:  # nothing follows: the RESPONSE frame itself ends the stream
            yield encode_frame(FrameType.RESPONSE, END_STREAM, stream_id,
                               encode_response(response.status, headers))
            return
        yield encode_frame(FrameType.RESPONSE, 0, stream_id, encode_response(response.status, headers))
        for chunk in chunks:
            yield from self._data(pending, stream_id, last=False)
            pending = chunk
        yield from self._data(pending, stream_id, last=True)

    def _data(self, chunk: bytes, stream_id: int, last: bool) -> Iterator[bytes]:
        size = self.data_frame_size
        for start in range(0, len(chunk), size):
            final = last and start + size >= len(chunk)
            yield encode_frame(FrameType.DATA, END_STREAM if final else 0, stream_id,
                               chunk[start:start + size])

    # ---------------------------------------------------------- client side
    def request_frame(self, request: BRequest) -> bytes:
        payload = encode_request(request.method, request.path, request.headers)
        return encode_frame(FrameType.REQUEST, END_STREAM, request.stream_id, payload)

    # ---------------------------------------------------------- either side
    @staticmethod
    def goaway_frame(last_stream: int, code: int, reason: str) -> bytes:
        return encode_frame(FrameType.GOAWAY, 0, 0, encode_goaway(last_stream, code, reason))

    @staticmethod
    def grease_frame() -> bytes:
        """A frame of a type nobody will ever assign. A peer that follows the
        spec skips it; a peer that does not is caught today, not in version 2."""
        frame_type = random.choice(GREASE_TYPES)
        return encode_frame(frame_type, random.randrange(256), 0, os.urandom(random.randrange(1, 24)))
