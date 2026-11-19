import datetime as dt

import pytest

from permit_alerts.checker import Checker
from permit_alerts.config import Settings
from permit_alerts.db import Database, Group, Member, Opening, Search, Target
from permit_alerts.matching import Filters, match
from permit_alerts.recgov.client import Slot, month_starts

D = dt.date


def camp(site, day, open_=True, cap=6):
    return Slot(site, f"Site {site}", day, 1 if open_ else 0, 1, cap)


# ------------------------------------------------------------------ matching


def test_month_starts_spans_year():
    assert month_starts(D(2026, 12, 20), D(2027, 2, 1)) == [D(2026, 12, 1), D(2027, 1, 1), D(2027, 2, 1)]


def test_permit_date_window_and_remaining():
    slots = [
        Slot("1", "Lodore", D(2027, 7, 9), 1, 1),
        Slot("1", "Lodore", D(2027, 7, 10), 1, 1),
        Slot("1", "Lodore", D(2027, 7, 11), 0, 1),
        Slot("1", "Lodore", D(2027, 7, 26), 2, 4),
    ]
    out = match("permit", "250014", slots, Filters(D(2027, 7, 10), D(2027, 7, 25)))
    assert [c.date for c in out] == [D(2027, 7, 10)]
    assert "250014/registration/detailed-availability?date=2027-07-10" in out[0].booking_url


def test_permit_group_size_only_applies_to_people_quotas():
    river = Slot("1", "Middle Fork", D(2027, 7, 10), 1, 7)
    trail = Slot("2", "Trailhead", D(2027, 7, 10), 3, 30, counts_people=True)
    out = match("permit", "x", [river, trail], Filters(D(2027, 7, 1), D(2027, 7, 31), group_size=8))
    assert [c.unit_id for c in out] == ["1"]


def test_permit_only_ids():
    slots = [Slot("1", "A", D(2027, 7, 10), 1, 1), Slot("2", "B", D(2027, 7, 10), 1, 1)]
    out = match("permit", "x", slots, Filters(D(2027, 7, 1), D(2027, 7, 31), only_ids=frozenset({"2"})))
    assert [c.unit_id for c in out] == ["2"]


def test_campground_runs_and_min_nights():
    slots = [
        camp("a", D(2027, 7, 10)), camp("a", D(2027, 7, 11)), camp("a", D(2027, 7, 12)),
        camp("a", D(2027, 7, 13), open_=False),
        camp("a", D(2027, 7, 14)),
        camp("b", D(2027, 7, 10)), camp("b", D(2027, 7, 11)),
    ]
    out = match("campground", "c", slots, Filters(D(2027, 7, 1), D(2027, 7, 31), min_nights=2))
    assert [(c.unit_id, c.date, c.nights) for c in out] == [
        ("a", D(2027, 7, 10), 3),
        ("b", D(2027, 7, 10), 2),
    ]


def test_campground_capacity():
    slots = [camp("small", D(2027, 7, 10), cap=4), camp("big", D(2027, 7, 10), cap=8)]
    out = match("campground", "c", slots, Filters(D(2027, 7, 1), D(2027, 7, 31), group_size=6))
    assert [c.unit_id for c in out] == ["big"]


# ------------------------------------------------------------------- checker


class FakeClient:
    def __init__(self):
        self.slots: list[Slot] = []

    async def availability(self, kind, facility_id, start, end):
        return [s for s in self.slots if start <= s.date <= end]

    async def facility_info(self, kind, facility_id):
        class I:
            name = "Fake River"
        return I()


class FakeNotifier:
    name = "fake"

    def __init__(self):
        self.sent = []
        self.fail = False

    async def send(self, alert):
        if self.fail:
            raise RuntimeError("down")
        self.sent.append(alert)


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = Database(f"sqlite:///{tmp_path / 't.db'}")
    db.create_all()
    with db.session() as s:
        g = Group(name="Raft crew", members=[Member(name="A", email="a@example.com")])
        future = dt.date.today() + dt.timedelta(days=200)
        search = Search(
            name="Lodore July", group=g, start_date=future, end_date=future + dt.timedelta(days=10),
            targets=[Target(kind="permit", facility_id="250014", name="Dinosaur")],
        )
        s.add(search)
        s.commit()
        sid = search.id
    client, notifier = FakeClient(), FakeNotifier()
    checker = Checker(db, client, [notifier], Settings(reminder_after_minutes=60, _env_file=None))
    return db, client, notifier, checker, sid, future


def _set_now(monkeypatch, when):
    monkeypatch.setattr("permit_alerts.checker.utcnow", lambda: when)


