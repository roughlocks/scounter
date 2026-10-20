"""End-to-end collect() test against a fake instagrapi client (no network).

This is the single most valuable test: it drives the real Collector with a
fake client returning canned pages and a mocked CDN, catching the --max-posts
cap, the incremental-bail optimization, album (sidecar) item-level download,
and 429 ride-through.
"""

from __future__ import annotations

import datetime
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

import scounter.collector as col
from scounter.collector import CollectionOptions, Collector
from scounter.instagrapi_source import InstagrapiSource
from scounter.storage import Manifest

UTC = datetime.timezone.utc


def _media(pk, code, media_type=1, resources=None, video=None):
    return types.SimpleNamespace(
        pk=pk, code=code, taken_at=datetime.datetime(2024, 6, 1, 12, 0, pk % 60, tzinfo=UTC),
        media_type=media_type, caption_text=f"caption {code}",
        thumbnail_url=f"http://cdn/{code}.jpg",
        video_url=video, resources=resources or [],
    )


def _user(pk="1", username="target", media_count=0):
    return types.SimpleNamespace(
        pk=pk, username=username, full_name="Target", biography="b",
        external_url=None, follower_count=10, following_count=5, media_count=media_count,
        igtv_count=0, is_private=False, is_verified=False, is_business=False,
        business_category_name=None, category_name=None,
        profile_pic_url="http://cdn/pic.jpg", profile_pic_url_hd="http://cdn/pic_hd.jpg",
    )


class FakeClient:
    def __init__(self, post_pages, tagged_pages=None):
        self._post_pages = post_pages
        self._tagged_pages = tagged_pages or []
        self._idx = 0
        self._tidx = 0
        self.raise_once = None
        # instagrapi sessions — install_pacing mounts a PacingAdapter here (no-op
        # for the fake, which returns canned data without real HTTP).
        _noop = types.SimpleNamespace(mount=lambda *a, **k: None)
        self.private = _noop
        self.public = _noop

    def user_info_by_username(self, name):
        return _user(media_count=sum(len(p) for p, _ in self._post_pages))

    def user_info_v1(self, pk):
        return _user()

    def search_users(self, query, count=50):
        return []

    def user_medias_paginated(self, user_id, amount=0, end_cursor=""):
        if self.raise_once is not None:
            exc = self.raise_once
            self.raise_once = None
            raise exc
        if self._idx >= len(self._post_pages):
            return [], ""
        page, nxt = self._post_pages[self._idx]
        self._idx += 1
        return list(page), nxt

    def usertag_medias_paginated(self, user_id, amount=0, end_cursor=""):
        if self._tidx >= len(self._tagged_pages):
            return [], ""
        page, nxt = self._tagged_pages[self._tidx]
        self._tidx += 1
        return list(page), nxt

    def user_stories(self, user_id, amount=None):
        return []

    def user_highlights(self, user_id, amount=0):
        return []

    def media_info(self, pk):
        return _media(pk, f"C{pk}")


class FakeResp:
    def __init__(self, data=b"x"):
        self.data = data
        self.status_code = 200

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        yield self.data


def _opts(**kw):
    base = dict(
        posts=True, reels=True, stories=False, highlights=False, tagged=False,
        profile_pic=False, metadata=False, request_delay=0.001, workers=2,
    )
    base.update(kw)
    return CollectionOptions(**base)


def _make_collector(tmp, fake):
    # Inject an InstagrapiSource wrapping the fake (instagrapi-shaped) client.
    # authenticate() is a no-op (client already set); install_pacing hits the
    # fake's no-op session mounts.
    source = InstagrapiSource(client=fake, logger=None)
    return Collector(output_root=Path(tmp), source=source, logger=None)


def test_max_posts_caps_pages():
    with tempfile.TemporaryDirectory() as tmp:
        pages = [([_media(i, f"C{i}") for i in range(50)], "cur1"),
                 ([_media(50 + i, f"C{50+i}") for i in range(50)], "")]
        fake = FakeClient(pages)
        c = _make_collector(tmp, fake)
        with patch.object(col.requests, "get", return_value=FakeResp()):
            result = c.collect("target", _opts(max_posts=10))
        # Only the first page should have been fetched (cap hit inside page 1).
        assert fake._idx == 1, fake._idx
        m = Manifest(Path(tmp) / "target" / "manifest.db")
        assert m.diff("target")["manifested"] == 10, m.diff("target")
        assert m.diff("target")["missing"] == 0
        assert result.errors == []
        print("max-posts cap: OK (1 page fetched, 10 items)")


