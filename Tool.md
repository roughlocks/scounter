# scounter — Technical Deep-Dive

> A cookies-only, full-HD Instagram archiver.
> This document describes **everything the tool does, how the code works
> (with excerpts), the design decisions behind it, and the problems it
> currently faces.** Written for a technical reviewer.
>
> **Revision 2** — incorporates a design review that removed Tor circuit
> rotation (it optimizes the wrong variable under cookie auth), fixed several
> real bugs, and added a child-table manifest, incremental-run bail, honest
> AIMD pacing, and a fake-client test suite.

---

## 1. What it is

`scounter` downloads and archives every retrievable artifact from an Instagram
profile — posts, reels, sidecar albums, stories, highlights, tagged posts, and
the profile picture — at **full resolution**, plus a structured metadata dump
and a Markdown report. Built for authorized OSINT / archival work.

It is the rewritten descendant of an `instaloader`-based tool. The engine was
swapped to [`instagrapi`](https://github.com/subzeroid/instagrapi) (Instagram's
private mobile API) for full-HD media; authentication is **cookies-only**
(`sessionid`); and a **map-first / download-second** architecture with a SQLite
manifest drives resumable, low-API-cost collection.

| | |
|---|---|
| Language | Python 3.10+ |
| Engine | `instagrapi>=2.5,<3` (private mobile API) |
| Auth | cookies-only (`sessionid`) — no username/password, no anonymous mode |
| Storage | SQLite (manifest) + JSON (reports) |
| Network | direct, or a **single stable** proxy / Tor circuit |

> **Tor rotation is intentionally NOT performed.** See §11 for why: under
> cookie auth the binding constraint is the *account*, not the source IP, and
> scattering one identity across many exits reads as account compromise.

---

## 2. Project layout

```
scounter/
├── __init__.py        version
├── __main__.py        enables `python -m scounter`
├── cli.py             argparse: `collect` + `link` subcommands
├── auth.py            cookies-only login, session cache (0600), expiry detection
├── collector.py       the engine: stories-first, enumerate→manifest, parallel HD download
├── pacing.py          rate-based AIMD pacer (consecutive + total 429 budget)
├── storage.py         SQLite manifest: media + media_resource child table + run_state
├── reporter.py        metadata.json + report.md
└── utils.py           logging, serialization, filenames, retry
tests/
└── test_collect.py    fake-client end-to-end collect() tests (no network)
```

---

## 3. The end-to-end collection flow

`Collector.collect()` (`collector.py:160`) drives the run:

```
1. Authenticator.login()              → instagrapi Client (cookies)
2. _resolve_profile(target)           → User (by username or numeric id)
3. _profile_metadata(user)            → profile dict; store user.media_count for diff
4. _download_profile_pic()            → HD profile pic
5. _collect_stories()                 → enumerate + download (ephemeral!)   light pacing
6. _collect_highlights()              → enumerate + download (per-highlight folder)
7. _enumerate_posts()                 → page posts/reels into manifest.db (cursor-resumable)
8. _enumerate_tagged()                → page tagged posts into manifest.db
9. _download_pending()                → parallel CDN download of everything in the manifest
10. write_reports()                   → metadata.json + report.md
```

Stories/highlights come **first** because they expire in 24h (or can be
deleted), so they're grabbed before the long posts crawl risks burning the
session. Only then are posts/reels/tagged enumerated, and only after
enumeration is complete are bytes downloaded.

---

## 4. Authentication (cookies-only)

`auth.py` is the **only** login path. The user supplies an Instagram
`sessionid` cookie (raw, `k=v; k=v` string, or Netscape file).
`Authenticator.login()` prefers a cached session (instagrapi settings JSON)
and falls back to `login_by_sessionid`:

```python
# auth.py
def login(self, sessionid=None, cookies_str=None, cookies_file=None) -> Client:
    sid = extract_sessionid(sessionid, cookies_str, cookies_file)
    if self.settings_path.exists():                 # 1. cached session
        cl = self._new_client()
        try:
            cl.set_settings(cl.load_settings(str(self.settings_path)))
            cl.init()
            if self._verify(cl):
                return cl
        except CookiesExpired:
            self.settings_path.unlink(missing_ok=True)
    if not sid:                                      # 2. fresh cookie login
        raise CookiesExpired(_NO_COOKIES_MSG)
    cl = self._new_client()
    cl.login_by_sessionid(sid)
    cl.dump_settings(str(self.settings_path))
    os.chmod(self.settings_path, 0o600)             # account-takeover artifact!
    return cl
```

