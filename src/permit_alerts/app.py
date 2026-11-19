"""Wires everything together and runs it in one process.

One asyncio loop runs: the dashboard (FastAPI/uvicorn), the search checker,
and the Discord bot. On a server the same `permit-alerts run` command works;
set PERMIT_ALERTS_HOST=0.0.0.0, a DATABASE_URL and a DASHBOARD_PASSWORD.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import webbrowser
from contextlib import asynccontextmanager
from dataclasses import dataclass

import uvicorn
from fastapi import FastAPI

from .checker import Checker
from .config import Settings, get_settings
from .db import Database
from .notify.discord_bot import DiscordManager
from .notify.email import EmailNotifier
from .recgov.client import RecGovClient

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    db: Database
    client: RecGovClient
    discord: DiscordManager
    email: EmailNotifier
    checker: Checker


def build_services(settings: Settings | None = None) -> Services:
    settings = settings or get_settings()
    db = Database(settings.resolved_database_url())
    db.create_all()
    client = RecGovClient(timeout=settings.request_timeout_seconds)
    discord_mgr = DiscordManager(settings)
    email = EmailNotifier(settings)
    checker = Checker(db, client, [discord_mgr, email], settings)
    return Services(settings, db, client, discord_mgr, email, checker)


def create_app(services: Services | None = None, start_background: bool = True) -> FastAPI:
    services = services or build_services()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if start_background:
            tasks.append(asyncio.create_task(services.discord.start(), name="discord"))
            tasks.append(asyncio.create_task(services.checker.run_forever(), name="checker"))
        try:
            yield
        finally:
            services.checker.stop()
            await services.discord.close()
            for t in tasks:
                t.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            await services.client.aclose()

    from .web.routes import build_router

    app = FastAPI(title="Permit Alerts", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.services = services
    app.include_router(build_router(services))
    return app


def run_app(open_browser: bool = True) -> int:
    settings = get_settings()
    app = create_app(build_services(settings))
    url = f"http://{'localhost' if settings.host in ('127.0.0.1', '0.0.0.0') else settings.host}:{settings.port}"
    log.info("Dashboard: %s   (data folder: %s)", url, settings.data_dir)
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    return 0