def test_incremental_bail():
    with tempfile.TemporaryDirectory() as tmp:
        # Page 1: 50 items, all pre-seeded as seen. Page 2: 50 new items.
        seen = [_media(i, f"C{i}") for i in range(50)]
        new = [_media(50 + i, f"C{50+i}") for i in range(50)]
        pages = [(seen, "cur1"), (new, "")]
        fake = FakeClient(pages)
        c = _make_collector(tmp, fake)
        # Pre-seed the manifest with the seen items.
        m = Manifest(Path(tmp) / "target" / "manifest.db")
        for s in seen:
            m.upsert(target="target", media_pk=str(s.pk), shortcode=s.code, kind="post",
                     date_utc="2024-06-01T12:00:00+00:00", url="u", video_url=None,
                     sidecar=0, collected_at="now", downloaded=1, local_path="x")
        m.close()
        with patch.object(col.requests, "get", return_value=FakeResp()):
            c.collect("target", _opts(seen_bail=40))
        # Should bail after page 1 (50 consecutive seen >= 40) and NOT fetch page 2.
        assert fake._idx == 1, fake._idx
        m = Manifest(Path(tmp) / "target" / "manifest.db")
        assert len(m.pending("target")) == 0  # page 2 (new items) never fetched
        print("incremental bail: OK (skipped page 2 after 50 seen)")


def test_album_item_level_download():
    with tempfile.TemporaryDirectory() as tmp:
        album = _media(1, "ALB1", media_type=8, resources=[
            types.SimpleNamespace(pk=11, thumbnail_url="http://cdn/r1.jpg", video_url=None),
            types.SimpleNamespace(pk=12, thumbnail_url=None, video_url="http://cdn/r2.mp4"),
        ])
        pages = [([album], "")]
        fake = FakeClient(pages)
        c = _make_collector(tmp, fake)
        with patch.object(col.requests, "get", return_value=FakeResp()):
            c.collect("target", _opts())
        m = Manifest(Path(tmp) / "target" / "manifest.db")
        assert m.diff("target")["missing"] == 0, m.diff("target")
        files = sorted(p.name for p in (Path(tmp) / "target" / "media" / "posts").iterdir())
        assert files == ["ALB1~1_20240601_120001.jpg", "ALB1~2_20240601_120001.mp4"], files
        # both resources marked downloaded
        assert len(m.pending_resources("target", "1")) == 0
        print("album item-level download: OK", files)


def test_429_ride_through():
    from instagrapi.exceptions import RateLimitError
    with tempfile.TemporaryDirectory() as tmp:
        pages = [([_media(i, f"C{i}") for i in range(5)], "")]
        fake = FakeClient(pages)
        fake.raise_once = RateLimitError("slow down")
        c = _make_collector(tmp, fake)
        with patch.object(col.requests, "get", return_value=FakeResp()):
            result = c.collect("target", _opts(max_consecutive_429=5, max_total_429=50))
        # The fake raises RateLimitError directly (bypassing the transport
        # adapter), so _api just retries and completes. AIMD counting is tested
        # in test_transport.py against a fake transport.
        assert result.errors == [], result.errors
        print("429 ride-through: OK (transient RateLimitError retried, completed)")


def test_shortcode_with_underscore_not_corrupted():
    from scounter.utils import media_filename, shortcode_from_filename
    # A legal shortcode containing _<digits> must survive the round-trip.
    fn = media_filename("ABC_123", "2024-06-01T12:30:45+00:00", "jpg")
    assert fn == "ABC_123_20240601_123045.jpg", fn
    assert shortcode_from_filename(fn) == "ABC_123", shortcode_from_filename(fn)
    # Album form keeps the index on the far side of '~'.
    afn = media_filename("ABC_123", "2024-06-01T12:30:45+00:00", "jpg", index=2)
    assert afn == "ABC_123~2_20240601_123045.jpg", afn
    assert shortcode_from_filename(afn) == "ABC_123", shortcode_from_filename(afn)
    print("shortcode underscore round-trip: OK")


