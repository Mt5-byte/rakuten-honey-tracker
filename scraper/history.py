"""Change-only rate history.

data/history/<site>.json  ->  {"v": 1, "stores": {"<store id>": [[ts, rate, kind, extra], ...]}}

Each entry says "from <ts> the store paid <rate> (<kind>)". A new entry is only appended when
something changes, so a year of twice-daily checks stays small. rate = null means no reward
(or the store was not listed). `extra` carries the Rakuten "was X%" baseline for boosts.
"""
from __future__ import annotations

from statistics import median

DAY = 86400


def new_history() -> dict:
    return {"v": 1, "stores": {}}


def record(hist: dict, sid: str, ts: int, rate: float | None, kind: str | None, extra=None) -> bool:
    """Append a change; returns True when a new entry was written."""
    entries = hist["stores"].setdefault(sid, [])
    if entries:
        last = entries[-1]
        if last[1] == rate and last[2] == kind and last[3] == extra:
            return False
        if ts <= last[0]:
            ts = last[0] + 1
    entries.append([ts, rate, kind, extra])
    return True


def seed(hist: dict, sid: str, points: list[tuple]) -> None:
    """Backfill a store that has no history yet with known earlier offers (ascending by ts)."""
    if hist["stores"].get(sid):
        return
    entries: list = []
    for ts, rate, kind, extra in sorted(points, key=lambda p: p[0]):
        if entries and entries[-1][1] == rate and entries[-1][2] == kind and entries[-1][3] == extra:
            continue
        if entries and ts <= entries[-1][0]:
            ts = entries[-1][0] + 1
        entries.append([int(ts), rate, kind, extra])
    if entries:
        hist["stores"][sid] = entries


def segments(entries: list, now: int):
    for i, e in enumerate(entries):
        start = e[0]
        end = entries[i + 1][0] if i + 1 < len(entries) else now
        yield start, max(end, start), e[1], e[2]


def _weighted_median(pairs: list[tuple[float, float]]) -> float | None:
    """pairs of (value, weight)."""
    pairs = [(v, w) for v, w in pairs if w > 0]
    if not pairs:
        return None
    pairs.sort()
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def stats(entries: list, now: int, window_days: int = 90) -> dict:
    """Highs, typical rate and change info for the store's current reward kind."""
    if not entries:
        return {}
    cur = entries[-1]
    kind = cur[2]
    cutoff = now - window_days * DAY
    high_all = None
    high_win = None
    weights: list[tuple[float, float]] = []
    for start, end, rate, k in segments(entries, now):
        if rate is None or k != kind:
            continue
        if high_all is None or rate > high_all[0]:
            high_all = (rate, start)
        if end > cutoff:
            if high_win is None or rate > high_win[0]:
                high_win = (rate, start)
            weights.append((rate, min(end, now) - max(start, cutoff)))
    prev_rate = None
    for e in reversed(entries[:-1]):
        if e[1] != cur[1] or e[2] != cur[2]:
            prev_rate = e[1] if e[2] == kind else None
            break
    return {
        "high90": high_win[0] if high_win else None,
        "high90_at": high_win[1] if high_win else None,
        "highAll": high_all[0] if high_all else None,
        "highAll_at": high_all[1] if high_all else None,
        "typical": _weighted_median(weights),
        "first_seen": entries[0][0],
        "changed_at": cur[0],
        "prev_rate": prev_rate,
        "changes": max(0, len([e for e in entries if e[1] is not None]) - 1),
    }
