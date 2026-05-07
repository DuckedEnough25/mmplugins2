# ticketallot.py — Ticket Allotment Plugin for Modmail
# Dynamically assigns new tickets to staff by role-based ratio distribution.
#
# Commands (prefix: ticketallot  |  alias: ta)
# ─────────────────────────────────────────────
#  ticketallot                         — Help overview
#  ticketallot status                  — Live ratio & plugin status
#  ticketallot toggle                  — Enable / disable plugin
#  ticketallot alertchannel #ch        — Set escalation alert channel
#  ticketallot reset                   — Wipe all assignment records
#
#  ticketallot role add @R ratio [dl] [esc]   — Add / update role
#  ticketallot role remove @R                 — Remove role
#  ticketallot role view                      — List all roles
#  ticketallot role deadline @R hours         — Update deadline for role
#  ticketallot role escalation @R hours       — Update escalation time for role
#
#  ticketallot assign #ch @member      — Manually assign a ticket
#  ticketallot complete [#ch]          — Mark ticket complete
#  ticketallot dashboard [mod]         — Paginated pending/completed view
#  ticketallot category @cat           — Set personal ticket category
#  ticketallot reminder R P            — Set personal reminder intervals
#  ticketallot remind stop             — Reset reminder cycle for ticket
#  ticketallot rolecheck               — Show current distribution vs targets
#
# Requirements: discord.py 2.x, babel

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Union, TYPE_CHECKING

import discord
import parsedatetime as pdt
from discord.ext import commands, tasks
from dateutil.relativedelta import relativedelta

from core import checks
from core.models import PermissionLevel
from core.paginator import EmbedPaginatorSession

# Monkey patch mins and secs into the units for parsedatetime
units = pdt.pdtLocales["en_US"].units
units["minutes"].append("mins")
units["minutes"].append("min")
units["seconds"].append("secs")
units["seconds"].append("sec")
units["hours"].append("hr")
units["hours"].append("hrs")

if TYPE_CHECKING:
    from discord.ext.commands import Context
    from typing_extensions import Self


def utcnow() -> datetime:
    return discord.utils.utcnow()


def ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime object is UTC aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def chunks(lst: list, n: int):
    """Split list into chunks of size n."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def human_join(seq: List[str], delim: str = ", ", final: str = "or") -> str:
    size = len(seq)
    if size == 0:
        return ""
    if size == 1:
        return seq[0]
    if size == 2:
        return f"{seq[0]} {final} {seq[1]}"
    return f"{delim.join(seq[:-1])}{delim}{final} {seq[-1]}"


class plural:
    def __init__(self, value: int):
        self.value: int = value

    def __format__(self, format_spec: str) -> str:
        v = self.value
        singular, sep, plural = format_spec.partition("|")
        plural = plural or f"{singular}s"
        if abs(v) != 1:
            return f"{v} {plural}"
        return f"{v} {singular}"


class ShortTime:
    compiled = re.compile(
        """
           (?:(?P<years>[0-9]+)\s*(?:years?|y))?             # e.g. 2y
           (?:(?P<months>[0-9]{1,2})\s*(?:months?|mo))?      # e.g. 2months
           (?:(?P<weeks>[0-9]{1,4})\s*(?:weeks?|w))?         # e.g. 10w
           (?:(?P<days>[0-9]{1,5})\s*(?:days?|d))?           # e.g. 14d
           (?:(?P<hours>[0-9]{1,5})\s*(?:hours?|h|hrs?))?    # e.g. 12h, 10 hr
           (?:(?P<minutes>[0-9]{1,5})\s*(?:minutes?|m|mins?))? # e.g. 10m, 5 min
           (?:(?P<seconds>[0-9]{1,5})\s*(?:seconds?|s|secs?))? # e.g. 15s, 30 sec
        """,
        re.VERBOSE | re.IGNORECASE,
    )

    discord_fmt = re.compile(r"<t:(?P<ts>[0-9]+)(?:\:?[RFfDdTt])?>")

    dt: datetime

    def __init__(self, argument: str, *, now: Optional[datetime] = None):
        match = self.compiled.fullmatch(argument)
        if match is None or not match.group(0):
            match = self.discord_fmt.fullmatch(argument)
            if match is not None:
                self.dt = datetime.fromtimestamp(int(match.group("ts")), tz=timezone.utc)
                return
            else:
                raise commands.BadArgument("invalid time provided")

        data = {k: int(v) for k, v in match.groupdict(default=0).items()}
        now = now or utcnow()
        self.dt = now + relativedelta(**data)

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        return cls(argument, now=ctx.message.created_at)


class HumanTime:
    calendar = pdt.Calendar(version=pdt.VERSION_CONTEXT_STYLE)

    def __init__(self, argument: str, *, now: Optional[datetime] = None):
        now = now or datetime.now(timezone.utc)
        # parsedatetime doesn't handle timezone-aware datetimes well sometimes
        # so we use a naive one for parsing and then localize
        naive_now = now.replace(tzinfo=None)
        dt, status = self.calendar.parseDT(argument, sourceTime=naive_now)
        
        if not status.hasDateOrTime:
            raise commands.BadArgument('invalid time provided, try e.g. "tomorrow" or "3 days"')

        if not status.hasTime:
            # replace it with the current time
            dt = dt.replace(hour=naive_now.hour, minute=naive_now.minute, second=naive_now.second, microsecond=naive_now.microsecond)

        self.dt: datetime = dt.replace(tzinfo=timezone.utc)
        self._past: bool = self.dt < now

    @classmethod
    async def convert(cls, ctx: Context, argument: str) -> Self:
        return cls(argument, now=ctx.message.created_at)


class Time(HumanTime):
    def __init__(self, argument: str, *, now: Optional[datetime] = None):
        try:
            o = ShortTime(argument, now=now)
        except Exception:
            super().__init__(argument, now=now)
        else:
            self.dt = o.dt
            self._past = self.dt < (now or utcnow())


def human_timedelta(
    dt: datetime,
    *,
    source: Optional[datetime] = None,
    accuracy: Optional[int] = 3,
    brief: bool = False,
    suffix: bool = True,
) -> str:
    now = source or utcnow()
    dt = ensure_utc(dt)
    now = ensure_utc(now)

    # Microsecond free zone
    now = now.replace(microsecond=0)
    dt = dt.replace(microsecond=0)

    if dt > now:
        delta = relativedelta(dt, now)
        output_suffix = ""
    else:
        delta = relativedelta(now, dt)
        output_suffix = " ago" if suffix else ""

    attrs = [
        ("year", "y"),
        ("month", "mo"),
        ("day", "d"),
        ("hour", "h"),
        ("minute", "m"),
        ("second", "s"),
    ]

    output = []
    for attr, brief_attr in attrs:
        elem = getattr(delta, attr + "s")
        if not elem:
            continue

        if attr == "day":
            weeks = delta.weeks
            if weeks:
                elem -= weeks * 7
                if not brief:
                    output.append(format(plural(weeks), "week"))
                else:
                    output.append(f"{weeks}w")

        if elem <= 0:
            continue

        if brief:
            output.append(f"{elem}{brief_attr}")
        else:
            output.append(format(plural(elem), attr))

    if accuracy is not None:
        output = output[:accuracy]

    if len(output) == 0:
        return "now"
    else:
        if not brief:
            return human_join(output, final="and") + output_suffix
        else:
            return " ".join(output) + output_suffix


class TicketAllot(commands.Cog):
    """
    Dynamically allot modmail tickets to staff members by role-ratio distribution.

    **How distribution works:**
    Each configured role has a target percentage of open tickets. When a new
    ticket arrives, the plugin picks the role that would overshoot its target
    the *least* by receiving this ticket. Within the chosen role, the member
    with the fewest currently open tickets is selected.

    **Escalation pipeline:**
    1. After `deadline_hours` the assigned member is tagged inside the ticket channel.
    2. After `escalation_hours` an alert is posted to the configured alert channel
       mentioning every configured role so no ticket falls through the cracks.
    """

    def __init__(self, bot):
        self.bot = bot
        self.db = self.bot.plugin_db.get_partition(self)

        self.config: Optional[dict] = None
        self.enabled: bool = True
        self.roles: Dict[str, dict] = {}
        self.alert_channel: Optional[int] = None
        self.member_categories: Dict[str, int] = {}
        self.member_reminders: Dict[str, dict] = {}
        self.assignments: Dict[str, dict] = {}
        self.auto_reminder_enabled: bool = True
        self.default_repeat: int = 120
        self.default_ping: int = 1

    async def cog_load(self):
        _default = {
            "enabled": True,
            "roles": {},
            "alert_channel": None,
            "member_categories": {},
            "member_reminders": {},
            "assignments": {},
            "auto_reminder_enabled": True,
            "default_repeat": 120,
            "default_ping": 1,
        }
        self.config = await self.db.find_one({"_id": "config"})
        if self.config is None:
            await self.db.find_one_and_update(
                {"_id": "config"}, {"$set": _default}, upsert=True
            )
            self.config = await self.db.find_one({"_id": "config"})

        for k, v in _default.items():
            if k not in self.config:
                self.config[k] = v

        self.enabled = self.config.get("enabled", True)
        self.roles = self.config.get("roles", {})
        self.alert_channel = self.config.get("alert_channel")
        self.member_categories = self.config.get("member_categories", {})
        self.member_reminders = self.config.get("member_reminders", {})
        self.assignments = self.config.get("assignments", {})
        self.auto_reminder_enabled = self.config.get("auto_reminder_enabled", True)
        self.default_repeat = self.config.get("default_repeat", 120)
        self.default_ping = self.config.get("default_ping", 1)

        self.deadline_check_loop.start()
        self.reminder_loop.start()

    def cog_unload(self):
        self.deadline_check_loop.cancel()
        self.reminder_loop.cancel()

    async def _save(self):
        await self.db.find_one_and_update(
            {"_id": "config"},
            {
                "$set": {
                    "enabled": self.enabled,
                    "roles": self.roles,
                    "alert_channel": self.alert_channel,
                    "member_categories": self.member_categories,
                    "member_reminders": self.member_reminders,
                    "assignments": self.assignments,
                    "auto_reminder_enabled": self.auto_reminder_enabled,
                    "default_repeat": self.default_repeat,
                    "default_ping": self.default_ping,
                }
            },
            upsert=True,
        )

    def _open_assignments(self) -> List[dict]:
        return [a for a in self.assignments.values() if not a.get("completed")]

    def _role_open_counts(self) -> Dict[str, int]:
        counts = {rid: 0 for rid in self.roles}
        for a in self._open_assignments():
            rid = str(a.get("role_id", 0))
            if rid in counts:
                counts[rid] += 1
        return counts

    def _member_open_count(self, member_id: int) -> int:
        return sum(1 for a in self._open_assignments() if a["member_id"] == member_id)

    def _member_total_count(self, member_id: int) -> int:
        return sum(1 for a in self.assignments.values() if a["member_id"] == member_id)

    def _pick_role(self) -> Optional[str]:
        """
        Select which role should receive the next ticket.

        For each role R, compute the *overshoot* it would have if it received
        the ticket:

            overshoot_R = (current_R + 1) / (total_open + 1) − target_ratio_R

        Assign to the role with the **minimum overshoot** (least over-represented).
        Ties are broken by highest target ratio (most important role goes first).
        Roles with no available members are skipped.
        """
        if not self.roles:
            return None

        total_after = len(self._open_assignments()) + 1
        role_counts = self._role_open_counts()

        best_role: Optional[str] = None
        best_overshoot = float("inf")

        for rid, cfg in self.roles.items():
            guild = self.bot.modmail_guild
            role = guild.get_role(int(rid))
            if role is None:
                continue
            members = [m for m in role.members if not m.bot]
            if not members:
                continue

            target = cfg.get("ratio", 0) / 100.0
            current = role_counts.get(rid, 0)
            overshoot = (current + 1) / total_after - target

            if overshoot < best_overshoot or (
                overshoot == best_overshoot
                and (best_role is None or cfg.get("ratio", 0) > self.roles[best_role].get("ratio", 0))
            ):
                best_overshoot = overshoot
                best_role = rid

        return best_role

    def _pick_member(self, role_id: str) -> Optional[discord.Member]:
        """
        Select the member in the role with the fewest total tickets.
        Ties are broken by fewest currently open tickets.
        """
        guild = self.bot.modmail_guild
        role = guild.get_role(int(role_id))
        if not role:
            return None
        members = [m for m in role.members if not m.bot]
        if not members:
            return None
        return min(members, key=lambda m: (self._member_total_count(m.id), self._member_open_count(m.id)))

    def _pick_higher_role(self, current_role_id: str) -> Optional[str]:
        """Select a higher-up role based on the next lower ratio priority tree."""
        current_cfg = self.roles.get(current_role_id)
        if not current_cfg:
            return None
        
        current_ratio = current_cfg.get("ratio", 0)
        
        candidates = {
            rid: cfg for rid, cfg in self.roles.items() 
            if rid != current_role_id and cfg.get("ratio", 0) < current_ratio
        }
        
        if not candidates:
            return None
        
        best_rid = max(
            candidates.keys(),
            key=lambda rid: (candidates[rid].get("ratio", 0), -int(rid))
        )
        return best_rid

    async def _get_log(self, channel) -> Optional[dict]:
        """Wait briefly and check if a channel is a modmail ticket."""
        await asyncio.sleep(2)
        if channel.guild != self.bot.modmail_guild:
            return None
        try:
            log = await self.bot.api.get_log(channel.id)
        except Exception:
            return None
        return log or None

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.TextChannel):
        if not self.enabled:
            return
        if not isinstance(channel, discord.TextChannel):
            return
        if not self.roles:
            return

        log = await self._get_log(channel)
        if not log:
            return

        role_id = self._pick_role()
        if not role_id:
            return

        member = self._pick_member(role_id)
        if not member:
            return

        now = utcnow()
        role_cfg = self.roles[role_id]
        role = self.bot.modmail_guild.get_role(int(role_id))

        self.assignments[str(channel.id)] = {
            "member_id": member.id,
            "role_id": int(role_id),
            "assigned_at": now.isoformat(),
            "channel_name": channel.name,
            "completed": False,
            "completed_at": None,
            "notified": False,
            "escalated": False,
            "last_reminder_at": None,
            "in_ping_mode": False,
        }
        await self._save()

        moved_to_category = None
        cat_id = self.member_categories.get(str(member.id))
        if cat_id:
            category = self.bot.modmail_guild.get_channel(cat_id)
            if isinstance(category, discord.CategoryChannel):
                try:
                    await channel.edit(category=category)
                    moved_to_category = category
                except discord.Forbidden:
                    pass

        deadline_h = role_cfg.get("deadline_hours", 0)
        embed = discord.Embed(
            title="🎫 Ticket Assigned",
            color=self.bot.main_color,
            timestamp=now,
        )
        embed.add_field(name="Assigned To", value=member.mention, inline=True)
        embed.add_field(name="Role", value=role.mention if role else f"<@&{role_id}>", inline=True)
        if moved_to_category:
            embed.add_field(name="Moved To", value=f"#{moved_to_category.name}", inline=True)
            embed.description = f"This ticket has been moved to {moved_to_category.mention}."

        if deadline_h:
            due = now + timedelta(hours=deadline_h)
            embed.add_field(
                name="Due By",
                value=f"{discord.utils.format_dt(due, 'f')} ({discord.utils.format_dt(due, 'R')})",
                inline=True,
            )
        embed.set_footer(text=f"Staff ID: {member.id} • Use `ticketallot complete` when done")
        await channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        """Auto-mark ticket as complete when the channel is deleted (ticket closed)."""
        cid = str(channel.id)
        if cid in self.assignments and not self.assignments[cid].get("completed"):
            self.assignments[cid]["completed"] = True
            self.assignments[cid]["completed_at"] = utcnow().isoformat()
            await self._save()


    @tasks.loop(minutes=5)
    async def deadline_check_loop(self):
        now = utcnow()
        changed = False

        for cid, a in list(self.assignments.items()):
            if a.get("completed"):
                continue

            rid = str(a.get("role_id", 0))
            if rid not in self.roles:
                continue

            cfg = self.roles[rid]
            assigned_at = ensure_utc(datetime.fromisoformat(a["assigned_at"]))
            elapsed_h = (now - assigned_at).total_seconds() / 3600

            deadline_h = cfg.get("deadline_hours", 0)
            escalation_h = cfg.get("escalation_hours", 0)

            if deadline_h and elapsed_h >= deadline_h and not a.get("notified"):
                channel = self.bot.modmail_guild.get_channel(int(cid))
                member = self.bot.modmail_guild.get_member(a["member_id"])
                if channel and member:
                    dl_str = human_timedelta(now + timedelta(hours=deadline_h), source=now, suffix=False)
                    embed = discord.Embed(
                        title="⏰ Deadline Reached",
                        description=(
                            f"{member.mention}, this ticket has been open for "
                            f"**{human_timedelta(assigned_at, source=now, suffix=False)}** and has reached its "
                            f"**{dl_str}** deadline. Please action it as soon as possible."
                        ),
                        color=discord.Color.orange(),
                        timestamp=now,
                    )
                    embed.set_footer(text="Automated deadline reminder")
                    try:
                        await channel.send(embed=embed)
                    except discord.HTTPException:
                        pass
                self.assignments[cid]["notified"] = True
                changed = True

            if escalation_h and elapsed_h >= escalation_h and not a.get("escalated"):
                if cfg.get("transfer"):
                    await self._handle_escalation_transfer(cid, a, now)
                
                await self._post_escalation(cid, a, now, elapsed_h)
                self.assignments[cid]["escalated"] = True
                changed = True

        if changed:
            await self._save()

    async def _handle_escalation_transfer(self, cid: str, a: dict, now: datetime):
        """Reassign ticket to a higher-up role member."""
        current_rid = str(a.get("role_id", 0))
        higher_rid = self._pick_higher_role(current_rid)
        if not higher_rid:
            return

        new_member = self._pick_member(higher_rid)
        if not new_member:
            return

        guild = self.bot.modmail_guild
        channel = guild.get_channel(int(cid))
        if not channel:
            return

        old_member = guild.get_member(a["member_id"])
        new_role = guild.get_role(int(higher_rid))

        a["member_id"] = new_member.id
        a["role_id"] = int(higher_rid)
        a["assigned_at"] = now.isoformat()
        a["notified"] = False
        a["escalated"] = False
        
        embed = discord.Embed(
            title="🚨 Ticket Escalated & Transferred",
            description=(
                f"This ticket has been escalated and automatically transferred to a higher-up role.\n"
                f"**New Assignee:** {new_member.mention}\n"
                f"**New Role:** {new_role.mention if new_role else f'<@&{higher_rid}>'}"
            ),
            color=discord.Color.red(),
            timestamp=now,
        )
        if old_member:
            embed.add_field(name="Previous Assignee", value=old_member.mention, inline=True)
        
        cat_id = self.member_categories.get(str(new_member.id))
        if cat_id:
            category = guild.get_channel(cat_id)
            if isinstance(category, discord.CategoryChannel):
                try:
                    await channel.edit(category=category)
                    embed.add_field(name="Moved To", value=f"#{category.name}", inline=True)
                except discord.Forbidden:
                    pass

        current_role = guild.get_role(int(current_rid))
        embed.set_footer(text=f"Escalated from {current_role.name if current_role else 'Unknown Role'}")
        await channel.send(embed=embed)

    @tasks.loop(minutes=1)
    async def reminder_loop(self):
        now = utcnow()
        changed = False

        for cid, a in list(self.assignments.items()):
            if a.get("completed"):
                continue

            mid = str(a.get("member_id", 0))
            if mid in self.member_reminders:
                cfg = self.member_reminders[mid]
                repeat_m = cfg["repeat"]
                ping_m = cfg["ping"]
            elif self.auto_reminder_enabled:
                repeat_m = self.default_repeat
                ping_m = self.default_ping
            else:
                continue

            last_ref_str = a.get("last_reminder_at")
            if last_ref_str:
                last_ref = ensure_utc(datetime.fromisoformat(last_ref_str))
            else:
                last_ref = ensure_utc(datetime.fromisoformat(a["assigned_at"]))

            elapsed_m = (now - last_ref).total_seconds() / 60
            in_ping = a.get("in_ping_mode", False)

            trigger_m = ping_m if in_ping else repeat_m

            if elapsed_m >= trigger_m:
                channel = self.bot.modmail_guild.get_channel(int(cid))
                member = self.bot.modmail_guild.get_member(int(mid))
                if channel and member:
                    try:
                        msg = f"🔔 {member.mention}, this ticket is still pending. (Interval: {trigger_m}m)"
                        await channel.send(msg)
                    except discord.HTTPException:
                        pass
                
                self.assignments[cid]["last_reminder_at"] = now.isoformat()
                self.assignments[cid]["in_ping_mode"] = True
                changed = True

        if changed:
            await self._save()

    async def _post_escalation(
        self, cid: str, a: dict, now: datetime, elapsed_h: float
    ):
        if not self.alert_channel:
            return
        alert_ch = self.bot.modmail_guild.get_channel(self.alert_channel)
        if not alert_ch:
            return

        guild = self.bot.modmail_guild
        member = guild.get_member(a["member_id"])
        assigned_role = guild.get_role(a.get("role_id", 0))
        ticket_ch = guild.get_channel(int(cid))

        role_mentions = " ".join(
            role.mention
            for rid in self.roles
            if (role := guild.get_role(int(rid))) is not None
        )

        embed = discord.Embed(
            title="🚨 Escalation Alert — Unresolved Ticket",
            color=discord.Color.red(),
            timestamp=now,
        )
        embed.add_field(
            name="Staff Member",
            value=member.mention if member else f"`{a['member_id']}`",
            inline=True,
        )
        embed.add_field(
            name="Role",
            value=assigned_role.mention if assigned_role else f"`{a.get('role_id')}`",
            inline=True,
        )
        embed.add_field(
            name="Ticket Channel",
            value=ticket_ch.mention if ticket_ch else f"#{a.get('channel_name', cid)}",
            inline=True,
        )
        assigned_at = ensure_utc(datetime.fromisoformat(a["assigned_at"]))
        embed.add_field(name="Open Duration", value=human_timedelta(assigned_at, source=now, suffix=False), inline=True)
        embed.description = (
            f"{role_mentions}\n\n"
            f"The ticket assigned to the above staff member has **not been completed** "
            f"within the escalation window."
        )
        embed.set_footer(text="Please ensure this ticket is addressed immediately.")

        try:
            await alert_ch.send(content=role_mentions, embed=embed)
        except discord.HTTPException:
            pass

    @reminder_loop.before_loop
    @deadline_check_loop.before_loop
    async def _before_loops(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)


    def _build_dashboard_embeds(
        self, target_member: Optional[discord.Member] = None
    ) -> List[discord.Embed]:
        now = utcnow()
        guild = self.bot.modmail_guild
        color = self.bot.main_color

        member_data: Dict[int, dict] = {}
        for cid, a in self.assignments.items():
            mid = a["member_id"]
            if target_member and mid != target_member.id:
                continue
            if mid not in member_data:
                member_data[mid] = {
                    "pending": [],
                    "completed": [],
                    "role_id": a.get("role_id"),
                }
            assigned_at = ensure_utc(datetime.fromisoformat(a["assigned_at"]))
            elapsed = (now - assigned_at).total_seconds()
            entry = {
                "cid": cid,
                "channel": a.get("channel_name", cid),
                "assigned_at": assigned_at,
                "elapsed": elapsed,
                "notified": a.get("notified", False),
                "escalated": a.get("escalated", False),
            }
            if a.get("completed"):
                if a.get("completed_at"):
                    comp_at = ensure_utc(datetime.fromisoformat(a["completed_at"]))
                    entry["resolution"] = (comp_at - assigned_at).total_seconds()
                else:
                    entry["resolution"] = elapsed
                member_data[mid]["completed"].append(entry)
            else:
                member_data[mid]["pending"].append(entry)

        pages: List[discord.Embed] = []

        if not target_member:
            role_embed = discord.Embed(
                title="📊 Ticket Dashboard — Role Summary",
                color=color,
                timestamp=now,
            )
            total_open = len(self._open_assignments())
            role_counts = self._role_open_counts()

            for rid, cfg in self.roles.items():
                role = guild.get_role(int(rid))
                rname = role.name if role else f"`{rid}`"
                open_c = role_counts.get(rid, 0)
                closed_c = sum(
                    1
                    for a in self.assignments.values()
                    if str(a.get("role_id")) == rid and a.get("completed")
                )
                total_c = open_c + closed_c
                actual_pct = f"{open_c / total_open * 100:.1f}%" if total_open else "—"
                target_pct = f"{cfg.get('ratio', 0)}%"

                dl_h = cfg.get("deadline_hours", 0)
                esc_h = cfg.get("escalation_hours", 0)
                dl_str = human_timedelta(now + timedelta(hours=dl_h), source=now, suffix=False)
                esc_str = human_timedelta(now + timedelta(hours=esc_h), source=now, suffix=False)
                transfer_str = "Enabled" if cfg.get("transfer") else "Disabled"

                lines = [
                    f"**Target:** {target_pct} | **Actual:** {actual_pct}",
                    f"**Open:** {open_c} | **Closed:** {closed_c} | **Total:** {total_c}",
                    f"**Deadline:** {dl_str} | **Escalation:** {esc_str}",
                    f"**Transfer on Escalation:** {transfer_str}",
                ]
                role_embed.add_field(name=rname, value="\n".join(lines), inline=False)

            if not self.roles:
                role_embed.description = "No roles configured."

            role_embed.set_footer(text="Page 1 / ? • Role Overview")
            pages.append(role_embed)

        if not member_data:
            no_data = discord.Embed(
                title="📊 Ticket Dashboard",
                description="No assignment data to display.",
                color=color,
            )
            pages.append(no_data)
            _fix_footers(pages)
            return pages

        for mid, data in member_data.items():
            m = guild.get_member(mid)
            display = m.display_name if m else f"User {mid}"
            role = guild.get_role(data["role_id"]) if data["role_id"] else None

            embed = discord.Embed(title=f"📋 {display}", color=color, timestamp=now)
            if m:
                embed.set_thumbnail(url=m.display_avatar.url)
            if role:
                embed.add_field(name="Role", value=role.mention, inline=True)

            pending = sorted(data["pending"], key=lambda x: x["elapsed"], reverse=True)
            completed = data["completed"]

            embed.add_field(name="⏳ Pending", value=str(len(pending)), inline=True)
            embed.add_field(name="✅ Completed", value=str(len(completed)), inline=True)

            if pending:
                lines = []
                for e in pending[:6]:
                    ch = guild.get_channel(int(e["cid"]))
                    ref = ch.mention if ch else f"#{e['channel']}"
                    flags = ""
                    if e["escalated"]:
                        flags += " 🚨"
                    elif e["notified"]:
                        flags += " ⏰"
                    duration = human_timedelta(e["assigned_at"], source=now, brief=True, suffix=False)
                    lines.append(f"{ref} — **{duration}**{flags}")
                embed.add_field(
                    name="Pending Tickets (oldest first)",
                    value="\n".join(lines),
                    inline=False,
                )

            if completed:
                res_times = [e.get("resolution", 0) for e in completed]
                avg = sum(res_times) / len(res_times)
                fastest = min(res_times)
                slowest = max(res_times)
                
                def fmt_sec(s):
                    return human_timedelta(now + timedelta(seconds=s), source=now, brief=True, suffix=False)

                embed.add_field(
                    name="Resolution Stats",
                    value=(
                        f"**Avg:** {fmt_sec(avg)}\n"
                        f"**Fastest:** {fmt_sec(fastest)}\n"
                        f"**Slowest:** {fmt_sec(slowest)}"
                    ),
                    inline=False,
                )

            pages.append(embed)

        _fix_footers(pages)
        return pages


    @checks.has_permissions(PermissionLevel.MOD)
    @commands.group(
        name="ticketallot",
        aliases=["ta"],
        invoke_without_command=True,
    )
    async def ticketallot_(self, ctx):
        """
        **🎫 Ticket Allotment Plugin**

        Automatically distributes incoming modmail tickets to staff members
        according to role-based ratio targets.

        ─────────────────────────────────────────────────
        **⚙️ Configuration (Admin only)**
        ─────────────────────────────────────────────────
        `{prefix}ticketallot toggle`                  — Enable / disable the plugin
        `{prefix}ticketallot alertchannel #channel`   — Set escalation alert channel
        `{prefix}ticketallot reset`                   — Clear all assignment records
        `{prefix}ticketallot status`                  — Current status & distribution

        ─────────────────────────────────────────────────
        **🎭 Role Management (Admin only)**
        ─────────────────────────────────────────────────
        `{prefix}ticketallot role add @Role ratio [deadline_h] [escalation_h]`
        `{prefix}ticketallot role remove @Role`
        `{prefix}ticketallot role view`
        `{prefix}ticketallot role deadline @Role hours`
        `{prefix}ticketallot role escalation @Role hours`

        ─────────────────────────────────────────────────
        **📋 Ticket Actions (Mod+)**
        ─────────────────────────────────────────────────
        `{prefix}ticketallot assign #channel @member` — Manually assign a ticket
        `{prefix}ticketallot complete [#channel]`     — Mark ticket as complete
        `{prefix}ticketallot dashboard [mod]`         — Paginated dashboard
        `{prefix}ticketallot category @category`     — Set your preferred ticket category
        `{prefix}ticketallot reminder R P`            — Set Repeat and Ping intervals
        `{prefix}ticketallot remind stop`             — Reset pings for current ticket
        `{prefix}ticketallot remind auto R P`        — Set default auto-reminder intervals
        `{prefix}ticketallot remind auto toggle`     — Toggle auto-reminder on assignment
        `{prefix}ticketallot rolecheck`               — Live ratio comparison

        ─────────────────────────────────────────────────
        💡 Ratios for all roles must sum to **100%**.
        🔗 Alias: `{prefix}ta <subcommand>`
        """
        if not ctx.invoked_subcommand:
            await ctx.send_help(ctx.command)


    @checks.has_permissions(PermissionLevel.ADMIN)
    @ticketallot_.group(name="role", invoke_without_command=True)
    async def ta_role(self, ctx):
        """
        Manage roles, their ratio targets, deadline timers and escalation timers.

        Ratios across all roles must total **100%**. The plugin warns you if
        adding a role would push the total over that limit.
        """
        if not ctx.invoked_subcommand:
            await ctx.send_help(ctx.command)

    @checks.has_permissions(PermissionLevel.ADMIN)
    @ta_role.command(name="add")
    async def ta_role_add(
        self,
        ctx,
        role: discord.Role,
        ratio: float,
        deadline: Time = None,
        escalation: Time = None,
        transfer: bool = False,
    ):
        """
        Add or update a role in the allotment system.

        **Arguments:**
        `role`       — The Discord role to configure.
        `ratio`      — Target percentage of tickets (0–100). E.g. `50` = 50%.
        `deadline`   — Time until assigned member is tagged (e.g. 24h, 1d). Default: 24h.
        `escalation` — Time until all roles are alerted (e.g. 48h, 2d). Default: 48h.
        `transfer`   — Whether to auto-transfer to a higher role on escalation.

        **Example:**
        ```
        {prefix}ticketallot role add @Senior-Support 50 12h 36h True
        {prefix}ticketallot role add @Junior-Support 20 1d 3d
        ```
        """
        if not (0 < ratio <= 100):
            return await ctx.send("❌ Ratio must be between 1 and 100.")

        now = utcnow()
        dl_dt = ensure_utc(deadline.dt) if deadline else now + timedelta(hours=24)
        esc_dt = ensure_utc(escalation.dt) if escalation else now + timedelta(hours=48)

        dl_seconds = (dl_dt - now).total_seconds()
        esc_seconds = (esc_dt - now).total_seconds()

        if esc_seconds < dl_seconds:
            return await ctx.send(
                "❌ Escalation time must be greater than or equal to deadline time."
            )

        other_total = sum(
            v.get("ratio", 0) for k, v in self.roles.items() if k != str(role.id)
        )
        projected = other_total + ratio
        if projected > 100:
            return await ctx.send(
                f"❌ Adding **{ratio}%** would make the total **{projected}%** (exceeds 100%). "
                f"Current total from other roles: **{other_total}%**."
            )

        self.roles[str(role.id)] = {
            "ratio": ratio,
            "deadline_hours": dl_seconds / 3600,
            "escalation_hours": esc_seconds / 3600,
            "transfer": transfer,
        }
        await self._save()

        embed = discord.Embed(
            title="✅ Role Added / Updated",
            color=self.bot.main_color,
        )
        embed.add_field(name="Role", value=role.mention)
        embed.add_field(name="Ratio", value=f"{ratio}%")
        embed.add_field(name="Deadline", value=human_timedelta(dl_dt, source=now))
        embed.add_field(name="Escalation", value=human_timedelta(esc_dt, source=now))
        embed.add_field(name="Transfer on Escalation", value="Enabled" if transfer else "Disabled")
        embed.add_field(
            name="New Total Ratio",
            value=f"**{projected}%** {'✅' if projected == 100 else '⚠️ (should be 100%)'}",
            inline=False,
        )
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMIN)
    @ta_role.command(name="remove", aliases=["del", "delete"])
    async def ta_role_remove(self, ctx, role: discord.Role):
        """
        Remove a role from the allotment system.

        Existing assignment records for this role are preserved but the role
        will no longer receive new tickets.

        **Example:**
        `{prefix}ticketallot role remove @Junior-Support`
        """
        rid = str(role.id)
        if rid not in self.roles:
            return await ctx.send(f"❌ {role.mention} is not in the allotment list.")
        del self.roles[rid]
        await self._save()
        new_total = sum(v.get("ratio", 0) for v in self.roles.values())
        await ctx.send(
            f"✅ Removed {role.mention} from allotment. "
            f"Remaining total ratio: **{new_total}%**."
        )

    @checks.has_permissions(PermissionLevel.ADMIN)
    @ta_role.command(name="view")
    async def ta_role_view(self, ctx):
        """
        List all configured roles with their ratios, deadlines and escalation times.

        Also shows the running total ratio so you can confirm it equals 100%.
        """
        if not self.roles:
            return await ctx.send(
                "No roles configured. Use `ticketallot role add @Role ratio` to add one."
            )

        embed = discord.Embed(title="🎭 Allotment Role Configuration", color=self.bot.main_color)
        total = 0.0
        guild = self.bot.modmail_guild

        for rid, cfg in self.roles.items():
            role = guild.get_role(int(rid))
            total += cfg.get("ratio", 0)
            members = len([m for m in role.members if not m.bot]) if role else 0
            
            dl_h = cfg.get("deadline_hours", 0)
            esc_h = cfg.get("escalation_hours", 0)
            now = utcnow()
            dl_str = human_timedelta(now + timedelta(hours=dl_h), source=now, suffix=False)
            esc_str = human_timedelta(now + timedelta(hours=esc_h), source=now, suffix=False)
            transfer_str = "Yes" if cfg.get("transfer") else "No"

            embed.add_field(
                name=role.name if role else f"Unknown Role ({rid})",
                value=(
                    f"Ratio: **{cfg.get('ratio', 0)}%**\n"
                    f"Deadline: **{dl_str}**\n"
                    f"Escalation: **{esc_str}**\n"
                    f"Transfer: **{transfer_str}**\n"
                    f"Members: **{members}**"
                ),
                inline=True,
            )

        embed.set_footer(
            text=f"Total Ratio: {total}% {'✅' if total == 100 else '⚠️  (should sum to 100%)'}"
        )
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMIN)
    @ta_role.command(name="deadline")
    async def ta_role_deadline(self, ctx, role: discord.Role, deadline: Time):
        """
        Update the deadline before a staff member is tagged inside the ticket.

        Must be less than the role's escalation time.

        **Example:**
        `{prefix}ticketallot role deadline @Senior-Support 8h`
        `{prefix}ticketallot role deadline @Junior-Support 1d`
        """
        rid = str(role.id)
        if rid not in self.roles:
            return await ctx.send(
                f"❌ {role.mention} is not configured. Add it first with `ticketallot role add`."
            )

        now = utcnow()
        dl_seconds = (ensure_utc(deadline.dt) - now).total_seconds()
        esc_h = self.roles[rid].get("escalation_hours", 0)

        if esc_h and dl_seconds > (esc_h * 3600):
            dl_str = human_timedelta(deadline.dt, source=now, suffix=False)
            esc_str = human_timedelta(now + timedelta(hours=esc_h), source=now, suffix=False)
            return await ctx.send(
                f"❌ Deadline (**{dl_str}**) must be less than or equal to escalation time (**{esc_str}**)."
            )

        self.roles[rid]["deadline_hours"] = dl_seconds / 3600
        await self._save()
        await ctx.send(f"✅ Deadline for {role.mention} updated to **{human_timedelta(deadline.dt, source=now)}**.")

    @checks.has_permissions(PermissionLevel.ADMIN)
    @ta_role.command(name="escalation")
    async def ta_role_escalation(self, ctx, role: discord.Role, escalation: Time, transfer: bool = False):
        """
        Update the escalation time before all roles are alerted.

        Must be greater than the role's deadline time.

        **Example:**
        `{prefix}ticketallot role escalation @Senior-Support 24h True`
        `{prefix}ticketallot role escalation @Junior-Support 3d`
        """
        rid = str(role.id)
        if rid not in self.roles:
            return await ctx.send(
                f"❌ {role.mention} is not configured. Add it first with `ticketallot role add`."
            )

        now = utcnow()
        esc_seconds = (ensure_utc(escalation.dt) - now).total_seconds()
        dl_h = self.roles[rid].get("deadline_hours", 0)

        if dl_h and esc_seconds < (dl_h * 3600):
            esc_str = human_timedelta(escalation.dt, source=now, suffix=False)
            dl_str = human_timedelta(now + timedelta(hours=dl_h), source=now, suffix=False)
            return await ctx.send(
                f"❌ Escalation time (**{esc_str}**) must be greater than or equal to deadline (**{dl_str}**)."
            )

        self.roles[rid]["escalation_hours"] = esc_seconds / 3600
        self.roles[rid]["transfer"] = transfer
        await self._save()
        await ctx.send(
            f"✅ Escalation for {role.mention} updated to **{human_timedelta(escalation.dt, source=now)}** "
            f"with transfer **{'enabled' if transfer else 'disabled'}**."
        )


    @checks.has_permissions(PermissionLevel.MOD)
    @ticketallot_.command(name="assign")
    async def ta_assign(
        self, ctx, channel: discord.TextChannel, member: discord.Member
    ):
        """
        Manually assign a ticket channel to a specific staff member.

        The plugin will still track deadlines and escalation for manually
        assigned tickets. The member's role is inferred from configured roles.

        **Example:**
        `{prefix}ticketallot assign #username-1234 @StaffMember`
        """
        role_id: Optional[int] = None
        best_ratio = -1
        for rid, cfg in self.roles.items():
            r = ctx.guild.get_role(int(rid))
            if r and r in member.roles and cfg.get("ratio", 0) > best_ratio:
                role_id = int(rid)
                best_ratio = cfg.get("ratio", 0)

        now = utcnow()
        self.assignments[str(channel.id)] = {
            "member_id": member.id,
            "role_id": role_id or 0,
            "assigned_at": now.isoformat(),
            "channel_name": channel.name,
            "completed": False,
            "completed_at": None,
            "notified": False,
            "escalated": False,
            "last_reminder_at": None,
            "in_ping_mode": False,
        }
        await self._save()

        moved_to_category = None
        cat_id = self.member_categories.get(str(member.id))
        if cat_id:
            category = ctx.guild.get_channel(cat_id)
            if isinstance(category, discord.CategoryChannel):
                try:
                    await channel.edit(category=category)
                    moved_to_category = category
                except discord.Forbidden:
                    pass

        role = ctx.guild.get_role(role_id) if role_id else None
        role_cfg = self.roles.get(str(role_id), {}) if role_id else {}
        deadline_h = role_cfg.get("deadline_hours", 0)

        embed = discord.Embed(
            title="🎫 Ticket Manually Assigned",
            color=self.bot.main_color,
            timestamp=now,
        )
        embed.add_field(name="Channel", value=channel.mention, inline=True)
        embed.add_field(name="Assigned To", value=member.mention, inline=True)
        embed.add_field(name="Role", value=role.mention if role else "None", inline=True)
        if moved_to_category:
            embed.add_field(name="Moved To", value=f"#{moved_to_category.name}", inline=True)
        
        if deadline_h:
            due = now + timedelta(hours=deadline_h)
            embed.add_field(
                name="Due By",
                value=f"{discord.utils.format_dt(due, 'f')} ({discord.utils.format_dt(due, 'R')})",
                inline=True,
            )
        embed.set_footer(text=f"Assigned by {ctx.author}")
        await ctx.send(embed=embed)

        try:
            desc = f"📌 Manually assigned to {member.mention} by {ctx.author.mention}."
            if moved_to_category:
                desc += f"\n\nThis ticket has been moved to {moved_to_category.mention}."
            
            ticket_embed = discord.Embed(
                title="🎫 Ticket Assigned",
                description=desc,
                color=self.bot.main_color,
                timestamp=now,
            )
            ticket_embed.add_field(name="Assigned To", value=member.mention, inline=True)
            ticket_embed.add_field(name="Role", value=role.mention if role else "None", inline=True)
            if moved_to_category:
                ticket_embed.add_field(name="Moved To", value=f"#{moved_to_category.name}", inline=True)
            
            if deadline_h:
                due = now + timedelta(hours=deadline_h)
                ticket_embed.add_field(
                    name="Due By",
                    value=f"{discord.utils.format_dt(due, 'f')} ({discord.utils.format_dt(due, 'R')})",
                    inline=True,
                )
            ticket_embed.set_footer(text=f"Staff ID: {member.id} • Use `ticketallot complete` when done")
            await channel.send(embed=ticket_embed)
        except discord.HTTPException:
            pass

    @checks.has_permissions(PermissionLevel.MOD)
    @ticketallot_.command(name="complete", aliases=["done", "close"])
    async def ta_complete(self, ctx, channel: discord.TextChannel = None):
        """
        Mark a ticket as completed in the allotment system.

        Defaults to the current channel if no channel is specified.
        The ticket channel itself is **not** closed by this command —
        it only updates allotment tracking.

        **Examples:**
        `{prefix}ticketallot complete`
        `{prefix}ticketallot complete #username-1234`
        """
        channel = channel or ctx.channel
        cid = str(channel.id)

        if cid not in self.assignments:
            return await ctx.send(
                "❌ No allotment record found for that channel. "
                "It may not have been assigned automatically."
            )
        if self.assignments[cid].get("completed"):
            comp_at = self.assignments[cid].get("completed_at")
            ts = f" (at {discord.utils.format_dt(ensure_utc(datetime.fromisoformat(comp_at)), 'R')})" if comp_at else ""
            return await ctx.send(f"ℹ️ This ticket is already marked as completed{ts}.")

        now = utcnow()
        assigned_at = ensure_utc(datetime.fromisoformat(self.assignments[cid]["assigned_at"]))
        elapsed = (now - assigned_at).total_seconds()

        self.assignments[cid]["completed"] = True
        self.assignments[cid]["completed_at"] = now.isoformat()
        await self._save()

        embed = discord.Embed(
            title="✅ Ticket Marked Complete",
            color=discord.Color.green(),
            timestamp=now,
        )
        member = ctx.guild.get_member(self.assignments[cid]["member_id"])
        embed.add_field(name="Channel", value=channel.mention)
        embed.add_field(name="Assignee", value=member.mention if member else "Unknown")
        embed.add_field(name="Resolution Time", value=human_timedelta(assigned_at, source=now, suffix=False))
        embed.set_footer(text=f"Completed by {ctx.author}")
        await ctx.send(embed=embed)


    @checks.has_permissions(PermissionLevel.MOD)
    @ticketallot_.command(name="dashboard", aliases=["dash", "stats", "board"])
    async def ta_dashboard(self, ctx, mod: discord.Member = None):
        """
        View a paginated dashboard of pending and completed ticket assignments.

        **Optional argument:**
        `mod` — Filter dashboard to show only this staff member's tickets.

        **Pages include:**
        • Role summary (targets vs actual distribution)
        • Per-member breakdown (pending tickets with duration, resolution stats)

        🔴 = escalated  ⏰ = deadline notified

        **Examples:**
        `{prefix}ticketallot dashboard`
        `{prefix}ticketallot dashboard @StaffMember`
        `{prefix}ta dash @StaffMember`
        """
        async with ctx.typing():
            embeds = self._build_dashboard_embeds(mod)

        if len(embeds) == 1:
            return await ctx.send(embed=embeds[0])

        session = EmbedPaginatorSession(ctx, *embeds)
        await session.run()

    @checks.has_permissions(PermissionLevel.MOD)
    @ticketallot_.command(name="category")
    async def ta_category(self, ctx, category: discord.CategoryChannel = None):
        """
        Set your preferred Discord category for tickets assigned to you.

        When a ticket is assigned to you, it will be automatically moved
        to this category.

        **Arguments:**
        `category` — The Discord category channel. Omit to clear your preference.

        **Example:**
        `{prefix}ticketallot category "My Tickets"`
        `{prefix}ticketallot category` (to clear)
        """
        mid = str(ctx.author.id)
        if category is None:
            if mid in self.member_categories:
                del self.member_categories[mid]
                await self._save()
                await ctx.send("✅ Your preferred ticket category has been cleared.")
            else:
                await ctx.send("ℹ️ You don't have a preferred ticket category set.")
            return

        self.member_categories[mid] = category.id
        await self._save()
        await ctx.send(f"✅ Your tickets will now be moved to the **{category.name}** category.")

    @checks.has_permissions(PermissionLevel.MOD)
    @ticketallot_.group(name="reminder", aliases=["remind"], invoke_without_command=True)
    async def ta_reminder(self, ctx, repeat: Time = None, ping: Time = None):
        """
        Set your personal ticket reminder intervals.

        **Arguments:**
        `repeat` — Time after which the first reminder is sent (e.g. 1h, 30m).
        `ping`   — Time between subsequent reminders (e.g. 10m, 5m).

        **Constraints:**
        • Ping must be at least 1 minute.
        • Ping must be at least 1 minute less than Repeat.

        **Example:**
        `{prefix}ticketallot reminder 1h 10m`
        `{prefix}ta remind 30m 5m`
        """
        mid = str(ctx.author.id)
        if repeat is None or ping is None:
            if mid in self.member_reminders:
                cfg = self.member_reminders[mid]
                return await ctx.send(
                    f"🔔 Your current reminder settings: **Repeat: {cfg['repeat']}m**, **Ping: {cfg['ping']}m**.\n"
                    f"Use `{ctx.prefix}ta remind stop` in a ticket to reset the cycle."
                )
            return await ctx.send_help(ctx.command)

        now = utcnow()
        rep_m = (ensure_utc(repeat.dt) - now).total_seconds() / 60
        ping_m = (ensure_utc(ping.dt) - now).total_seconds() / 60

        if ping_m < 1:
            return await ctx.send("❌ Ping interval must be at least 1 minute.")
        if ping_m >= rep_m:
            return await ctx.send("❌ Ping interval must be at least 1 minute less than the Repeat interval.")

        self.member_reminders[mid] = {"repeat": int(rep_m), "ping": int(ping_m)}
        await self._save()
        await ctx.send(
            f"✅ Reminder set! You will be reminded of tickets after **{human_timedelta(repeat.dt, source=now)}**, "
            f"then every **{human_timedelta(ping.dt, source=now)}** until you use `{ctx.prefix}ta remind stop`."
        )

    @checks.has_permissions(PermissionLevel.MOD)
    @ta_reminder.command(name="stop", aliases=["reset"])
    async def ta_reminder_stop(self, ctx, channel: discord.TextChannel = None):
        """
        Stop the current ping cycle for a ticket and wait for the next Repeat interval.

        Defaults to the current channel.
        """
        channel = channel or ctx.channel
        cid = str(channel.id)

        if cid not in self.assignments or self.assignments[cid].get("completed"):
            return await ctx.send("❌ This channel is not an active ticket assignment.")

        self.assignments[cid]["last_reminder_at"] = utcnow().isoformat()
        self.assignments[cid]["in_ping_mode"] = False
        await self._save()
        await ctx.send("✅ Reminder cycle reset. I will wait for your Repeat interval before reminding you again.")

    @checks.has_permissions(PermissionLevel.ADMIN)
    @ta_reminder.group(name="auto", invoke_without_command=True)
    async def ta_reminder_auto(self, ctx, repeat: Time = None, ping: Time = None):
        """
        Manage default automatic reminder intervals for newly assigned tickets.

        **Arguments:**
        `repeat` — Time after which the first reminder is sent (e.g. 2h, 1h).
        `ping`   — Time between subsequent reminders (e.g. 1m, 5m).

        **Constraints:**
        • Ping must be at least 1 minute.
        • Ping must be at least 1 minute less than Repeat.

        **Example:**
        `{prefix}ticketallot remind auto 2h 1m`
        """
        if repeat is None or ping is None:
            state = "Enabled" if self.auto_reminder_enabled else "Disabled"
            return await ctx.send(
                f"🤖 **Auto-Reminder Status:** {state}\n"
                f"Default Repeat: **{self.default_repeat}m**, Default Ping: **{self.default_ping}m**.\n"
                f"Use `{ctx.prefix}ta remind auto <repeat> <ping>` to update these."
            )

        now = utcnow()
        rep_m = (ensure_utc(repeat.dt) - now).total_seconds() / 60
        ping_m = (ensure_utc(ping.dt) - now).total_seconds() / 60

        if ping_m < 1:
            return await ctx.send("❌ Default ping interval must be at least 1 minute.")
        if ping_m >= rep_m:
            return await ctx.send("❌ Default ping interval must be at least 1 minute less than the Repeat interval.")

        self.default_repeat = int(rep_m)
        self.default_ping = int(ping_m)
        await self._save()
        await ctx.send(
            f"✅ Default auto-reminder set! New tickets will be reminded after **{human_timedelta(repeat.dt, source=now)}**, "
            f"then every **{human_timedelta(ping.dt, source=now)}**."
        )

    @checks.has_permissions(PermissionLevel.ADMIN)
    @ta_reminder_auto.command(name="toggle")
    async def ta_reminder_auto_toggle(self, ctx):
        """Toggle automatic reminders for newly assigned tickets."""
        self.auto_reminder_enabled = not self.auto_reminder_enabled
        await self._save()
        state = "enabled ✅" if self.auto_reminder_enabled else "disabled ❌"
        await ctx.send(f"✅ Automatic reminders for new assignments are now **{state}**.")


    @checks.has_permissions(PermissionLevel.MOD)
    @ticketallot_.command(name="rolecheck", aliases=["ratio"])
    async def ta_rolecheck(self, ctx):
        """
        Display a live snapshot of how current ticket distribution compares to targets.

        Shows each role's target percentage, actual percentage, delta, and per-member load.
        """
        open_all = self._open_assignments()
        total = len(open_all)
        role_counts = self._role_open_counts()
        guild = self.bot.modmail_guild

        embed = discord.Embed(
            title="📈 Live Distribution Check",
            color=self.bot.main_color,
            timestamp=utcnow(),
        )
        embed.add_field(name="Total Open Tickets", value=str(total), inline=False)

        for rid, cfg in self.roles.items():
            role = guild.get_role(int(rid))
            rname = role.name if role else f"`{rid}`"
            count = role_counts.get(rid, 0)
            target = cfg.get("ratio", 0)
            actual = count / total * 100 if total else 0
            delta = actual - target
            delta_str = f"+{delta:.1f}%" if delta >= 0 else f"{delta:.1f}%"
            status = "✅" if abs(delta) <= 5 else ("🔴" if delta > 0 else "🟡")

            members = [m for m in (role.members if role else []) if not m.bot]
            member_lines = []
            for m in members:
                mc = self._member_open_count(m.id)
                member_lines.append(f"  {m.display_name}: **{mc}** open")

            value = (
                f"Target: **{target}%** | Actual: **{actual:.1f}%** | Δ {delta_str} {status}\n"
                f"Tickets: **{count}**\n"
                + ("\n".join(member_lines) if member_lines else "  *No members*")
            )
            embed.add_field(name=rname, value=value, inline=False)

        if not self.roles:
            embed.description = "No roles configured."

        await ctx.send(embed=embed)


    @checks.has_permissions(PermissionLevel.MOD)
    @ticketallot_.command(name="status")
    async def ta_status(self, ctx):
        """
        Show plugin status, configuration summary, and current open ticket count.
        """
        guild = self.bot.modmail_guild
        embed = discord.Embed(title="🎫 Ticket Allotment — Status", color=self.bot.main_color)

        embed.add_field(
            name="Plugin", value="✅ Enabled" if self.enabled else "❌ Disabled", inline=True
        )

        alert_ch = guild.get_channel(self.alert_channel) if self.alert_channel else None
        embed.add_field(
            name="Alert Channel",
            value=alert_ch.mention if alert_ch else "⚠️ Not configured",
            inline=True,
        )

        open_c = len(self._open_assignments())
        total_c = len(self.assignments)
        embed.add_field(
            name="Assignments",
            value=f"Open: **{open_c}** | Total: **{total_c}**",
            inline=True,
        )

        total_ratio = sum(v.get("ratio", 0) for v in self.roles.values())
        embed.add_field(
            name="Roles",
            value=f"{len(self.roles)} configured | Total ratio: **{total_ratio}%**",
            inline=True,
        )

        embed.add_field(
            name="Check Loop",
            value="✅ Running" if self.deadline_check_loop.is_running() else "❌ Stopped",
            inline=True,
        )
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMIN)
    @ticketallot_.command(name="toggle")
    async def ta_toggle(self, ctx):
        """
        Enable or disable the automatic ticket allotment plugin.

        When disabled, new tickets are **not** assigned automatically.
        Existing assignments and their deadline tracking continue unaffected.
        """
        self.enabled = not self.enabled
        await self._save()
        state = "enabled ✅" if self.enabled else "disabled ❌"
        embed = discord.Embed(
            title="Ticket Allotment",
            description=f"Automatic allotment is now **{state}**.",
            color=self.bot.main_color,
        )
        await ctx.send(embed=embed)

    @checks.has_permissions(PermissionLevel.ADMIN)
    @ticketallot_.command(name="alertchannel", aliases=["alert"])
    async def ta_alertchannel(self, ctx, channel: discord.TextChannel):
        """
        Set the channel where escalation alerts are posted.

        All configured role mentions will be sent to this channel when a ticket
        exceeds its escalation time.

        **Example:**
        `{prefix}ticketallot alertchannel #staff-escalations`
        """
        self.alert_channel = channel.id
        await self._save()
        await ctx.send(
            f"✅ Escalation alerts will be sent to {channel.mention}."
        )

    @checks.has_permissions(PermissionLevel.ADMIN)
    @ticketallot_.command(name="reset")
    async def ta_reset(self, ctx):
        """
        ⚠️ **Clear all assignment records.**

        This removes every pending and completed assignment entry from the database.
        Role configurations, ratios, deadlines and escalation settings are **preserved**.

        This action is **irreversible**.
        """
        count = len(self.assignments)
        self.assignments = {}
        await self._save()
        await ctx.send(
            f"✅ Cleared **{count}** assignment record(s). Role configuration is intact."
        )



def _fix_footers(pages: List[discord.Embed]):
    n = len(pages)
    for i, p in enumerate(pages):
        existing = p.footer.text or ""
        base = existing.split(" • Page ")[0].split("Page ")[0].rstrip(" •")
        p.set_footer(text=f"{base} • Page {i+1}/{n}".lstrip(" •"))


async def setup(bot):
    await bot.add_cog(TicketAllot(bot))
