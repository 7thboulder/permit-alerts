"""Talks to Recreation.gov's public website API.

These are the same JSON endpoints the Recreation.gov website calls from the
browser. They are undocumented, so every endpoint lives here in one place.

Permits come in three families, which use different endpoints:
  * "permits"  /api/permits/{id}/availability/month    (4 Rivers, Dinosaur, Ruby Horsethief...)
  * "permitwbe" /api/permitwbe/{id}/availability/month (Westwater, Canyonlands, Dolores...)
  * "inyo"     /api/permitinyo/{id}/availability        (Mt. Whitney, Yosemite wilderness...)
The family for a permit is detected on first use and remembered.
"""

from __future__ import annotations

import asyncio
import calendar
import datetime as dt
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE = "https://www.recreation.gov"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Campsite statuses that mean "you can book this night right now".
CAMP_OPEN_STATUSES = {"Available"}
# Very large totals mean "no quota" (e.g. 99999 on some permits).
UNLIMITED = 9999


class RecGovError(Exception):
    pass


class NotFound(RecGovError):
    pass


@dataclass(frozen=True)
class Slot:
    """One bookable unit on one date, as reported by Recreation.gov."""

    unit_id: str  # division id (permits) or campsite id (campgrounds)
    unit_name: str
    date: dt.date
    remaining: int
    total: int | None
    capacity: int | None = None  # campsite max people, when known
    counts_people: bool = False  # True when remaining is a count of people, not groups


@dataclass
class FacilityInfo:
    kind: str  # "permit" or "campground"
    facility_id: str
    name: str
    units: dict[str, str] = field(default_factory=dict)  # unit id -> name
    family: str | None = None  # permit API family


def month_starts(start: dt.date, end: dt.date) -> list[dt.date]:
    out = []
    cur = start.replace(day=1)
    while cur <= end:
        out.append(cur)
        days = calendar.monthrange(cur.year, cur.month)[1]
        cur = cur + dt.timedelta(days=days)
    return out


def _parse_day(key: str) -> dt.date:
    return dt.date.fromisoformat(key[:10])


