"""Database models. Works with SQLite on a desktop and Postgres on a server."""

from __future__ import annotations

import datetime as dt
from typing import Iterator

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Group(Base):
    """People who get alerted together. One Discord channel per group."""

    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Reserved for multi-user accounts later; NULL means the local owner.
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    discord_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)

    members: Mapped[list[Member]] = relationship(
        back_populates="group", cascade="all, delete-orphan", order_by="Member.id"
    )
    searches: Mapped[list[Search]] = relationship(back_populates="group")


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    email: Mapped[str] = mapped_column(String(254), default="")
    discord_username: Mapped[str] = mapped_column(String(64), default="")

    group: Mapped[Group] = relationship(back_populates="members")


class Search(Base):
    __tablename__ = "searches"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    interval_seconds: Mapped[int] = mapped_column(Integer, default=45)
    # Smallest party size that must fit (campsite capacity / permit group size).
    group_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Campgrounds: only alert for this many nights in a row or more.
    min_nights: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    group: Mapped[Group] = relationship(back_populates="searches")
    targets: Mapped[list[Target]] = relationship(
        back_populates="search", cascade="all, delete-orphan", order_by="Target.id"
    )
    openings: Mapped[list[Opening]] = relationship(
        back_populates="search", cascade="all, delete-orphan"
    )


class Target(Base):
    """One permit or campground a search watches."""

    __tablename__ = "targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    search_id: Mapped[int] = mapped_column(ForeignKey("searches.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))  # "permit" or "campground"
    facility_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(200), default="")
    # Optional narrowing: permit division ids (launch points/entrances) or
    # campsite ids. Empty means everything at the facility.
    only_ids: Mapped[list] = mapped_column(JSON, default=list)

    search: Mapped[Search] = relationship(back_populates="targets")


class Opening(Base):
    """A slot that became available during a search. The trend log."""

    __tablename__ = "openings"

    id: Mapped[int] = mapped_column(primary_key=True)
    search_id: Mapped[int] = mapped_column(
        ForeignKey("searches.id", ondelete="CASCADE"), index=True
    )
    target_id: Mapped[int] = mapped_column(ForeignKey("targets.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(20))
    facility_id: Mapped[str] = mapped_column(String(40), index=True)
    facility_name: Mapped[str] = mapped_column(String(200), default="")
    # Stable key for the slot: division or campsite id plus start date (+nights).
    slot_key: Mapped[str] = mapped_column(String(200), index=True)
    slot_label: Mapped[str] = mapped_column(String(200), default="")
    date: Mapped[dt.date] = mapped_column(Date)
    nights: Mapped[int] = mapped_column(Integer, default=1)
    remaining: Mapped[int] = mapped_column(Integer, default=1)
    total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    booking_url: Mapped[str] = mapped_column(String(300), default="")
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # Set when this is what's left of an earlier opening (e.g. part of a campsite
    # run got booked). Not counted as a new opening and not re-alerted.
    continues_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # True when it was already open on the search's first check.
    on_first_check: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    alerted_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    reminded_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    search: Mapped[Search] = relationship(back_populates="openings")


class Database:
    def __init__(self, url: str):
        kwargs = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
        self.engine: Engine = create_engine(url, **kwargs)
        if url.startswith("sqlite"):
            event.listen(self.engine, "connect", _sqlite_pragmas)
        self.sessionmaker = sessionmaker(self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Tiny upgrade step: add columns that newer versions introduced.

        Only nullable/defaulted columns are ever added this way, so it is safe
        to run on every start.
        """
        insp = inspect(self.engine)
        with self.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                existing = {c["name"] for c in insp.get_columns(table.name)}
                for col in table.columns:
                    if col.name in existing:
                        continue
                    ddl = col.type.compile(dialect=self.engine.dialect)
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))

    def session(self) -> Session:
        return self.sessionmaker()

    def session_scope(self) -> Iterator[Session]:
        with self.sessionmaker() as s:
            yield s


def _sqlite_pragmas(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()