**Expiry is explicit, never silent.** `LoginRequired` / rejection messages
become `CookiesExpired` → CLI prints the message and exits **3** (distinct from
a generic fatal `1`).

**The session cache is a credential.** `settings.json` contains the full
session + device fingerprint — a complete account-takeover artifact. It is now
`chmod 0600` at write time (gitignore alone is insufficient).

**Credential-input ranking** (documented in the CLI warning): `--sessionid` on
argv leaks via `ps`; `SCOUNTER_SESSIONID` leaks via `/proc/<pid>/environ`; a
Netscape `--cookies-file` with mode 0600 is the only path not observable by
other local users. The CLI warns when `--sessionid` is used on argv.

---

## 5. How full-HD is guaranteed (and why no Selenium)

instagrapi's **private mobile API** returns media objects whose
`extract_media_v1` picks the **highest-resolution** candidate:

```python
# instagrapi/extractors.py (upstream)
media["video_url"]    = sorted(media["video_versions"],     key=lambda o: o["height"]*o["width"])[-1]["url"]
media["thumbnail_url"] = sorted(media["image_versions2"]["candidates"], key=lambda o: o["height"]*o["width"])[-1]["url"]
```

So every `Media` from enumeration **already carries an HD URL**. We store it and
later download the bytes straight from the CDN — no re-fetch, no API call, HD by
construction. This is also why Selenium is not used (web pages serve compressed
thumbnails; HD only comes from the private API/CDN; a headless browser is
slower and more detectable for no HD gain).

---

## 6. The map-first architecture (SQLite manifest)

`storage.py` records every discoverable item before any bytes are fetched.
Carousel items live in a **child table** so albums resume at item granularity.

```sql
CREATE TABLE media (
    target TEXT, media_pk TEXT, shortcode TEXT, kind TEXT,   -- post|reel|story|highlight|tagged
    date_utc TEXT, caption TEXT, url TEXT, video_url TEXT,
    sidecar INTEGER DEFAULT 0, downloaded INTEGER DEFAULT 0,
    local_path TEXT, attempts INTEGER DEFAULT 0, collected_at TEXT,
    PRIMARY KEY (target, media_pk)
);
-- Item-level resume for albums: a 10-image album failing on item 7 restarts at 7.
CREATE TABLE media_resource (
    target TEXT, media_pk TEXT, idx INTEGER,
    url TEXT, video_url TEXT, downloaded INTEGER DEFAULT 0, local_path TEXT,
    PRIMARY KEY (target, media_pk, idx)
);
CREATE TABLE run_state (target TEXT, key TEXT, value TEXT, PRIMARY KEY (target, key));
```

`Manifest` methods: `upsert`, `is_seen`, `mark_downloaded`, `inc_attempts`,
`pending(target, kind, max_attempts)`, `upsert_resources`,
`pending_resources`, `mark_resource_downloaded`, `set_cursor/get_cursor`,
`set_state/get_state` (generic — pacer state + profile media_count live here),
`set_profile_media_count`, `counts`, `diff`, `find_by_shortcode/date/local_path`.

**Connection handling (fixed):** one shared connection
(`check_same_thread=False`) guarded by an `RLock`, with WAL set once at init.
The previous design opened a fresh connection **per `is_seen` call** (~5,000
opens per enumeration) and re-set WAL every time (WAL is persistent in the file;
only `synchronous=NORMAL` is genuinely per-connection). DB access is not the
bottleneck, so serializing it is fine and thread-safe for the parallel download.

### Why this matters

Splitting enumeration from download is the biggest rate-limit win. Enumeration
pages ~50 items per API request, so listing 5,000 posts is ~100 lightweight JSON
calls vs. thousands for download-interleaved paging — roughly **50× cheaper**.

---

## 7. Enumeration — cursor-resumable, incremental, capped

`_enumerate_posts()` (`collector.py`) walks the feed one page at a time with
`user_medias_paginated(user_id, amount, end_cursor) -> (page, next_cursor)`,
checkpointing the cursor after every page.

Three fixes from the review:

**(a) `--max-posts` actually caps (was broken).** The old inner-loop `break`
only exited the page loop; the outer `while True` kept paginating to the end of
the feed. Now a `stop` sentinel is checked in the outer loop:

