from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# How far back we look for repetition, and how small/large a repeating
# block has to be to count. A short min length avoids flagging single
# repeated characters/punctuation runs; the max keeps the KMP pass cheap.
_TAIL_CHARS = 400
_MIN_PERIOD = 8
_MIN_REPEATS = 3
_BUFFER_CAP = 4000


@dataclass
class LoopAlert:
    snippet: str
    window: int
    repeats: int


class RepetitionDetector:
    """Flags a stuck generation by watching for a block of text tiling
    itself several times in a row. Pure string matching, no model calls.

    Finds the period of the trailing window via the KMP prefix function
    (period = window_len - lps[-1]) rather than checking a fixed list of
    block sizes, so it catches a repeating phrase of any length."""

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> LoopAlert | None:
        if not text:
            return None
        self._buffer = (self._buffer + text)[-_BUFFER_CAP:]
        return self._check()

    def _check(self) -> LoopAlert | None:
        buf = self._buffer
        window = min(len(buf), _TAIL_CHARS)
        if window < _MIN_PERIOD * _MIN_REPEATS:
            return None
        tail = buf[-window:]

        lps = [0] * window
        length = 0
        for i in range(1, window):
            while length and tail[i] != tail[length]:
                length = lps[length - 1]
            if tail[i] == tail[length]:
                length += 1
            lps[i] = length

        period = window - lps[-1]
        if period < _MIN_PERIOD or window % period != 0:
            return None
        repeats = window // period
        if repeats < _MIN_REPEATS:
            return None

        block = tail[:period]
        if not block.strip():
            return None
        if not all(tail[i * period : (i + 1) * period] == block for i in range(repeats)):
            return None
        return LoopAlert(snippet=block, window=period, repeats=repeats)


@dataclass
class StreamGuard:
    id: int
    phase: str
    detector: RepetitionDetector = field(default_factory=RepetitionDetector)
    alert: LoopAlert | None = None
    alert_at: float = 0.0
    seq: int = 0
    acked_seq: int = -1
    stop_requested: bool = False


_next_id = 0
_active: StreamGuard | None = None


def start_stream(phase: str) -> StreamGuard:
    global _next_id, _active
    _next_id += 1
    _active = StreamGuard(id=_next_id, phase=phase)
    return _active


def feed(guard: StreamGuard, text: str) -> None:
    result = guard.detector.feed(text)
    if result is not None:
        if guard.alert is None:
            guard.seq += 1
            guard.alert_at = time.time()
        guard.alert = result
    else:
        guard.alert = None


def status() -> dict[str, Any]:
    guard = _active
    if guard is None or guard.alert is None:
        return {"active": False, "should_alert": False}
    return {
        "active": True,
        "should_alert": guard.seq > guard.acked_seq,
        "id": guard.id,
        "phase": guard.phase,
        "seq": guard.seq,
        "snippet": guard.alert.snippet,
        "window": guard.alert.window,
        "repeats": guard.alert.repeats,
        "since": guard.alert_at,
    }


def request_stop(guard_id: int) -> bool:
    guard = _active
    if guard is None or guard.id != guard_id:
        return False
    guard.stop_requested = True
    return True


def ack(guard_id: int, seq: int) -> bool:
    guard = _active
    if guard is None or guard.id != guard_id:
        return False
    guard.acked_seq = max(guard.acked_seq, seq)
    return True
