"""Bounded observation waits: finite, cancellable, and never a claim on the work."""

from __future__ import annotations

import asyncio
import time

# One explicitly requested observation may cover a long peer turn, so a coordinator
# that cannot do other work meanwhile pays one model turn instead of dozens. The
# server never negotiates the client's own request deadline: a caller asking for
# more than its transport allows still loses the response to its own timeout.
MAX_WAIT_SECONDS = 1200

MODES = ("auto", "bounded")

# A wait this long or shorter keeps the original cadence, so the established
# short-wait path detects a change exactly as promptly as it used to.
_BRISK_SECONDS = 30
_BRISK = 0.15

# Steps a longer wait takes before it looks again, by how long it has already
# waited. A twenty-minute observation must not read the store eight thousand times.
_STEPS = ((5.0, _BRISK), (30.0, 0.5), (120.0, 1.0))
_SLOWEST = 2.0

# However long a read may be deferred, a cancelled caller is noticed within this.
CANCELLATION_CADENCE = 0.2


def step_interval(elapsed: float) -> float:
    """How long to wait before looking again, given how long we have waited already."""
    for boundary, step in _STEPS:
        if elapsed < boundary:
            return step
    return _SLOWEST


class Observation:
    """One bounded wait over somebody else's state; it observes, it does not hold."""

    def __init__(self, requested_seconds: int, mode: str = "auto"):
        if mode not in MODES:
            # An unrecognised mode must not quietly become the stricter behaviour.
            raise ValueError(f"wait_mode must be one of {', '.join(MODES)}")
        self.requested = requested_seconds
        self.mode = mode
        self._started = time.monotonic()
        self._deadline = self._started + requested_seconds
        self._observed = time.time()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self._deadline

    @property
    def follows_delivery(self) -> bool:
        """Auto returns as soon as delivery can wake the caller; bounded waits for the state."""
        return self.mode == "auto"

    def observed(self) -> None:
        """Mark the moment state was actually read, which is what the caller learns about."""
        self._observed = time.time()

    async def pause(self) -> None:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            return
        interval = (
            step_interval(self.elapsed) if self.requested > _BRISK_SECONDS else _BRISK
        )
        await asyncio.sleep(min(interval, remaining))

    def record(self, result: dict, reason: str) -> dict:
        """Describe the wait that just ended. Expiry is an observation, not a task state."""
        if not self.requested:
            return result
        result["wait"] = {
            "mode": self.mode,
            "requested_seconds": self.requested,
            # Equal to the request today; an enclosing deadline could shorten it.
            "effective_seconds": self.requested,
            "elapsed_seconds": round(self.elapsed, 2),
            "reason": reason,
            "observed_at": self._observed,
        }
        return result
