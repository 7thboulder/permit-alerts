"""Dashboard pages and the small JSON API they use."""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from collections import Counter, defaultdict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from ..db import Group, Member, Opening, Search, Target, utcnow
from ..notify.base import Alert, AlertItem
from ..recgov.client import RecGovError

log = logging.getLogger(__name__)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic(auto_error=False)

# Settings the Settings page may write to the .env file.
EDITABLE = [
    ("discord_token", "Discord bot token", True),
    ("discord_guild_id", "Discord server ID", False),
    ("smtp_host", "SMTP host", False),
    ("smtp_port", "SMTP port", False),
    ("smtp_username", "SMTP username", False),
    ("smtp_password", "SMTP password", True),
    ("smtp_from", "Send alerts from (email)", False),
]


def _flash(request: Request, url: str, msg: str, kind: str = "ok") -> RedirectResponse:
    from urllib.parse import quote

    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}msg={quote(msg)}&kind={kind}", status_code=303)


def parse_members(text: str) -> list[dict]:
    """One member per line: name, phone, email, discord username (comma separated).

    Fields can be left blank, e.g. "Sam, , sam@example.com, samraft".
    Lines with just an email or just a @discordname also work.
    """
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 1:
            p = parts[0]
            if "@" in p and not p.startswith("@"):
                out.append({"name": "", "phone": "", "email": p, "discord_username": ""})
            elif p.startswith("@"):
                out.append({"name": "", "phone": "", "email": "", "discord_username": p[1:]})
            elif sum(ch.isdigit() for ch in p) >= 7:
                out.append({"name": "", "phone": p, "email": "", "discord_username": ""})
            else:
                out.append({"name": p, "phone": "", "email": "", "discord_username": ""})
            continue
        parts += [""] * (4 - len(parts))
        out.append(
            {
                "name": parts[0],
                "phone": parts[1],
                "email": parts[2],
                "discord_username": parts[3].lstrip("@"),
            }
        )
    return out


def members_text(group: Group | None) -> str:
    if not group:
        return ""
    return "\n".join(
        ", ".join([m.name, m.phone, m.email, m.discord_username]).rstrip(", ") for m in group.members
    )


def write_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text().splitlines() if path.exists() else []
    keys = {f"PERMIT_ALERTS_{k.upper()}": v for k, v in updates.items()}
    seen = set()
    out = []
    for line in lines:
        k = line.split("=", 1)[0].strip()
        if k in keys:
            out.append(f"{k}={keys[k]}")
            seen.add(k)
        else:
            out.append(line)
    for k, v in keys.items():
        if k not in seen:
            out.append(f"{k}={v}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n")