```python
stop = False
while not stop:
    page, cursor = self._api(self.client.user_medias_paginated, str(user.pk),
                             amount=0, end_cursor=cursor, label="enumerate_posts")
    for m in page or []:
        if manifest.is_seen(target, m.pk):
            seen_in_a_row += 1
            continue
        seen_in_a_row = 0
        ...
        manifest.upsert(**self._media_row(target, m, kind))
        if row["sidecar"]:
            manifest.upsert_resources(target, str(m.pk), self._extract_resources(m))
        added += 1
        if options.max_posts is not None and added >= options.max_posts:
            stop = True
            break
    manifest.set_cursor(target, "posts", cursor)
    if stop or not page or not cursor:
        break
    # (b) incremental bail:
    if seen_in_a_row >= options.seen_bail:
        break
```

**(b) Incremental bail.** The feed is newest-first, so a daily run against a
5,000-post account previously cost the same ~100 API calls as the first run,
forever. Now it stops after `--seen-bail` (default 150, ~3 pages) consecutive
already-seen items — surviving pinned posts / out-of-order edges.

**(c) Carousel resources** are stored in the child table at enumeration time
(`_extract_resources`), not as a JSON blob, so each item is independently
queryable and resumable.

---

## 8. Pacing at the transport layer (`transport.py` + `_api`)

**Revision 4 moved pacing to the transport layer.** A `PacingAdapter` is mounted
on `cl.private`/`cl.public` and paces **every actual HTTP request** — including
instagrapi's internal auth-refresh/follow-up requests — so the pacer counts real
requests, not method calls. `_api` now owns only retry policy and exception
translation.

This also fixes a **live bug the review found**: instagrapi's default adapter
mounted `Retry(status_forcelist=[429, …])`, so a persistent 429 was retried by
urllib3 and, on exhaustion, raised `MaxRetryError`→`requests.RetryError` — *not*
an `HTTPError` — so instagrapi's 429→`ClientThrottledError` mapping (which fires
only after `raise_for_status`) never ran, and `_RATE_LIMIT_EXCS` never matched.
Persistent 429s silently bypassed AIMD. The fix is the "third option": retry
only **transport** errors in urllib3, never status codes, so every 429 reaches
the adapter as a real `Response` (1:1):

```python
# transport.py — Retry(connect/read only, never status)
Retry(total=3, connect=3, read=3, status=0, status_forcelist=[],
      raise_on_status=False, respect_retry_after_header=False)

class PacingAdapter(HTTPAdapter):
    def send(self, request, **kwargs):
        self.pacer.before_request()             # rate sleep (lock not held during sleep)
        resp = super().send(request, **kwargs)  # transport-only retry inside
        if resp.status_code == 429:
            self.pacer.record_429()             # may raise RateLimitExhausted -> propagates
        elif 200 <= resp.status_code < 300:
            self.pacer.record_success()
        return resp
```

`respect_retry_after_header=False` so Instagram's `Retry-After` can't cause an
invisible multi-minute sleep inside one "request". `install_pacing(session,
pacer)` is a standalone, engine-agnostic function (works on any
`requests.Session`) — when `InstagrapiSource` is extracted, the call moves into
it unchanged, and a future `InstaloaderSource` reuses it.

The pacer is now a **token bucket** (capacity ~10, refill 1 token per AIMD-adapted
`interval`). This gives the ephemeral phase (stories/highlights) a burst to drain
at full speed before throttling naturally — which is what the old `light` flag
was a hand-rolled patch for, so the flag dissolved (net deletion). AIMD adapts
the refill interval: 429 → `interval *= 2` (multiplicative), success →
`interval -= step` (additive). The pre-request *sleep is outside the lock*, so
worker threads don't serialize behind each other's waits (verified by
`test_before_request_does_not_hold_lock_during_sleep`).

`_api` is now just retry + exception translation (mechanism in the adapter,
policy here, one retry loop total):

```python
# collector.py
def _api(self, fn, *args, label="api", **kwargs):
    while True:
        try:
            return fn(*args, **kwargs)
        except RateLimitExhausted:
            raise
        except _RATE_LIMIT_EXCS:        # adapter already recorded the 429
            continue
        except LoginRequired as exc:
            raise CookiesExpired(_EXPIRED_MSG) from exc
        except ClientError as exc:
            if _is_rejection(exc):
                raise CookiesExpired(_EXPIRED_MSG) from exc
            raise
```

Media downloads deliberately do **not** go through `_api` — they are CDN fetches
(see §10). Because the adapter is hit from worker threads (`_refresh_resources`),
all pacer state (`delay`, `consecutive_429`, `total_429`) is now guarded by a
lock (a new requirement the move introduced).

---

## 9. Pacing — rate-based honest AIMD (`pacing.py`)

Under cookie auth the binding constraint is the **account's** request budget,
not the source IP, so we pace against a *chosen rate* (a minimum interval) with
small uniform jitter — not a human-emulation random walk.

