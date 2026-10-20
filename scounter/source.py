"""Engine-neutral source interface — the boundary between the collector and the
Instagram engine (private mobile API today; web API later).

The Source returns manifest-shaped data (``MediaRow``, ``ProfileInfo``, ...) and
NEVER leaks an engine object. Engine exceptions are translated to ``SourceError``
subtypes, so the collector/storage/pacing/transport/reporter import no engine
package — only the engine implementation module does.

Cookie parsing lives here (engine-neutral); the engine-specific login does not.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


# --------------------------------------------------------------------------- data
@dataclass
class MediaRow:
    """A single media item, manifest-shaped. The boundary object."""

    media_pk: str
    shortcode: str | None
    kind: str               # post | reel | story | highlight | tagged
    date_utc: str | None
    caption: str | None
    url: str | None
    video_url: str | None
    sidecar: bool
    resources: list[dict[str, Any]] = field(default_factory=list)  # [{idx,url,video_url}]


@dataclass
class ProfileInfo:
    username: str | None
    user_id: Any
    full_name: str | None
    biography: str | None
    external_url: str | None
    followers: int | None
    followees: int | None
    posts_count: int | None
    igtv_count: int
    is_private: bool | None
    is_verified: bool | None
    is_business: bool
    business_category: str | None
    category: str | None
    profile_pic_url: str | None          # already upgraded to full-res by the Source
    media_count: int | None


@dataclass
class HighlightInfo:
    pk: str
    title: str | None
    items: list[MediaRow] = field(default_factory=list)


@dataclass
class ResumeToken:
    """Opaque pagination position. The Source owns its meaning (the private API
    uses an ``end_cursor`` string; a web-API engine would use a count). Stored
    verbatim in the manifest cursor column."""

    value: str | None = None


# --------------------------------------------------------------------------- errors
class SourceError(Exception):
    """Base for engine-translated errors raised by a MediaSource."""


class SourceAuthExpired(SourceError):
    """The session was rejected (expired/invalid cookies). -> CookiesExpired."""


class SourceUnavailable(SourceError):
    """A non-auth engine failure (transient/network/unknown)."""


# --------------------------------------------------------------------------- cookie parsing (neutral)
def parse_cookies(cookies_str: str | None = None, cookies_file: str | None = None) -> dict[str, str]:
    """Parse cookies from a 'k=v; k=v' string and/or a Netscape cookie file."""
    cookies: dict[str, str] = {}
    if cookies_file:
        text = Path(cookies_file).read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            line = line.strip()
            if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5].strip()] = parts[6].strip()
    if cookies_str:
        for pair in cookies_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                cookies[k.strip()] = v.strip()
    return cookies


def extract_sessionid(
    sessionid: str | None = None,
    cookies_str: str | None = None,
    cookies_file: str | None = None,
) -> str | None:
    if sessionid:
        sid = sessionid.strip()
        return sid or None
    return parse_cookies(cookies_str, cookies_file).get("sessionid")


# --------------------------------------------------------------------------- protocol
class MediaSource(Protocol):
    """The contract the collector depends on. Engine-neutral."""

    def install_pacing(self, pacer: Any) -> None: ...
    def authenticate(
        self, sessionid: str | None, cookies_str: str | None, cookies_file: str | None
    ) -> None: ...
    def resolve_profile(self, target: str) -> ProfileInfo: ...
    def iter_media(
        self, uid: str, resume: ResumeToken | None
    ) -> Iterator[tuple[list[MediaRow], ResumeToken]]: ...
    def iter_tagged(
        self, uid: str, resume: ResumeToken | None
    ) -> Iterator[tuple[list[MediaRow], ResumeToken]]: ...
    def stories(self, uid: str) -> list[MediaRow]: ...
    def highlights(self, uid: str) -> list[HighlightInfo]: ...
    def refresh_urls(self, pk: str) -> MediaRow: ...
    def maybe_rotate(self) -> None: ...
