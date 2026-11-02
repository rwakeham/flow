"""
Recurring transaction pattern detection.

Analyzes historical manual transactions to identify repeating patterns
and predicts upcoming transactions within a configurable look-ahead window.

Sub-stream splitting
--------------------
A single payee can have more than one recurring series of the same sign --
e.g. an Acme deposit of $4000 on the 15th *and* $6000 on the last day of the
month. The coarse grouping below keys only on (description keywords, sign), so
those two series land in one group and the single-stream math misreads them as
one irregular/biweekly pattern with a meaningless median amount.

To fix that, each coarse group is split into sub-streams by day-of-month before
the cadence math runs. The split is deliberately conservative (see
``_split_into_substreams``) and is only *adopted* when every resulting
sub-stream independently validates as a pattern -- otherwise we fall back to
analyzing the whole group exactly as before.

This behavior can be disabled (reverting to the original single-stream-per-group
algorithm) via the ``FLOW_RECURRING_SUBSTREAMS`` environment variable or the
``split_substreams`` argument -- see ``_substreams_enabled``.
"""

from __future__ import annotations

import math
import os
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from reconciler import _keywords


def _substreams_enabled(override: bool | None) -> bool:
    """Whether sub-stream splitting is active.

    Explicit ``override`` (from the function argument) wins. Otherwise the
    ``FLOW_RECURRING_SUBSTREAMS`` env var controls it, defaulting to enabled.
    Set ``FLOW_RECURRING_SUBSTREAMS=0`` (or false/no/off) to revert to the
    original single-stream-per-group behavior with no code change.
    """
    if override is not None:
        return override
    return os.environ.get("FLOW_RECURRING_SUBSTREAMS", "1").strip().lower() not in (
        "0", "false", "no", "off", "",
    )


def _canonical_key(description: str, amount: float) -> str:
    kws = sorted(_keywords(description))
    sign = "credit" if amount > 0 else "debit"
    return (" ".join(kws) if kws else description.lower()) + "|" + sign


_FREQUENCY_BANDS = [
    (6,   8,   "weekly"),
    (13,  16,  "biweekly"),
    (27,  35,  "monthly"),
    (85,  96,  "quarterly"),
    (350, 380, "annual"),
]

# Day-of-month proximity (in days) within which transactions are treated as the
# same monthly anchor. End-of-month dates (28/30/31) and weekend-shifted dates
# stay together; clearly distinct anchors (e.g. the 15th vs the 31st) separate.
_DOM_CLUSTER_TOLERANCE = 6
_MONTH_RING = 31  # day-of-month wraps on a ~monthly ring (31 ~ 1)


def _classify_frequency(median_interval: float) -> str | None:
    for lo, hi, label in _FREQUENCY_BANDS:
        if lo <= median_interval <= hi:
            return label
    return None


def _split_into_substreams(group_txns: list, min_occurrences: int) -> list[list]:
    """Partition a payee group into monthly-anchored sub-streams.

    Clusters transactions by day-of-month on a circular (wrap-around) ring,
    cutting the ring wherever the gap between adjacent day-of-month values
    exceeds ``_DOM_CLUSTER_TOLERANCE``. Two interleaved monthly series produce
    two tight, well-separated clusters; an evenly spread series (weekly /
    biweekly / a single monthly anchor) yields a single cluster and is left
    intact.

    Returns the original group as a single-element list unless it splits into
    two or more clusters that *each* have at least ``min_occurrences`` members.
    That size guard prevents fragmenting an evenly-spread series into many tiny
    pieces, so callers can treat a multi-element result as a confident split.
    """
    if len(group_txns) < 2 * min_occurrences:
        return [group_txns]

    ordered = sorted(group_txns, key=lambda t: t.date.day)
    days = [t.date.day for t in ordered]
    n = len(ordered)

    # Circular gap from each day-of-month to the next (wrapping past month end).
    gaps = [(days[(i + 1) % n] - days[i]) % _MONTH_RING for i in range(n)]
    cut_after = [i for i in range(n) if gaps[i] > _DOM_CLUSTER_TOLERANCE]

    # 0 or 1 large gap means the values form a single arc on the ring.
    if len(cut_after) < 2:
        return [group_txns]

    # Walk the ring starting just after a cut so each cluster is contiguous.
    start = (cut_after[0] + 1) % n
    clusters: list[list] = []
    current: list = []
    for k in range(n):
        idx = (start + k) % n
        current.append(ordered[idx])
        if gaps[idx] > _DOM_CLUSTER_TOLERANCE:
            clusters.append(current)
            current = []
    if current:
        clusters.append(current)

    if len(clusters) < 2 or any(len(c) < min_occurrences for c in clusters):
        return [group_txns]
    return clusters


@dataclass
class RecurringPattern:
    description: str
    amount: float
    next_expected_date: date
    frequency: str
    confidence: float
    occurrence_count: int