**AIMD is now honest:**
- 429 → **multiplicative decrease** of rate: `delay = min(max_delay, delay * 2)`.
- success → **additive increase** of rate: `delay = max(base_delay, delay - step)`.

(The previous `delay * 0.95` was multiplicative decrease of delay = MIMD; the
stability property of AIMD comes from the increase being additive.)

**Total 429 budget (fixed):** the old guard reset `consecutive_429` on any
success, so an alternating 429/success pattern retried forever and never tripped
`RateLimitExhausted`. Now it trips on **either** a consecutive streak **or** a
run-wide total:

```python
def record_429(self) -> None:
    self.consecutive_429 += 1
    self.total_429 += 1
    self.delay = min(self.max_delay, self.delay * 2)
    if self.consecutive_429 >= self.max_consecutive_429 or self.total_429 >= self.max_total_429:
        self.save()
        raise RateLimitExhausted(...)
```

**State in the manifest, not a file.** The pacer checkpoints `delay` +
`consecutive_429` + `total_429` into the manifest's `run_state` table (key
`pacer`) — one less file (`pacer.json` is gone), one less format to desync.

---

## 10. Parallel HD download (`_download_pending`)

Once the manifest is populated, `_download_pending()` pulls pending post/reel/
tagged rows (dead-lettering any with `attempts >= --max-attempts`) and downloads
them with a `ThreadPoolExecutor`:

```python
rows = [r for r in manifest.pending(target, max_attempts=options.max_attempts)
        if r["kind"] in ("post", "reel", "tagged")]
with ThreadPoolExecutor(max_workers=self.workers) as ex:
    futures = {ex.submit(self._download_one, dict(r), manifest, target, dirs): r["media_pk"]
               for r in rows}
    for fut in as_completed(futures):
        try:
            fut.result()
        except Exception as exc:
            manifest.inc_attempts(target, pk)     # dead-letter after max_attempts
            result.errors.append(f"download_skip:{pk}:{exc}")
```

**Media is always fetched directly from the CDN** (no proxy/Tor). CDN GETs carry
no session cookie and are not identity-bound, so they don't trigger integrity
scoring and aren't rate-limited — they're also far faster off-Tor. (If you need
the CDN to not see your IP, that's an anonymity concern, not an account-safety
one; see §11.)

`_download_one()` handles:
- **Albums** — iterates only `pending_resources` (item-level resume), downloads
  each as `<shortcode>~<idx>_<date>.<ext>`, marks each resource done, then marks
  the parent done.
- **Single media** — `<shortcode>_<date>.<ext>`, with stale-URL refresh.

**Stale-URL refresh:** CDN URLs are signed and expire. A 403/410 raises
`ExpiredUrl` → `_refresh_resources` re-fetches HD URLs via `media_info`
(**paced and under `_refresh_lock`** — the only instagrapi client access from
worker threads). Same-run downloads cost zero API calls; only long-delayed
resumes pay the occasional refresh.

---

## 11. Network: stable egress only — no circuit rotation

This is the headline change from the review.

**The design contradiction that was removed:** the old build authenticated with
a `sessionid` (pinning identity to the account + the instagrapi device
fingerprint that `dump_settings` persists) **and** rotated Tor circuits
(NEWNYM). But rate limiting and integrity scoring on authenticated private-API
traffic is keyed overwhelmingly on the **account**, not the source IP. So
NEWNYM bought ~nothing on the constraint that actually binds, and made things
*worse*: a single authenticated account whose egress hops across a dozen
unrelated Tor exits inside five minutes is a canonical **account-compromise
signal**. The failure mode isn't a 429 you can back off from — it's
`checkpoint_required` and a dead session.

Cookies-only auth and circuit rotation pull in opposite directions: cookie auth
**pins** identity; rotation **scatters** network location. The coherent choice
under the HD requirement (which forces authentication) is the **fidelity story**:
authenticate, and keep a stable, boring egress consistent with where that account
plausibly lives. Pace against the account's budget, not the IP's.

**What was removed:** `tor.py`, `_maybe_rotate`, `reset_connections`,
`--tor-rotate`, `--no-hybrid`, the hybrid-proxy split, the `_api_calls` counter,
and the `stem` dependency.

**What remains:** `--proxy` (a stable proxy, ideally residential in the
account's region) and `--tor` (a **single stable** Tor circuit, for anonymity
only — less reliable than a residential IP because Tor exits are themselves
distrusted datacenter IPs, but it no longer flips the compromise detector).