def test_profile_resolution_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        fake = FakeClient([([], "")])

        def boom(name):
            raise RuntimeError("400 bad request (simulated business-profile schema)")

        fake.user_info_by_username = boom
        fake.search_users = lambda q, count=50: [_user(pk="77", username="target")]
        c = _make_collector(tmp, fake)
        with patch.object(col.requests, "get", return_value=FakeResp()):
            result = c.collect("target", _opts())
        # Primary failed, search fallback resolved it -> no "profile_not_found" error.
        assert result.errors == [], result.errors
        print("profile-resolution fallback: OK (resolved via search when primary 400'd)")


def test_collect_characterization_snapshot():
    """Precondition for the InstagrapiSource extraction: a fixed dataset run
    through collect(), with the manifest snapshotted to a canonical JSON. The
    refactor's invariant is 'manifest byte-identical before and after'."""
    import json as _json

    album = _media(3, "CALB", media_type=8, resources=[
        types.SimpleNamespace(pk=31, thumbnail_url="http://cdn/a1.jpg", video_url=None),
        types.SimpleNamespace(pk=32, thumbnail_url=None, video_url="http://cdn/a2.mp4"),
    ])
    post_pages = [([
        _media(1, "CP1", media_type=1),
        _media(2, "CR2", media_type=2, video="http://cdn/cr2.mp4"),
        album,
    ], "")]
    tagged_pages = [([_media(4, "CT4", media_type=1)], "")]

    with tempfile.TemporaryDirectory() as tmp:
        fake = FakeClient(post_pages, tagged_pages=tagged_pages)
        c = _make_collector(tmp, fake)
        with patch.object(col.requests, "get", return_value=FakeResp()):
            result = c.collect("target", _opts(posts=True, reels=True, tagged=True,
                                               stories=False, highlights=False))
        assert result.errors == [], result.errors
        m = Manifest(Path(tmp) / "target" / "manifest.db")
        rows = m.all("target")
        snap = _json.dumps([{
            "pk": r["media_pk"], "shortcode": r["shortcode"], "kind": r["kind"],
            "sidecar": r["sidecar"], "downloaded": r["downloaded"],
            "resources": m.resource_count("target", r["media_pk"]),
        } for r in rows], sort_keys=True)
        # 4 rows: 1 photo post, 1 reel, 1 album (2 resources), 1 tagged.
        assert len(rows) == 4, rows
        kinds = sorted((r["shortcode"], r["kind"], r["sidecar"]) for r in rows)
        assert kinds == [("CALB", "post", 1), ("CP1", "post", 0),
                         ("CR2", "reel", 0), ("CT4", "tagged", 0)], kinds
        assert m.resource_count("target", "3") == 2          # album has 2 resources
        assert len(m.pending_resources("target", "3")) == 0  # both downloaded
        assert m.diff("target")["missing"] == 0
        print("characterization snapshot: OK (4 rows, album 2 resources, all downloaded)")
        # The canonical `snap` string is the byte-identical invariant the
        # InstagrapiSource extraction must preserve. (Printed for golden capture.)
        print("  snapshot:", snap)


class FakeAuthClient:
    """Minimal stand-in for instagrapi.Client to test the auth cookie-loading
    logic offline (no network). Records settings; resolves user_id from cookies
    or authorization_data like the real Client property does."""

    def __init__(self):
        self.settings: dict = {}
        self.username: str | None = None

    def set_proxy(self, p):  # noqa: ANN001
        pass

    def init(self):
        pass

    @property
    def user_id(self):
        c = self.settings.get("cookies", {})
        a = self.settings.get("authorization_data", {})
        uid = c.get("ds_user_id") or a.get("ds_user_id")
        try:
            return int(uid) if uid else None
        except (TypeError, ValueError):
            return None

    def user_info_v1(self, uid):  # noqa: ANN001
        return types.SimpleNamespace(username="realuser")

    @property
    def private(self):
        return types.SimpleNamespace(
            cookies=types.SimpleNamespace(set=lambda *a, **k: None)
        )

    def dump_settings(self, path):  # noqa: ANN001
        pass


