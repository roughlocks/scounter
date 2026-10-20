"""SQLite manifest: the blueprint of every piece of media to collect.

Keeps a durable, queryable record of every post/reel/story/highlight/tagged
item discovered — its pk, shortcode, date, type, and download state — plus a
child table of carousel resources so albums resume at the *item* level. This
is what lets us *enumerate first, download second*: we map everything out,
then fetch bytes, ticking items off.

Also stores per-category pagination cursors (so an interrupted enumeration
resumes mid-stream), generic run-state (pacer state, profile media_count),
and the indexes that power the ``link`` lookup command.

Thread-safety: one shared connection (``check_same_thread=False``) guarded by
a lock — DB access is not the bottleneck, and WAL lets readers/writers coexist.
This avoids opening ~5000 short-lived connections per enumeration.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    target       TEXT NOT NULL,
    media_pk     TEXT NOT NULL,
    shortcode    TEXT,
    kind         TEXT NOT NULL,          -- post | reel | story | highlight | tagged
    date_utc     TEXT,
    caption      TEXT,
    url          TEXT,
    video_url    TEXT,
    sidecar      INTEGER DEFAULT 0,
    downloaded   INTEGER DEFAULT 0,
    local_path   TEXT,
    attempts     INTEGER DEFAULT 0,
    collected_at TEXT,
    PRIMARY KEY (target, media_pk)
);
CREATE INDEX IF NOT EXISTS idx_media_kind      ON media(target, kind);
CREATE INDEX IF NOT EXISTS idx_media_shortcode ON media(shortcode);
CREATE INDEX IF NOT EXISTS idx_media_date       ON media(date_utc);
CREATE INDEX IF NOT EXISTS idx_media_pending    ON media(target, downloaded);

-- Carousel items live in a child table so albums resume at item granularity:
-- a 10-image album that fails on item 7 restarts from item 7, not item 1.
CREATE TABLE IF NOT EXISTS media_resource (
    target     TEXT NOT NULL,
    media_pk   TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    url        TEXT,
    video_url  TEXT,
    downloaded INTEGER DEFAULT 0,
    local_path TEXT,
    PRIMARY KEY (target, media_pk, idx)
);

CREATE TABLE IF NOT EXISTS run_state (
    target TEXT NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT,
    PRIMARY KEY (target, key)
);
"""


