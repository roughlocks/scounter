"""Tor circuit rotation (NEWNYM) — re-added per explicit request.

Rotates the exit IP every N items by signalling NEWNYM on Tor's control port
and closing the engine session's keepalive pools so the next request opens a
fresh socket through the new circuit. Cookies/proxies are preserved by
``Session.close()``.

Rotation is driven from the collector's main-thread loops (enumerate/stories/
highlights) via ``Source.maybe_rotate()`` — never from the parallel download
workers, so there's no race with in-flight requests. The download phase is CDN
(not identity-bound, not rate-limited) and doesn't rotate.

Requires: ``pip install scounter[tor]`` (stem + PySocks) and a running Tor
daemon or Tor Browser with the control port reachable
(9051 daemon / 9151 Tor Browser).
"""

from __future__ import annotations

import logging
import time
from typing import Any

DEFAULT_SOCKS_PORT = 9050
DEFAULT_CONTROL_PORT = 9051
NEWNYM_COOLDOWN = 11  # Tor enforces ~10 s between NEWNYM signals
_CIRCUIT_SETTLE = 3  # seconds for a new circuit to become usable

_last_rotation: float = 0.0


def rotate_circuit(
    control_port: int = DEFAULT_CONTROL_PORT,
    password: str | None = None,
    logger: logging.Logger | None = None,
) -> bool:
    """Signal Tor to build a new circuit (new exit IP). Returns True on success."""
    global _last_rotation

    try:
        from stem import Signal
        from stem.control import Controller
    except ImportError:
        if logger:
            logger.error("Tor rotation requires 'stem' — run: pip install 'scounter[tor]'")
        return False

    elapsed = time.time() - _last_rotation
    if elapsed < NEWNYM_COOLDOWN:
        wait = NEWNYM_COOLDOWN - elapsed
        if logger:
            logger.debug("Waiting %.0fs for Tor NEWNYM cooldown", wait)
        time.sleep(wait)

    try:
        with Controller.from_port(port=control_port) as controller:
            if password:
                controller.authenticate(password)
            else:
                controller.authenticate()  # cookie auth (Tor Browser / default daemon)
            controller.signal(Signal.NEWNYM)
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("Tor circuit rotation failed: %s", exc)
        return False

    _last_rotation = time.time()
    time.sleep(_CIRCUIT_SETTLE)
    if logger:
        logger.debug("Tor circuit rotated (NEWNYM)")
    return True


def reset_connections(client: Any) -> None:
    """Close keepalive pools so the next request uses the new Tor exit.

    ``requests.Session.close()`` clears pooled connections but preserves
    cookies, headers, proxy settings, and mounted adapters.
    """
    for attr in ("private", "public"):
        sess = getattr(client, attr, None)
        if sess is not None and hasattr(sess, "close"):
            try:
                sess.close()
            except Exception:  # noqa: BLE001
                pass
