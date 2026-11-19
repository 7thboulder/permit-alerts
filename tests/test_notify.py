import datetime as dt

from permit_alerts.config import Settings
from permit_alerts.notify.base import Alert, AlertItem
from permit_alerts.notify.discord_bot import DiscordManager, channel_name
from permit_alerts.notify.email import build_message


def alert(n=2, **kw):
    items = [AlertItem("Dinosaur Green And Yampa River Permits", "Gates of Lodore", dt.date(2027, 7, 1) + dt.timedelta(days=i), 1, 1, 1,
                       f"https://www.recreation.gov/permits/250014/x?{i}", "permit") for i in range(n)]
    return Alert(1, "Lodore July", 1, "Raft crew", emails=["a@example.com", "b@example.com"],
                 discord_channel_id=123, items=items, **kw)


def test_channel_name():
    assert channel_name("Raft Crew 2027!", 7) == "raft-crew-2027-7"


def test_email_bcc_and_links():
    msg = build_message(alert(), "me@example.com")
    assert msg["Bcc"] == "a@example.com, b@example.com"
    assert "2 new openings for Lodore July" == msg["Subject"]
    assert "recreation.gov/permits/250014" in msg.get_body(("html",)).get_content()


async def test_discord_send_builds_embed():
    sent = {}

    class FakeChannel:
        async def send(self, **kw):
            sent.update(kw)

    class FakeGuild:
        def get_channel(self, cid):
            return FakeChannel() if cid == 123 else None

    mgr = DiscordManager(Settings(_env_file=None, discord_token="x", discord_guild_id=1))
    mgr.ready.set()
    mgr.client.get_guild = lambda gid: FakeGuild()
    await mgr.send(alert(25))
    emb = sent["embed"]
    assert sent["content"] == "@everyone"
    assert len(emb.fields) == 20 and "and 5 more" in emb.footer.text
    assert "Book on Recreation.gov" in emb.fields[0].value

    sent.clear()
    await mgr.send(alert(1, baseline=True))
    assert sent["content"] is None
