"""What happens to a BHTTP/1 request at bserve: map the path to a file under
the root and answer with status, headers and the bytes.

    GET  /index.html      -> 200 + the file
    HEAD /index.html      -> 200, headers only
    GET  /missing.html    -> 404
    GET  /../etc/passwd   -> 400  (climbs out of the root: not a valid path)
    POST /index.html      -> 405
A frame too malformed to become a BRequest never gets here: the connection
(network/bhttp_server.py) answers those with 400 itself.
"""
from __future__ import annotations

from typing import Optional

from ..models.bhttp import BRequest, BResponse, Method, method_name
from ..models.file_store import BadPath, FileStore, NotFound
from ..views.bhttp_view import BHttpView


class FileController:
    ALLOWED = (Method.GET, Method.HEAD)

    def __init__(self, store: FileStore, view: Optional[BHttpView] = None,
                 chunk_size: int = 64 * 1024) -> None:
        self.store = store
        self.view = view or BHttpView()
        self.chunk_size = chunk_size

    def handle(self, request: BRequest) -> BResponse:
        if request.method not in self.ALLOWED:
            return self.view.error(405, f"{method_name(request.method)} is not allowed",
                                   headers=[("allow", "GET, HEAD")])
        try:
            resource = self.store.lookup(request.path)
        except BadPath as exc:
            return self.view.error(400, f"bad path {request.path!r}: {exc}")
        except NotFound:
            return self.view.error(404, f"{request.path} is not here")
        headers = [
            ("content-type", resource.content_type),
            ("content-length", str(resource.size)),
            ("last-modified", resource.last_modified),
            ("etag", resource.etag),
        ]
        return BResponse(200, headers, resource.chunks(self.chunk_size),
                         send_body=request.method != Method.HEAD)