class Manifest:
    """A small, dependency-free SQLite store for one collection target."""

    def __init__(self, db_path: Path, logger: logging.Logger | None = None) -> None:
        self.db_path = Path(db_path)
        self.logger = logger or logging.getLogger("scounter")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ----------------------------------------------------------- media rows
    def upsert(self, **fields: Any) -> None:
        """Insert or update a media row. Requires target + media_pk."""
        cols = list(fields.keys())
        if not {"target", "media_pk"}.issubset(cols):
            raise ValueError("upsert requires 'target' and 'media_pk'")
        placeholders = ",".join("?" for _ in cols)
        col_list = ",".join(cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in ("target", "media_pk"))
        sql = (
            f"INSERT INTO media({col_list}) VALUES({placeholders}) "
            f"ON CONFLICT(target, media_pk) DO UPDATE SET {updates}"
        )
        with self._cursor() as cur:
            cur.execute(sql, [fields[c] for c in cols])

    def is_seen(self, target: str, media_pk: Any) -> bool:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT 1 FROM media WHERE target=? AND media_pk=?",
                (target, str(media_pk)),
            ).fetchone()
        return row is not None

    def mark_downloaded(self, target: str, media_pk: Any, local_path: Path | str | None) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE media SET downloaded=1, local_path=? WHERE target=? AND media_pk=?",
                (str(local_path) if local_path is not None else None, target, str(media_pk)),
            )

    def inc_attempts(self, target: str, media_pk: Any) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE media SET attempts=attempts+1 WHERE target=? AND media_pk=?",
                (target, str(media_pk)),
            )

    def pending(
        self, target: str, kind: str | None = None, max_attempts: int | None = None
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM media WHERE target=? AND downloaded=0"
        params: list[Any] = [target]
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if max_attempts is not None:
            sql += " AND attempts<?"
            params.append(max_attempts)
        sql += " ORDER BY date_utc"
        with self._cursor() as cur:
            return cur.execute(sql, params).fetchall()

    # ----------------------------------------------------------- carousel resources
    def upsert_resources(self, target: str, media_pk: str, resources: list[dict[str, Any]]) -> None:
        """Replace a media's carousel resources (idx, url, video_url)."""
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM media_resource WHERE target=? AND media_pk=?",
                (target, str(media_pk)),
            )
            for r in resources:
                cur.execute(
                    "INSERT OR REPLACE INTO media_resource(target, media_pk, idx, url, video_url) "
                    "VALUES(?,?,?,?,?)",
                    (target, str(media_pk), r["idx"], r.get("url"), r.get("video_url")),
                )

    def pending_resources(self, target: str, media_pk: str) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            return cur.execute(
                "SELECT * FROM media_resource WHERE target=? AND media_pk=? AND downloaded=0 "
                "ORDER BY idx",
                (target, str(media_pk)),
            ).fetchall()

    def resource_count(self, target: str, media_pk: str) -> int:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT COUNT(*) AS c FROM media_resource WHERE target=? AND media_pk=?",
                (target, str(media_pk)),
            ).fetchone()
        return row["c"] if row else 0

    def resource_by_idx(self, target: str, media_pk: str, idx: int) -> sqlite3.Row | None:
        with self._cursor() as cur:
            return cur.execute(
                "SELECT * FROM media_resource WHERE target=? AND media_pk=? AND idx=?",
                (target, str(media_pk), idx),
            ).fetchone()

    def mark_resource_downloaded(
        self, target: str, media_pk: str, idx: int, local_path: Path | str | None
    ) -> None:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE media_resource SET downloaded=1, local_path=? "
                "WHERE target=? AND media_pk=? AND idx=?",
                (str(local_path) if local_path is not None else None, target, str(media_pk), idx),
            )

    # ----------------------------------------------------------- cursors / state
    def set_cursor(self, target: str, kind: str, cursor: str | None) -> None:
        self.set_state(target, f"cursor:{kind}", cursor or "")

    def get_cursor(self, target: str, kind: str) -> str:
        return self.get_state(target, f"cursor:{kind}") or ""

    def set_state(self, target: str, key: str, value: str | None) -> None:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO run_state(target,key,value) VALUES(?,?,?) "
                "ON CONFLICT(target,key) DO UPDATE SET value=excluded.value",
                (target, key, value if value is not None else ""),
            )

    def get_state(self, target: str, key: str) -> str | None:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT value FROM run_state WHERE target=? AND key=?", (target, key)
            ).fetchone()
        return row["value"] if row else None

    def set_profile_media_count(self, target: str, count: int | None) -> None:
        self.set_state(target, "profile_media_count", str(count) if count is not None else None)

    def get_profile_media_count(self, target: str) -> int | None:
        v = self.get_state(target, "profile_media_count")
        return int(v) if v else None

    # ----------------------------------------------------------- queries
    def counts(self, target: str) -> dict[str, int]:
        out: dict[str, int] = {}
        with self._cursor() as cur:
            for row in cur.execute(
                "SELECT kind, COUNT(*) AS c, COALESCE(SUM(downloaded),0) AS d "
                "FROM media WHERE target=? GROUP BY kind",
                (target,),
            ):
                out[row["kind"]] = row["c"]
                out[f"{row['kind']}_done"] = row["d"]
        return out

    def diff(self, target: str) -> dict[str, int | None]:
        with self._cursor() as cur:
            row = cur.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(downloaded),0) AS done "
                "FROM media WHERE target=?",
                (target,),
            ).fetchone()
        total = row["total"] or 0
        done = row["done"] or 0
        profile_count = self.get_profile_media_count(target)
        return {
            "manifested": total,
            "downloaded": done,
            "missing": total - done,
            "profile_count": profile_count,
        }

    def find_by_shortcode(self, shortcode: str) -> sqlite3.Row | None:
        with self._cursor() as cur:
            return cur.execute(
                "SELECT * FROM media WHERE shortcode=?", (shortcode,)
            ).fetchone()

    def find_by_date(self, date_prefix: str) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            return cur.execute(
                "SELECT * FROM media WHERE date_utc LIKE ? ORDER BY date_utc",
                (f"{date_prefix}%",),
            ).fetchall()

    def find_by_local_path(self, path: str) -> sqlite3.Row | None:
        with self._cursor() as cur:
            return cur.execute(
                "SELECT * FROM media WHERE local_path=?", (path,)
            ).fetchone()

    def all(self, target: str) -> list[sqlite3.Row]:
        with self._cursor() as cur:
            return cur.execute(
                "SELECT * FROM media WHERE target=? ORDER BY date_utc", (target,)
            ).fetchall()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
