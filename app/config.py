"""Every tunable number in one place, so each one can be defended in one place."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BServeConfig:
    """The BHTTP/1 file server."""

    root: str = "./www"
    host: str = ""                   # "" = every interface, IPv4 and IPv6
    port: int = 9000
    idle_timeout: float = 60.0       # silence allowed between frames
    frame_timeout: float = 30.0      # once a frame starts, it must finish within this
    max_connections: int = 128
    data_frame_size: int = 16 * 1024
    grease: bool = False             # send an unknown frame before every response
    verbose: bool = False            # hexdump every frame to stderr