def test_authenticate_loads_full_cookie_set():
    import tempfile

    from scounter.instagrapi_source import InstagrapiSource
    src = InstagrapiSource(session_dir=Path(tempfile.mkdtemp()), logger=None)
    fake = FakeAuthClient()
    src._new_client = lambda: fake  # type: ignore[assignment]
    cookies = "sessionid=AAA%3A1; ds_user_id=12345678; mid=XYZ; ig_did=ABC; rur=PRN"
    src.authenticate(None, cookies, None)
    # ALL five cookies loaded onto the session, not just sessionid:
    loaded = fake.settings["cookies"]
    assert loaded["sessionid"] == "AAA%3A1"
    assert loaded["ds_user_id"] == "12345678"
    assert loaded["mid"] == "XYZ"
    assert loaded["ig_did"] == "ABC"
    assert loaded["rur"] == "PRN"
    ad = fake.settings["authorization_data"]
    assert ad["ds_user_id"] == "12345678"
    assert ad["sessionid"] == "AAA%3A1"
    assert ad["should_use_header_over_cookies"] is True
    print("full-cookie auth: OK (all 5 cookies loaded, not just sessionid)")


def test_authenticate_rejects_when_sessionid_missing():
    import tempfile

    from scounter.instagrapi_source import InstagrapiSource
    from scounter.source import SourceAuthExpired
    src = InstagrapiSource(session_dir=Path(tempfile.mkdtemp()), logger=None)
    src._new_client = lambda: FakeAuthClient()  # type: ignore[assignment]
    try:
        src.authenticate(None, None, None)
        raise AssertionError("expected SourceAuthExpired")
    except SourceAuthExpired:
        pass
    print("auth rejects (no sessionid): OK")


def test_authenticate_rejects_when_ds_user_id_missing():
    import tempfile

    from scounter.instagrapi_source import InstagrapiSource
    from scounter.source import SourceAuthExpired
    src = InstagrapiSource(session_dir=Path(tempfile.mkdtemp()), logger=None)
    src._new_client = lambda: FakeAuthClient()  # type: ignore[assignment]
    # sessionid that doesn't start with digits AND no ds_user_id cookie
    try:
        src.authenticate("nonnumeric_sessionid_value", None, None)
        raise AssertionError("expected SourceAuthExpired")
    except SourceAuthExpired:
        pass
    print("auth rejects (no ds_user_id): OK")


def test_maybe_rotate_cadence():
    from unittest.mock import patch

    import scounter.tor as tor_mod
    from scounter.instagrapi_source import InstagrapiSource
    fake = FakeClient([([], "")])  # has no-op private/public mounts
    src = InstagrapiSource(client=fake, tor_rotate=3, logger=None)
    calls = {"n": 0}

    def fake_rotate(**kw):
        calls["n"] += 1
        return True

    with patch.object(tor_mod, "rotate_circuit", side_effect=fake_rotate):
        for _ in range(10):
            src.maybe_rotate()
    # rotate on items 3, 6, 9 -> 3 calls
    assert calls["n"] == 3, calls["n"]
    print("maybe_rotate cadence: OK (3 rotations over 10 items, every 3)")


def test_maybe_rotate_off_when_disabled():
    from unittest.mock import patch

    import scounter.tor as tor_mod
    from scounter.instagrapi_source import InstagrapiSource
    fake = FakeClient([([], "")])
    src = InstagrapiSource(client=fake, tor_rotate=0, logger=None)
    calls = {"n": 0}

    def boom(**kw):
        calls["n"] += 1
        return True

    with patch.object(tor_mod, "rotate_circuit", side_effect=boom):
        for _ in range(10):
            src.maybe_rotate()
    assert calls["n"] == 0, calls["n"]
    print("maybe_rotate disabled: OK (no rotations when tor_rotate=0)")


if __name__ == "__main__":
    test_shortcode_with_underscore_not_corrupted()
    test_max_posts_caps_pages()
    test_incremental_bail()
    test_album_item_level_download()
    test_429_ride_through()
    test_profile_resolution_fallback()
    test_collect_characterization_snapshot()
    test_authenticate_loads_full_cookie_set()
    test_authenticate_rejects_when_sessionid_missing()
    test_authenticate_rejects_when_ds_user_id_missing()
    test_maybe_rotate_cadence()
    test_maybe_rotate_off_when_disabled()
    print("\nALL FAKE-CLIENT TESTS PASSED")
