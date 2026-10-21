"""Task lock with an acquisition generation and monotonic progress timestamps."""
from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Callable


@dataclass(frozen=True)
class RunSnapshot:
    generation: int
    started_at: float
    progressed_at: float


class RunLock:
    """Primitive-lock semantics, including handoff to a worker for release.

    Metadata changes on every acquire/release, so watchdog samples cannot miss
    a completed task and accidentally attribute its age to a newer operation.
    """
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._metadata = threading.RLock()
        self._clock = clock
        self._generation = 0
        self._snapshot: RunSnapshot | None = None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            with self._metadata:
                self._generation += 1
                now = self._clock()
                self._snapshot = RunSnapshot(self._generation, now, now)
        return acquired

    def release(self) -> None:
        with self._metadata:
            if not self._lock.locked():
                raise RuntimeError("release unlocked lock")
            self._snapshot = None
            self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    def snapshot(self) -> RunSnapshot | None:
        with self._metadata:
            return self._snapshot

    def progress(self) -> None:
        with self._metadata:
            current = self._snapshot
            if current is not None:
                self._snapshot = RunSnapshot(
                    current.generation, current.started_at, self._clock()
                )

    def stalled(self, seconds: float) -> RunSnapshot | None:
        with self._metadata:
            current = self._snapshot
            if current is not None and self._clock() - current.progressed_at >= seconds:
                return current
        return None

    def if_still_stalled(self, expected: RunSnapshot, seconds: float, action: Callable) -> bool:
        # The final decision and callback are serialized with release/progress.
        with self._metadata:
            if self._snapshot != expected or self._clock() - expected.progressed_at < seconds:
                return False
            action()
            return True

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()


run_lock = RunLock()


def progress() -> None:
    """Call only after observable task progress, never from a heartbeat timer."""
    run_lock.progress()