A residual `rotate_circuit` cost the review noted is now moot: there is no
11s+3s NEWNYM sleep that the pacer couldn't account for.

---

## 12. The `link` command — manifest-primary, filename-fallback

Filenames are deterministic and reversible: `<shortcode>_<YYYYMMDD_HHMMSS>.<ext>`,
albums `<shortcode>~<idx>_<date>.<ext>`. The album index uses `~` (outside the
base64url shortcode alphabet `[A-Za-z0-9_-]`), so a shortcode ending in
`_<digits>` is **never** truncated (the old `_<idx>_` delimiter corrupted e.g.
`ABC_123` → `ABC`).

```python
# utils.py
def media_filename(shortcode, date_utc, ext, index=None) -> str:
    code = sanitize(shortcode or "unknown", max_len=40)
    if index is not None:
        code = f"{code}~{index}"          # '~' not in [A-Za-z0-9_-]
    ...
    return f"{code}_{stamp}.{ext.lstrip('.')}"

def shortcode_from_filename(filename) -> str | None:
    stem = Path(filename).stem
    m = re.match(r"^(.+?)(?:~\d+)?_\d{8}_\d{6}$", stem)   # strips optional ~idx + date
    return m.group(1) if m else (stem or None)
```

The **manifest is the primary source of truth** (it stores `local_path` and
`shortcode`); filename parsing is only a fallback for orphaned files. `link`
tries `find_by_local_path` first, then `find_by_shortcode(shortcode_from_filename(...))`.

```
$ python -m scounter link ABC_123_20240601_123045.jpg
target : natgeo
kind   : post
ig url : https://www.instagram.com/p/ABC_123/
local  : output/natgeo/media/posts/ABC_123_20240601_123045.jpg
```

Accepts a shortcode, a filename, or a date (YYYY / YYYY-MM / YYYY-MM-DD).

---

## 13. Resume / losslessness

Three checkpoints make every run resumable:
1. **Manifest** — every enumerated item + download flag; `pending()` returns
   what's left, albums at item granularity.
2. **Pagination cursor** (`run_state`) — enumeration resumes from the last page.
3. **Pacer state** (`run_state`, key `pacer`) — adapted delay + 429 counters
   survive a restart.

`--fast-update` is the user-facing resume flag.

---

## 14. CLI surface

```
python -m scounter collect <target> [options]
python -m scounter link <query> [--target TARGET] [-o DIR]
```

| Group | Flags |
|---|---|
| Auth | `--sessionid`, `--cookies`, `--cookies-file` (or `SCOUNTER_*` env vars) |
| Network | `--proxy URL`, `--tor` (single stable circuit) |
| Scope | `--everything`, `--no-posts/--no-reels/--no-stories/--no-highlights/--no-tagged`, `--no-profile-pic`, `--no-metadata` |
| Tuning | `--fast-update`, `--max-posts N`, `--request-delay SEC`, `--max-429 N`, `--max-total-429 N`, `--seen-bail N`, `--max-attempts N`, `--workers N`, `-o`, `-v` |

Exit codes: `0` success · `2` soft errors · `1` fatal · `3` **cookies expired** · `130` interrupted.

---

## 15. Output layout

```
output/<target>/
├── media/{profile_pic,posts,reels,stories,highlights/<title>/,tagged}/
├── manifest.db             SQLite blueprint (media + media_resource + run_state)
├── metadata.json           profile + collection counts
├── report.md               summary (per-category counts + missing diff vs profile_count)
├── collection.log
└── .sessions/settings.json cached instagrapi session (chmod 0600 — git-ignore)
```

`diff()` now reports `profile_count` (from `user.media_count`) alongside
`manifested/downloaded/missing`. `missing` means "enumerated but not
downloaded"; `manifested vs profile_count` is the only real completeness signal
(it can't detect items deleted in flight / pagination gaps, but it catches an
enumeration that came up short).

---

## 16. Problems / known risks (revised)

### 16.1 Not validated against live Instagram
Unit + fake-client tests pass (§17), but **no live run is confirmed** — that
needs a real `sessionid`. instagrapi behaviors (exact exception types, cursor
semantics, `login_by_sessionid` acceptance) are assumed from the source.

### 16.2 `login_by_sessionid` is the single point of total failure (promoted)
A browser `sessionid` was minted for a *web* session; handing it to a client
claiming to be an Android phone is a session/device-class mismatch. This is the
most likely thing in the system to fail, and there is **no fallback HD path**
today. Recommended hedge: keep an instaloader web path behind `--engine
instaloader` as a degraded-fidelity fallback so a private-API break doesn't take
availability to zero. (Not yet implemented.)