def build_router(services) -> APIRouter:
    settings = services.settings
    db = services.db

    def auth(creds: Annotated[HTTPBasicCredentials | None, Depends(security)]):
        pw = settings.dashboard_password
        if not pw:
            return
        if creds is None or not secrets.compare_digest(creds.password, pw):
            raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})

    r = APIRouter(dependencies=[Depends(auth)])

    def render(request: Request, name: str, **ctx):
        ctx.setdefault("msg", request.query_params.get("msg"))
        ctx.setdefault("kind", request.query_params.get("kind", "ok"))
        ctx["discord_status"] = services.discord.status
        ctx["email_on"] = settings.email_enabled
        return TEMPLATES.TemplateResponse(request, name, ctx)

    async def sync_discord(group: Group) -> str:
        if not settings.discord_enabled or not services.discord.ready.is_set():
            return ""
        try:
            channel_id, missing = await services.discord.ensure_group_channel(
                group.id,
                group.name,
                group.discord_channel_id,
                [m.discord_username for m in group.members if m.discord_username],
            )
        except Exception as e:
            log.exception("Discord sync failed")
            return f" Discord channel could not be updated: {e}"
        group.discord_channel_id = channel_id
        if missing:
            return (
                " These Discord users aren't in your server yet, so send them the invite link "
                f"on the Groups page: {', '.join(missing)}."
            )
        return " Discord channel is ready."

    # ---------------------------------------------------------------- pages

    @r.get("/", response_class=HTMLResponse)
    def home(request: Request):
        with db.session() as s:
            searches = list(
                s.scalars(
                    select(Search)
                    .options(selectinload(Search.targets), selectinload(Search.group))
                    .order_by(Search.active.desc(), Search.created_at.desc())
                )
            )
            counts = dict(
                s.execute(
                    select(Opening.search_id, func.count())
                    .where(Opening.on_first_check.is_(False), Opening.continues_id.is_(None))
                    .group_by(Opening.search_id)
                ).all()
            )
            open_now = dict(
                s.execute(
                    select(Opening.search_id, func.count())
                    .where(Opening.closed_at.is_(None))
                    .group_by(Opening.search_id)
                ).all()
            )
            recent = list(
                s.scalars(
                    select(Opening)
                    .where(Opening.on_first_check.is_(False), Opening.continues_id.is_(None))
                    .order_by(Opening.opened_at.desc())
                    .limit(15)
                )
            )
            names = {x.id: x.name for x in searches}
        return render(
            request, "home.html", searches=searches, counts=counts, open_now=open_now,
            recent=recent, names=names, now=utcnow(),
        )

    @r.get("/searches/new", response_class=HTMLResponse)
    def new_search(request: Request):
        with db.session() as s:
            groups = list(s.scalars(select(Group).options(selectinload(Group.members)).order_by(Group.name)))
        today = dt.date.today()
        return render(
            request, "search_form.html", search=None, groups=groups, targets=[],
            defaults={"start": today, "end": today + dt.timedelta(days=30),
                      "interval": settings.default_interval_seconds},
            min_interval=settings.min_interval_seconds,
        )

    @r.get("/searches/{sid}/edit", response_class=HTMLResponse)
    def edit_search(request: Request, sid: int):
        with db.session() as s:
            search = s.scalar(
                select(Search).where(Search.id == sid)
                .options(selectinload(Search.targets), selectinload(Search.group))
            )
            if not search:
                raise HTTPException(404)
            groups = list(s.scalars(select(Group).options(selectinload(Group.members)).order_by(Group.name)))
            targets = [
                {"kind": t.kind, "id": t.facility_id, "name": t.name, "only": ",".join(map(str, t.only_ids or []))}
                for t in search.targets
            ]
        return render(
            request, "search_form.html", search=search, groups=groups, targets=targets,
            defaults={"start": search.start_date, "end": search.end_date, "interval": search.interval_seconds},
            min_interval=settings.min_interval_seconds,
        )

    @r.post("/searches/save")
    async def save_search(request: Request):
        form = await request.form()
        sid = form.get("id")
        try:
            start = dt.date.fromisoformat(str(form.get("start_date")))
            end = dt.date.fromisoformat(str(form.get("end_date")))
        except ValueError:
            return _flash(request, "/searches/new", "Please pick a start and end date.", "error")
        if end < start:
            return _flash(request, "/searches/new", "The end date is before the start date.", "error")
        kinds = form.getlist("target_kind")
        ids = form.getlist("target_id")
        tnames = form.getlist("target_name")
        onlys = form.getlist("target_only")
        targets = [
            (k, i.strip(), n, [x.strip() for x in (o or "").split(",") if x.strip()])
            for k, i, n, o in zip(kinds, ids, tnames, onlys)
            if i.strip() and k in ("permit", "campground")
        ]
        if not targets:
            return _flash(request, "/searches/new", "Add at least one permit or campground.", "error")
        interval = max(settings.min_interval_seconds, int(form.get("interval_seconds") or 45))
        group_size = int(form.get("group_size")) if form.get("group_size") else None
        min_nights = max(1, int(form.get("min_nights") or 1))

        note = ""
        with db.session() as s:
            # Group: an existing one, or a new one from the members box.
            group_choice = str(form.get("group_id") or "new")
            if group_choice == "new":
                members = parse_members(str(form.get("members") or ""))
                gname = str(form.get("group_name") or "").strip() or str(form.get("name") or "My group")
                group = Group(name=gname, members=[Member(**m) for m in members])
                s.add(group)
                s.flush()
                note = await sync_discord(group)
            else:
                group = s.get(Group, int(group_choice))
                if group is None:
                    return _flash(request, "/searches/new", "That group no longer exists.", "error")

            if sid:
                search = s.get(Search, int(sid))
                if search is None:
                    raise HTTPException(404)
                changed_window = (search.start_date, search.end_date) != (start, end)
            else:
                search = Search(created_at=utcnow())
                s.add(search)
                changed_window = False
            search.name = str(form.get("name") or "").strip() or targets[0][2] or "Search"
            search.group = group
            search.start_date, search.end_date = start, end
            search.interval_seconds = interval
            search.group_size = group_size
            search.min_nights = min_nights
            search.active = True

            existing = {(t.kind, t.facility_id): t for t in search.targets}
            keep = []
            for kind, fid, name, only in targets:
                t = existing.get((kind, fid)) or Target(kind=kind, facility_id=fid)
                t.name = name or t.name
                t.only_ids = only
                keep.append(t)
            search.targets = keep
            if changed_window:
                # Avoid a flood of "new" alerts for dates that were only just added to the window.
                search.last_checked_at = None
                for o in search.openings:
                    if o.closed_at is None:
                        o.closed_at = utcnow()
            s.commit()
            sid = search.id
        services.checker.reconcile()
        return _flash(request, "/", f"Search saved and running.{note}")

    @r.post("/searches/{sid}/toggle")
    def toggle_search(request: Request, sid: int):
        with db.session() as s:
            search = s.get(Search, sid)
            if not search:
                raise HTTPException(404)
            search.active = not search.active
            if search.active:
                search.last_error = None
            s.commit()
            state = "resumed" if search.active else "paused"
        services.checker.reconcile()
        return _flash(request, "/", f"Search {state}.")

    @r.post("/searches/{sid}/delete")
    def delete_search(request: Request, sid: int):
        with db.session() as s:
            search = s.get(Search, sid)
            if search:
                s.delete(search)
                s.commit()
        services.checker.reconcile()
        return _flash(request, "/", "Search deleted (its opening history was removed too).")

    @r.get("/searches/{sid}", response_class=HTMLResponse)
    def search_detail(request: Request, sid: int):
        with db.session() as s:
            search = s.scalar(
                select(Search).where(Search.id == sid)
                .options(selectinload(Search.targets), selectinload(Search.group).selectinload(Group.members))
            )
            if not search:
                raise HTTPException(404)
            openings = list(
                s.scalars(select(Opening).where(Opening.search_id == sid).order_by(Opening.opened_at.desc()).limit(500))
            )
        return render(request, "search_detail.html", search=search, openings=openings, now=utcnow())

    # --------------------------------------------------------------- groups

    @r.get("/groups", response_class=HTMLResponse)
    async def groups_page(request: Request):
        with db.session() as s:
            groups = list(
                s.scalars(select(Group).options(selectinload(Group.members), selectinload(Group.searches)).order_by(Group.name))
            )
        invite = None
        if services.discord.ready.is_set():
            try:
                invite = await services.discord.invite_url()
            except Exception as e:
                log.warning("could not make invite: %s", e)
        return render(request, "groups.html", groups=groups, invite=invite, members_text=members_text)

    @r.post("/groups/save")
    async def save_group(
        request: Request,
        id: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = "",
        members: Annotated[str, Form()] = "",
    ):
        with db.session() as s:
            group = s.get(Group, int(id)) if id else None
            if group is None:
                group = Group()
                s.add(group)
            group.name = name.strip() or "Group"
            group.members = [Member(**m) for m in parse_members(members)]
            s.flush()
            note = await sync_discord(group)
            s.commit()
        return _flash(request, "/groups", f"Group saved.{note}")

    @r.post("/groups/{gid}/delete")
    async def delete_group(request: Request, gid: int):
        with db.session() as s:
            group = s.get(Group, gid)
            if group is None:
                raise HTTPException(404)
            if group.searches:
                return _flash(request, "/groups", "Delete or move this group's searches first.", "error")
            channel_id = group.discord_channel_id
            s.delete(group)
            s.commit()
        if channel_id and services.discord.ready.is_set():
            try:
                await services.discord.delete_group_channel(channel_id)
            except Exception as e:
                log.warning("could not delete channel: %s", e)
        return _flash(request, "/groups", "Group deleted.")

    @r.post("/groups/{gid}/test")
    async def test_group(request: Request, gid: int):
        with db.session() as s:
            group = s.scalar(select(Group).where(Group.id == gid).options(selectinload(Group.members)))
            if group is None:
                raise HTTPException(404)
            note = await sync_discord(group)
            s.commit()
            alert = Alert(
                search_id=0, search_name="Test alert", group_id=group.id, group_name=group.name,
                emails=[m.email for m in group.members if m.email],
                phones=[m.phone for m in group.members if m.phone],
                discord_channel_id=group.discord_channel_id,
                items=[AlertItem("Example River Permit", "Example launch", dt.date.today(), 1, 1, 4,
                                 "https://www.recreation.gov", "permit")],
            )
        results = []
        for n in services.checker.notifiers:
            if not getattr(n, "configured", True):
                results.append(f"{n.name}: not set up (see Settings)")
                continue
            if n.name == "email" and not alert.emails:
                results.append("email: no email addresses in this group")
                continue
            if n.name == "discord" and not alert.discord_channel_id:
                results.append("discord: group has no channel yet")
                continue
            try:
                await n.send(alert)
                results.append(f"{n.name}: sent")
            except Exception as e:
                results.append(f"{n.name}: failed ({e})")
        return _flash(request, "/groups", "Test alert: " + "; ".join(results) + "." + note)

    # ---------------------------------------------------------------- trends

    @r.get("/trends", response_class=HTMLResponse)
    def trends(request: Request, facility: str = ""):
        with db.session() as s:
            facilities = s.execute(
                select(Opening.facility_id, Opening.facility_name, func.count())
                .where(Opening.on_first_check.is_(False), Opening.continues_id.is_(None))
                .group_by(Opening.facility_id, Opening.facility_name)
                .order_by(func.count().desc())
            ).all()
            q = select(Opening).where(Opening.on_first_check.is_(False), Opening.continues_id.is_(None))
            if facility:
                q = q.where(Opening.facility_id == facility)
            rows = list(s.scalars(q))
        by_day: Counter = Counter()
        by_hour: Counter = Counter()
        by_weekday: Counter = Counter()
        by_trip_month: Counter = Counter()
        lead_days: list[int] = []
        durations: list[float] = []
        for o in rows:
            local = o.opened_at.replace(tzinfo=dt.timezone.utc).astimezone()
            by_day[local.date().isoformat()] += 1
            by_hour[local.hour] += 1
            by_weekday[local.strftime("%a")] += 1
            by_trip_month[o.date.strftime("%Y-%m")] += 1
            lead_days.append((o.date - local.date()).days)
            if o.closed_at:
                durations.append((o.closed_at - o.opened_at).total_seconds() / 60)
        durations.sort()
        stats = {
            "total": len(rows),
            "median_minutes_open": round(durations[len(durations) // 2], 1) if durations else None,
            "median_lead_days": sorted(lead_days)[len(lead_days) // 2] if lead_days else None,
        }
        days = sorted(by_day)
        chart = {
            "days": {"labels": days, "values": [by_day[d] for d in days]},
            "hours": {"labels": [f"{h:02d}:00" for h in range(24)], "values": [by_hour[h] for h in range(24)]},
            "weekdays": {
                "labels": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
                "values": [by_weekday[d] for d in ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]],
            },
            "trip_months": {"labels": sorted(by_trip_month), "values": [by_trip_month[m] for m in sorted(by_trip_month)]},
        }
        return render(request, "trends.html", facilities=facilities, facility=facility, chart=chart, stats=stats)

    # -------------------------------------------------------------- settings

    @r.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        values = {k: getattr(settings, k) for k, _, _ in EDITABLE}
        return render(
            request, "settings.html", fields=EDITABLE, values=values, data_dir=settings.data_dir,
            bot_invite=services.discord.bot_invite_link(),
        )

    @r.post("/settings")
    async def save_settings(request: Request):
        form = await request.form()
        updates = {}
        for key, _, secret in EDITABLE:
            val = str(form.get(key) or "").strip()
            if secret and not val:
                continue  # blank secret field = keep the current value
            updates[key] = val
        write_env(settings.data_dir / ".env", updates)
        # Email settings apply right away; Discord needs a restart to reconnect.
        for key, val in updates.items():
            if key.startswith("smtp_"):
                setattr(settings, key, int(val) if key == "smtp_port" and val else val)
        return _flash(request, "/settings", "Saved. Restart Permit Alerts for Discord changes to take effect.")

    # ------------------------------------------------------------------ api

    @r.get("/api/find")
    async def api_find(q: str, kind: str | None = None):
        if len(q.strip()) < 2:
            return JSONResponse([])
        try:
            return JSONResponse(await services.client.search(q, kind=kind, size=12))
        except RecGovError as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @r.get("/api/units")
    async def api_units(kind: str, id: str):
        """Launch points / trailheads for a permit, so a search can be narrowed."""
        if kind != "permit":
            return JSONResponse({})
        try:
            info = await services.client.facility_info(kind, id)
        except RecGovError as e:
            return JSONResponse({"error": str(e)}, status_code=502)
        return JSONResponse(info.units)

    @r.get("/healthz")
    def healthz():
        return {"ok": True, "discord": services.discord.status}

    return r