class RecGovClient:
    def __init__(
        self,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_seconds: float = 10.0,
        max_concurrency: int = 4,
    ):
        self._http = httpx.AsyncClient(
            base_url=BASE,
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        )
        # Short cache so several searches watching the same place share one request.
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_seconds = cache_seconds
        self._sem = asyncio.Semaphore(max_concurrency)
        self._families: dict[str, str] = {}
        self._info: dict[tuple[str, str], FacilityInfo] = {}
        self._backoff_until = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ http

    async def _get_json(self, path: str, params: dict | None = None, cache: bool = True) -> Any:
        key = path + "?" + "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        now = time.monotonic()
        if cache and key in self._cache and now - self._cache[key][0] < self._cache_seconds:
            return self._cache[key][1]
        wait = self._backoff_until - now
        if wait > 0:
            await asyncio.sleep(wait)
        async with self._sem:
            for attempt in range(3):
                try:
                    r = await self._http.get(path, params=params)
                except httpx.TransportError as e:
                    if attempt == 2:
                        raise RecGovError(f"network error: {e}") from e
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                if r.status_code == 429 or r.status_code >= 500 and r.status_code != 404:
                    # Back off for everyone, with jitter, so we don't hammer the site.
                    delay = min(60.0, 5.0 * (2**attempt)) + random.random() * 2
                    self._backoff_until = time.monotonic() + delay
                    log.warning("Recreation.gov %s on %s, backing off %.0fs", r.status_code, path, delay)
                    if attempt == 2:
                        raise RecGovError(f"Recreation.gov returned {r.status_code}")
                    await asyncio.sleep(delay)
                    continue
                if r.status_code in (400, 404):
                    raise NotFound(_error_text(r))
                if r.status_code != 200:
                    raise RecGovError(f"Recreation.gov returned {r.status_code}: {_error_text(r)}")
                data = r.json()
                self._cache[key] = (time.monotonic(), data)
                if len(self._cache) > 2000:
                    self._cache.clear()
                return data
        raise RecGovError("unreachable")

    # ---------------------------------------------------------------- search

    async def search(self, query: str, kind: str | None = None, size: int = 10) -> list[dict]:
        """Find permits and campgrounds by name, like the site's search box.

        Uses the site's type-ahead (good with partial words) and then full search.
        """
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()
        rows: list[dict] = []
        try:
            sug = await self._get_json("/api/search/suggest", {"q": query})
            rows += sug.get("inventory_suggestions") or []
        except RecGovError:
            pass
        params: dict[str, Any] = {"q": query, "size": size}
        if kind in ("permit", "campground"):
            params["fq"] = f"entity_type:{kind}"
        data = await self._get_json("/api/search", params)
        rows += data.get("results") or []
        for r in rows:
            et = r.get("entity_type")
            if et not in ("permit", "campground") or (kind and et != kind):
                continue
            key = (et, str(r.get("entity_id")))
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "kind": et,
                    "id": str(r.get("entity_id")),
                    "name": r.get("name", ""),
                    "parent": r.get("parent_name") or "",
                    "state": r.get("state_code") or "",
                }
            )
        return out[:size]

    # ----------------------------------------------------------------- info

    async def facility_info(self, kind: str, facility_id: str) -> FacilityInfo:
        cached = self._info.get((kind, facility_id))
        if cached:
            return cached
        if kind == "permit":
            p = (await self._get_json(f"/api/permitcontent/{facility_id}"))["payload"]
            units = {k: (d.get("name") or k) for k, d in (p.get("divisions") or {}).items()}
            info = FacilityInfo("permit", facility_id, p.get("name") or facility_id, units)
        elif kind == "campground":
            res = await self.search_by_id("campground", facility_id)
            info = FacilityInfo("campground", facility_id, res.get("name") or f"Campground {facility_id}")
        else:
            raise ValueError(kind)
        self._info[(kind, facility_id)] = info
        return info

    async def search_by_id(self, kind: str, facility_id: str) -> dict:
        data = await self._get_json(
            "/api/search", {"fq": [f"entity_id:{facility_id}", f"entity_type:{kind}"], "size": 1}
        )
        res = data.get("results") or []
        return res[0] if res else {}

    # --------------------------------------------------------- availability

    async def availability(
        self, kind: str, facility_id: str, start: dt.date, end: dt.date
    ) -> list[Slot]:
        """All slots with dates in [start, end]. Includes unavailable ones (remaining 0)."""
        if kind == "campground":
            slots = await self._campground(facility_id, start, end)
        elif kind == "permit":
            slots = await self._permit(facility_id, start, end)
        else:
            raise ValueError(kind)
        return [s for s in slots if start <= s.date <= end]

    async def _campground(self, cid: str, start: dt.date, end: dt.date) -> list[Slot]:
        out: list[Slot] = []
        for m in month_starts(start, end):
            data = await self._get_json(
                f"/api/camps/availability/campground/{cid}/month",
                {"start_date": f"{m.isoformat()}T00:00:00.000Z"},
            )
            for site_id, site in (data.get("campsites") or {}).items():
                name = site.get("site") or site_id
                if site.get("loop"):
                    name = f"Site {name} ({site['loop']})"
                else:
                    name = f"Site {name}"
                cap = site.get("max_num_people") or None
                for day, status in (site.get("availabilities") or {}).items():
                    open_ = status in CAMP_OPEN_STATUSES
                    out.append(Slot(str(site_id), name, _parse_day(day), 1 if open_ else 0, 1, cap))
        return out

    async def _permit(self, pid: str, start: dt.date, end: dt.date) -> list[Slot]:
        family = self._families.get(pid)
        if family is None:
            family = await self.detect_permit_family(pid, start)
        info = await self.facility_info("permit", pid)
        if family == "inyo":
            return await self._permit_inyo(pid, start, end, info)
        out: list[Slot] = []
        for m in month_starts(start, end):
            out.extend(await self._permit_month(pid, family, m, info))
        return out

    async def detect_permit_family(self, pid: str, near: dt.date | None = None) -> str:
        near = (near or dt.date.today()).replace(day=1)
        for family in ("permits", "permitwbe", "inyo"):
            try:
                if family == "inyo":
                    await self._get_json(
                        f"/api/permitinyo/{pid}/availability",
                        {
                            "start_date": near.isoformat(),
                            # Inyo only accepts whole months.
                            "end_date": near.replace(
                                day=calendar.monthrange(near.year, near.month)[1]
                            ).isoformat(),
                            "commercial_acct": "false",
                        },
                    )
                else:
                    await self._permit_month_raw(pid, family, near)
            except NotFound as e:
                # permitwbe says this when a season simply isn't open yet: still the right family.
                if family == "permitwbe" and "season" in str(e).lower():
                    break
                continue
            break
        else:
            raise NotFound(f"Recreation.gov has no availability API for permit {pid}")
        self._families[pid] = family
        log.info("permit %s uses the %s API", pid, family)
        return family

    async def _permit_month_raw(self, pid: str, family: str, month: dt.date) -> dict:
        if family == "permits":
            return await self._get_json(
                f"/api/permits/{pid}/availability/month",
                {
                    "start_date": f"{month.isoformat()}T00:00:00.000Z",
                    "commercial_acct": "false",
                    "is_lottery": "false",
                },
            )
        return await self._get_json(
            f"/api/permitwbe/{pid}/availability/month", {"start_date": month.isoformat()}
        )

    async def _permit_month(self, pid: str, family: str, month: dt.date, info: FacilityInfo) -> list[Slot]:
        try:
            data = await self._permit_month_raw(pid, family, month)
        except NotFound as e:
            if "season" in str(e).lower():
                return []  # season not open yet for this month
            raise
        out: list[Slot] = []
        avail = (data.get("payload") or {}).get("availability") or {}
        for div_key, div in avail.items():
            div_id = str(div.get("division_id") or div_key)
            for day, q in (div.get("date_availability") or {}).items():
                total = q.get("total")
                out.append(
                    Slot(
                        div_id,
                        info.units.get(div_id, info.name),
                        _parse_day(day),
                        int(q.get("remaining") or 0),
                        None if total is None or total >= UNLIMITED else int(total),
                    )
                )
        return out

    async def _permit_inyo(self, pid: str, start: dt.date, end: dt.date, info: FacilityInfo) -> list[Slot]:
        out: list[Slot] = []
        for m in month_starts(start, end):
            last = m.replace(day=calendar.monthrange(m.year, m.month)[1])
            data = await self._get_json(
                f"/api/permitinyo/{pid}/availability",
                {"start_date": m.isoformat(), "end_date": last.isoformat(), "commercial_acct": "false"},
            )
            for day, divs in (data.get("payload") or {}).items():
                for div_id, q in (divs or {}).items():
                    if q.get("is_walkup"):
                        continue
                    total = q.get("total")
                    out.append(
                        Slot(
                            str(div_id),
                            info.units.get(str(div_id), f"Trailhead {div_id}"),
                            _parse_day(day),
                            int(q.get("remaining") or 0),
                            None if total is None or total >= UNLIMITED else int(total),
                            counts_people=True,
                        )
                    )
        return out


def _error_text(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error") or r.text[:200])
    except Exception:
        return r.text[:200]


def booking_url(kind: str, facility_id: str, unit_id: str, date: dt.date) -> str:
    if kind == "campground":
        return f"{BASE}/camping/campsites/{unit_id}"
    return f"{BASE}/permits/{facility_id}/registration/detailed-availability?date={date.isoformat()}"