### 16.3 Failure-mode category change from the engine swap
`instaloader` looks like a browser hitting web endpoints; `instagrapi`
impersonates the Android app with a synthesized device fingerprint. Integrity
systems treat unrecognized-device private-API traffic far more aggressively.
The pacing/backoff apparatus is built for "429 → back off → resume"; it is
largely irrelevant to "checkpoint → account gone." Mitigation: don't run from an
account you care about; keep egress stable (§11).

### 16.4 instagrapi dependency brittleness
Pinned to `>=2.5,<3` (was unbounded). A compatibility shim that vendors the four
attributes actually depended on (`code`, `taken_at`, `thumbnail_url`/`video_url`,
`resources`) would surface breakage in one place; not yet added. Most field
access uses `getattr(..., default)` defensively.

### 16.5 Comments capture was removed
The original tool captured per-post comments. In map-first design that's a paced
per-post API call (rate-limit-prone) and was dropped. Re-adding means a
`comments` table + a paced `media_comments` pass.

### 16.6 `--tor` stable circuit is still suboptimal
A single Tor exit is a distrusted datacenter IP; if it gets blocked there's no
rotation to fall back on (by design). A residential proxy via `--proxy` is more
reliable; `--tor` is for anonymity, accepting the reliability tradeoff.

### 16.7 Stories break the map-first invariant (asymmetry)
Stories/highlights are enumerate+download in one pass (signed URLs expire fast,
and the content is 24h-ephemeral), so they get **no resume guarantee** and their
diff accounting differs from posts'. This is the right call but is now stated
explicitly rather than left implicit.

### 16.8 Thread-safety invariant
Parallel downloads use bare `requests.get` (thread-safe). The only instagrapi
client access from workers is `_refresh_resources`, serialized under
`_refresh_lock`. Any future client call inside a worker must take the lock.

### 16.9 No CI / committed regression net beyond the fake-client tests
`tests/test_collect.py` covers the high-value paths but there's no CI pipeline.

### 16.10 Legal / ethical
Collects public data at scale with rate-limit evasion. README carries a
disclaimer; no in-app authorization gate.

---

## 17. What has been verified

- **ruff** (scounter + tests): all pass. **mypy**: no issues (9 source files).
- **`tests/test_collect.py`** — fake instagrapi client driving the real
  `collect()` with a mocked CDN:
  - `--max-posts` caps both items **and** pages (1 page fetched for cap=10).
  - Incremental bail skips page 2 after N consecutive seen items.
  - Album (sidecar) downloads item-level: `ALB1~1_...jpg`, `ALB1~2_...mp4`,
    both resources marked done.
  - 429 ride-through: one `RateLimitError` is absorbed; run completes;
    `total_429 >= 1`.
  - Shortcode-with-underscore round-trip (`ABC_123` survives; `~` index stripped).
- **`link`** e2e: resolves by shortcode, by album filename (`~` index), by date.
- **Auth**: `settings.json` is `chmod 0600` after dump.

---

## 18. Configuration reference

| Var | Default | Purpose |
|---|---|---|
| `SCOUNTER_SESSIONID` | — | Instagram `sessionid` cookie |
| `SCOUNTER_COOKIES` | — | `k=v; k=v` cookie string |
| `SCOUNTER_COOKIES_FILE` | — | Netscape cookie file |
| `SCOUNTER_TOR_SOCKS_PORT` | 9050 | Tor SOCKS5h port (for `--tor`) |

(`SCOUNTER_TOR_CONTROL_PORT` / `SCOUNTER_TOR_PASSWORD` were removed with rotation.)

---

## 19. Suggested next steps

**Done in revision 3:**
- Profile resolution **fallback chain** (primary → `search_users` → `user_info_v1`);
  real failures keep their type instead of masking as "profile not found".
- **`_upgrade_pic_url`** — `/sNNNxNNN/` → `/s1080x1080/` for full-res profile pics.
- Credential-input safety table + daemon-vs-Tor-Browser port note in the README.

**Done in revision 4 (this pass — git SHA `1178059`):**
- **Pacing moved to the transport layer** (`transport.py`): a `PacingAdapter`
  mounted on `cl.private`/`cl.public` paces every real HTTP request and records
  429/success; `_api` keeps only retry + exception translation.
