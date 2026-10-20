"""Deterministic relative-date parsing for the temporal recall arm (#178).

Pure functions: the ONLY clock input is the caller-supplied
``prompt_timestamp`` (canonical ``YYYY-MM-DDTHH:MM:SSZ``).  No wall reads,
no ``ZMEM_TEST_NOW`` (that seam belongs to recall.py's epoch path), no
store access.

Phrase vocabulary (exactly the 20 fixture phrases, matched
longest-first by the caller):
  today, yesterday, tomorrow, last week, this week, next week,
  last month, this month, next month, last year, this year,
  past 24 hours, past 7 days, past 30 days, two days ago,
  three days ago, in two days, earlier today, last quarter, this quarter.

Boundary rules: UTC calendar boundaries for day/week/month/quarter/year
phrases (ISO weeks start Monday; quarters are calendar); fixed timedeltas
anchored at the prompt timestamp for ``past N hours/days``, ``N days ago``
and ``in two days``.  Malformed, unrecognized, inverted, or collapsed
ranges answer ``("", "")`` — never raise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

MAX_TEMPORAL_DAYS = 3650
TEMPORAL_ARM_DEFAULT_CAP = 4

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Longest-first so "earlier today" wins over "today" and "past 30 days"
# over any substring.  Callers iterate this tuple in order and take the
# first substring hit (issue #178 plan R5).
PHRASES_LONGEST_FIRST: tuple[str, ...] = (
    "three days ago",
    "past 24 hours",
    "past 30 days",
    "past 7 days",
    "in two days",
    "two days ago",
    "earlier today",
    "last quarter",
    "this quarter",
    "yesterday",
    "last month",
    "next month",
    "this month",
    "last week",
    "next week",
    "this week",
    "last year",
    "this year",
    "tomorrow",
    "today",
)


def _parse_ts(value: str) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, _TS_FORMAT)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _fmt(moment: datetime) -> str:
    return moment.strftime(_TS_FORMAT)


def _quarter_start(moment: datetime) -> datetime:
    month = ((moment.month - 1) // 3) * 3 + 1
    return moment.replace(month=month, day=1, hour=0, minute=0, second=0,
                          microsecond=0)


def _month_add(moment: datetime, months: int) -> datetime:
    total = moment.year * 12 + (moment.month - 1) + months
    year, month = divmod(total, 12)
    return moment.replace(year=year, month=month + 1, day=1, hour=0,
                          minute=0, second=0, microsecond=0)


def _week_start(moment: datetime) -> datetime:
    # ISO weeks start Monday (the fixture pins 2026-09-10, a Thursday,
    # into [2026-09-07, 2026-09-14)).
    day = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday())


def parse_relative_range(phrase: str,
                         *, prompt_timestamp: str) -> tuple[str, str]:
    """Resolve one recognized phrase against the prompt timestamp.

    Returns canonical ``(start, end)`` strings, or ``("", "")`` for a
    malformed prompt timestamp, an unrecognized phrase, or an
    inverted/collapsed range.  Never raises.
    """
    # v18 (#178 review CL2-15): the never-raises contract is enforced here,
    # at the single public entry. datetime arithmetic at the boundary years
    # (the CLI's _iso8601 validator accepts 0001 and 9999) raised
    # OverflowError/ValueError out of replace()/timedelta below, converting
    # the documented ("", "") answer into an exception that the arm's
    # blanket except then silently swallowed.
    try:
        return _parse_relative_range_inner(phrase, prompt_timestamp)
    except (OverflowError, ValueError):
        return ("", "")


def _parse_relative_range_inner(
        phrase: str, prompt_timestamp: str) -> tuple[str, str]:
    prompt = _parse_ts(prompt_timestamp)
    if prompt is None:
        return ("", "")
    text = (phrase or "").strip().lower()
    if not text:
        return ("", "")

    day0 = prompt.replace(hour=0, minute=0, second=0, microsecond=0)
    week0 = _week_start(prompt)
    month0 = day0.replace(day=1)
    year0 = day0.replace(month=1, day=1)
    quarter0 = _quarter_start(prompt)

    start: datetime | None = None
    end: datetime | None = None

    if text == "today":
        start, end = day0, day0 + timedelta(days=1)
    elif text == "yesterday":
        start, end = day0 - timedelta(days=1), day0
    elif text == "tomorrow":
        start, end = day0 + timedelta(days=1), day0 + timedelta(days=2)
    elif text == "this week":
        start, end = week0, week0 + timedelta(days=7)
    elif text == "last week":
        start, end = week0 - timedelta(days=7), week0
    elif text == "next week":
        start, end = week0 + timedelta(days=7), week0 + timedelta(days=14)
    elif text == "this month":
        start, end = month0, _month_add(month0, 1)
    elif text == "last month":
        start, end = _month_add(month0, -1), month0
    elif text == "next month":
        start, end = _month_add(month0, 1), _month_add(month0, 2)
    elif text == "this year":
        start, end = year0, year0.replace(year=year0.year + 1)
    elif text == "last year":
        start, end = year0.replace(year=year0.year - 1), year0
    elif text == "past 24 hours":
        start, end = prompt - timedelta(hours=24), prompt
    elif text == "past 7 days":
        start, end = prompt - timedelta(days=7), prompt
    elif text == "past 30 days":
        start, end = prompt - timedelta(days=30), prompt
    elif text == "two days ago":
        start, end = prompt - timedelta(days=2), prompt - timedelta(days=1)
    elif text == "three days ago":
        start, end = prompt - timedelta(days=3), prompt - timedelta(days=2)
    elif text == "in two days":
        start, end = prompt + timedelta(days=2), prompt + timedelta(days=3)
    elif text == "earlier today":
        start, end = day0, prompt
    elif text == "last quarter":
        start, end = _quarter_start(
            quarter0 - timedelta(days=1)), quarter0
    elif text == "this quarter":
        start, end = quarter0, _quarter_start(
            _month_add(quarter0, 3))
    else:
        return ("", "")

    if start is None or end is None or end <= start:
        return ("", "")
    return (_fmt(start), _fmt(end))


def clamp_range(start: str, end: str,
                *, prompt_timestamp: str) -> tuple[str, str]:
    """Clamp explicit occurrence endpoints to the temporal window.

    Endpoints later than the prompt timestamp clamp to the prompt;
    endpoints older than ``prompt - MAX_TEMPORAL_DAYS`` clamp to that
    lower bound.  Malformed inputs answer ``("", "")``; never raises.
    """
    # Same boundary-year guard as parse_relative_range (review CL2-15):
    # `floor` arithmetic at year 0001 raised OverflowError past the
    # documented ("", "") contract.
    try:
        return _clamp_range_inner(start, end, prompt_timestamp=prompt_timestamp)
    except (OverflowError, ValueError):
        return ("", "")


def _clamp_range_inner(start: str, end: str,
                       *, prompt_timestamp: str) -> tuple[str, str]:
    prompt = _parse_ts(prompt_timestamp)
    start_moment = _parse_ts(start)
    end_moment = _parse_ts(end)
    if prompt is None or start_moment is None or end_moment is None:
        return ("", "")
    floor = prompt - timedelta(days=MAX_TEMPORAL_DAYS)
    clamped_start = max(start_moment, floor)
    clamped_end = min(max(end_moment, floor), prompt)
    if clamped_end <= clamped_start:
        return ("", "")
    return (_fmt(clamped_start), _fmt(clamped_end))
