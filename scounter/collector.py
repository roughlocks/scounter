"""Core collection engine — engine-neutral.

The Collector owns the *flow* (stories/highlights first, then enumerate, then
parallel download) and storage. It depends only on the neutral
:class:`scounter.source.MediaSource` protocol; the concrete engine is injected
by the CLI and never imported here. Engine exceptions arrive as ``SourceError``
subtypes.

Flow:
  1. Authenticate the injected source (cookies-only).
  2. Grab stories + highlights first — they vanish in 24h / can be deleted.
  3. Enumerate posts/reels/tagged into the SQLite manifest, one page at a time,
     cursor-checkpointed; incremental runs bail after N consecutive seen items.
     Media objects from the private API already carry full-HD URLs.
  4. Download pending media in parallel straight from the CDN (unthrottled).
     Stale signed URLs are refreshed on demand via a paced, locked source call.

Under cookie auth the binding constraint is the account's budget, not the source
IP, so egress is kept stable — never rotated.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from scounter import utils
from scounter.pacing import Pacer, RateLimitExhausted
from scounter.source import (
    MediaRow,
    MediaSource,
    ProfileInfo,
    ResumeToken,
    SourceAuthExpired,
    SourceUnavailable,
)
from scounter.storage import Manifest

MEDIA_DIRNAME = "media"


class ExpiredUrl(Exception):
    """A stored CDN URL was rejected (signed URL expired) — needs refresh."""


class CookiesExpired(Exception):
    """The session was rejected (expired/invalid cookies). -> CLI exit code 3."""


_EXPIRED_MSG = (
    "Your Instagram sessionid/cookies were rejected (expired or invalid).\n"
    "Re-export a fresh sessionid from your browser and pass it via --sessionid, "
    "--cookies, or --cookies-file (or the SCOUNTER_SESSIONID / SCOUNTER_COOKIES / "
    "SCOUNTER_COOKIES_FILE env vars)."
)


@dataclass
class CollectionOptions:
    """Toggle which artifact categories to collect."""

    profile_pic: bool = True
    posts: bool = True
    reels: bool = True
    stories: bool = True
    highlights: bool = True
    tagged: bool = True
    metadata: bool = True
    fast_update: bool = False
    max_posts: int | None = None
    max_comments: int | None = None
    request_delay: float = 4.0
    max_consecutive_429: int = 5
    max_total_429: int = 50
    seen_bail: int = 150          # incremental run: bail after this many consecutive seen items
    max_attempts: int = 3         # dead-letter a download after this many failures
    workers: int = 4


@dataclass
class CollectionResult:
    target: str
    started_at: str
    finished_at: str | None = None
    profile: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    authenticated: bool = False


class Collector:
    def __init__(
        self,
        output_root: Path,
        source: MediaSource,
        sessionid: str | None = None,
        cookies_str: str | None = None,
        cookies_file: str | None = None,
        workers: int = 4,
        logger: logging.Logger | None = None,
    ) -> None:
        self.output_root = Path(output_root)
        self.logger = logger or logging.getLogger("scounter")
        self.session_dir = self.output_root / ".sessions"
        self.source = source
        self.workers = max(1, workers)
        self.pacer = Pacer(logger=self.logger)
        self._refresh_lock = threading.Lock()
        self._sessionid = sessionid
        self._cookies_str = cookies_str
        self._cookies_file = cookies_file

    # ------------------------------------------------------------------ public
    def collect(self, target: str, options: CollectionOptions) -> CollectionResult:
        target_dir = self.output_root / utils.sanitize(target)
        media_dir = target_dir / MEDIA_DIRNAME
        media_dir.mkdir(parents=True, exist_ok=True)
        manifest = Manifest(target_dir / "manifest.db", logger=self.logger)
        self.pacer.reset(
            options.request_delay,
            options.max_consecutive_429,
            options.max_total_429,
            manifest,
            target,
        )

        result = CollectionResult(target=target, started_at=utils.utc_now_iso())

        try:
            self.source.authenticate(self._sessionid, self._cookies_str, self._cookies_file)
            self.source.install_pacing(self.pacer)
            result.authenticated = True

            try:
                info = self.source.resolve_profile(target)
            except SourceAuthExpired:
                raise
            except RateLimitExhausted:
                raise
            except SourceUnavailable as exc:
                self.logger.error("Could not resolve @%s: %s", target, exc)
                result.errors.append(str(exc))
                result.finished_at = utils.utc_now_iso()
                return result

            result.profile = self._profile_metadata(info)
            manifest.set_profile_media_count(target, info.media_count)

            if options.profile_pic:
                self._download_profile_pic(info, media_dir, result)

            if options.stories:
                self._collect_stories(info, media_dir, manifest, target, result)
            if options.highlights:
                self._collect_highlights(info, media_dir, manifest, target, result)

            if options.posts or options.reels:
                self._enumerate(info, "posts", manifest, target, options, result)
            if options.tagged:
                self._enumerate(info, "tagged", manifest, target, options, result)

            self._download_pending(manifest, target, media_dir, options, result)
        except SourceAuthExpired as exc:
            raise CookiesExpired(str(exc) or _EXPIRED_MSG) from None
        except RateLimitExhausted as exc:
            self.logger.error("Rate limit exhausted — stopping. %s", exc)
            result.errors.append("rate_limit_exhausted")
        except KeyboardInterrupt:
            self.logger.warning("Interrupted — checkpoint saved for resume.")
            result.errors.append("interrupted")
        finally:
            self.pacer.save()
            result.counts = {**manifest.counts(target), **manifest.diff(target)}
            result.finished_at = utils.utc_now_iso()
            manifest.close()
        return result

    # ------------------------------------------------------------------ profile
    @staticmethod
    def _profile_metadata(info: ProfileInfo) -> dict[str, Any]:
        data: dict[str, Any] = {
            "username": info.username,
            "user_id": info.user_id,
            "full_name": info.full_name,
            "biography": info.biography,
            "external_url": info.external_url,
            "followers": info.followers,
            "followees": info.followees,
            "posts_count": info.posts_count,
            "igtv_count": info.igtv_count,
            "is_private": info.is_private,
            "is_verified": info.is_verified,
            "is_business": info.is_business,
            "business_category": info.business_category,
            "category": info.category,
            "profile_pic_url": info.profile_pic_url,
            "collected_at": utils.utc_now_iso(),
        }
        return {k: utils.to_jsonable(v) for k, v in data.items()}

    def _download_profile_pic(self, info: ProfileInfo, media_dir: Path, result: CollectionResult) -> None:
        url = info.profile_pic_url
        if not url:
            return
        dest_dir = media_dir / "profile_pic"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{utils.sanitize(info.username or 'unknown')}.jpg"
        try:
            self._download_url(url, dest)
            result.profile["profile_pic_downloaded"] = True
            result.profile["profile_pic_path"] = str(dest)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Profile pic download failed: %s", exc)
            result.errors.append(f"profile_pic:{exc}")

    # ------------------------------------------------------------------ stories / highlights
    def _collect_stories(
        self, info: ProfileInfo, media_dir: Path, manifest: Manifest, target: str, result: CollectionResult
    ) -> None:
        self.logger.info("Collecting stories for @%s ...", info.username or target)
        stories_dir = media_dir / "stories"
        stories_dir.mkdir(parents=True, exist_ok=True)
        try:
            stories = self.source.stories(str(info.user_id))
        except SourceAuthExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"stories:{exc}")
            return
        for row in stories:
            if manifest.is_seen(target, row.media_pk):
                continue
            url = row.video_url or row.url
            ext = "mp4" if row.video_url else "jpg"
            dest = stories_dir / utils.media_filename(row.media_pk, row.date_utc, ext)
            try:
                self._download_url(url, dest)
                manifest.upsert(
                    target=target, media_pk=row.media_pk, shortcode=None, kind="story",
                    date_utc=row.date_utc, url=row.url, video_url=row.video_url,
                    local_path=str(dest), downloaded=1, collected_at=utils.utc_now_iso(),
                )
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("story %s failed: %s", row.media_pk, exc)
                result.errors.append(f"story_skip:{exc}")
            self.source.maybe_rotate()

    def _collect_highlights(
        self, info: ProfileInfo, media_dir: Path, manifest: Manifest, target: str, result: CollectionResult
    ) -> None:
        self.logger.info("Collecting highlights for @%s ...", info.username or target)
        hl_root = media_dir / "highlights"
        hl_root.mkdir(parents=True, exist_ok=True)
        try:
            highlights = self.source.highlights(str(info.user_id))
        except SourceAuthExpired:
            raise
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"highlights:{exc}")
            return
        for hl in highlights:
            hl_dir = hl_root / utils.sanitize(hl.title or hl.pk)
            hl_dir.mkdir(parents=True, exist_ok=True)
            for row in hl.items:
                if manifest.is_seen(target, row.media_pk):
                    continue
                url = row.video_url or row.url
                ext = "mp4" if row.video_url else "jpg"
                dest = hl_dir / utils.media_filename(row.media_pk, row.date_utc, ext)
                try:
                    self._download_url(url, dest)
                    manifest.upsert(
                        target=target, media_pk=row.media_pk, shortcode=None, kind="highlight",
                        date_utc=row.date_utc, url=row.url, video_url=row.video_url,
                        local_path=str(dest), downloaded=1, collected_at=utils.utc_now_iso(),
                    )
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("highlight item %s failed: %s", row.media_pk, exc)
                    result.errors.append(f"highlight_skip:{exc}")
                self.source.maybe_rotate()

    # ------------------------------------------------------------------ enumerate
    def _store_row(self, target: str, row: MediaRow, manifest: Manifest) -> None:
        manifest.upsert(
            target=target, media_pk=row.media_pk, shortcode=row.shortcode, kind=row.kind,
            date_utc=row.date_utc, caption=row.caption, url=row.url, video_url=row.video_url,
            sidecar=int(row.sidecar), collected_at=utils.utc_now_iso(),
        )
        if row.sidecar:
            manifest.upsert_resources(target, row.media_pk, row.resources)

    def _enumerate(
        self, info: ProfileInfo, kind: str, manifest: Manifest, target: str,
        options: CollectionOptions, result: CollectionResult,
    ) -> None:
        self.logger.info("Enumerating %s for @%s ...", kind, info.username or target)
        resume = ResumeToken(manifest.get_cursor(target, kind) or None)
        added = 0
        seen_in_a_row = 0
        stop = False
        iter_fn = self.source.iter_media if kind == "posts" else self.source.iter_tagged
        try:
            for page, token in iter_fn(str(info.user_id), resume):
                for row in page:
                    if manifest.is_seen(target, row.media_pk):
                        seen_in_a_row += 1
                        continue
                    seen_in_a_row = 0
                    if kind == "posts":
                        if row.kind == "reel" and not options.reels:
                            continue
                        if row.kind == "post" and not options.posts:
                            continue
                    if options.max_posts is not None and added >= options.max_posts:
                        stop = True
                        break
                    self._store_row(target, row, manifest)
                    added += 1
                    self.source.maybe_rotate()
                manifest.set_cursor(target, kind, token.value or "")
                if stop:
                    break
                if not page:
                    break
                if seen_in_a_row >= options.seen_bail:
                    self.logger.info("Incremental bail: %d consecutive already-seen items.",
                                     seen_in_a_row)
                    break
        except SourceAuthExpired:
            raise
        except RateLimitExhausted:
            raise
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"enumerate_{kind}:{exc}")
        self.logger.info("Enumerated %d new %s items.", added, kind)

    # ------------------------------------------------------------------ download
    def _download_pending(
        self, manifest: Manifest, target: str, media_dir: Path,
        options: CollectionOptions, result: CollectionResult,
    ) -> None:
        rows = [
            r for r in manifest.pending(target, max_attempts=options.max_attempts)
            if r["kind"] in ("post", "reel", "tagged")
        ]
        if not rows:
            return
        dirs = {
            "post": media_dir / "posts",
            "reel": media_dir / "reels",
            "tagged": media_dir / "tagged",
        }
        for d in dirs.values():
            d.mkdir(parents=True, exist_ok=True)
        self.logger.info("Downloading %d media items (%d workers) ...", len(rows), self.workers)
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futures = {
                ex.submit(self._download_one, dict(r), manifest, target, dirs): r["media_pk"]
                for r in rows
            }
            done = 0
            for fut in as_completed(futures):
                pk = futures[fut]
                try:
                    fut.result()
                    done += 1
                    if done % 25 == 0:
                        self.logger.info("  ...downloaded %d/%d", done, len(rows))
                except Exception as exc:  # noqa: BLE001
                    manifest.inc_attempts(target, pk)
                    self.logger.warning("download %s failed: %s", pk, exc)
                    result.errors.append(f"download_skip:{pk}:{exc}")
        self.logger.info("Download pass complete: %d/%d", done, len(rows))

    def _download_one(
        self, row: dict[str, Any], manifest: Manifest, target: str,
        dirs: dict[str, Path],
    ) -> None:
        pk = row["media_pk"]
        shortcode = row["shortcode"]
        date_utc = row["date_utc"]
        dest_dir = dirs[row["kind"]]

        if row["sidecar"]:
            pending_res = manifest.pending_resources(target, pk)
            if not pending_res:
                self._refresh_resources(target, pk, manifest)
                pending_res = manifest.pending_resources(target, pk)
            first: str | None = None
            for r in pending_res:
                url = r["video_url"] or r["url"]
                ext = "mp4" if r["video_url"] else "jpg"
                dest = dest_dir / utils.media_filename(
                    shortcode or str(pk), date_utc, ext, index=r["idx"]
                )
                try:
                    self._download_url(url, dest)
                except ExpiredUrl:
                    self._refresh_resources(target, pk, manifest)
                    fresh = manifest.resource_by_idx(target, pk, r["idx"])
                    if fresh:
                        self._download_url(fresh["video_url"] or fresh["url"], dest)
                manifest.mark_resource_downloaded(target, pk, r["idx"], str(dest))
                if first is None:
                    first = str(dest)
            manifest.mark_downloaded(target, pk, first)
            return

        url = row["video_url"] or row["url"]
        ext = "mp4" if row["video_url"] else "jpg"
        dest = dest_dir / utils.media_filename(shortcode or str(pk), date_utc, ext)
        self._download_with_refresh(url, dest, target, pk, manifest)
        manifest.mark_downloaded(target, pk, dest)

    def _download_with_refresh(
        self, url: str | None, dest: Path, target: str, pk: str, manifest: Manifest,
    ) -> None:
        try:
            self._download_url(url, dest)
            return
        except ExpiredUrl:
            self.logger.info("URL expired for %s, refreshing ...", pk)
        media_url = self._refresh_resources(target, pk, manifest, single=True)
        if media_url:
            self._download_url(media_url, dest)

    def _refresh_resources(
        self, target: str, pk: str, manifest: Manifest, single: bool = False,
    ) -> str | None:
        """Re-fetch HD URLs via the source (paced, locked, thread-safe)."""
        with self._refresh_lock:
            row = self.source.refresh_urls(pk)
        manifest.upsert(
            target=target, media_pk=pk, url=row.url, video_url=row.video_url,
            sidecar=int(row.sidecar),
        )
        if row.sidecar:
            manifest.upsert_resources(target, pk, row.resources)
        if single:
            return row.video_url or row.url
        return None

    def _download_url(self, url: str | None, dest: Path) -> None:
        """Fetch bytes straight from the CDN (not identity-bound; always direct)."""
        if not url:
            raise ExpiredUrl("no url stored")
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = requests.get(str(url), stream=True, timeout=120)
        if resp.status_code in (403, 410):
            raise ExpiredUrl(f"{resp.status_code} for {url}")
        resp.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(65536):
                if chunk:
                    fh.write(chunk)
