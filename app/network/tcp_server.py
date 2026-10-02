"""A threaded TCP accept loop: one thread per connection, and a cap on how many.

This is the whole "framework" the brief says not to import, written out.
bserve (network/bhttp_server.py) runs on top of it.
"""
from __future__ import annotations

import itertools
import logging
import socket
import threading
import time
from typing import Callable, Optional, Tuple

log = logging.getLogger(__name__)

# handler(sock, peer, connection_id) -- owns the socket until it returns
Handler = Callable[[socket.socket, Tuple, int], None]


def listen(host: str, port: int, backlog: int = 128) -> socket.socket:
    """Bind a listening socket.

    host "" listens on IPv6 *and* IPv4 when the OS allows it. This matters on
    Windows, where "localhost" resolves to ::1 first: an IPv4-only server makes
    every client pay for a refused ::1 attempt before it reaches 127.0.0.1.
    """
    if host in ("", "::") and socket.has_dualstack_ipv6():
        return socket.create_server(("", port), family=socket.AF_INET6,
                                    dualstack_ipv6=True, backlog=backlog)
    return socket.create_server((host or "0.0.0.0", port), backlog=backlog)


class TcpServer:
    def __init__(
        self,
        host: str,
        port: int,
        handler: Handler,
        max_connections: int = 128,
        on_busy: Optional[Callable[[socket.socket], None]] = None,
        name: str = "server",
    ) -> None:
        self.handler = handler
        self.on_busy = on_busy
        self.name = name
        self._slots = threading.BoundedSemaphore(max_connections)
        self._ids = itertools.count(1)
        self.accepted = 0                    # TCP handshakes completed, for logs and tests
        self._stopping = threading.Event()
        self._listener = listen(host, port)
        self._listener.settimeout(0.5)       # wake up now and then to notice shutdown()
        self.address = self._listener.getsockname()[:2]

    @property
    def port(self) -> int:
        return self.address[1]

    def serve_forever(self) -> None:
        log.info("%s listening on port %d", self.name, self.port)
        try:
            while not self._stopping.is_set():
                try:
                    sock, peer = self._listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    if self._stopping.is_set():
                        break
                    raise
                self._dispatch(sock, peer)
        finally:
            self._listener.close()

    def start_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name=f"{self.name}-accept", daemon=True)
        thread.start()
        return thread

    def shutdown(self) -> None:
        self._stopping.set()

    # ------------------------------------------------------------------
    def _dispatch(self, sock: socket.socket, peer: Tuple) -> None:
        self.accepted += 1                   # only the accept thread writes this
        sock.settimeout(None)                # the handler chooses its own timeouts
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if not self._slots.acquire(blocking=False):
            log.warning("connection limit reached, turning away %s", _peer_text(peer))
            try:
                if self.on_busy:
                    self.on_busy(sock)
            finally:
                sock.close()
            return
        connection_id = next(self._ids)
        log.info("conn #%d: TCP handshake from %s", connection_id, _peer_text(peer))
        threading.Thread(
            target=self._run, args=(sock, peer, connection_id),
            name=f"{self.name}-conn-{connection_id}", daemon=True,
        ).start()

    def _run(self, sock: socket.socket, peer: Tuple, connection_id: int) -> None:
        try:
            self.handler(sock, peer, connection_id)
        except Exception:  # one broken connection must never take the server down
            log.exception("conn #%d: handler crashed", connection_id)
        finally:
            sock.close()
            self._slots.release()


def _peer_text(peer: Tuple) -> str:
    host, port = peer[0], peer[1]
    if host.startswith("::ffff:"):           # IPv4 client on a dual-stack socket
        host = host[7:]
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def lingering_close(sock: socket.socket, linger: float = 2.0) -> None:
    """Close without destroying the last response.

    If we close() while the client's next (pipelined) request is still unread in
    our receive buffer, TCP answers with RST and the client may lose the response
    we just sent. So: send FIN first, then read and discard for a moment.
    """
    deadline = time.monotonic() + linger
    try:
        sock.shutdown(socket.SHUT_WR)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            if not sock.recv(64 * 1024):
                break                         # client saw our FIN and closed too
    except OSError:
        pass
