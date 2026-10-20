"""Token-bucket pacing with honest AIMD, for authenticated private-API traffic.

A token bucket (capacity ~10, refill 1 token per `interval` seconds) gives a
short ephemeral phase (stories/highlights) a burst to drain at full speed before
throttling naturally — which is what the old `light` flag was a hand-rolled
patch for. With a bucket, the flag dissolves: no per-call context to thread.

AIMD adapts the **refill interval**:
  - 429  → **multiplicative decrease** (`interval *= 2`).
  - success → **additive increase** (`interval -= step`).

A 429 trips ``RateLimitExhausted`` on *either* a consecutive streak **or** a
run-wide total budget (an alternating 429/success pattern can't retry forever).

Pacing lives at the transport layer (:mod:`scounter.transport`): a
``PacingAdapter`` calls ``before_request`` per real HTTP request and records
429/success. Because the adapter is hit from worker threads, all pacer state is
guarded by a lock — and the pre-request *sleep is outside the lock*, so workers
don't serialize behind each other's waits.

State (interval + 429 counters) is checkpointed into the manifest's ``run_state``
table. The bucket level is ephemeral (a resume starts with a full bucket).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scounter.storage import Manifest


class RateLimitExhausted(Exception):
    """Raised when the 429 budget is exhausted: stop and resume later."""


class Pacer:
    """Token-bucket pacer with honest AIMD and a consecutive + total 429 budget."""

    def __init__(
        self,
        base_interval: float = 4.0,
        capacity: int = 10,
        step: float = 0.5,
        max_interval: float = 900.0,
        max_consecutive_429: int = 5,
        max_total_429: int = 50,
        logger: logging.Logger | None = None,
    ) -> None:
        self.base_interval = base_interval
        self.capacity = capacity
        self.step = step
        self.max_interval = max_interval
        self.max_consecutive_429 = max_consecutive_429
        self.max_total_429 = max_total_429
        self.logger = logger or logging.getLogger("scounter")
        self.interval = base_interval          # AIMD-adapted per-token interval
        self.consecutive_429 = 0
        self.total_429 = 0
        self._lock = threading.RLock()
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._manifest: Manifest | None = None
        self._target: str | None = None
        self.load()

    def reset(
        self,
        base_interval: float,
        max_consecutive_429: int,
        max_total_429: int,
        manifest: Manifest,
        target: str,
    ) -> None:
        """Reconfigure for a new target/run, then load any checkpoint."""
        self.base_interval = base_interval
        self.max_consecutive_429 = max_consecutive_429
        self.max_total_429 = max_total_429
        self._manifest = manifest
        self._target = target
        with self._lock:
            self.interval = base_interval
            self.consecutive_429 = 0
            self.total_429 = 0
            self._tokens = float(self.capacity)   # resume starts with a full bucket
            self._last = time.monotonic()
        self.load()

    def load(self) -> None:
        if self._manifest is None or self._target is None:
            return
        blob = self._manifest.get_state(self._target, "pacer")
        if not blob:
            return
        try:
            state = json.loads(blob)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("Pacer checkpoint load failed: %s", exc)
            return
        with self._lock:
            self.interval = float(state.get("interval", self.base_interval))
            self.consecutive_429 = int(state.get("consecutive_429", 0))
            self.total_429 = int(state.get("total_429", 0))
        self.logger.info(
            "Resumed pacer: interval=%.1fs (total 429s=%d)", self.interval, self.total_429
        )

    def save(self) -> None:
        if self._manifest is None or self._target is None:
            return
        with self._lock:
            blob = json.dumps({
                "interval": self.interval,
                "consecutive_429": self.consecutive_429,
                "total_429": self.total_429,
                "last_saved": time.time(),
            })
        self._manifest.set_state(self._target, "pacer", blob)

    def _refill_locked(self) -> None:
        now = time.monotonic()
        self._tokens = min(
            float(self.capacity), self._tokens + (now - self._last) / self.interval
        )
        self._last = now

    def before_request(self) -> float:
        """Consume one token, waiting for one if the bucket is empty.

        The lock is held only for the refill/check/consume — the sleep is
        *outside* the lock so worker threads don't serialize behind each other.
        """
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return 0.0
                deficit = 1.0 - self._tokens      # tokens needed to reach 1
                wait = deficit * self.interval
            time.sleep(wait)                       # NOT under the lock

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_429 = 0
            self.interval = max(self.base_interval, self.interval - self.step)  # additive

    def record_429(self) -> None:
        """Multiplicative decrease. Trip on a consecutive streak OR the total budget."""
        with self._lock:
            self.consecutive_429 += 1
            self.total_429 += 1
            self.interval = min(self.max_interval, self.interval * 2)  # multiplicative
            trip = (
                self.consecutive_429 >= self.max_consecutive_429
                or self.total_429 >= self.max_total_429
            )
        if trip:
            self.save()
            self.logger.error(
                "Rate limit exhausted (consecutive=%d, total=%d). Checkpoint saved. Resume later.",
                self.consecutive_429, self.total_429,
            )
            raise RateLimitExhausted(
                f"429 budget exhausted (consecutive={self.consecutive_429}, "
                f"total={self.total_429}). interval -> {self.interval:.1f}s. Resume later."
            )
        self.logger.warning(
            "429: interval -> %.1fs (consecutive %d/%d, total %d/%d), riding through",
            self.interval, self.consecutive_429, self.max_consecutive_429,
            self.total_429, self.max_total_429,
        )
