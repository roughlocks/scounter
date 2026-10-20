"""Transport-layer pacing tests: a fake HTTP transport returning canned response
sequences, asserting exact request counts, AIMD state, and the
RateLimitExhausted budget — offline, no network, no instagrapi.

This is the harness the pacer move is verifiable with: once urllib3 status-retry
is removed, a fake adapter returning (200, 200, 429, 429, 200...) lets us assert
exact request counts, sleep totals, and AIMD state.
"""

from __future__ import annotations

import time
import types
from unittest.mock import patch

import requests
from requests.adapters import HTTPAdapter

from scounter.pacing import Pacer, RateLimitExhausted
from scounter.transport import PacingAdapter, _build_retry


def _resp(status: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(status_code=status)


def _prep() -> requests.PreparedRequest:
    return requests.Request("GET", "http://example.test/").prepare()


def test_retry_is_transport_only_not_status():
    r = _build_retry()
    assert r.status == 0
    assert list(r.status_forcelist) == []
    assert getattr(r, "respect_retry_after_header", False) is False
    print("retry config: status=0, empty forcelist, retry-after off")


def test_adapter_counts_1to1_and_adapts():
    pacer = Pacer(base_interval=0.001, capacity=10, step=0.001,
                  max_consecutive_429=3, max_total_429=100)
    start = pacer.interval
    adapter = PacingAdapter(pacer)
    preq = _prep()

    # one 429 -> total_429=1, consecutive=1, interval doubled (multiplicative decrease)
    with patch.object(HTTPAdapter, "send", return_value=_resp(429)):
        adapter.send(preq)
    assert pacer.total_429 == 1 and pacer.consecutive_429 == 1
    assert pacer.interval > start

    # one 200 -> consecutive reset, additive recovery (interval - step)
    i_before = pacer.interval
    with patch.object(HTTPAdapter, "send", return_value=_resp(200)):
        adapter.send(preq)
    assert pacer.consecutive_429 == 0
    assert pacer.interval == max(pacer.base_interval, i_before - pacer.step)

    # 5xx is neither 429 nor 2xx -> pacer untouched
    with patch.object(HTTPAdapter, "send", return_value=_resp(500)):
        adapter.send(preq)
    assert pacer.total_429 == 1
    print("adapter 1:1 + AIMD: OK")


def test_adapter_trips_at_budget():
    pacer = Pacer(base_interval=0.001, capacity=10, step=0.001,
                  max_consecutive_429=3, max_total_429=100)
    adapter = PacingAdapter(pacer)
    preq = _prep()
    raised = False
    with patch.object(HTTPAdapter, "send", return_value=_resp(429)):
        for _ in range(3):
            try:
                adapter.send(preq)
            except RateLimitExhausted:
                raised = True
                break
    assert raised, "expected RateLimitExhausted after 3 consecutive 429s"
    assert pacer.consecutive_429 >= 3
    print("budget trip: OK")


def test_adapter_counts_exact_requests():
    pacer = Pacer(base_interval=0.001, capacity=10, step=0.001,
                  max_consecutive_429=99, max_total_429=99)
    adapter = PacingAdapter(pacer)
    preq = _prep()
    seq = [_resp(200), _resp(200), _resp(429), _resp(200)]
    it = iter(seq)
    with patch.object(HTTPAdapter, "send", side_effect=lambda req, **kw: next(it)):
        for _ in seq:
            adapter.send(preq)
    # 4 sends, exactly one 429 recorded; ended on a success so consecutive reset.
    assert pacer.total_429 == 1
    assert pacer.consecutive_429 == 0
    print("exact request count: OK (4 sends, 1 recorded 429)")


def test_before_request_does_not_hold_lock_during_sleep():
    """Workers must not serialize behind each other's sleeps.

    A token-bucket pacer with capacity 0 always sleeps ~interval; if that sleep
    held the lock, a second thread could not acquire it. This is the failure
    mode that only bites on long-delayed resumes (--workers collapses to 1).
    """
    import threading

    pacer = Pacer(base_interval=0.3, capacity=1, step=0.001,
                  max_consecutive_429=99, max_total_429=99)
    pacer.before_request()                    # drain the 1 token so the next call must wait
    done = threading.Event()

    def worker():
        pacer.before_request()                # sleeps ~0.3s for a refill
        done.set()

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.05)                          # let the worker enter its sleep
    got = pacer._lock.acquire(timeout=0.1)    # would block if the worker held it
    if got:
        pacer._lock.release()
    done.wait(2.0)
    t.join()
    assert got, "before_request held the lock during sleep -> workers would serialize"
    print("lock-not-held-during-sleep: OK (workers don't serialize)")


if __name__ == "__main__":
    test_retry_is_transport_only_not_status()
    test_adapter_counts_1to1_and_adapts()
    test_adapter_trips_at_budget()
    test_adapter_counts_exact_requests()
    test_before_request_does_not_hold_lock_during_sleep()
    print("\nALL TRANSPORT TESTS PASSED")