- **Fixed the Q1 live bug**: persistent 429s were surfacing as
  `requests.RetryError` (urllib3 exhausted its `status_forcelist=[429]` retry),
  bypassing instagrapi's 429→`ClientThrottledError` mapping and thus AIMD.
  Replaced with `Retry(status=0, status_forcelist=[], respect_retry_after_header=False)`
  so every 429 reaches the adapter 1:1.
- **Pacer is now thread-safe** (lock on `delay`/`consecutive_429`/`total_429`) —
  the move exposes it to worker threads via `_refresh_resources`.
- **`install_pacing(session, pacer)`** is a standalone, engine-agnostic function
  (foresight for `MediaSource` extraction).
- **Fake-transport harness** (`tests/test_transport.py`): canned response
  sequences assert exact request counts, AIMD state, and the budget trip offline.
- **Five-cookie capture** in `auth.py`: the full set (`sessionid` + `ds_user_id`
  + `mid` + `ig_did` + `rur`) is captured when provided, even though instagrapi
  only uses `sessionid` — so a future `InstaloaderSource` can use its HD
  (iPhone API) path rather than being foreclosed by an accidentally-discarded
  cookie.
- **Doc/code drift guard**: the repo is now git-tracked; this doc carries an
  **artifact digest** of `scounter/**/*.py` (footer) — computable pre-commit,
  no self-reference — and a pre-commit hook fails if `scounter/**/*.py` is
  staged without `Tool.md` also staged. (Revision 4's commit-SHA stamp was
  wrong: it pointed at the parent commit, so `git show` returned the *old* code
  — a drift generator inside the anti-drift mechanism. Replaced with the digest.)

**Done in revision 5 (this pass):**
- **Token-bucket pacer** (capacity ~10, AIMD-adapted refill interval). The
  ephemeral phase drains the burst then throttles naturally, so the `light`
  flag dissolved (net deletion). AIMD is now on the refill interval
  (multiplicative decrease / additive increase).
- **Sleep is outside the lock** in `before_request` — verified by
  `test_before_request_does_not_hold_lock_during_sleep` (the failure mode that
  only bites on long-delayed resumes, where `--workers 4` would silently
  collapse to 1).
- **Characterization snapshot** (`test_collect_characterization_snapshot`): a
  fixed dataset run through `collect()`, manifest snapshotted to canonical JSON.
  This is the **precondition for the `InstagrapiSource` extraction** — the
  refactor's invariant is "manifest byte-identical before and after."
- **Five-cookie stash is in-memory only** (`InstagrapiSource.captured_cookies`),
  not a file — so it introduces no new on-disk credential. A future
  `InstaloaderSource` reads it in-process; nothing is persisted beyond the
  existing `settings.json` (already `0600`).

**Done in revision 6 (this pass — `InstagrapiSource` extraction):**
- **`MediaSource` Protocol + neutral types** (`source.py`): `MediaRow`,
  `ProfileInfo`, `HighlightInfo`, `ResumeToken`, and `SourceError` subtypes
  (`SourceAuthExpired`, `SourceUnavailable`). Cookie parsing lives here.
- **`InstagrapiSource`** (`instagrapi_source.py`) — the single engine
  implementation, owning auth (folded in from `auth.py`), `_media_row`/`_classify`/
  `_extract_resources`/`_upgrade_pic_url`/`install_pacing`, and instagrapi→neutral
  exception translation. `auth.py` deleted.
