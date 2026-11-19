"""Notifier interface. Discord and email today; Twilio SMS can be added later
by writing one more class with the same `send` method."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class AlertItem:
    facility_name: str
    label: str
    date: dt.date
    nights: int
    remaining: int
    total: int | None
    booking_url: str
    kind: str  # "permit" or "campground"


@dataclass
class Alert:
    search_id: int
    search_name: str
    group_id: int
    group_name: str
    # Group contacts, so each notifier can pick what it needs.
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    discord_channel_id: int | None = None
    reminder: bool = False  # True for the "still open after an hour" follow-up
    baseline: bool = False  # True for the summary of what was already open when the search started
    items: list[AlertItem] = field(default_factory=list)

    def title(self) -> str:
        n = len(self.items)
        what = "opening" if n == 1 else "openings"
        if self.baseline:
            return f"Search started: {n} already showing open for {self.search_name}"
        if self.reminder:
            return f"Still open after an hour: {n} {what} for {self.search_name}"
        return f"{n} new {what} for {self.search_name}"

    def lines(self, limit: int = 15) -> list[str]:
        out = []
        for it in self.items[:limit]:
            when = it.date.strftime("%a %b %d, %Y")
            if it.kind == "campground":
                nights = f"{it.nights} night{'s' if it.nights != 1 else ''}"
                out.append(f"{it.facility_name} · {it.label} · {when} · {nights}")
            else:
                if it.total is None:
                    left = "no limit" if it.remaining >= 9999 else f"{it.remaining} left"
                else:
                    left = f"{it.remaining} of {it.total} left"
                out.append(f"{it.facility_name} · {it.label} · {when} · {left}")
        if len(self.items) > limit:
            out.append(f"…and {len(self.items) - limit} more")
        return out

    def text(self) -> str:
        body = [self.title(), ""]
        for line, it in zip(self.lines(), self.items):
            body.append(f"• {line}\n  Book: {it.booking_url}")
        if len(self.items) > 15:
            body.append(f"…and {len(self.items) - 15} more")
        return "\n".join(body)


class Notifier(Protocol):
    name: str

    async def send(self, alert: Alert) -> None:
        """Deliver the alert. Raise on failure so the checker can retry."""
        ...
