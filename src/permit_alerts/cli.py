"""Command line entry point: `permit-alerts <command>`."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
import sys

from .matching import Filters, match
from .recgov.client import RecGovClient


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


async def _find(args) -> int:
    c = RecGovClient()
    try:
        for r in await c.search(args.query, kind=args.kind, size=args.size):
            parent = f"  ({r['parent']})" if r["parent"] else ""
            print(f"{r['kind']:<10} {r['id']:<10} {r['name']}{parent}")
    finally:
        await c.aclose()
    return 0


async def _check(args) -> int:
    """One-off check that prints what a search would alert on right now."""
    c = RecGovClient()
    try:
        info = await c.facility_info(args.kind, args.id)
        slots = await c.availability(args.kind, args.id, args.start, args.end)
        f = Filters(
            start=args.start,
            end=args.end,
            group_size=args.group_size,
            min_nights=args.min_nights,
            only_ids=frozenset(args.only or []),
        )
        found = match(args.kind, args.id, slots, f)
        family = c._families.get(args.id, "campground" if args.kind == "campground" else "?")
        print(f"{info.name} [{args.kind} {args.id}, api={family}]")
        print(f"  {len(slots)} slots checked between {args.start} and {args.end}, {len(found)} open matches")
        for x in found[: args.limit]:
            extra = f"{x.nights} night(s)" if args.kind == "campground" else (
                f"{x.remaining} left" if x.total is None else f"{x.remaining}/{x.total} left"
            )
            print(f"  {x.date}  {x.label[:50]:<50} {extra}")
        if len(found) > args.limit:
            print(f"  ... {len(found) - args.limit} more")
    finally:
        await c.aclose()
    return 0


def _run(args) -> int:
    from .app import run_app

    return run_app(open_browser=not args.no_browser)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="permit-alerts")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="start the checker, Discord bot, and dashboard (default)")
    r.add_argument("--no-browser", action="store_true", help="don't open the dashboard")

    f = sub.add_parser("find", help="look up permit and campground ids by name")
    f.add_argument("query")
    f.add_argument("--kind", choices=["permit", "campground"])
    f.add_argument("--size", type=int, default=10)

    c = sub.add_parser("check", help="check one permit or campground once and print matches")
    c.add_argument("kind", choices=["permit", "campground"])
    c.add_argument("id")
    c.add_argument("start", type=_date)
    c.add_argument("end", type=_date)
    c.add_argument("--group-size", type=int)
    c.add_argument("--min-nights", type=int, default=1)
    c.add_argument("--only", nargs="*", help="division or campsite ids to limit to")
    c.add_argument("--limit", type=int, default=20)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        for noisy in ("httpx", "discord", "discord.gateway", "discord.client"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        logging.getLogger("discord.client").setLevel(logging.ERROR)  # hides voice-support notices
    if args.cmd == "find":
        return asyncio.run(_find(args))
    if args.cmd == "check":
        return asyncio.run(_check(args))
    if args.cmd in (None, "run"):
        if args.cmd is None:
            args.no_browser = False
        return _run(args)
    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