async def test_alert_flow(env, monkeypatch):
    db, client, notifier, checker, sid, day = env
    t0 = dt.datetime(2026, 11, 19, 12, 0)

    # First check: nothing open, nothing sent.
    _set_now(monkeypatch, t0)
    await checker.check_search(sid)
    assert notifier.sent == []

    # Something opens: one alert.
    client.slots = [Slot("1", "Lodore", day, 1, 1)]
    _set_now(monkeypatch, t0 + dt.timedelta(minutes=1))
    await checker.check_search(sid)
    assert len(notifier.sent) == 1 and not notifier.sent[0].reminder

    # Still open 30 min later: nothing new.
    _set_now(monkeypatch, t0 + dt.timedelta(minutes=31))
    await checker.check_search(sid)
    assert len(notifier.sent) == 1

    # An hour after the alert: one reminder.
    _set_now(monkeypatch, t0 + dt.timedelta(minutes=62))
    await checker.check_search(sid)
    assert len(notifier.sent) == 2 and notifier.sent[1].reminder

    # Hours later, still open: nothing more.
    _set_now(monkeypatch, t0 + dt.timedelta(minutes=300))
    await checker.check_search(sid)
    assert len(notifier.sent) == 2

    # Closes, then reopens: a new opening and a new alert.
    client.slots = []
    _set_now(monkeypatch, t0 + dt.timedelta(minutes=301))
    await checker.check_search(sid)
    client.slots = [Slot("1", "Lodore", day, 1, 1)]
    _set_now(monkeypatch, t0 + dt.timedelta(minutes=302))
    await checker.check_search(sid)
    assert len(notifier.sent) == 3 and not notifier.sent[2].reminder

    with db.session() as s:
        rows = s.query(Opening).order_by(Opening.id).all()
        assert len(rows) == 2
        assert rows[0].closed_at is not None and rows[1].closed_at is None


async def test_first_check_sends_one_summary_and_no_reminder(env, monkeypatch):
    db, client, notifier, checker, sid, day = env
    client.slots = [Slot("1", "Lodore", day, 1, 1), Slot("1", "Lodore", day + dt.timedelta(days=1), 1, 1)]
    t0 = dt.datetime(2026, 11, 19, 12, 0)
    _set_now(monkeypatch, t0)
    await checker.check_search(sid)
    assert len(notifier.sent) == 1 and notifier.sent[0].baseline and len(notifier.sent[0].items) == 2
    _set_now(monkeypatch, t0 + dt.timedelta(hours=2))
    await checker.check_search(sid)
    assert len(notifier.sent) == 1


async def test_failed_send_is_retried_next_check(env, monkeypatch):
    db, client, notifier, checker, sid, day = env
    t0 = dt.datetime(2026, 11, 19, 12, 0)
    _set_now(monkeypatch, t0)
    await checker.check_search(sid)
    client.slots = [Slot("1", "Lodore", day, 1, 1)]
    notifier.fail = True
    _set_now(monkeypatch, t0 + dt.timedelta(minutes=1))
    await checker.check_search(sid)
    notifier.fail = False
    _set_now(monkeypatch, t0 + dt.timedelta(minutes=2))
    await checker.check_search(sid)
    assert len(notifier.sent) == 1


async def test_past_search_is_paused(env):
    db, client, notifier, checker, sid, day = env
    with db.session() as s:
        search = s.get(Search, sid)
        search.start_date = D(2020, 1, 1)
        search.end_date = D(2020, 1, 5)
        s.commit()
    assert await checker.check_search(sid) is None
    with db.session() as s:
        assert s.get(Search, sid).active is False


async def test_campsite_run_shrinking_is_not_a_new_alert(env, monkeypatch):
    db, client, notifier, checker, sid, day = env
    with db.session() as s:
        search = s.get(Search, sid)
        search.targets = [Target(kind="campground", facility_id="232496", name="Furnace Creek")]
        s.commit()
    t0 = dt.datetime(2026, 11, 19, 12, 0)
    _set_now(monkeypatch, t0)
    await checker.check_search(sid)  # nothing open
    nights = [day + dt.timedelta(days=i) for i in range(3)]
    client.slots = [camp("23", n) for n in nights]
    _set_now(monkeypatch, t0 + dt.timedelta(minutes=1))
    await checker.check_search(sid)
    assert len(notifier.sent) == 1 and notifier.sent[0].items[0].nights == 3
    # Someone books the first night: the remaining 2 nights are not news.
    client.slots = [camp("23", n) for n in nights[1:]]
    _set_now(monkeypatch, t0 + dt.timedelta(minutes=2))
    await checker.check_search(sid)
    assert len(notifier.sent) == 1
    # A new night next to them opens up: that is news.
    client.slots.append(camp("23", nights[-1] + dt.timedelta(days=1)))
    _set_now(monkeypatch, t0 + dt.timedelta(minutes=3))
    await checker.check_search(sid)
    assert len(notifier.sent) == 2 and notifier.sent[1].items[0].nights == 3
