"""The instagrapi implementation of :class:`scounter.source.MediaSource`.

This is the ONLY module under ``scounter/`` that imports instagrapi — the
collector, storage, pacing, transport, and reporter depend only on the neutral
``MediaSource`` protocol. Engine exceptions are translated to ``SourceError``
subtypes; engine objects never escape.

Authentication is cookies-only (``login_by_sessionid``). The full five-cookie set
is captured (instagrapi uses only ``sessionid``; the other four are kept for a
future ``InstaloaderSource`` HD path). The session cache (``settings.json``) is
written ``0600`` — it is a full account-takeover artifact.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from instagrapi import Client
from instagrapi.exceptions import (
    ClientError,
    ClientThrottledError,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
)

from scounter import tor as tor_mod
from scounter import transport
from scounter.pacing import RateLimitExhausted
from scounter.source import (
    HighlightInfo,
    MediaRow,
    ProfileInfo,
    ResumeToken,
    SourceAuthExpired,
    SourceUnavailable,
    parse_cookies,
)

_RATE_LIMIT_EXCS = (RateLimitError, PleaseWaitFewMinutes, ClientThrottledError)
_SETTINGS_FILENAME = "settings.json"

# Cookies instaloader's iPhone (HD) API maps to device headers. instagrapi only
# needs sessionid, but a future InstaloaderSource needs all five — capture them.
_IPHONE_COOKIES = ("sessionid", "ds_user_id", "mid", "ig_did", "rur")


def _u(url: Any) -> str | None:
    return str(url) if url is not None else None


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(timespec="seconds")
    return str(dt)


def _is_rejection(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "login_required" in msg or "forbidden" in msg or "inactive user" in msg


def _ds_user_id_from_sessionid(sid: str) -> str | None:
    """Best-effort: instagrapi's login_by_sessionid parses the user id from the
    start of the sessionid. Prefer the real ds_user_id cookie when available."""
    m = re.search(r"^\d+", sid)
    return m.group() if m else None


class InstagrapiSource:
    """``MediaSource`` backed by instagrapi's private mobile API (full-HD)."""

    def __init__(
        self,
        client: Any = None,
        proxy: str | None = None,
        session_dir: Path | None = None,
        tor_rotate: int = 0,
        tor_control_port: int = 9051,
        tor_password: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._proxy = proxy
        self._session_dir = Path(session_dir) if session_dir else None
        self.logger = logger or logging.getLogger("scounter")
        # Full cookie set when provided (in-memory only — no new on-disk credential).
        self.captured_cookies: dict[str, str] | None = None
        # Tor circuit rotation (NEWNYM) every N items; 0 = off (single stable circuit).
        self._tor_rotate = tor_rotate
        self._tor_control_port = tor_control_port
        self._tor_password = tor_password
        self._rot_count = 0

    # ----------------------------------------------------------- auth
    @property
    def _settings_path(self) -> Path:
        assert self._session_dir is not None
        return self._session_dir / _SETTINGS_FILENAME

    def _new_client(self) -> Client:
        cl = Client()
        if self._proxy:
            cl.set_proxy(self._proxy)
        return cl

    def _verify(self, cl: Client) -> str | None:
        if cl.user_id is None:
            return None
        try:
            user = cl.user_info_v1(cl.user_id)
            cl.username = user.username
            return user.username
        except LoginRequired as exc:
            raise SourceAuthExpired from exc
        except ClientError as exc:
            if _is_rejection(exc):
                raise SourceAuthExpired from exc
            self.logger.warning("Session verify failed: %s", exc)
            return None

    def authenticate(
        self, sessionid: str | None, cookies_str: str | None, cookies_file: str | None
    ) -> None:
        """Cookies-only login, loading the FULL cookie set onto the session
        (sessionid + ds_user_id + mid + ig_did + rur), not just sessionid.

        instagrapi's ``login_by_sessionid`` only uses the sessionid and
        regex-parses ``ds_user_id`` out of it (asserting if it doesn't start with
        digits — a common cause of "doesn't sign in"). Loading the full set uses
        the real ``ds_user_id`` cookie and carries the device cookies (``mid`` is
        read into the session by ``init()``). Raises ``SourceAuthExpired`` on
        rejection.
        """
        if self._client is not None:
            return  # already authenticated (e.g. injected for tests)
        cookies = parse_cookies(cookies_str, cookies_file)
        if not cookies and sessionid:                 # bare sessionid fallback
            cookies = {"sessionid": sessionid.strip()}
        self.captured_cookies = cookies or None
        sid = cookies.get("sessionid") if cookies else None
        if not sid:
            raise SourceAuthExpired(
                "No sessionid found in the supplied cookies. Export the full cookie "
                "set (sessionid + ds_user_id + mid + ig_did + rur) via --cookies-file."
            )
        ds_user_id = (cookies.get("ds_user_id") if cookies else None) or _ds_user_id_from_sessionid(sid)
        if not ds_user_id:
            raise SourceAuthExpired(
                "Could not determine ds_user_id from the cookies. Supply the full "
                "cookie set (sessionid + ds_user_id + mid + ig_did + rur) via "
                "--cookies-file."
            )
        assert self._session_dir is not None
        self._session_dir.mkdir(parents=True, exist_ok=True)

        # 1. Prefer a cached session.
        if self._settings_path.exists():
            cl = self._new_client()
            try:
                cl.set_settings(cl.load_settings(str(self._settings_path)))
                cl.init()
                if self._verify(cl):
                    self.logger.info("Reusing cached session @%s", cl.username)
                    self._client = cl
                    return
            except SourceAuthExpired:
                self.logger.warning("Cached session expired; fresh cookies required.")
                self._settings_path.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("Cached session unusable (%s); falling back to cookies.", exc)

        # 2. Fresh login: load the FULL cookie set manually onto the session.
        cl = self._new_client()
        cl.settings["cookies"] = cookies
        cl.settings["authorization_data"] = {
            "ds_user_id": str(ds_user_id),
            "sessionid": sid,
            "should_use_header_over_cookies": True,   # build Authorization from sessionid
        }
        cl.init()                                     # applies cookies + mid + Authorization header
        cl.private.cookies.set("ds_user_id", str(ds_user_id), domain=".instagram.com", path="/")
        username = self._verify(cl)
        if not username:
            raise SourceAuthExpired(
                "The supplied cookies were rejected (expired or invalid). Re-export a "
                "fresh full cookie set from your browser via --cookies-file."
            )
        try:
            cl.dump_settings(str(self._settings_path))
            os.chmod(self._settings_path, 0o600)        # account-takeover artifact
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Could not cache session: %s", exc)
        self.logger.info("Authenticated via cookies @%s (full cookie set)", username)
        self._client = cl

    def install_pacing(self, pacer: Any) -> None:
        """Mount the PacingAdapter on the instagrapi sessions (transport layer)."""
        transport.install_pacing(self._client.private, pacer)
        transport.install_pacing(self._client.public, pacer)

    def maybe_rotate(self) -> None:
        """Rotate the Tor circuit every N items (NEWNYM + connection reset).

        Called from the collector's main-thread loops. No-op when rotation is
        off (``tor_rotate == 0``) or no client is bound yet.
        """
        if not self._tor_rotate or self._client is None:
            return
        self._rot_count += 1
        if self._rot_count % self._tor_rotate != 0:
            return
        self.logger.info("Rotating Tor circuit (every %d items)...", self._tor_rotate)
        if tor_mod.rotate_circuit(
            control_port=self._tor_control_port,
            password=self._tor_password,
            logger=self.logger,
        ):
            tor_mod.reset_connections(self._client)

    # ----------------------------------------------------------- engine call wrapper
    def _api(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Retry rate-limit exceptions; translate instagrapi errors to Source*.

        Pacing + 429 recording live in the transport adapter (one per real HTTP
        request). Here: retry on rate-limit, propagate RateLimitExhausted,
        translate auth/rejection to SourceAuthExpired, other ClientError to
        SourceUnavailable.
        """
        while True:
            try:
                return fn(*args, **kwargs)
            except RateLimitExhausted:
                raise
            except _RATE_LIMIT_EXCS:
                continue  # adapter already recorded the 429; retry after its next sleep
            except LoginRequired as exc:
                raise SourceAuthExpired from exc
            except ClientError as exc:
                if _is_rejection(exc):
                    raise SourceAuthExpired from exc
                raise SourceUnavailable(str(exc)) from exc

    # ----------------------------------------------------------- media -> MediaRow
    @staticmethod
    def _classify(media: Any) -> str:
        return "reel" if getattr(media, "media_type", None) == 2 else "post"

    @staticmethod
    def _extract_resources(media: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for i, r in enumerate(getattr(media, "resources", []) or [], start=1):
            out.append({
                "idx": i,
                "url": _u(getattr(r, "thumbnail_url", None)),
                "video_url": _u(getattr(r, "video_url", None)),
            })
        return out

    def _media_row(self, media: Any, kind: str) -> MediaRow:
        sidecar = getattr(media, "media_type", None) == 8
        return MediaRow(
            media_pk=str(media.pk),
            shortcode=getattr(media, "code", None),
            kind=kind,
            date_utc=_iso(getattr(media, "taken_at", None)),
            caption=getattr(media, "caption_text", None),
            url=_u(getattr(media, "thumbnail_url", None)),
            video_url=_u(getattr(media, "video_url", None)),
            sidecar=sidecar,
            resources=self._extract_resources(media) if sidecar else [],
        )

    @staticmethod
    def _upgrade_pic_url(url: Any) -> str | None:
        if not url:
            return None
        return re.sub(r"/s\d+x\d+/", "/s1080x1080/", str(url))

    # ----------------------------------------------------------- MediaSource
    def resolve_profile(self, target: str) -> ProfileInfo:
        user = self._resolve_primary(target)
        if user is None and not target.isdigit():
            user = self._resolve_via_search(target)
        if user is None:
            raise SourceUnavailable(f"profile_not_found:{target}")
        if getattr(user, "is_private", False):
            self.logger.warning("@%s is private; limited data unless you follow them.", target)
        return self._profile_info(user)

    def _resolve_primary(self, target: str) -> Any:
        try:
            if target.isdigit():
                return self._api(self._client.user_info_v1, int(target))
            return self._api(self._client.user_info_by_username, target)
        except SourceAuthExpired:
            raise
        except RateLimitExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Primary lookup for @%s failed (%s); trying search.", target, exc)
            return None

    def _resolve_via_search(self, target: str) -> Any:
        try:
            matches = self._api(self._client.search_users, target)
            for u in matches or []:
                if (getattr(u, "username", "") or "").lower() == target.lower():
                    return self._api(self._client.user_info_v1, int(u.pk))
        except SourceAuthExpired:
            raise
        except RateLimitExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            self.logger.error("Profile @%s could not be resolved: %s", target, exc)
        return None

    def _profile_info(self, user: Any) -> ProfileInfo:
        return ProfileInfo(
            username=getattr(user, "username", None),
            user_id=getattr(user, "pk", None),
            full_name=getattr(user, "full_name", None),
            biography=getattr(user, "biography", None),
            external_url=str(user.external_url) if getattr(user, "external_url", None) else None,
            followers=getattr(user, "follower_count", None),
            followees=getattr(user, "following_count", None),
            posts_count=getattr(user, "media_count", None),
            igtv_count=getattr(user, "igtv_count", 0),
            is_private=getattr(user, "is_private", None),
            is_verified=getattr(user, "is_verified", None),
            is_business=getattr(user, "is_business", False),
            business_category=getattr(user, "business_category_name", None),
            category=getattr(user, "category_name", None),
            profile_pic_url=self._upgrade_pic_url(
                getattr(user, "profile_pic_url_hd", None) or getattr(user, "profile_pic_url", None)
            ),
            media_count=getattr(user, "media_count", None),
        )

    def _iter_paginated(
        self, fn: Any, uid: str, resume: ResumeToken | None,
    ) -> Iterator[tuple[list[MediaRow], ResumeToken]]:
        cursor = resume.value if resume and resume.value else ""
        while True:
            page, next_cursor = self._api(fn, uid, amount=0, end_cursor=cursor)
            yield [self._media_row(m, self._classify(m)) for m in page or []], ResumeToken(next_cursor)
            if not page or not next_cursor:
                return
            cursor = next_cursor

    def iter_media(
        self, uid: str, resume: ResumeToken | None
    ) -> Iterator[tuple[list[MediaRow], ResumeToken]]:
        return self._iter_paginated(self._client.user_medias_paginated, uid, resume)

    def iter_tagged(
        self, uid: str, resume: ResumeToken | None
    ) -> Iterator[tuple[list[MediaRow], ResumeToken]]:
        # Tagged posts are never reels (no classify); kind is "tagged".
        cursor = resume.value if resume and resume.value else ""
        while True:
            page, next_cursor = self._api(self._client.usertag_medias_paginated, uid,
                                          amount=0, end_cursor=cursor)
            yield [self._media_row(m, "tagged") for m in page or []], ResumeToken(next_cursor)
            if not page or not next_cursor:
                return
            cursor = next_cursor

    def stories(self, uid: str) -> list[MediaRow]:
        out: list[MediaRow] = []
        for st in self._api(self._client.user_stories, uid) or []:
            out.append(MediaRow(
                media_pk=str(st.pk), shortcode=None, kind="story",
                date_utc=_iso(getattr(st, "taken_at", None)),
                caption=None,
                url=_u(getattr(st, "thumbnail_url", None)),
                video_url=_u(getattr(st, "video_url", None)),
                sidecar=False,
            ))
        return out

    def highlights(self, uid: str) -> list[HighlightInfo]:
        out: list[HighlightInfo] = []
        for hl in self._api(self._client.user_highlights, uid) or []:
            full = self._api(self._client.highlight_info, hl.pk)
            items = []
            for item in getattr(full, "items", []) or []:
                items.append(MediaRow(
                    media_pk=str(item.pk), shortcode=None, kind="highlight",
                    date_utc=_iso(getattr(item, "taken_at", None)),
                    caption=None,
                    url=_u(getattr(item, "thumbnail_url", None)),
                    video_url=_u(getattr(item, "video_url", None)),
                    sidecar=False,
                ))
            out.append(HighlightInfo(pk=str(hl.pk), title=getattr(hl, "title", None), items=items))
        return out

    def refresh_urls(self, pk: str) -> MediaRow:
        """Re-fetch a media's HD URLs via media_info (paced, called under the
        collector's refresh lock)."""
        media = self._api(self._client.media_info, int(pk))
        return self._media_row(media, self._classify(media))
