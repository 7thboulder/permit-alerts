"""Runs every active search on its own timer, logs openings, and sends alerts.

Alert rules (per opening):
  * alert as soon as it is seen open,
  * one reminder `reminder_after_minutes` later if it is still open,
  * nothing more after that. If it closes and opens again, that is a new opening.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .config import Settings
from .db import Database, Opening, Search, Target, utcnow
from .matching import Filters, match
from .notify.base import Alert, AlertItem, Notifier
from .recgov.client import RecGovClient, RecGovError

log = logging.getLogger(__name__)


class Checker:
    def __init__(
        self,
        db: Database,
        client: RecGovClient,
        notifiers: Iterable[Notifier],
        settings: Settings,
    ):
        self.db = db
        self.client = client
        self.notifiers = list(notifiers)
        self.settings = settings
        self._tasks: dict[int, asyncio.Task] = {}
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------ scheduling

    async def run_forever(self, reconcile_every: float = 3.0) -> None:
        """Keep one task per active search; pick up dashboard changes quickly."""
        try:
            while not self._stopping.is_set():
                self.reconcile()
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=reconcile_every)
                except asyncio.TimeoutError:
                    pass
        finally:
            for t in self._tasks.values():
                t.cancel()

    def stop(self) -> None:
        self._stopping.set()

    def reconcile(self) -> None:
        with self.db.session() as s:
            active = set(s.scalars(select(Search.id).where(Search.active.is_(True))))
        for sid in list(self._tasks):
            if sid not in active or self._tasks[sid].done():
                self._tasks.pop(sid).cancel()
        for sid in active - set(self._tasks):
            self._tasks[sid] = asyncio.create_task(self._loop(sid), name=f"search-{sid}")

    async def _loop(self, search_id: int) -> None:
        while True:
            started = asyncio.get_running_loop().time()
            try:
                interval = await self.check_search(search_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # never let one search kill the app
                log.exception("search %s check crashed", search_id)
                interval = self.settings.default_interval_seconds
            if interval is None:
                return  # search was deleted or paused
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(1.0, interval - elapsed))

    # --------------------------------------------------------------- checking

    async def check_search(self, search_id: int) -> int | None:
        """Check one search once. Returns seconds until the next check, or None to stop."""
        with self.db.session() as s:
            search = s.scalar(
                select(Search)
                .where(Search.id == search_id)
                .options(selectinload(Search.targets), selectinload(Search.group))
            )
            if search is None or not search.active:
                return None
            today = dt.date.today()
            if search.end_date < today:
                search.active = False
                search.last_error = "Date range has passed; search paused."
                s.commit()
                return None
            interval = max(self.settings.min_interval_seconds, search.interval_seconds)
            first_check = search.last_checked_at is None
            start = max(search.start_date, today)
            errors: list[str] = []
            now = utcnow()
            new_rows: list[Opening] = []

            for target in search.targets:
                filters = Filters(
                    start=start,
                    end=search.end_date,
                    group_size=search.group_size,
                    min_nights=search.min_nights or 1,
                    only_ids=frozenset(str(x) for x in (target.only_ids or [])),
                )
                try:
                    slots = await self.client.availability(
                        target.kind, target.facility_id, start, search.end_date
                    )
                    if not target.name:
                        info = await self.client.facility_info(target.kind, target.facility_id)
                        target.name = info.name
                except RecGovError as e:
                    errors.append(f"{target.name or target.facility_id}: {e}")
                    continue  # leave this target's open openings untouched
                candidates = {c.slot_key: c for c in match(target.kind, target.facility_id, slots, filters)}
                new_rows += self._update_openings(s, search, target, candidates, now, first_check)

            search.last_checked_at = now
            search.last_error = "; ".join(errors) or None
            s.commit()

            await self._send_pending(s, search, now)
            s.commit()
            if new_rows:
                log.info("search %s: %d new openings", search.id, len(new_rows))
            return interval

    def _update_openings(self, s, search, target: Target, candidates: dict, now, first_check) -> list[Opening]:
        open_rows = {
            o.slot_key: o
            for o in s.scalars(
                select(Opening).where(
                    Opening.search_id == search.id,
                    Opening.target_id == target.id,
                    Opening.closed_at.is_(None),
                )
            )
        }
        # Campground nights that were already open (and alerted on) before this check.
        # A run that only shrinks or splits (someone booked part of it) is not news.
        covered: dict[tuple[str, dt.date], Opening] = {}
        if target.kind == "campground":
            for o in open_rows.values():
                unit = o.slot_key.split(":", 1)[0]
                for i in range(o.nights):
                    covered[(unit, o.date + dt.timedelta(days=i))] = o
        new_rows = []
        for key, c in candidates.items():
            row = open_rows.get(key)
            if row is not None:
                row.remaining = c.remaining
                continue
            nights = [(c.unit_id, c.date + dt.timedelta(days=i)) for i in range(c.nights)]
            previous = covered.get(nights[0]) if target.kind == "campground" else None
            if previous is not None and not all(n in covered for n in nights):
                previous = None  # at least one night is newly open: alert
            row = Opening(
                search_id=search.id,
                target_id=target.id,
                kind=target.kind,
                facility_id=target.facility_id,
                facility_name=target.name or target.facility_id,
                slot_key=key,
                slot_label=c.label,
                date=c.date,
                nights=c.nights,
                remaining=c.remaining,
                total=c.total,
                booking_url=c.booking_url,
                opened_at=now,
                on_first_check=first_check,
            )
            if previous is not None:
                row.continues_id = previous.id
                row.on_first_check = previous.on_first_check
                row.opened_at = previous.opened_at
                row.alerted_at = previous.alerted_at
                row.reminded_at = previous.reminded_at
            s.add(row)
            if previous is None:
                new_rows.append(row)
        for key, row in open_rows.items():
            if key not in candidates:
                row.closed_at = now
        s.flush()
        return new_rows

    async def _send_pending(self, s, search: Search, now: dt.datetime) -> None:
        open_rows = list(
            s.scalars(
                select(Opening)
                .where(Opening.search_id == search.id, Opening.closed_at.is_(None))
                .order_by(Opening.date, Opening.slot_label)
            )
        )
        # Already open on the very first check: one "here's what's open now" message, no reminder.
        baseline = [o for o in open_rows if o.alerted_at is None and o.on_first_check]
        fresh = [o for o in open_rows if o.alerted_at is None and not o.on_first_check]
        remind_before = now - dt.timedelta(minutes=self.settings.reminder_after_minutes)
        due = [
            o for o in open_rows
            if o.alerted_at is not None and o.reminded_at is None and o.alerted_at <= remind_before
        ]
        if baseline and await self._deliver(self._alert(search, baseline, reminder=False, baseline=True)):
            for o in baseline:
                o.alerted_at = now
                o.reminded_at = now
        if fresh and await self._deliver(self._alert(search, fresh, reminder=False)):
            for o in fresh:
                o.alerted_at = now
        if due and await self._deliver(self._alert(search, due, reminder=True)):
            for o in due:
                o.reminded_at = now

    def _alert(self, search: Search, rows: list[Opening], reminder: bool, baseline: bool = False) -> Alert:
        g = search.group
        return Alert(
            search_id=search.id,
            search_name=search.name,
            group_id=g.id,
            group_name=g.name,
            emails=[m.email for m in g.members if m.email],
            phones=[m.phone for m in g.members if m.phone],
            discord_channel_id=g.discord_channel_id,
            reminder=reminder,
            baseline=baseline,
            items=[
                AlertItem(
                    facility_name=o.facility_name,
                    label=o.slot_label,
                    date=o.date,
                    nights=o.nights,
                    remaining=o.remaining,
                    total=o.total,
                    booking_url=o.booking_url,
                    kind=o.kind,
                )
                for o in rows
            ],
        )

    async def _deliver(self, alert: Alert) -> bool:
        """True if at least one notifier delivered it (or none are set up yet)."""
        if not self.notifiers:
            log.warning("No notifiers configured; alert logged only:\n%s", alert.text())
            return True
        results = await asyncio.gather(*(n.send(alert) for n in self.notifiers), return_exceptions=True)
        ok = False
        for n, r in zip(self.notifiers, results):
            if isinstance(r, Exception):
                log.error("%s alert failed for search %s: %s", n.name, alert.search_id, r)
            else:
                ok = True
        return ok