def _analyze_stream(
    stream_txns: list,
    today: date,
    min_occurrences: int,
    min_confidence: float,
) -> RecurringPattern | None:
    """Run cadence/confidence analysis on a single candidate series.

    Returns a ``RecurringPattern`` if the series is regular, recent, and
    confident enough; otherwise ``None``. The look-ahead window is *not*
    applied here — that filter is the caller's job — so validity (is this a
    real recurring series?) stays independent of whether the next occurrence
    happens to be due soon.
    """
    if len(stream_txns) < min_occurrences:
        return None

    sorted_txns = sorted(stream_txns, key=lambda t: t.date)
    dates = [t.date for t in sorted_txns]
    amounts = [float(t.amount) for t in sorted_txns]

    intervals = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    if not intervals:
        return None

    median_interval = statistics.median(intervals)
    freq = _classify_frequency(median_interval)
    if freq is None:
        return None

    if len(intervals) > 1 and median_interval > 0:
        stdev_interval = statistics.stdev(intervals)
        regularity = max(0.0, 1.0 - (stdev_interval / median_interval))
    else:
        regularity = 1.0

    mean_amt = statistics.mean(amounts)
    if len(amounts) > 1 and mean_amt != 0:
        stdev_amt = statistics.stdev(amounts)
        amount_consistency = max(0.0, 1.0 - abs(stdev_amt / mean_amt))
    else:
        amount_consistency = 1.0

    occurrence_score = min(1.0, math.log(len(stream_txns)) / math.log(12))

    confidence = (
        0.4 * occurrence_score
        + 0.3 * regularity
        + 0.3 * amount_consistency
    )

    step = max(1, round(median_interval))

    # Staleness penalty: one grace period for the currently-due cycle, then
    # confidence drops 50% per additional missed cycle and reaches zero at 2
    # missed cycles — ensuring patterns absent for 2+ periods disappear.
    days_since_last = (today - dates[-1]).days
    missed_cycles = max(0.0, days_since_last / step - 1.0)
    confidence *= max(0.0, 1.0 - missed_cycles * 0.5)

    if confidence < min_confidence:
        return None

    next_date = dates[-1] + timedelta(days=step)
    while next_date < today:
        next_date += timedelta(days=step)

    return RecurringPattern(
        description=sorted_txns[-1].description,
        amount=round(statistics.median(amounts), 2),
        next_expected_date=next_date,
        frequency=freq,
        confidence=round(confidence, 3),
        occurrence_count=len(stream_txns),
    )


def detect_recurring_patterns(
    txns: list,
    days_ahead: int = 31,
    min_occurrences: int = 3,
    min_confidence: float = 0.40,
    split_substreams: bool | None = None,
) -> list[RecurringPattern]:
    """
    Detect recurring patterns from a transaction list and return predictions
    whose next expected date falls within [today, today + days_ahead].

    Only manual transactions are analyzed.

    When sub-stream splitting is enabled (the default; see
    ``_substreams_enabled``), a payee group that cleanly separates into multiple
    monthly-anchored series — e.g. an Acme deposit on the 15th and another on
    the last day of the month — is reported as multiple patterns. The split is
    only adopted when every resulting sub-stream independently validates;
    otherwise the whole group is analyzed as a single series, identical to the
    original behavior.
    """
    today = date.today()
    lookahead = today + timedelta(days=days_ahead)
    use_substreams = _substreams_enabled(split_substreams)

    manual_txns = [t for t in txns if t.source == "manual"]
    if len(manual_txns) < min_occurrences:
        return []

    groups: dict[str, list] = {}
    for t in manual_txns:
        key = _canonical_key(t.description, float(t.amount))
        groups.setdefault(key, []).append(t)

    patterns: list[RecurringPattern] = []

    for group_txns in groups.values():
        if len(group_txns) < min_occurrences:
            continue

        substreams = (
            _split_into_substreams(group_txns, min_occurrences)
            if use_substreams
            else [group_txns]
        )

        group_patterns: list[RecurringPattern] = []
        if len(substreams) > 1:
            # Only adopt the split if every sub-stream is itself a valid
            # pattern; otherwise fall back to the whole group as one series.
            split_patterns = [
                _analyze_stream(s, today, min_occurrences, min_confidence)
                for s in substreams
            ]
            if all(p is not None for p in split_patterns):
                group_patterns = [p for p in split_patterns if p is not None]

        if not group_patterns:
            whole = _analyze_stream(
                group_txns, today, min_occurrences, min_confidence
            )
            if whole is not None:
                group_patterns = [whole]

        # Surface only patterns whose next occurrence is within the window.
        patterns.extend(p for p in group_patterns if p.next_expected_date <= lookahead)

    return sorted(patterns, key=lambda p: p.next_expected_date)
