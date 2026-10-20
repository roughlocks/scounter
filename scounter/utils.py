"""Utility helpers: logging, serialization, path safety, filenames, retry."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from scounter.pacing import RateLimitExhausted

T = TypeVar("T")

# Characters that are unsafe in filenames across operating systems.
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize(name: str, max_len: int = 80) -> str:
    """Make a string safe to use as a directory or file name."""
    name = _UNSAFE_CHARS.sub("_", name).strip().rstrip(".")
    return (name or "unknown")[:max_len]


def setup_logging(log_file: Path | None = None, verbose: bool = False) -> logging.Logger:
    """Configure root logger with console (+ optional file) output."""
    logger = logging.getLogger("scounter")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)-7s] %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def media_filename(shortcode: str | None, date_utc: Any, ext: str, index: int | None = None) -> str:
    """Deterministic, reversible filename: ``<shortcode>_<YYYYMMDD_HHMMSS>.<ext>``.

    The shortcode is embedded so the ``link`` command can recover the original
    Instagram URL (``instagram.com/p/<shortcode>/``) from the filename. Album
    items use ``~`` as the index delimiter (``<shortcode>~<idx>_<date>``) — ``~``
    is outside the base64url shortcode alphabet ``[A-Za-z0-9_-]``, so a shortcode
    ending in ``_<digits>`` is never mistaken for an album index.

    Accepts a datetime or an ISO-format string (as stored in the manifest).
    """
    code = sanitize(shortcode or "unknown", max_len=40)
    if index is not None:
        code = f"{code}~{index}"
    dt: datetime | None = None
    if isinstance(date_utc, datetime):
        dt = date_utc
    elif date_utc:
        try:
            dt = datetime.fromisoformat(str(date_utc))
        except ValueError:
            dt = None
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        stamp = dt.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S")
    else:
        stamp = "nodate"
    ext = ext.lstrip(".")
    return f"{code}_{stamp}.{ext}"


def shortcode_from_filename(filename: str) -> str | None:
    """Extract the leading shortcode token from a media filename written by scounter.

    Handles ``<shortcode>_<YYYYMMDD_HHMMSS>``, the album form
    ``<shortcode>~<idx>_<YYYYMMDD_HHMMSS>``, and leaves shortcodes containing
    underscores intact. This is a *fallback* for orphaned files — the manifest's
    ``local_path`` is the primary source of truth for the ``link`` command.
    """
    stem = Path(filename).stem
    m = re.match(r"^(.+?)(?:~\d+)?_\d{8}_\d{6}$", stem)
    if not m:
        return stem or None
    return m.group(1)


def to_jsonable(obj: Any) -> Any:
    """Recursively convert arbitrary objects (engine types, datetimes) into
    JSON-serializable primitives."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj.isoformat(timespec="seconds")
    if isinstance(obj, (set, frozenset)):
        return sorted(to_jsonable(x) for x in obj)
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, Path):
        return str(obj)
    # engine types are pydantic models.
    for dumper in ("model_dump", "dict"):
        fn = getattr(obj, dumper, None)
        if callable(fn):
            try:
                return to_jsonable(fn())
            except Exception:  # noqa: BLE001
                pass
    if hasattr(obj, "username") and isinstance(obj.username, str):
        return obj.username
    if hasattr(obj, "__dict__"):
        return to_jsonable(vars(obj))
    return str(obj)


def write_json(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_jsonable(data), indent=2, ensure_ascii=False), encoding="utf-8")


def retry(
    fn: Callable[..., T],
    args: tuple = (),
    kwargs: dict | None = None,
    attempts: int = 3,
    delay: float = 2.0,
    backoff: float = 2.0,
    logger: logging.Logger | None = None,
    label: str = "operation",
) -> T | None:
    """Run *fn* with retries on transient exceptions. Returns None if all fail."""
    kwargs = kwargs or {}
    current_delay = delay
    for attempt in range(1, attempts + 1):
        try:
            return fn(*args, **kwargs)
        except RateLimitExhausted:
            raise
        except Exception as exc:  # noqa: BLE001 - we want to retry on any error
            if logger:
                logger.warning("%s failed (attempt %d/%d): %s", label, attempt, attempts, exc)
            if attempt == attempts:
                if logger:
                    logger.error("%s exhausted retries: %s", label, exc)
                return None
            time.sleep(current_delay)
            current_delay *= backoff
    return None
