"""Cron schedules, and the slots they name in a time zone.

The syntax is the one GitHub's `schedule:` takes: five fields (minute, hour,
day of month, month, day of week) of numbers, `*`, lists with `,`, ranges
with `-` and steps with `/`, plus month and day names. Day of week 0 and 7
are Sunday. When both day fields are restricted, a day matching either one
is enough; a field that starts with `*` counts as unrestricted, as in cron.

Slots are wall-clock times in the job's zone, as UTC timestamps. When clocks
go back, a time in the repeated hour is its first occurrence, so it runs
once. When they go forward, a time in the skipped hour is the first minute
that exists after it.
"""

import datetime
from collections.abc import Iterator
from dataclasses import dataclass

MIN_GAP = 5  # minutes; the tick runs every 5
LOOKBACK = 400  # days; older slots are not listed one by one
_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_DAYS = {d: i for i, d in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))}
_FIELDS = (("minute", 0, 59, {}), ("hour", 0, 23, {}), ("day of month", 1, 31, {}),
           ("month", 1, 12, _MONTHS), ("day of week", 0, 7, _DAYS))


@dataclass(frozen=True)
class Schedule:
    text: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 is Sunday
    any_day: bool
    any_weekday: bool

    def on(self, date: datetime.date) -> bool:
        if date.month not in self.months:
            return False
        dom = date.day in self.days
        dow = (date.weekday() + 1) % 7 in self.weekdays
        if self.any_day or self.any_weekday:
            return dom and dow
        return dom or dow


def _number(text: str, names: dict[str, int], what: str) -> int:
    value = names.get(text.upper())
    if value is not None:
        return value
    if not text.isdigit():
        raise ValueError(f"{text!r} is not a number in the {what} field")
    return int(text)


def _field(text: str, what: str, lo: int, hi: int, names: dict[str, int]) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) == 0:
                raise ValueError(f"{part!r}: the step in the {what} field must be a number "
                                 "above 0")
            step = int(step_text)
        if base == "*":
            first, last = lo, hi
        elif "-" in base:
            a, _, b = base.partition("-")
            first, last = _number(a, names, what), _number(b, names, what)
        else:
            first = _number(base, names, what)
            last = hi if slash else first
        if not lo <= first <= hi or not lo <= last <= hi:
            raise ValueError(f"{part!r} is outside {lo}-{hi} in the {what} field")
        if first > last:
            raise ValueError(f"{part!r}: the range in the {what} field runs backwards")
        out.update(range(first, last + 1, step))
    return out


def parse(text: str) -> Schedule:
    """A Schedule, or ValueError saying what is wrong with the line."""
    fields = text.split()
    if len(fields) != 5:
        raise ValueError(f"{text!r} has {len(fields)} fields; cron has 5: minute, hour, "
                         "day of month, month, day of week")
    sets = [_field(f, what, lo, hi, names) for f, (what, lo, hi, names) in zip(fields, _FIELDS)]
    weekdays = {d % 7 for d in sets[4]}
    s = Schedule(text=" ".join(fields), minutes=tuple(sorted(sets[0])),
                 hours=tuple(sorted(sets[1])), days=frozenset(sets[2]),
                 months=frozenset(sets[3]), weekdays=frozenset(weekdays),
                 any_day=fields[2].startswith("*"), any_weekday=fields[4].startswith("*"))
    gaps = [b - a for a, b in zip(s.minutes, s.minutes[1:])]
    if any((h + 1) % 24 in s.hours for h in s.hours):
        gaps.append(60 - s.minutes[-1] + s.minutes[0])
    if gaps and min(gaps) < MIN_GAP:
        raise ValueError(f"{text!r} fires more often than every {MIN_GAP} minutes, and "
                         f"cronward ticks every {MIN_GAP}")
    if not any(s.on(d) for d in _dates(datetime.date(2028, 1, 1), 366 * 4)):
        raise ValueError(f"{text!r} never fires (no such date)")
    return s


def _dates(first: datetime.date, count: int) -> Iterator[datetime.date]:
    for i in range(count):
        yield first + datetime.timedelta(days=i)


def instant(wall: datetime.datetime, zone: datetime.tzinfo) -> float:
    """The UTC timestamp of a naive wall-clock time in zone: its first
    occurrence, or for a time the clocks skip, the first minute after it."""
    ts = wall.replace(tzinfo=zone, fold=0).timestamp()
    if datetime.datetime.fromtimestamp(ts, zone).replace(tzinfo=None) == wall:
        return ts
    probe = wall
    for _ in range(24 * 60):
        probe += datetime.timedelta(minutes=1)
        t = probe.replace(tzinfo=zone, fold=0).timestamp()
        if datetime.datetime.fromtimestamp(t, zone).replace(tzinfo=None) == probe:
            return t
    return ts


def slots(s: Schedule, zone: datetime.tzinfo, after: float, until: float) -> list[float]:
    """The slots after `after` and up to `until`, oldest first. Only the last
    LOOKBACK days are listed."""
    after = max(after, until - LOOKBACK * 86400)
    if until <= after:
        return []
    first = datetime.datetime.fromtimestamp(after, zone).date() - datetime.timedelta(days=1)
    last = datetime.datetime.fromtimestamp(until, zone).date() + datetime.timedelta(days=1)
    found = set()
    for date in _dates(first, (last - first).days + 1):
        if not s.on(date):
            continue
        for h in s.hours:
            for m in s.minutes:
                ts = instant(datetime.datetime(date.year, date.month, date.day, h, m), zone)
                if after < ts <= until:
                    found.add(ts)
    return sorted(found)


def next_slot(s: Schedule, zone: datetime.tzinfo, after: float) -> float | None:
    """The first slot after `after`, within 5 years."""
    day = 86400
    start = after
    for _ in range(5 * 12):
        found = slots(s, zone, start, start + 31 * day)
        if found:
            return found[0]
        start += 31 * day
    return None
