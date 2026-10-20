"""Command-line interface for scounter (cookies-only, full-HD)."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from scounter import __version__
from scounter.collector import CollectionOptions, Collector
from scounter.instagrapi_source import InstagrapiSource
from scounter.reporter import write_reports
from scounter.storage import Manifest
from scounter.utils import sanitize, setup_logging, shortcode_from_filename

IG_POST_URL = "https://www.instagram.com/p/{code}/"


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-o", "--output", default="output", help="Output directory (default: ./output)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scounter",
        description=(
            "Cookies-only, full-HD Instagram archiver. Maps out a profile's "
            "posts, reels, stories, highlights and tagged posts, then downloads "
            "everything in parallel. Also exposes a `link` command to resolve a "
            "downloaded file/shortcode/date back to its Instagram URL."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"scounter {__version__}")
    sub = parser.add_subparsers(dest="command")

    # ----- collect ----------------------------------------------------------
    collect = sub.add_parser(
        "collect",
        help="Collect media from an Instagram profile",
        description=(
            "Collect media from an Instagram profile. Stories and highlights are "
            "grabbed first (they expire), then posts/reels/tagged are enumerated "
            "into a manifest and downloaded in parallel."
        ),
        epilog=(
            "Examples:\n"
            "  scounter collect natgeo --cookies-file cookies.txt --everything\n"
            "  SCOUNTER_SESSIONID=... scounter collect natgeo --everything\n"
            "  scounter collect natgeo --everything --request-delay 8   # polite long run\n"
            "  scounter collect natgeo --fast-update                    # resume\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    collect.add_argument("target", help="Instagram username (or numeric user id) to collect")
    _common(collect)

    auth = collect.add_argument_group("authentication (cookies-only)")
    auth.add_argument("--cookies-file", default=None,
                      help="Netscape cookie file (e.g. from 'Get cookies.txt') with the "
                           "FULL set: sessionid + ds_user_id + mid + ig_did + rur. "
                           "RECOMMENDED — the full set is what actually authenticates. "
                           "Or set SCOUNTER_COOKIES_FILE.")
    auth.add_argument("--cookies", default=None,
                      help="Full cookie set as 'k=v; k=v; ...' (sessionid + ds_user_id + "
                           "mid + ig_did + rur). Or set SCOUNTER_COOKIES.")
    auth.add_argument("--sessionid", default=None,
                      help="Bare 'sessionid' cookie only — FALLBACK: may be rejected "
                           "without ds_user_id. Prefer --cookies-file. Or set "
                           "SCOUNTER_SESSIONID.")

    net = collect.add_argument_group("proxy & network")
    net_choice = net.add_mutually_exclusive_group()
    net_choice.add_argument("--proxy", metavar="URL", default=None,
                            help="Route requests through a stable proxy URL, e.g. "
                                 "http://user:pass@host:port or socks5h://127.0.0.1:9050. "
                                 "Use a residential proxy in the account's region for reliability.")
    net_choice.add_argument("--tor", action="store_true",
                            help="Route through Tor (SOCKS5h). With --tor-rotate, circuits are "
                                 "rotated (NEWNYM) every N items. Needs Tor running "
                                 "(daemon port 9050, or Tor Browser 9150 via "
                                 "SCOUNTER_TOR_SOCKS_PORT).")
    net.add_argument("--tor-rotate", type=int, default=5, metavar="N",
                     help="With --tor: build a new Tor circuit (NEWNYM) every N items "
                          "(default: 5; set 0 to disable rotation and use one stable circuit). "
                          "Requires the Tor control port (SCOUNTER_TOR_CONTROL_PORT; "
                          "9051 daemon / 9151 Tor Browser) and stem (pip install scounter[tor]).")

    scope = collect.add_argument_group("what to collect")
    scope.add_argument("--everything", action="store_true",
                       help="Collect ALL artifacts (equivalent to all --no-* off)")
    scope.add_argument("--no-posts", action="store_true")
    scope.add_argument("--no-reels", action="store_true")
    scope.add_argument("--no-stories", action="store_true")
    scope.add_argument("--no-highlights", action="store_true")
    scope.add_argument("--no-tagged", action="store_true")
    scope.add_argument("--no-profile-pic", action="store_true")
    scope.add_argument("--no-metadata", action="store_true", help="Skip writing metadata.json / report.md")

    tune = collect.add_argument_group("tuning")
    tune.add_argument("--fast-update", action="store_true",
                      help="Skip media already on disk (resume support)")
    tune.add_argument("--max-posts", type=int, default=None,
                      help="Cap number of posts/reels/tagged enumerated")
    tune.add_argument("--request-delay", type=float, default=4.0, metavar="SECONDS",
                      help="Base interval between API requests; AIMD-adapted on 429 (default: 4).")
    tune.add_argument("--max-429", type=int, default=5, metavar="N",
                      help="Stop after N consecutive 429s and checkpoint for resume (default: 5).")
    tune.add_argument("--max-total-429", type=int, default=50, metavar="N",
                      help="Run-wide 429 budget; stop after N total 429s (default: 50). "
                           "Guards against an alternating 429/success loop retrying forever.")
    tune.add_argument("--seen-bail", type=int, default=150, metavar="N",
                      help="Incremental run: stop enumerating after N consecutive already-seen "
                           "items (default: 150, ~3 pages). The feed is newest-first.")
    tune.add_argument("--max-attempts", type=int, default=3, metavar="N",
                      help="Dead-letter a download after N failures (default: 3).")
    tune.add_argument("--workers", type=int, default=4, metavar="N",
                      help="Parallel CDN download workers for media (default: 4).")

    # ----- link -------------------------------------------------------------
    link = sub.add_parser(
        "link",
        help="Resolve a file/shortcode/date to its Instagram URL",
        description=(
            "Look up a previously collected item and print its Instagram URL and "
            "local path. Accepts a shortcode, a downloaded filename, or a date "
            "(YYYY-MM-DD / YYYYMMDD)."
        ),
    )
    link.add_argument("query", help=" shortcode, downloaded filename, or date")
    link.add_argument("--target", default=None,
                      help="Restrict search to one target (else all targets under --output are searched)")
    _common(link)

    return parser


def _options_from_args(args: argparse.Namespace) -> CollectionOptions:
    everything = args.everything
    return CollectionOptions(
        profile_pic=everything or not args.no_profile_pic,
        posts=everything or not args.no_posts,
        reels=everything or not args.no_reels,
        stories=everything or not args.no_stories,
        highlights=everything or not args.no_highlights,
        tagged=everything or not args.no_tagged,
        metadata=everything or not args.no_metadata,
        fast_update=args.fast_update,
        max_posts=args.max_posts,
        request_delay=args.request_delay,
        max_consecutive_429=args.max_429,
        max_total_429=args.max_total_429,
        seen_bail=args.seen_bail,
        max_attempts=args.max_attempts,
        workers=args.workers,
    )


def _resolve_cookies(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "sessionid": args.sessionid or os.environ.get("SCOUNTER_SESSIONID"),
        "cookies_str": args.cookies or os.environ.get("SCOUNTER_COOKIES"),
        "cookies_file": args.cookies_file or os.environ.get("SCOUNTER_COOKIES_FILE"),
    }


def _run_collect(args: argparse.Namespace) -> int:
    output_root = Path(args.output)
    target_dir = output_root / sanitize(args.target)
    target_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logging(log_file=target_dir / "collection.log", verbose=args.verbose)
    logger.info("scounter %s — target @%s", __version__, args.target)

    options = _options_from_args(args)
    cookies = _resolve_cookies(args)
    if args.sessionid:
        logger.warning(
            "--sessionid on the command line is visible to other users via `ps`. "
            "Prefer --cookies-file (or the SCOUNTER_SESSIONID env var, which is still "
            "readable via /proc). A Netscape cookie file with mode 0600 is the safest path."
        )
    # Composition root: the concrete engine is constructed here and injected into
    # the (engine-neutral) Collector. collector/source/storage import no engine.
    socks_port = int(os.environ.get("SCOUNTER_TOR_SOCKS_PORT", 9050))
    proxy_url = args.proxy or (f"socks5h://127.0.0.1:{socks_port}" if args.tor else None)
    tor_rotate = args.tor_rotate if args.tor else 0
    source = InstagrapiSource(
        proxy=proxy_url,
        session_dir=output_root / ".sessions",
        tor_rotate=tor_rotate,
        tor_control_port=int(os.environ.get("SCOUNTER_TOR_CONTROL_PORT", 9051)),
        tor_password=os.environ.get("SCOUNTER_TOR_PASSWORD"),
        logger=logger,
    )
    collector = Collector(
        output_root=output_root,
        source=source,
        sessionid=cookies["sessionid"],
        cookies_str=cookies["cookies_str"],
        cookies_file=cookies["cookies_file"],
        workers=args.workers,
        logger=logger,
    )

    try:
        result = collector.collect(args.target, options)
    except KeyboardInterrupt:
        logger.warning("Interrupted by user")
        return 130
    except Exception as exc:  # noqa: BLE001 - CookiesExpired lands here too
        logger.error("%s", exc)
        return 3 if "cookies" in str(exc).lower() or "sessionid" in str(exc).lower() else 1

    if options.metadata:
        paths = write_reports(result, output_root)
        logger.info("Reports written:")
        for name, path in paths.items():
            logger.info("  %s: %s", name, path)
    logger.info("Collection complete. %s", result.counts)
    if "interrupted" in result.errors:
        return 130
    return 0 if not result.errors else 2


def _manifest_db_paths(output_root: Path, target: str | None) -> list[Path]:
    if target:
        p = output_root / sanitize(target) / "manifest.db"
        return [p] if p.exists() else []
    return sorted(output_root.glob("*/manifest.db"))


def _is_date(query: str) -> bool:
    q = query.strip()
    digits = q.replace("-", "")
    if digits.isdigit() and len(digits) in (4, 6, 8):
        return True
    import re
    return bool(re.match(r"^\d{4}(-\d{2}(-\d{2})?)?$", q))


def _date_prefix(query: str) -> str:
    """Normalize a date query to the ISO-ish prefix stored in the manifest."""
    q = query.strip()
    digits = q.replace("-", "")
    if not digits.isdigit():
        return q
    if len(digits) == 4:
        return digits
    if len(digits) == 6:
        return f"{digits[:4]}-{digits[4:]}"
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return q


def _run_link(args: argparse.Namespace) -> int:
    output_root = Path(args.output)
    dbs = _manifest_db_paths(output_root, args.target)
    if not dbs:
        print("No manifest.db found. Run `scounter collect` first.", file=sys.stderr)
        return 1

    query = args.query.strip()
    found = False

    if _is_date(query):
        prefix = _date_prefix(query)
        for db in dbs:
            m = Manifest(db)
            for drow in m.find_by_date(prefix):
                _print_link(db.parent.name, drow)
                found = True
    else:
        looks_like_file = "." in query or "/" in query or "\\" in query
        # Primary source of truth: the manifest's local_path (exact match).
        if looks_like_file:
            for db in dbs:
                m = Manifest(db)
                row: sqlite3.Row | None = m.find_by_local_path(query)
                if row:
                    _print_link(db.parent.name, row)
                    found = True
                    break
        # Fallback: parse the shortcode out of the filename / use the query as one.
        if not found:
            code = (shortcode_from_filename(query) or query) if looks_like_file else query
            for db in dbs:
                m = Manifest(db)
                row = m.find_by_shortcode(code)
                if row:
                    _print_link(db.parent.name, row)
                    found = True
                    break
                    break

    if not found:
        print(f"No match for {query!r}.", file=sys.stderr)
        return 1
    return 0


def _print_link(target_name: str, row: sqlite3.Row) -> None:
    code = row["shortcode"]
    url = IG_POST_URL.format(code=code) if code else "(no public URL — story/highlight)"
    print(f"target : {target_name}")
    print(f"kind   : {row['kind']}")
    print(f"date   : {row['date_utc']}")
    print(f"ig url : {url}")
    print(f"local  : {row['local_path'] or '(not downloaded)'}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "collect":
        return _run_collect(args)
    if args.command == "link":
        return _run_link(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
