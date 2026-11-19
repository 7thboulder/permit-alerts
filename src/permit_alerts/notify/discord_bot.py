"""Discord bot that runs the whole alert server.

The bot needs a token from the Discord developer portal and to be invited to
one server with the Manage Channels, Manage Roles, Create Invite and Send
Messages permissions (Administrator is simplest). It then:
  * creates a "Permit Alerts" category,
  * creates one private channel per group, named after the group,
  * gives each group member (by Discord username) access to their channel,
  * posts alerts there and pings the group.
Nobody has to paste webhooks or create channels by hand.
"""

from __future__ import annotations

import asyncio
import logging
import re

import discord

from ..config import Settings
from .base import Alert

log = logging.getLogger(__name__)

ALERT_COLOR = 0x2E7D32
REMINDER_COLOR = 0xF9A825


def channel_name(group_name: str, group_id: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", group_name.lower()).strip("-")[:80] or "group"
    return f"{slug}-{group_id}"


class DiscordManager:
    """Owns the bot connection. Also acts as the Discord notifier."""

    name = "discord"

    def __init__(self, settings: Settings):
        self.settings = settings
        intents = discord.Intents.default()
        intents.members = True  # needed to find group members by username
        self.client = discord.Client(intents=intents)
        self.ready = asyncio.Event()
        self.status = "not started"
        self._invite_url: str | None = None

        @self.client.event
        async def on_ready():
            guild = self.client.get_guild(settings.discord_guild_id)
            if guild is None:
                self.status = (
                    f"Bot is logged in as {self.client.user} but is not in server "
                    f"{settings.discord_guild_id}. Invite it using the link on the Settings page."
                )
                log.error(self.status)
                return
            self.status = f"Connected as {self.client.user} to {guild.name}"
            log.info(self.status)
            self.ready.set()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if not self.settings.discord_enabled:
            self.status = "Discord is not set up yet"
            return
        self.status = "connecting"
        try:
            await self.client.start(self.settings.discord_token)
        except discord.LoginFailure:
            self.status = "Discord rejected the bot token. Check it on the Settings page."
            log.error(self.status)
        except discord.PrivilegedIntentsRequired:
            self.status = (
                "Turn on 'Server Members Intent' for the bot in the Discord developer portal, "
                "then restart Permit Alerts."
            )
            log.error(self.status)
        except Exception as e:  # keep the rest of the app running
            self.status = f"Discord error: {e}"
            log.exception("Discord bot stopped")

    async def close(self) -> None:
        if not self.client.is_closed():
            await self.client.close()

    @property
    def configured(self) -> bool:
        return self.settings.discord_enabled

    @property
    def guild(self) -> discord.Guild | None:
        return self.client.get_guild(self.settings.discord_guild_id)

    async def _wait_ready(self, timeout: float = 20) -> discord.Guild:
        await asyncio.wait_for(self.ready.wait(), timeout)
        g = self.guild
        if g is None:
            raise RuntimeError("Discord bot is not in the configured server")
        return g

    # ---------------------------------------------------------- server setup

    async def _category(self, guild: discord.Guild) -> discord.CategoryChannel:
        name = self.settings.discord_category_name
        for c in guild.categories:
            if c.name == name:
                return c
        return await guild.create_category(name, reason="Permit Alerts setup")

    async def ensure_group_channel(
        self, group_id: int, group_name: str, channel_id: int | None, usernames: list[str]
    ) -> tuple[int, list[str]]:
        """Create or update a group's private channel.

        Returns (channel id, usernames that aren't in the server yet).
        """
        guild = await self._wait_ready()
        channel = guild.get_channel(channel_id) if channel_id else None
        category = await self._category(guild)
        if channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, embed_links=True, mention_everyone=True
                ),
            }
            channel = await guild.create_text_channel(
                channel_name(group_name, group_id),
                category=category,
                overwrites=overwrites,
                topic=f"Permit alerts for {group_name}",
                reason="Permit Alerts group",
            )
            log.info("created Discord channel #%s for group %s", channel.name, group_id)
        elif channel.name != channel_name(group_name, group_id):
            await channel.edit(name=channel_name(group_name, group_id))

        missing = []
        wanted: set[int] = set()
        for username in usernames:
            member = await self._find_member(guild, username)
            if member is None:
                missing.append(username)
                continue
            wanted.add(member.id)
            if not channel.permissions_for(member).view_channel:
                await channel.set_permissions(
                    member, view_channel=True, send_messages=True, read_message_history=True
                )
        # Remove people who were taken out of the group.
        for target in list(channel.overwrites):
            if isinstance(target, discord.Member) and target.id != guild.me.id and target.id not in wanted:
                await channel.set_permissions(target, overwrite=None)
        return channel.id, missing

    async def delete_group_channel(self, channel_id: int | None) -> None:
        if not channel_id:
            return
        guild = await self._wait_ready()
        channel = guild.get_channel(channel_id)
        if channel is not None:
            await channel.delete(reason="Permit Alerts group deleted")

    async def _find_member(self, guild: discord.Guild, username: str) -> discord.Member | None:
        u = username.strip().lstrip("@").lower()
        if not u:
            return None
        for m in guild.members:
            if m.name.lower() == u or (m.global_name or "").lower() == u:
                return m
        try:
            found = await guild.query_members(query=u, limit=5)
        except Exception:
            found = []
        for m in found:
            if m.name.lower() == u:
                return m
        return None

    async def invite_url(self) -> str:
        """A reusable invite to the server so group members can join."""
        if self._invite_url:
            return self._invite_url
        guild = await self._wait_ready()
        category = await self._category(guild)
        target = guild.system_channel or next(
            (c for c in guild.text_channels if c.category != category), None
        )
        if target is None:
            target = await guild.create_text_channel("welcome")
        invite = await target.create_invite(max_age=0, max_uses=0, unique=False, reason="Permit Alerts")
        self._invite_url = invite.url
        return invite.url

    def bot_invite_link(self) -> str | None:
        if self.client.user is None:
            return None
        perms = discord.Permissions(administrator=True)
        return discord.utils.oauth_url(self.client.user.id, permissions=perms, scopes=("bot",))

    # ------------------------------------------------------------- notifying

    async def send(self, alert: Alert) -> None:
        if not alert.discord_channel_id:
            return  # group not linked to Discord yet
        guild = await self._wait_ready()
        channel = guild.get_channel(alert.discord_channel_id)
        if channel is None:
            raise RuntimeError(f"Discord channel {alert.discord_channel_id} is gone")
        embed = discord.Embed(
            title=alert.title()[:256],
            color=REMINDER_COLOR if alert.reminder else ALERT_COLOR,
        )
        for line, item in list(zip(alert.lines(limit=20), alert.items))[:20]:
            name, _, rest = line.partition(" · ")
            embed.add_field(
                name=rest[:256] or name[:256],
                value=f"{name[:200]}\n[Book on Recreation.gov]({item.booking_url})",
                inline=False,
            )
        if len(alert.items) > 20:
            embed.set_footer(text=f"…and {len(alert.items) - 20} more. See the dashboard.")
        # Ping everyone who can see this private channel, except for the start-up summary.
        content = None if alert.baseline else "@everyone"
        await channel.send(
            content=content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=True),
        )
