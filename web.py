"""The parts of answering a request that neither game owns.

Reading a bearer token, working out the address a phone should open, and
replaying a write that lost a race are the same job whichever game is being
played, so they live here rather than in one game's routes.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Callable
from typing import Any

from fastapi import Request

from errors import GameError, StoreConflict

# Two instances can both write a room; the loser of the race replays its request.
WRITE_ATTEMPTS = 4

# Set by `server.main()` when the game is hosted from a laptop, so a join link
# printed for phones on the Wi-Fi carries the port that was actually used.
listen_port = 8765


def set_listen_port(port: int) -> None:
    global listen_port
    listen_port = port


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip()


def retry_on_conflict(work: Callable[[], Any]) -> Any:
    """Replay a request when another instance wrote the same room first."""
    for attempt in range(WRITE_ATTEMPTS):
        try:
            return work()
        except StoreConflict:
            if attempt == WRITE_ATTEMPTS - 1:
                break
    raise GameError("The table changed while you tapped. Try that again.", 409, "write_conflict")


def lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def public_origin(request: Request) -> str:
    """Origin phones should open. Localhost is rewritten to the LAN address."""
    configured = (
        os.getenv("PUBLIC_ORIGIN") or os.getenv("IMPOSTER_PUBLIC_ORIGIN") or ""
    ).strip().rstrip("/")
    if configured:
        return configured
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host_header = forwarded_host or request.headers.get("host") or f"127.0.0.1:{listen_port}"
    hostname, separator, port = host_header.partition(":")
    proto = (
        (request.headers.get("x-forwarded-proto") or request.url.scheme or "http")
        .split(",")[0]
        .strip()
    )
    if hostname in {"127.0.0.1", "localhost", "::1", "[::1]"}:
        hostname = lan_ip()
        port = port or str(listen_port)
        return f"http://{hostname}:{port}"
    if not separator:
        return f"{proto}://{host_header}"
    return f"{proto}://{host_header}"
