"""Turns raw Recreation.gov slots into the openings a search cares about."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from itertools import groupby

from .recgov.client import Slot, booking_url


@dataclass(frozen=True)
class Candidate:
    """Something that is open right now and matches a search."""

    slot_key: str
    unit_id: str
    label: str
    date: dt.date  # first date (launch date / first night)
    nights: int
    remaining: int
    total: int | None
    booking_url: str


@dataclass(frozen=True)
class Filters:
    start: dt.date
    end: dt.date
    group_size: int | None = None
    min_nights: int = 1
    only_ids: frozenset[str] = frozenset()


def match(kind: str, facility_id: str, slots: list[Slot], f: Filters) -> list[Candidate]:
    slots = [s for s in slots if f.start <= s.date <= f.end]
    if f.only_ids:
        slots = [s for s in slots if s.unit_id in f.only_ids]
    if kind == "campground":
        return _campground(facility_id, slots, f)
    return _permit(facility_id, slots, f)


def _permit(facility_id: str, slots: list[Slot], f: Filters) -> list[Candidate]:
    out = []
    for s in slots:
        if s.remaining <= 0:
            continue
        # Quotas that count people (wilderness trailheads) must fit the whole group.
        # River permits count launches/groups, so any remaining launch is a match.
        if f.group_size and s.counts_people and s.remaining < f.group_size:
            continue
        out.append(
            Candidate(
                slot_key=f"{s.unit_id}:{s.date.isoformat()}",
                unit_id=s.unit_id,
                label=s.unit_name,
                date=s.date,
                nights=1,
                remaining=s.remaining,
                total=s.total,
                booking_url=booking_url("permit", facility_id, s.unit_id, s.date),
            )
        )
    return sorted(out, key=lambda c: (c.date, c.label))


def _campground(facility_id: str, slots: list[Slot], f: Filters) -> list[Candidate]:
    """One candidate per run of consecutive open nights at one campsite."""
    out = []
    open_slots = sorted((s for s in slots if s.remaining > 0), key=lambda s: (s.unit_id, s.date))
    for unit_id, group in groupby(open_slots, key=lambda s: s.unit_id):
        nights = list(group)
        cap = nights[0].capacity
        if f.group_size and cap and cap < f.group_size:
            continue
        run: list[Slot] = []
        for s in nights + [None]:  # sentinel flushes the last run
            if s is not None and run and s.date == run[-1].date + dt.timedelta(days=1):
                run.append(s)
                continue
            if run and len(run) >= max(1, f.min_nights):
                first = run[0]
                out.append(
                    Candidate(
                        slot_key=f"{unit_id}:{first.date.isoformat()}:{len(run)}",
                        unit_id=unit_id,
                        label=first.unit_name,
                        date=first.date,
                        nights=len(run),
                        remaining=1,
                        total=1,
                        booking_url=booking_url("campground", facility_id, unit_id, first.date),
                    )
                )
            run = [s] if s is not None else []
    return sorted(out, key=lambda c: (c.date, c.label))