- **Collector is engine-neutral**: it imports no engine package and depends only
  on `MediaSource`. The CLI is the composition root (constructs `InstagrapiSource`,
  injects it). Verify: `grep -rln 'from instagrapi\|import instagrapi' scounter/`
  → **one file** (`instagrapi_source.py`). (The bare-substring grep also hits
  `cli.py`, which must name our `instagrapi_source` module — the composition root;
  that's the irreducible minimum.)
- **Cursor shape pre-decided & implemented**: `iter_media(uid, resume)` yields
  `(list[MediaRow], ResumeToken)` — an opaque, Source-owned token (instagrapi's
  `end_cursor`; a future web engine's count). The collector loop is
  engine-agnostic; step 3 won't reopen step 1.
- **Refactor invariant held**: the characterization snapshot is **byte-identical**
  before and after the extraction (verified — same canonical JSON). "Pure
  refactor" is a result, not an assertion.

**Done in revision 7 (this pass — full-cookie auth):**
- **Sign in with the FULL cookie set**, not just `sessionid`. The old path used
  instagrapi's `login_by_sessionid`, which regex-parses `ds_user_id` out of the
  sessionid (and asserts if it doesn't start with digits — a likely cause of
  "doesn't sign in"). `authenticate` now loads `sessionid + ds_user_id + mid +
  ig_did + rur` onto the session via `settings["cookies"]` + `authorization_data`
  + `init()`, using the real `ds_user_id` cookie. Bare `--sessionid` is a
  fallback. Verified offline (`test_authenticate_loads_full_cookie_set` +
  rejection tests). CLI/README now lead with `--cookies-file`.

**Done in revision 8 (this pass — Tor rotation re-added):**
- **NEWNYM circuit rotation restored** per explicit request, on the current
  infrastructure. New `tor.py` (`rotate_circuit` + `reset_connections`); the
  `MediaSource` Protocol gains `maybe_rotate()`; `InstagrapiSource` rotates every
  `--tor-rotate N` items (NEWNYM + close instagrapi keepalive pools so the new
  circuit takes effect). `--tor-rotate` flag + `SCOUNTER_TOR_CONTROL_PORT` /
  `SCOUNTER_TOR_PASSWORD` env vars restored; `stem` re-added to the `tor` extra.
- Rotation is driven from the collector's **main-thread** loops
  (enumerate/stories/highlights), never from the parallel download workers — no
  race with in-flight requests. CDN downloads don't rotate (not identity-bound).
- Verified: `test_maybe_rotate_cadence` (3 rotations over 10 items at N=3) and
  `test_maybe_rotate_off_when_disabled` (no rotations at N=0).
- **Honest caveat retained in the doc:** under cookie auth, scattering one
  identity across many Tor exits can itself trip Instagram's compromise
  detector. Rotation is opt-in (`--tor-rotate`, default 5; `0` = stable circuit).

**Still open (in priority order):**
1. **`quality` column** (`hd` | `degraded`) + an `--upgrade` pass that re-runs
   `refresh_urls` for degraded rows only — so a degraded capture (fallback
   engine) is recoverable, not a lowered bar. Stories/highlights degrade
   permanently (they expire) → log loudly.
2. **Write `InstaloaderSource`**, then `--engine` + the **differential test**
   (both engines, same profile, diff manifests → answer Q2 empirically: is the
   fallback genuinely degraded, or was "lower fidelity" an artifact of
   cookies-only-with-`sessionid`?).
3. **Live validation** with a real `sessionid`.
4. **Vendor a compat shim** for the four Media attributes depended on.
5. **Re-add comments capture** as an optional paced manifest-backed pass.
6. **CI** to lock in the `tests/` behaviors.

---

## 20. Resolved / open questions for the reviewer

1. **Transport-layer pacer (Q1) — RESOLVED.** Took the "third option" (not a/b):
   strip status-based retry from urllib3, keep transport-error retry there, put
   pacing+measurement in the adapter, leave rate-limit retry in `_api`. This
   also fixed the live `RetryError`-bypasses-`ClientThrottledError` bug.
   Verified offline with `tests/test_transport.py`. *One thing this does not
   fix:* accurate request count is honest pacing; it doesn't change that
   rotation under cookie auth harms the account — but rotation is already
   removed (§11), so N/A here.
2. **HD vs degraded fallback (Q2) — PARTIALLY ADDRESSED, needs the differential
   test.** The premise may be wrong: instaloader's HD path needs all five
   cookies (Tool 2 §4.3), and scounter had been discarding four of them.
   Five-cookie capture is now in place. The remaining step is the differential
   test (step 3 above) to answer empirically whether `InstaloaderSource` is
   genuinely degraded. If it is, the `quality` column + `--upgrade` pass (step 2)
   makes degradation recoverable rather than a lowered bar — except for
   stories/highlights, which expire.
3. **`MediaSource` scope (Q3) — RESOLVED: stage it.** Extract `InstagrapiSource`
   first (pure refactor, tests green), then five-cookie + quality column, then
   `InstaloaderSource`, then `--engine` + differential test. The Source returns
   manifest-shaped dicts and never leaks an engine object.

---

*Technical review document for `scounter` v0.1.0 — revision 8.

**Code integrity:** `find scounter -name '*.py' | sort | xargs sha256sum | sha256sum`
→ `b5a6a805f0a37ae9c397d99d20fff99a0df7fade9419179e2622116e3ef4364f`.
This digest describes exactly the code this doc describes — computable
pre-commit, no self-reference (unlike a commit SHA, which a file can't contain
for its own commit). A pre-commit hook fails if any `scounter/**/*.py` is staged
without `Tool.md` also staged, so the doc travels in the same commit as the
behavior it describes; `git log -1 -- Tool.md` then answers "which code does
this describe." Regenerate the digest whenever `scounter/**` changes.*
