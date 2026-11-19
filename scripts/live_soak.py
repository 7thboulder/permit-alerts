"""Live soak test: run real searches against Recreation.gov for a few minutes.

Usage: python scripts/live_soak.py [minutes]
Uses a throwaway database and prints every alert instead of sending it.
"""

import asyncio
import datetime as dt
import logging
import sys
import tempfile
from pathlib import Path

from permit_alerts.checker import Checker
from permit_alerts.config import Settings
from permit_alerts.db import Database, Group, Member, Opening, Search, Target
from permit_alerts.recgov.client import RecGovClient

TODAY = dt.date.today()
SEARCHES = [
    ("Joshua Tree next 3 weeks", "campground", "272300", TODAY, TODAY + dt.timedelta(days=21), 1),
    ("Furnace Creek 2+ nights", "campground", "232496", TODAY, TODAY + dt.timedelta(days=30), 2),
    ("Zion Watchman", "campground", "232445", TODAY, TODAY + dt.timedelta(days=30), 1),
    ("Chisos Basin", "campground", "234038", TODAY, TODAY + dt.timedelta(days=45), 1),
    ("Ruby Horsethief", "permit", "74466", TODAY, TODAY + dt.timedelta(days=30), 1),
    ("Westwater", "permit", "621744", TODAY, TODAY + dt.timedelta(days=30), 1),
    ("Dinosaur Green/Yampa", "permit", "250014", TODAY, TODAY + dt.timedelta(days=120), 1),
    ("Middle Fork Salmon", "permit", "234623", dt.date(2027, 6, 1), dt.date(2027, 7, 31), 1),
    ("Mt Whitney", "permit", "445860", dt.date(2027, 6, 1), dt.date(2027, 6, 30), 1),
]


class PrintNotifier:
    name = "print"

    def __init__(self):
        self.count = 0

    async def send(self, alert):
        self.count += 1
        print(f"\n=== ALERT {dt.datetime.now():%H:%M:%S} ===\n{alert.text()[:1200]}\n", flush=True)


async def main(minutes: float):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    tmp = Path(tempfile.mkdtemp())
    db = Database(f"sqlite:///{tmp / 'soak.db'}")
    db.create_all()
    with db.session() as s:
        g = Group(name="Soak", members=[Member(name="Test", email="test@example.com")])
        for name, kind, fid, start, end, nights in SEARCHES:
            s.add(Search(name=name, group=g, start_date=start, end_date=end, interval_seconds=45,
                         min_nights=nights, targets=[Target(kind=kind, facility_id=fid)]))
        s.commit()
    client = RecGovClient()
    notifier = PrintNotifier()
    checker = Checker(db, client, [notifier], Settings(_env_file=None))
    task = asyncio.create_task(checker.run_forever())
    await asyncio.sleep(minutes * 60)
    checker.stop()
    await task
    await client.aclose()
    with db.session() as s:
        print("\n=== SUMMARY ===")
        for search in s.query(Search).all():
            rows = s.query(Opening).filter_by(search_id=search.id).all()
            new = [r for r in rows if not r.on_first_check]
            closed = [r for r in rows if r.closed_at]
            print(f"{search.name:<26} checked={search.last_checked_at:%H:%M:%S} "
                  f"open_at_start={len(rows) - len(new)} new_openings={len(new)} closed={len(closed)} "
                  f"error={search.last_error}")
            for r in new[:5]:
                print(f"    NEW {r.opened_at:%H:%M:%S} {r.slot_label} {r.date} nights={r.nights} rem={r.remaining}")
    print(f"alerts sent: {notifier.count}")


if __name__ == "__main__":
    asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 5))
