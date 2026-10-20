"""Transport-layer pacing: pace every *actual* HTTP request, not method calls.

Why this exists (the bug it fixes): the engine's default session adapter mounts
``Retry(status_forcelist=[429, 500, 502, 503, 504])``. A persistent 429 is
retried by urllib3 and, on exhaustion, raises ``MaxRetryError`` →
``requests.exceptions.RetryError`` — which is *not* a ``requests.HTTPError``,
so the engine's 429→throttled-error mapping (which fires only after
``raise_for_status``) never runs. The result: persistent 429s bypass the AIMD
layer entirely and surface as fatal/masked errors.

Fix (the "third option"): retry only **transport** errors (connect/read) in
urllib3, never status codes. Every 429/5xx now reaches us as a real ``Response``
— one adapter ``send()`` per actual HTTP request, 1:1. ``PacingAdapter.send``
does the pre-request sleep and records 429/success on the way out; ``_api``
keeps retry policy, AIMD, and exception translation. Mechanism in the adapter,
policy in ``_api``, one retry loop total.

``respect_retry_after_header=False`` so Instagram's ``Retry-After`` header
doesn't cause an invisible, potentially multi-minute sleep inside what the
caller thinks is one request.

``install_pacing`` is engine-agnostic (any ``requests.Session``); when
``InstagrapiSource`` is extracted, this call moves into it unchanged, and a
future ``InstaloaderSource`` calls the same function against instaloader's
session.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

if TYPE_CHECKING:
    from scounter.pacing import Pacer


def _build_retry() -> Retry:
    """Retry transport errors only — never status codes.

    ``status=0`` + empty ``status_forcelist`` means every 4xx/5xx is returned
    to the adapter as a real response (so the pacer sees it 1:1).
    """
    kwargs: dict = dict(
        total=3, connect=3, read=3, status=0, status_forcelist=[], raise_on_status=False
    )
    try:
        return Retry(respect_retry_after_header=False, **kwargs)
    except TypeError:
        # Older urllib3 without respect_retry_after_header.
        return Retry(**kwargs)


class PacingAdapter(HTTPAdapter):
    """Pace every HTTP request (pre-request sleep) and record 429/success.

    The pre-request sleep is the rate pacer; recording is the measurement.
    Both live here so the pacer counts real HTTP requests, including any
    internal auth-refresh/follow-up requests a method call may issue.
    """

    def __init__(self, pacer: Pacer, **kwargs) -> None:
        self.pacer = pacer
        kwargs.setdefault("max_retries", _build_retry())
        super().__init__(**kwargs)

    def send(self, request, **kwargs):  # type: ignore[override]
        self.pacer.before_request()               # rate sleep (lock not held during sleep)
        resp = super().send(request, **kwargs)    # transport-only retry inside
        if resp.status_code == 429:
            self.pacer.record_429()               # may raise RateLimitExhausted -> propagates
        elif 200 <= resp.status_code < 300:
            self.pacer.record_success()
        # 4xx/5xx (non-429): don't touch the pacer; let _api handle the exception.
        return resp


def install_pacing(session, pacer: Pacer) -> None:
    """Mount a PacingAdapter on a requests.Session, replacing existing adapters."""
    adapter = PacingAdapter(pacer)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
