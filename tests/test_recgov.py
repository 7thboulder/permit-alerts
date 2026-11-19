import datetime as dt

import httpx
import respx

from permit_alerts.recgov.client import RecGovClient
from permit_alerts.web.routes import parse_members

BASE = "https://www.recreation.gov"


def content(divs):
    return {"payload": {"name": "Test Permit", "divisions": {k: {"name": v} for k, v in divs.items()}}}


@respx.mock
async def test_permits_family():
    respx.get(f"{BASE}/api/permitcontent/250014").respond(json=content({"380": "Gates of Lodore"}))
    respx.get(f"{BASE}/api/permits/250014/availability/month").respond(json={"payload": {"availability": {
        "380": {"division_id": "380", "date_availability": {
            "2027-07-10T00:00:00Z": {"total": 1, "remaining": 1},
            "2027-07-11T00:00:00Z": {"total": 1, "remaining": 0},
        }}}}})
    c = RecGovClient()
    slots = await c.availability("permit", "250014", dt.date(2027, 7, 1), dt.date(2027, 7, 31))
    assert c._families["250014"] == "permits"
    assert [(s.unit_name, s.date, s.remaining) for s in slots] == [
        ("Gates of Lodore", dt.date(2027, 7, 10), 1), ("Gates of Lodore", dt.date(2027, 7, 11), 0)]
    await c.aclose()


@respx.mock
async def test_permitwbe_family_and_unlimited():
    respx.get(f"{BASE}/api/permitcontent/621744").respond(json=content({"744": "Westwater"}))
    respx.get(f"{BASE}/api/permits/621744/availability/month").respond(404, json={"error": "server issue"})
    respx.get(f"{BASE}/api/permitwbe/621744/availability/month").respond(json={"payload": {"availability": {
        "744": {"division_id": "744", "date_availability": {"2027-07-10T00:00:00Z": {"total": 99999, "remaining": 99999}}}}}})
    c = RecGovClient()
    slots = await c.availability("permit", "621744", dt.date(2027, 7, 1), dt.date(2027, 7, 31))
    assert c._families["621744"] == "permitwbe"
    assert slots[0].total is None and slots[0].unit_name == "Westwater"
    await c.aclose()


@respx.mock
async def test_inyo_family_counts_people():
    respx.get(f"{BASE}/api/permitcontent/445860").respond(json=content({"166": "Whitney Trail"}))
    respx.get(f"{BASE}/api/permits/445860/availability/month").respond(404, json={"error": "x"})
    respx.get(f"{BASE}/api/permitwbe/445860/availability/month").respond(400, json={"error": "invalid permit id"})
    respx.get(f"{BASE}/api/permitinyo/445860/availability").respond(json={"payload": {
        "2027-06-01": {"166": {"total": 60, "remaining": 3, "is_walkup": False},
                       "999": {"total": 5, "remaining": 5, "is_walkup": True}}}})
    c = RecGovClient()
    slots = await c.availability("permit", "445860", dt.date(2027, 6, 1), dt.date(2027, 6, 30))
    assert c._families["445860"] == "inyo"
    assert len(slots) == 1 and slots[0].counts_people and slots[0].remaining == 3
    await c.aclose()


@respx.mock
async def test_campground_statuses():
    respx.get(f"{BASE}/api/search").respond(json={"results": [{"name": "Watchman"}]})
    respx.get(f"{BASE}/api/camps/availability/campground/232445/month").respond(json={"campsites": {
        "420": {"site": "C029", "loop": "Loop C", "max_num_people": 6, "availabilities": {
            "2027-05-01T00:00:00Z": "Available", "2027-05-02T00:00:00Z": "Reserved", "2027-05-03T00:00:00Z": "NYR"}}}})
    c = RecGovClient()
    slots = await c.availability("campground", "232445", dt.date(2027, 5, 1), dt.date(2027, 5, 31))
    assert [(s.date.day, s.remaining) for s in slots] == [(1, 1), (2, 0), (3, 0)]
    assert slots[0].unit_name == "Site C029 (Loop C)" and slots[0].capacity == 6
    await c.aclose()


@respx.mock
async def test_rate_limit_backoff_then_success(monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr("permit_alerts.recgov.client.asyncio.sleep", no_sleep)
    route = respx.get(f"{BASE}/api/permitcontent/1")
    route.side_effect = [httpx.Response(429), httpx.Response(200, json=content({}))]
    c = RecGovClient()
    info = await c.facility_info("permit", "1")
    assert info.name == "Test Permit"
    await c.aclose()


def test_parse_members():
    got = parse_members("Ezra, 970-555-0100, ezra@example.com, @ezra_rafts\nsam@example.com\n@lee\n\nJo")
    assert got[0] == {"name": "Ezra", "phone": "970-555-0100", "email": "ezra@example.com", "discord_username": "ezra_rafts"}
    assert got[1]["email"] == "sam@example.com"
    assert got[2]["discord_username"] == "lee"
    assert got[3]["name"] == "Jo"
