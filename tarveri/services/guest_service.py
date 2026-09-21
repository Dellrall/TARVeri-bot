"""
Guest verification, referral code management, and private thread ticket orchestration.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import string
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

import discord

from tarveri.config import (
    GUEST_ROLE_COLOR,
    GUEST_ROLE_PATTERN,
    get_configured_tz,
    now_formatted,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.role_manager import RoleManager
from tarveri.utils import format_ticket_seq, parse_db_timestamp

logger = logging.getLogger("tarveri")


def generate_code_string(length: int = 6) -> str:
    """Generates a clean, readable random alphanumeric code formatted like TAR-8X2K9P."""
    alphabet = string.ascii_uppercase + string.digits
    # Exclude ambiguous characters
    clean_alphabet = "".join(c for c in alphabet if c not in "0O1I")
    rand_part = "".join(secrets.choice(clean_alphabet) for _ in range(length))
    return f"TAR-{rand_part}"


class GuestService:
    def __init__(
        self,
        bot: discord.Client,
        db: Database,
        admin_role_name: str = "TARVeri Admin",
        rate_limiter: RateLimiter | None = None,
    ):
        self.bot = bot
        self.db = db
        self.admin_role_name = admin_role_name
        self.rate_limiter = rate_limiter
        self.role_manager = RoleManager(db)
        self._lock = asyncio.Lock()
        self._role_locks: dict[int, asyncio.Lock] = {}
        self._escalation_task: asyncio.Task[None] | None = None

    def _get_guild_role_lock(self, guild_id: int) -> asyncio.Lock:
        return self.role_manager.get_guild_role_lock(guild_id)

    async def create_referral_code(
        self,
        guild_id: int,
        referrer_user: discord.User | discord.Member,
        ttl_hours: int = 48,
        max_active: int = 3,
    ) -> tuple[bool, str]:
        """
        Generates a new referral code for a verified student if under active limit.
        Returns (success, code_or_error_message).
        """
        async with self._lock:
            is_bl, bl_reason = await self.db.is_blacklisted(guild_id, user_id=referrer_user.id)
            if is_bl:
                return (
                    False,
                    f"⛔ You are blacklisted from generating referral codes in this server.{f' Reason: {bl_reason}' if bl_reason else ''}",
                )

            active_count = await self.db.count_active_referrals_for_user(guild_id, referrer_user.id)
            if active_count >= max_active:
                return (
                    False,
                    f"⚠️ You already have **{active_count}** active referral code(s) (maximum allowed is {max_active}). "
                    "Please wait for your previous codes to be used or expire before creating more.",
                )

            expires_at = (datetime.now(get_configured_tz()) + timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")
            for _ in range(5):
                candidate_code = generate_code_string()
                existing = await self.db.get_referral_code(candidate_code, guild_id)
                if not existing:
                    await self.db.create_referral_code(candidate_code, guild_id, referrer_user.id, expires_at)
                    guild = getattr(referrer_user, "guild", None)
                    await self.db.log(
                        "INFO",
                        "REFERRAL_CREATED",
                        f"Student {referrer_user} (ID: {referrer_user.id}) created referral code '{candidate_code}' (Expires: {expires_at})",
                        guild=guild,
                        user_id=referrer_user.id,
                    )
                    return True, candidate_code

            return False, "❌ Failed to generate unique referral code. Please try again."

    async def validate_referral_code(
        self, guild_id: int, code: str
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """
        Validates whether a referral code is usable in this guild.
        Returns (is_valid, error_reason_if_any, code_record).
        Uses a uniform generic error message to prevent oracle/enumeration attacks.
        """
        generic_error = "❌ Invalid, expired, or already used referral code. Please check with your friend and try again."
        normalized = code.strip().upper().replace(" ", "")
        if not normalized.startswith("TAR-") and len(normalized) == 6:
            normalized = f"TAR-{normalized}"

        record = await self.db.get_referral_code(normalized, guild_id)
        if not record:
            return False, generic_error, None

        if record["status"] != "ACTIVE":
            return False, generic_error, record

        now_str = now_formatted()
        if record["expires_at"] <= now_str:
            await self.db.update_referral_code_status(normalized, guild_id, "EXPIRED")
            return False, generic_error, record

        return True, "", record

    def is_channel_accessible_for_guest_threads(self, ch: discord.TextChannel | None, guild: discord.Guild) -> bool:
        """
        Validates that a text channel is appropriate for guest verification threads:
        1. Channel exists and is a TextChannel.
        2. Bot has permission to view channel, send messages, and create/manage threads.
        3. Channel is NOT an admin/mod/staff-only locked channel (normal unverified users must be able to view threads).
        """
        if not ch or not isinstance(ch, discord.TextChannel):
            return False

        # 1. Bot permissions check
        bot_member = getattr(guild, "me", None)
        if hasattr(ch, "permissions_for") and bot_member:
            bot_perms = ch.permissions_for(bot_member)
            can_view = getattr(bot_perms, "view_channel", True)
            can_thread = (
                getattr(bot_perms, "create_private_threads", False)
                or getattr(bot_perms, "create_public_threads", False)
                or getattr(bot_perms, "manage_threads", False)
            )
            can_send = getattr(bot_perms, "send_messages", True) or getattr(bot_perms, "send_messages_in_threads", True)
            if not (can_view and can_thread and can_send):
                return False

        # 2. Exclude staff/mod/admin restricted channels where normal users are not allowed
        staff_keywords = (
            "admin", "mod", "staff", "audit", "log", "backups", "database",
            "secret", "private", "mgmt", "management", "officer", "council"
        )
        ch_name_lower = ch.name.lower()
        if any(kw in ch_name_lower for kw in staff_keywords):
            # If name indicates staff/mod/admin channel, verify if @everyone is locked out
            if hasattr(ch, "permissions_for") and hasattr(guild, "default_role") and guild.default_role:
                everyone_perms = ch.permissions_for(guild.default_role)
                if not getattr(everyone_perms, "view_channel", True):
                    return False
            else:
                return False

        # 3. Check @everyone visibility (normal users must have view_channel to access threads)
        if hasattr(ch, "permissions_for") and hasattr(guild, "default_role") and guild.default_role:
            everyone_perms = ch.permissions_for(guild.default_role)
            if hasattr(everyone_perms, "view_channel") and not everyone_perms.view_channel:
                return False

        return True

    async def find_parent_review_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """
        Finds or auto-creates the best user-accessible parent text channel in which to spawn
        private guest review threads, ensuring normal applicants and referring students can be
        pulled into and participate in the thread.

        Priority:
        1. Configured help channel (if accessible to normal users)
        2. Configured review channel (if accessible to normal users)
        3. Public/User-accessible help, support, or verification channels
        4. Any public text channel where normal users have view permissions and bot can spawn threads
        5. Auto-creation of '#ask-for-help' channel (bound to default usage in DB)
        6. Fallback to any channel where bot has thread permissions
        """
        settings = await self.db.get_guild_settings(guild.id)
        # 1. Configured help channel (highest priority for user accessibility)
        if settings and settings[1]:
            ch = guild.get_channel(settings[1])
            if isinstance(ch, discord.TextChannel):
                if self.is_channel_accessible_for_guest_threads(ch, guild):
                    return ch
            else:
                # Stale or deleted help channel in DB
                await self.db.clear_stale_channel_setting(guild.id, "help")
                await self.db.log(
                    "WARNING",
                    "STALE_CHANNEL_HEALED",
                    f"Configured help channel ID {settings[1]} no longer exists in '{guild.name}'. Setting cleared.",
                    guild=guild,
                )

        # 2. Configured review channel
        if settings and settings[3]:
            ch = guild.get_channel(settings[3])
            if isinstance(ch, discord.TextChannel):
                if self.is_channel_accessible_for_guest_threads(ch, guild):
                    return ch
            else:
                # Stale or deleted review channel in DB
                await self.db.clear_stale_channel_setting(guild.id, "review")
                await self.db.log(
                    "WARNING",
                    "STALE_CHANNEL_HEALED",
                    f"Configured review channel ID {settings[3]} no longer exists in '{guild.name}'. Setting cleared.",
                    guild=guild,
                )

        # 3. Autodetect public user-accessible channels by helpful keywords in priority order
        keywords = ("ask-for-help", "help", "support", "bantuan", "verification", "verify", "guest", "inquiries", "inquiry", "questions", "general")
        for kw in keywords:
            for ch in getattr(guild, "text_channels", []):
                if kw in ch.name.lower() and self.is_channel_accessible_for_guest_threads(ch, guild):
                    return ch

        # 4. Check any other public text channel where @everyone can view and bot can thread
        for ch in getattr(guild, "text_channels", []):
            if self.is_channel_accessible_for_guest_threads(ch, guild):
                return ch

        # 5. Auto-create '#ask-for-help' channel and tie it to default usage in database
        bot_member = getattr(guild, "me", None)
        can_create_ch = False
        if bot_member:
            guild_perms = getattr(bot_member, "guild_permissions", None)
            if guild_perms and (getattr(guild_perms, "manage_channels", False) or getattr(guild_perms, "administrator", False)):
                can_create_ch = True

        if can_create_ch and hasattr(guild, "create_text_channel"):
            try:
                overwrites = {}
                if hasattr(guild, "default_role") and guild.default_role:
                    overwrites[guild.default_role] = discord.PermissionOverwrite(
                        view_channel=True,
                        read_message_history=True,
                        send_messages=True,
                        send_messages_in_threads=True,
                        add_reactions=True,
                    )
                if bot_member:
                    overwrites[bot_member] = discord.PermissionOverwrite(
                        view_channel=True,
                        manage_channels=True,
                        manage_threads=True,
                        create_private_threads=True,
                        create_public_threads=True,
                        send_messages=True,
                        send_messages_in_threads=True,
                        embed_links=True,
                        attach_files=True,
                    )

                new_ch = await guild.create_text_channel(
                    name="ask-for-help",
                    overwrites=overwrites,
                    topic="💬 TARVeri Help & Guest Verification • Ask questions or track your verification review threads here.",
                    reason="TARVeri: Auto-created help channel for user-accessible verification and guest review threads",
                )

                # Tie newly created channel to default usage in database
                await self.db.set_guild_help_channel(guild.id, new_ch.id)

                # Send pinned welcome & guidance message
                embed = discord.Embed(
                    title="💬 Welcome to #ask-for-help",
                    description=(
                        "This channel is dedicated to **TARUMT student verification help**, inquiries, and private guest verification review threads.\n\n"
                        "• 🎓 **Students**: Run `/verify` to receive your faculty and campus roles.\n"
                        "• 🎟️ **Guests**: Use your referral code or submit a guest application.\n"
                        "• ❓ **Need Assistance?**: Ask here and our staff or moderators will assist you."
                    ),
                    color=discord.Color.blue(),
                )
                embed.set_footer(text="TARVeri Automated Server Assistance")
                try:
                    msg = await new_ch.send(embed=embed)
                    if hasattr(msg, "pin"):
                        await msg.pin(reason="TARVeri: Pinned help channel guidance")
                except Exception as exc:
                    logger.debug("Failed pinning guidance message in new help channel: %s", exc)

                await self.db.log(
                    "INFO",
                    "AUTO_CHANNEL_CREATED",
                    f"Auto-created user-accessible #{new_ch.name} (ID: {new_ch.id}) for verification help and guest review threads",
                    guild=guild,
                )
                return new_ch
            except (discord.HTTPException, discord.Forbidden) as e:
                logger.warning(f"Failed to auto-create #ask-for-help channel in '{guild.name}': {e}")

        # 6. Fallback to any channel where bot can create private threads if auto-creation wasn't possible
        for ch in getattr(guild, "text_channels", []):
            if hasattr(ch, "permissions_for") and bot_member:
                perms = ch.permissions_for(bot_member)
                if getattr(perms, "view_channel", False) and (getattr(perms, "create_private_threads", False) or getattr(perms, "manage_threads", False)):
                    return ch

        return None

    async def find_guest_role(self, guild: discord.Guild, configured_name: str | None = None) -> discord.Role | None:
        """
        Thoroughly searches for an existing guest role in a guild across in-memory cache and live API.
        Dynamically matches configured names, known aliases, and guest/visitor regex patterns.
        """
        def _match_guest_in_list(roles: Sequence[discord.Role]) -> discord.Role | None:
            if not roles:
                return None

            # 1. Check configured guest role name first
            if configured_name:
                for r in roles:
                    if getattr(r, "name", None) == configured_name:
                        return r
                conf_clean = configured_name.strip().lower()
                for r in roles:
                    if getattr(r, "name", "").strip().lower() == conf_clean:
                        return r

            # 2. Check dynamic regex pattern (matches "Guest", "Guests", "Visitor", "Visitors", "Guest (Approved)", etc.)
            matched = []
            for r in roles:
                r_name = getattr(r, "name", "").strip()
                if not r_name:
                    continue
                if GUEST_ROLE_PATTERN.search(r_name):
                    matched.append(r)

            if matched:
                return max(matched, key=lambda r: getattr(r, "position", 0))

            return None

        return await self.role_manager.find_role_in_guild(guild, _match_guest_in_list)

    async def get_or_create_guest_role(self, guild: discord.Guild) -> discord.Role | None:
        """
        Retrieves the guest role for the guild.
        First checks server configuration in DB, then searches for existing roles matching
        'Guest(Approved)', 'Guest (Approved)', 'Guest', etc. across cache and live API.
        Only creates a new 'Guest(Approved)' role if no matching guest role exists.
        Guarantees idempotency via double-checked locking across concurrent tasks.
        """
        settings = await self.db.get_guild_settings(guild.id)
        configured_name = settings[2].strip() if settings and settings[2] else None

        def _match_guest_in_list(roles: Sequence[discord.Role]) -> discord.Role | None:
            if not roles:
                return None
            if configured_name:
                for r in roles:
                    if getattr(r, "name", None) == configured_name:
                        return r
                conf_clean = configured_name.strip().lower()
                for r in roles:
                    if getattr(r, "name", "").strip().lower() == conf_clean:
                        return r

            matched = []
            for r in roles:
                r_name = getattr(r, "name", "").strip()
                if not r_name:
                    continue
                if GUEST_ROLE_PATTERN.search(r_name):
                    matched.append(r)

            if matched:
                return max(matched, key=lambda r: getattr(r, "position", 0))
            return None

        role_name_to_create = configured_name or "Guest(Approved)"
        permissions = discord.Permissions(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            add_reactions=True,
            use_external_emojis=True,
            connect=True,
            speak=True,
            use_voice_activation=True,
        )
        return await self.role_manager.get_or_create_role(
            guild,
            role_name=role_name_to_create,
            matcher=_match_guest_in_list,
            colour=GUEST_ROLE_COLOR,
            permissions=permissions,
            reason="TARVeri: Auto-created Guest(Approved) role for verified guests",
        )

    async def get_admin_role_or_fallback(self, guild: discord.Guild) -> discord.Role | None:
        """
        Intelligently discovers the server's administrator or staff role in priority order:
        1. Configured per-guild admin role from database (guild_settings.admin_role_name)
        2. Configured global admin role name (self.admin_role_name or 'TARVeri Admin')
        3. Common administrative role names: 'Admin', 'Administrator', 'Staff', 'Moderator', 'Mod'
        4. Server roles with Administrator or Manage Guild permissions.
        """
        if not guild or not hasattr(guild, "roles"):
            return None

        roles = list(guild.roles) if isinstance(guild.roles, (list, tuple, set)) else []
        if not roles:
            return None

        # 1. Guild-specific configured role from database
        try:
            settings = await self.db.get_guild_settings(guild.id)
            if settings and len(settings) > 4 and settings[4]:
                guild_admin_role_name = settings[4].strip()
                for r in roles:
                    if r.name == guild_admin_role_name or r.name.lower() == guild_admin_role_name.lower():
                        return r
        except Exception as e:
            logger.debug(f"Failed to fetch guild settings for admin role check: {e}")

        # 2. Configured global name
        if self.admin_role_name:
            for r in roles:
                if r.name == self.admin_role_name or r.name.lower() == self.admin_role_name.lower():
                    return r

        # 3. Known administrative aliases
        aliases = (
            "admin",
            "administrator",
            "administrators",
            "staff",
            "moderator",
            "moderators",
            "mod",
            "mods",
            "management",
            "server admin",
            "tarveri admin",
        )
        for alias in aliases:
            for r in roles:
                if r.name.lower() == alias:
                    return r

        # 4. Roles with Administrator or Manage Guild permissions (highest role first)
        for r in reversed(roles):
            if getattr(r, "is_default", lambda: False)():
                continue
            perms = getattr(r, "permissions", None)
            if perms and (getattr(perms, "administrator", False) or getattr(perms, "manage_guild", False)):
                return r

        return None

    async def get_admin_candidates(
        self,
        guild: discord.Guild,
        exclude_ids: set[int] | None = None,
    ) -> list[discord.Member]:
        """
        Discovers and ranks all administrator/moderator members in the server sorted by authority hierarchy:
        1. Server Owner
        2. Members with Administrator permission
        3. Members with Manage Guild / Manage Threads permission
        4. Members with configured or discovered Admin role
        Sorted by authority (highest ranking first, based on role hierarchy and admin permissions).
        """
        if not guild:
            return []

        if hasattr(guild, "chunk") and not getattr(guild, "chunked", True):
            try:
                await guild.chunk()
            except Exception as e:
                logger.debug(f"Guild chunking skipped/failed: {e}")

        admin_role = await self.get_admin_role_or_fallback(guild)
        admin_members: set[discord.Member] = set()

        # A. Role members
        if admin_role:
            if hasattr(admin_role, "members") and admin_role.members:
                admin_members.update(admin_role.members)
            elif hasattr(guild, "members") and guild.members:
                for m in guild.members:
                    if admin_role in getattr(m, "roles", []):
                        admin_members.add(m)

        # B. Permission-based admins
        if hasattr(guild, "members") and guild.members:
            for m in guild.members:
                if getattr(m, "bot", False):
                    continue
                perms = getattr(m, "guild_permissions", None)
                if perms and (
                    getattr(perms, "administrator", False)
                    or getattr(perms, "manage_guild", False)
                    or getattr(perms, "manage_threads", False)
                ):
                    admin_members.add(m)

        # C. Server owner
        owner_obj = getattr(guild, "owner", None)
        if owner_obj and isinstance(getattr(owner_obj, "id", None), int) and getattr(owner_obj, "bot", None) is not True:
            admin_members.add(owner_obj)
        elif getattr(guild, "owner_id", None) and isinstance(guild.owner_id, int):
            owner_m = guild.get_member(guild.owner_id)
            if owner_m and isinstance(getattr(owner_m, "id", None), int):
                admin_members.add(owner_m)

        # Filter out bots and excluded IDs
        effective_exclude = set(exclude_ids or set())
        me_id = getattr(getattr(guild, "me", None), "id", None)
        if me_id:
            effective_exclude.add(me_id)

        valid_admins = [
            m for m in admin_members
            if m and getattr(m, "bot", None) is not True and m.id not in effective_exclude
        ]

        def authority_key(m: discord.Member) -> tuple[int, int, int, int]:
            is_owner = 1 if (m == getattr(guild, "owner", None) or getattr(m, "id", None) == getattr(guild, "owner_id", None)) else 0
            perms = getattr(m, "guild_permissions", None)
            has_admin = 1 if (perms and getattr(perms, "administrator", None) is True) else 0
            has_manage_guild = 1 if (perms and getattr(perms, "manage_guild", None) is True) else 0
            top_role = getattr(m, "top_role", None)
            top_role_pos = getattr(top_role, "position", 0) if isinstance(getattr(top_role, "position", None), int) else 0
            return (is_owner, has_admin, has_manage_guild, top_role_pos)

        valid_admins.sort(key=authority_key, reverse=True)
        return valid_admins

    async def get_target_admin_mentions_batch(
        self,
        guild: discord.Guild,
        count: int = 2,
        exclude_ids: set[int] | None = None,
    ) -> tuple[str, list[int]]:
        """
        Selects a batch of up to `count` target administrators to tag:
        1. Discovers and ranks candidate administrators (Authority hierarchy).
        2. Prioritizes ACTIVE (Online, Idle, DND) admins first, followed by highest-authority offline admins.
        3. Returns a tuple of (mention_string, list_of_admin_ids).
        4. If no individual admins are available, falls back to Admin Role mention.
        """
        candidates = await self.get_admin_candidates(guild, exclude_ids=exclude_ids)
        if candidates:
            active_admins: list[discord.Member] = []
            offline_admins: list[discord.Member] = []
            for m in candidates:
                status = getattr(m, "status", None)
                if status in (discord.Status.online, discord.Status.idle, discord.Status.dnd) or str(status).lower() in ("online", "idle", "dnd"):
                    active_admins.append(m)
                else:
                    offline_admins.append(m)

            ordered = active_admins + offline_admins
            batch = ordered[:count]

            if batch:
                mentions = ", ".join(
                    m.mention if isinstance(getattr(m, "mention", None), str) else f"<@{m.id}>"
                    for m in batch
                )
                admin_ids = [
                    int(m.id) if (isinstance(getattr(m, "id", None), int) or str(getattr(m, "id", "")).isdigit()) else 0
                    for m in batch
                ]
                return mentions, [i for i in admin_ids if i > 0]

        # Fallback to role mention or owner ID
        admin_role = await self.get_admin_role_or_fallback(guild)
        if admin_role:
            role_mention = (
                admin_role.mention
                if isinstance(getattr(admin_role, "mention", None), str)
                else f"<@&{getattr(admin_role, 'id', 0)}>"
            )
            return role_mention, []
        if getattr(guild, "owner_id", None) and isinstance(guild.owner_id, int):
            return f"<@{guild.owner_id}>", [guild.owner_id]
        return "@Staff", []

    async def get_target_admin_mention(
        self,
        guild: discord.Guild,
        exclude_ids: set[int] | None = None,
        count: int = 2,
    ) -> str:
        """Convenience method that returns the mention string for the target admin batch."""
        mention_str, _ = await self.get_target_admin_mentions_batch(
            guild, count=count, exclude_ids=exclude_ids
        )
        return mention_str



    async def open_guest_review_ticket(
        self,
        guild: discord.Guild,
        applicant: discord.Member,
        referral_code: str | None = None,
        reason: str | None = None,
        referrer_id: int | None = None,
    ) -> tuple[bool, str, discord.Thread | None]:
        """
        Creates a private review thread, invites the applicant & referring student,
        and posts the review embed with action buttons.
        """
        async with self._lock:
            # 0. Check blacklist for applicant
            is_bl, bl_reason = await self.db.is_blacklisted(guild.id, user_id=applicant.id)
            if is_bl:
                await self.db.log(
                    "WARNING",
                    "BLACKLIST_ATTEMPT_BLOCKED",
                    f"Blacklisted user {applicant} (ID: {applicant.id}) attempted to open a guest ticket in '{guild.name}'. Reason: {bl_reason}",
                    user_id=applicant.id,
                    guild=guild,
                )
                return (
                    False,
                    f"⛔ You are blacklisted from requesting guest access in this server.{f' Reason: {bl_reason}' if bl_reason else ''}",
                    None,
                )

            # 1. Rate limiting on guest/referral attempts
            if self.rate_limiter:
                if self.rate_limiter.is_rate_limited(applicant.id):
                    await self.db.log(
                        "WARNING",
                        "RATE_LIMITED",
                        f"{applicant} exceeded guest application attempt limit",
                        user_id=applicant.id,
                        guild=guild,
                    )
                    return (
                        False,
                        "⏳ You've made too many attempts recently. Please wait a few minutes before trying again.",
                        None,
                    )
                self.rate_limiter.record_attempt(applicant.id)

            # 2. Check if applicant is already a verified student
            is_student = bool(await self.db.get_verification_by_user(applicant.id))
            if is_student:
                return (
                    False,
                    "ℹ️ You are already verified as a TARUMT student! You do not need guest access.",
                    None,
                )

            # 3. Check if applicant already has the Guest role
            guest_role = await self.get_or_create_guest_role(guild)
            if guest_role and guest_role in applicant.roles:
                return (
                    False,
                    f"ℹ️ You already have the **{guest_role.name}** role in this server.",
                    None,
                )

            # 4. Prevent duplicate open tickets
            existing_open = await self.db.get_open_guest_ticket_for_applicant(guild.id, applicant.id)
            if existing_open:
                return (
                    False,
                    "⏳ You already have an active guest review ticket in progress! Please check your private threads.",
                    None,
                )

            # 5. If referral code is used, validate and lock code status
            if referral_code:
                is_valid, err_msg, record = await self.validate_referral_code(guild.id, referral_code)
                if not is_valid or not record:
                    return False, err_msg, None

                # Edge case: self-referral prevention
                if record["referrer_discord_id"] == applicant.id:
                    return (
                        False,
                        "❌ You cannot use your own referral code.",
                        None,
                    )

                referrer_id = record["referrer_discord_id"]
                if referrer_id:
                    is_ref_bl, _ = await self.db.is_blacklisted(guild.id, user_id=referrer_id)
                    if is_ref_bl:
                        return (
                            False,
                            "❌ This referral code is invalid because the referring user is blacklisted in this server.",
                            None,
                        )

                await self.db.update_referral_code_status(
                    referral_code, guild.id, "PENDING_APPROVAL", used_by_discord_id=applicant.id
                )

            # 6. Locate parent channel for thread creation
            parent_ch = await self.find_parent_review_channel(guild)
            if not parent_ch:
                # Revert referral code status if channel creation fails
                if referral_code:
                    await self.db.update_referral_code_status(referral_code, guild.id, "ACTIVE")
                return (
                    False,
                    "❌ Could not find a suitable channel to create the private review thread. Please contact an admin.",
                    None,
                )

            # 7. Create Private Thread with alphanumeric tracking number (e.g. guest-a0001-username)
            seq = await self.db.get_next_guild_ticket_seq(guild.id)
            seq_code = format_ticket_seq(seq)
            clean_name = "".join(c for c in applicant.display_name if c.isalnum() or c in "-_")[:20].lower() or "guest"
            thread_name = f"guest-{seq_code.lower()}-{clean_name}"
            try:
                thread = await parent_ch.create_thread(
                    name=thread_name,
                    type=discord.ChannelType.private_thread,
                    auto_archive_duration=1440,
                    reason=f"TARVeri Guest Verification Review #{seq_code} for {applicant}",
                )
            except (discord.HTTPException, discord.Forbidden) as e:
                if referral_code:
                    await self.db.update_referral_code_status(referral_code, guild.id, "ACTIVE")
                logger.error(f"Failed to create private thread '{thread_name}' in #{parent_ch.name} ({guild.name}): {e}")
                return False, f"❌ Failed to create private thread: {e}", None

            # 8. Grant thread chat & participation permissions to applicant on parent channel
            if hasattr(parent_ch, "set_permissions"):
                try:
                    app_overwrite = parent_ch.overwrites_for(applicant)
                    app_overwrite.view_channel = True
                    app_overwrite.send_messages_in_threads = True
                    app_overwrite.read_message_history = True
                    app_overwrite.attach_files = True
                    app_overwrite.embed_links = True
                    app_overwrite.add_reactions = True
                    res = parent_ch.set_permissions(
                        applicant,
                        overwrite=app_overwrite,
                        reason=f"TARVeri: Allow guest applicant {applicant} to view and chat in review thread",
                    )
                    if inspect.isawaitable(res):
                        await res
                except (discord.HTTPException, discord.Forbidden):
                    pass

            # 9. Invite applicant, referrer, and all admin team members
            try:
                await thread.add_user(applicant)
            except (discord.HTTPException, discord.Forbidden):
                pass

            referrer_member: discord.Member | None = None
            if referrer_id:
                referrer_member = guild.get_member(referrer_id)
                if referrer_member:
                    if hasattr(parent_ch, "set_permissions"):
                        try:
                            ref_overwrite = parent_ch.overwrites_for(referrer_member)
                            ref_overwrite.view_channel = True
                            ref_overwrite.send_messages_in_threads = True
                            ref_overwrite.read_message_history = True
                            ref_overwrite.attach_files = True
                            ref_overwrite.embed_links = True
                            ref_overwrite.add_reactions = True
                            res = parent_ch.set_permissions(
                                referrer_member,
                                overwrite=ref_overwrite,
                                reason=f"TARVeri: Allow referring student {referrer_member} to view and chat in review thread",
                            )
                            if inspect.isawaitable(res):
                                await res
                        except (discord.HTTPException, discord.Forbidden):
                            pass

                    try:
                        await thread.add_user(referrer_member)
                    except (discord.HTTPException, discord.Forbidden):
                        pass

            # Auto-invite all admin team members & reviewers to private review thread
            exclude_ids = {applicant.id, getattr(getattr(guild, "me", None), "id", None)}
            if referrer_id:
                exclude_ids.add(referrer_id)

            admin_candidates = await self.get_admin_candidates(guild, exclude_ids=exclude_ids)

            # Invite discovered admin members (capped at 25 to respect Discord rate limits)
            invited_count = 0
            for adm_m in admin_candidates:
                try:
                    await thread.add_user(adm_m)
                    invited_count += 1
                    if invited_count >= 25:
                        break
                except (discord.HTTPException, discord.Forbidden):
                    pass


            # Determine initial 2 admins to tag
            _, initial_pinged_ids = await self.get_target_admin_mentions_batch(
                guild, count=2, exclude_ids=exclude_ids
            )
            pinged_str = ",".join(str(i) for i in initial_pinged_ids) if initial_pinged_ids else ""
            now_ts = now_formatted()

            # 9. Save ticket to database
            ticket_id = await self.db.create_guest_ticket(
                guild_id=guild.id,
                applicant_id=applicant.id,
                channel_id=thread.id,
                referrer_id=referrer_id,
                referral_code=referral_code,
                reason=reason,
                ticket_seq=seq,
                pinged_admin_ids=pinged_str,
                last_pinged_at=now_ts,
            )

            await self.db.log(
                "INFO",
                "GUEST_TICKET_OPENED",
                f"Opened guest review ticket #{seq_code} (DB ID: {ticket_id}) for applicant {applicant} (ID: {applicant.id})"
                + (f" with referral code '{referral_code}' (Vouched by ID: {referrer_id})" if referral_code else ""),
                guild=guild,
                user_id=applicant.id,
            )

            return True, f"✅ Guest ticket #{seq_code} created in private thread {thread.mention}!", thread

    async def approve_guest_application(
        self,
        ticket: dict[str, Any],
        guild: discord.Guild,
        admin_user: discord.User | discord.Member,
        reason: str | None = None,
    ) -> tuple[bool, str]:
        """Approves guest application, assigns guest role, updates DB, and archives thread."""
        ticket_id = ticket["ticket_id"]
        applicant_id = ticket["applicant_id"]
        referral_code = ticket.get("referral_code")
        approval_reason = reason.strip() if reason else "Approved by admin"

        # 0. Atomic DB transition check to ensure idempotency across concurrent admin actions
        closed = await self.db.close_guest_ticket(
            ticket_id, "APPROVED", closed_by_admin_id=admin_user.id, close_reason=approval_reason, only_if_open=True
        )
        if not closed:
            latest = await self.db.get_guest_ticket_by_id(ticket_id)
            status_str = latest.get("status") if latest else "UNKNOWN"
            return False, f"⚠️ Ticket #{ticket_id} is already resolved ({status_str})."

        # 1. Assign Guest Role
        guest_role = await self.get_or_create_guest_role(guild)
        if not guest_role:
            return False, "❌ Guest role does not exist and bot lacks permission to create it."

        applicant_member = guild.get_member(applicant_id)
        if not applicant_member:
            try:
                applicant_member = await guild.fetch_member(applicant_id)
            except (discord.NotFound, discord.HTTPException):
                applicant_member = None

        if applicant_member:
            try:
                await applicant_member.add_roles(
                    guest_role, reason=f"TARVeri: Guest approved by {admin_user}"
                )
            except discord.HTTPException as e:
                return False, f"❌ Failed to assign guest role to applicant: {e}"

            try:
                await applicant_member.send(
                    f"🎉 **Congratulations!** Your guest application to **{guild.name}** has been approved by staff.\n"
                    f"You have been granted the **{guest_role.name}** role. Welcome to the server!"
                )
            except discord.Forbidden:
                pass

        # 2. Update referral code status
        if referral_code:
            await self.db.update_referral_code_status(
                referral_code, guild.id, "USED", used_by_discord_id=applicant_id
            )

        await self.db.log(
            "INFO",
            "GUEST_APPROVED",
            f"Admin {admin_user} (ID: {admin_user.id}) approved guest ticket #{ticket_id} for user ID {applicant_id} in '{guild.name}'. Reason: {approval_reason}",
            guild=guild,
            user_id=applicant_id,
        )

        await self._cleanup_channel_overwrites(guild, ticket)

        return True, f"✅ Guest application approved by {admin_user.mention}! Assigned **{guest_role.name}** role."

    async def reject_guest_application(
        self,
        ticket: dict[str, Any],
        guild: discord.Guild,
        admin_user: discord.User | discord.Member,
        reason: str | None = None,
    ) -> tuple[bool, str]:
        """Rejects guest application, sends notification DM, kicks user, and updates DB."""
        ticket_id = ticket["ticket_id"]
        applicant_id = ticket["applicant_id"]
        referral_code = ticket.get("referral_code")
        reject_reason = reason.strip() if reason else "Guest application not approved by server administration."

        # 0. Atomic DB transition check to ensure idempotency across concurrent admin actions
        closed = await self.db.close_guest_ticket(
            ticket_id, "REJECTED", closed_by_admin_id=admin_user.id, close_reason=reject_reason, only_if_open=True
        )
        if not closed:
            latest = await self.db.get_guest_ticket_by_id(ticket_id)
            status_str = latest.get("status") if latest else "UNKNOWN"
            return False, f"⚠️ Ticket #{ticket_id} is already resolved ({status_str})."

        applicant_member = guild.get_member(applicant_id)
        if not applicant_member:
            try:
                applicant_member = await guild.fetch_member(applicant_id)
            except (discord.NotFound, discord.HTTPException):
                applicant_member = None

        # 1. Send DM before kicking
        if applicant_member:
            try:
                await applicant_member.send(
                    f"⚠️ Your guest access application to **{guild.name}** was not approved.\n"
                    f"**Reason:** {reject_reason}"
                )
            except discord.Forbidden:
                pass

            # 2. Kick member
            if guild.me.guild_permissions.kick_members and applicant_member.top_role < guild.me.top_role:
                try:
                    await applicant_member.kick(
                        reason=f"TARVeri: Guest application rejected by {admin_user}: {reject_reason}"
                    )
                except discord.HTTPException as e:
                    logger.warning(f"Could not kick rejected guest {applicant_member}: {e}")

        # 3. Update referral code status
        if referral_code:
            await self.db.update_referral_code_status(
                referral_code, guild.id, "REJECTED", used_by_discord_id=applicant_id
            )

        await self.db.log(
            "INFO",
            "GUEST_REJECTED",
            f"Admin {admin_user} (ID: {admin_user.id}) rejected guest ticket #{ticket_id} for user ID {applicant_id} in '{guild.name}'. Reason: {reject_reason}",
            guild=guild,
            user_id=applicant_id,
        )

        await self._cleanup_channel_overwrites(guild, ticket)

        return True, f"🛑 Guest application rejected and applicant removed from the server by {admin_user.mention}."

    async def close_guest_ticket_manually(
        self,
        ticket: dict[str, Any],
        guild: discord.Guild,
        admin_user: discord.User | discord.Member,
        reason: str | None = None,
    ) -> tuple[bool, str]:
        """
        Manually closes a guest review ticket without granting guest roles or kicking/banning the user.
        Useful for spam handling, inquiry resolutions, administrative dismissals, or manual reconsideration.
        """
        ticket_id = ticket["ticket_id"]
        applicant_id = ticket["applicant_id"]
        referral_code = ticket.get("referral_code")
        seq = ticket.get("ticket_seq") or ticket_id
        seq_code = format_ticket_seq(seq)
        close_reason = reason.strip() if reason and reason.strip() else "Manually closed by administrator (No action taken)"

        # 0. Atomic DB transition check to ensure idempotency across concurrent admin actions
        closed = await self.db.close_guest_ticket(
            ticket_id, "CLOSED", closed_by_admin_id=admin_user.id, close_reason=close_reason, only_if_open=True
        )
        if not closed:
            latest = await self.db.get_guest_ticket_by_id(ticket_id)
            status_str = latest.get("status") if latest else "UNKNOWN"
            return False, f"⚠️ Ticket #{seq_code} is already resolved ({status_str})."

        # 1. Update referral code status if applicable
        if referral_code:
            await self.db.update_referral_code_status(
                referral_code, guild.id, "CLOSED", used_by_discord_id=applicant_id
            )

        # 2. Cleanup temporary channel overwrites
        await self._cleanup_channel_overwrites(guild, ticket)

        # 3. Log administrative action
        await self.db.log(
            "INFO",
            "GUEST_TICKET_CLOSED_MANUAL",
            f"Admin {admin_user} (ID: {admin_user.id}) manually closed guest ticket #{seq_code} (DB ID: {ticket_id}) for applicant ID {applicant_id} in '{guild.name}' without kicking. Reason: {close_reason}",
            guild=guild,
            user_id=applicant_id,
        )

        return True, f"🔒 Guest review ticket #{seq_code} manually closed by {admin_user.mention} (Applicant remains in server, no roles altered)."

    async def _get_or_fetch_thread(self, guild: discord.Guild, channel_id: int | None) -> discord.Thread | None:
        """Helper to resolve a thread by ID from in-memory cache or Discord API fetch."""
        if not channel_id:
            return None
        if hasattr(guild, "get_thread"):
            thread = guild.get_thread(channel_id)
            if thread:
                return thread
        if hasattr(guild, "get_channel"):
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.Thread):
                return channel
        # Fetch directly from Discord API via bot.fetch_channel
        bot = self.bot or getattr(guild, "_state", None) and getattr(guild._state, "_get_client", lambda: None)()
        if bot and hasattr(bot, "fetch_channel"):
            try:
                res = bot.fetch_channel(channel_id)
                fetched = await res if inspect.isawaitable(res) else res
                if isinstance(fetched, discord.Thread):
                    return fetched
            except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                return None
        return None

    async def _cleanup_channel_overwrites(self, guild: discord.Guild, ticket: dict[str, Any]) -> None:
        """Cleans up temporary channel permission overwrites granted to applicant/referrer."""
        channel_id = ticket.get("channel_id")
        if not channel_id:
            return

        parent_ch = None
        thread = await self._get_or_fetch_thread(guild, channel_id)
        if thread and hasattr(thread, "parent"):
            parent_ch = thread.parent
        elif hasattr(guild, "get_channel"):
            ch = guild.get_channel(channel_id)
            if ch and hasattr(ch, "parent"):
                parent_ch = ch.parent

        if not parent_ch or not hasattr(parent_ch, "set_permissions"):
            return

        applicant_id = ticket.get("applicant_id")
        if applicant_id:
            target = guild.get_member(applicant_id) or discord.Object(id=applicant_id)
            try:
                res = parent_ch.set_permissions(target, overwrite=None, reason="TARVeri: Review ticket closed")
                if inspect.isawaitable(res):
                    await res
            except (discord.HTTPException, discord.Forbidden):
                pass

        referrer_id = ticket.get("referrer_id")
        if referrer_id:
            target_ref = guild.get_member(referrer_id) or discord.Object(id=referrer_id)
            try:
                res = parent_ch.set_permissions(target_ref, overwrite=None, reason="TARVeri: Review ticket closed")
                if inspect.isawaitable(res):
                    await res
            except (discord.HTTPException, discord.Forbidden):
                pass

    async def handle_member_leave_or_ban(
        self,
        guild: discord.Guild,
        user: discord.User | discord.Member,
        is_ban: bool = False,
    ) -> None:
        """
        Revokes guest access, expires open review tickets, archives private review threads,
        and invalidates active referrals when a user leaves, is kicked, or is banned from the server.
        """
        revocation_status = "BANNED" if is_ban else "LEFT_SERVER"
        close_reason = "Member banned from server" if is_ban else "Member left the server"
        referrer_close_reason = "Referring student banned from server" if is_ban else "Referring student left server"

        # 1. Clean up active review threads and channel permissions in real time
        try:
            open_applicant_ticket = await self.db.get_open_guest_ticket_for_applicant(guild.id, user.id)
            if open_applicant_ticket:
                thread_id = open_applicant_ticket.get("channel_id")
                thread = await self._get_or_fetch_thread(guild, thread_id)

                if thread and not getattr(thread, "archived", False):
                    reason_msg = "banned from" if is_ban else "left"
                    try:
                        await thread.send(
                            f"🛑 **Guest applicant {reason_msg} the server.** This review ticket has been automatically closed and the thread is archived."
                        )
                    except (discord.HTTPException, discord.Forbidden):
                        pass

                    # Try to update starter review embed if found
                    try:
                        from tarveri.cogs.guest_cog import build_review_embed
                        starter_msg = getattr(thread, "starter_message", None)
                        if starter_msg and hasattr(starter_msg, "edit"):
                            embed = build_review_embed(
                                open_applicant_ticket, guild, None, status_override=revocation_status
                            )
                            await starter_msg.edit(embed=embed, view=discord.ui.View())
                    except Exception as exc:
                        logger.debug("Failed updating applicant review message: %s", exc)

                    try:
                        await thread.edit(archived=True, locked=True, reason=f"TARVeri: Applicant {reason_msg} server")
                    except (discord.HTTPException, discord.Forbidden):
                        pass

                await self._cleanup_channel_overwrites(guild, open_applicant_ticket)

            # Check open tickets referred by this user
            open_referred = await self.db.list_guest_tickets(guild.id, status="OPEN")
            for t in open_referred:
                if t.get("referrer_id") == user.id:
                    thread_id = t.get("channel_id")
                    thread = await self._get_or_fetch_thread(guild, thread_id)

                    if thread and not getattr(thread, "archived", False):
                        reason_msg = "banned from" if is_ban else "left"
                        try:
                            await thread.send(
                                f"🛑 **Referring student (<@{user.id}>) {reason_msg} the server.** This referral ticket has been cancelled and the thread is archived."
                            )
                        except (discord.HTTPException, discord.Forbidden):
                            pass

                        try:
                            from tarveri.cogs.guest_cog import build_review_embed
                            starter_msg = getattr(thread, "starter_message", None)
                            if starter_msg and hasattr(starter_msg, "edit"):
                                applicant_member = guild.get_member(t["applicant_id"])
                                embed = build_review_embed(
                                    t, guild, applicant_member, status_override=revocation_status
                                )
                                await starter_msg.edit(embed=embed, view=discord.ui.View())
                        except Exception as exc:
                            logger.debug("Failed updating referrer review message: %s", exc)

                        try:
                            await thread.edit(archived=True, locked=True, reason=f"TARVeri: Referrer {reason_msg} server")
                        except (discord.HTTPException, discord.Forbidden):
                            pass

                    await self._cleanup_channel_overwrites(guild, t)
        except Exception as e:
            logger.warning(f"Error notifying/archiving review threads for departing user {user}: {e}")

        # 2. Update database records
        revoked_tickets = await self.db.revoke_guest_tickets_for_user(
            guild.id, user.id, status=revocation_status, close_reason=close_reason
        )
        revoked_referred_tickets = await self.db.cancel_open_tickets_referred_by_user(
            guild.id, user.id, close_reason=referrer_close_reason
        )
        revoked_referrals = await self.db.revoke_active_referrals_for_user(
            guild.id, user.id, status=revocation_status
        )

        action_type = "GUEST_REVOKED_ON_BAN" if is_ban else "GUEST_REVOKED_ON_LEAVE"
        action_verb = "banned from" if is_ban else "left / was removed from"

        total_affected = revoked_tickets + revoked_referred_tickets + revoked_referrals
        if total_affected > 0:
            await self.db.log(
                "INFO",
                action_type,
                f"Revoked guest status ({revoked_tickets} ticket(s), {revoked_referred_tickets} referred ticket(s), {revoked_referrals} referral(s)) for {user} (ID: {user.id}) who {action_verb} '{guild.name}'",
                guild=guild,
                user_id=user.id,
            )

    async def reconcile_downtime_state(self) -> dict[str, int]:
        """
        Reconciles missed events (member leaves, bans, thread deletions, code expirations)
        that occurred while the bot was offline or in maintenance.
        """
        summary = {
            "reconciled_tickets": 0,
            "reconciled_referrals": 0,
            "expired_referrals": 0,
        }

        # 1. Cleanup expired referral codes
        summary["expired_referrals"] = await self.db.cleanup_expired_referrals()

        # 2. Check all open tickets
        open_tickets = await self.db.get_open_guest_tickets()
        for t in open_tickets:
            guild = self.bot.get_guild(t["guild_id"]) if self.bot else None
            if not guild:
                continue

            # Ensure guild member cache is populated
            if hasattr(guild, "chunk") and not getattr(guild, "chunked", True):
                try:
                    await guild.chunk()
                except Exception as exc:
                    logger.debug("Failed chunking guild members during downtime reconciliation: %s", exc)

            applicant_id = t["applicant_id"]
            applicant_member = guild.get_member(applicant_id)
            if not applicant_member and hasattr(guild, "fetch_member"):
                try:
                    applicant_member = await guild.fetch_member(applicant_id)
                except (discord.NotFound, discord.HTTPException):
                    applicant_member = None

            # If applicant left or was banned during maintenance
            if not applicant_member:
                is_ban = False
                if hasattr(guild, "fetch_ban"):
                    try:
                        await guild.fetch_ban(discord.Object(id=applicant_id))
                        is_ban = True
                    except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                        is_ban = False

                obj_user = discord.Object(id=applicant_id)
                await self.handle_member_leave_or_ban(guild, obj_user, is_ban=is_ban)
                summary["reconciled_tickets"] += 1

                # Clean up thread if it exists
                thread = await self._get_or_fetch_thread(guild, t["channel_id"])
                if thread and not getattr(thread, "archived", False):
                    try:
                        await thread.send("🛑 **Guest applicant left or was removed from server during maintenance.** Thread archived.")
                        await thread.edit(archived=True, locked=True)
                    except (discord.HTTPException, discord.Forbidden):
                        pass
                continue

            # Check if applicant was manually granted the guest role by an admin during downtime
            guest_role = await self.get_or_create_guest_role(guild)
            if (
                guest_role
                and applicant_member
                and hasattr(applicant_member, "roles")
                and guest_role in getattr(applicant_member, "roles", [])
            ):
                await self.db.close_guest_ticket(
                    t["ticket_id"],
                    status="APPROVED",
                    close_reason="Applicant was manually granted guest role by admin",
                    only_if_open=True,
                )
                if t.get("referral_code"):
                    await self.db.update_referral_code_status(
                        t["referral_code"], guild.id, "USED", used_by_discord_id=applicant_id
                    )
                thread = await self._get_or_fetch_thread(guild, t["channel_id"])

                if thread and not getattr(thread, "archived", False):
                    try:
                        await thread.send("✅ **Guest role was already granted to applicant.** Review ticket auto-resolved.")
                        await thread.edit(archived=True, locked=True)
                    except (discord.HTTPException, discord.Forbidden):
                        pass

                await self.db.log(
                    "INFO",
                    "MANUAL_GUEST_GRANT_RECONCILED",
                    f"Ticket #{t.get('ticket_seq', t['ticket_id'])} auto-resolved because applicant {applicant_member} already has guest role.",
                    guild=guild,
                    user_id=applicant_id,
                )
                summary["reconciled_tickets"] += 1
                continue

            # If referrer left or was banned during maintenance
            referrer_id = t.get("referrer_id")
            if referrer_id:
                referrer_member = guild.get_member(referrer_id)
                if not referrer_member and hasattr(guild, "fetch_member"):
                    try:
                        referrer_member = await guild.fetch_member(referrer_id)
                    except (discord.NotFound, discord.HTTPException):
                        referrer_member = None

                if not referrer_member:
                    is_ban = False
                    if hasattr(guild, "fetch_ban"):
                        try:
                            await guild.fetch_ban(discord.Object(id=referrer_id))
                            is_ban = True
                        except (discord.NotFound, discord.HTTPException, discord.Forbidden):
                            is_ban = False

                    await self.handle_member_leave_or_ban(guild, discord.Object(id=referrer_id), is_ban=is_ban)
                    summary["reconciled_tickets"] += 1

                    thread = await self._get_or_fetch_thread(guild, t["channel_id"])
                    if thread and not getattr(thread, "archived", False):
                        try:
                            await thread.send("🛑 **Referring student left or was removed from server during maintenance.** Application cancelled.")
                            await thread.edit(archived=True, locked=True)
                        except (discord.HTTPException, discord.Forbidden):
                            pass
                    continue

            # Check if thread was deleted during maintenance
            thread = await self._get_or_fetch_thread(guild, t["channel_id"])

            if not thread:
                await self.db.close_guest_ticket(
                    t["ticket_id"],
                    status="EXPIRED",
                    close_reason="Review thread was deleted during maintenance",
                    only_if_open=True,
                )
                summary["reconciled_tickets"] += 1

        # 3. Check active referral codes whose creators left during maintenance
        active_referrals = await self.db.get_all_active_referrals()
        for ref in active_referrals:
            guild = self.bot.get_guild(ref["guild_id"]) if self.bot else None
            if not guild:
                continue
            creator_id = ref["referrer_discord_id"]
            creator = guild.get_member(creator_id)
            if not creator and hasattr(guild, "fetch_member"):
                try:
                    creator = await guild.fetch_member(creator_id)
                except (discord.NotFound, discord.HTTPException):
                    creator = None

            if not creator:
                await self.db.revoke_active_referrals_for_user(
                    guild.id, creator_id, status="LEFT_SERVER"
                )
                summary["reconciled_referrals"] += 1

        total_reconciled = summary["reconciled_tickets"] + summary["reconciled_referrals"] + summary["expired_referrals"]
        if total_reconciled > 0:
            await self.db.log(
                "INFO",
                "MAINTENANCE_RECONCILIATION_COMPLETE",
                f"Maintenance catch-up: Reconciled {summary['reconciled_tickets']} ticket(s), {summary['reconciled_referrals']} orphaned referral(s), {summary['expired_referrals']} expired referral(s)",
            )
            logger.info(
                f"Maintenance catch-up complete: {summary['reconciled_tickets']} tickets, "
                f"{summary['reconciled_referrals']} referrals, {summary['expired_referrals']} expired codes."
            )

        return summary

    async def check_and_escalate_tickets(
        self,
        interval_seconds: float = 3600.0,
    ) -> int:
        """
        Scans all open guest tickets. If a ticket has been pending for >= interval_seconds (default 1 hour)
        without an admin response, escalates by tagging the next batch of 2 admins in the thread.
        """
        open_tickets = await self.db.get_open_guest_tickets()
        if not open_tickets:
            return 0

        escalated_count = 0
        now_dt = datetime.now(tz=get_configured_tz())

        for t in open_tickets:
            last_ping_str = t.get("last_pinged_at") or t.get("created_at")
            last_ping_dt = parse_db_timestamp(last_ping_str)
            if not last_ping_dt:
                continue

            elapsed = (now_dt - last_ping_dt).total_seconds()
            if elapsed < interval_seconds:
                continue

            guild_id = t["guild_id"]
            channel_id = t["channel_id"]
            ticket_id = t["ticket_id"]

            guild = self.bot.get_guild(guild_id) if self.bot else None
            if not guild:
                continue

            thread = await self._get_or_fetch_thread(guild, channel_id)

            if not thread or getattr(thread, "archived", False) or getattr(thread, "locked", False):
                continue

            # Check if an admin has replied in the thread since the last ping
            admin_has_replied = False
            if hasattr(thread, "history"):
                try:
                    async for msg in thread.history(limit=50, oldest_first=False):
                        if msg.author.bot:
                            continue
                        if msg.author.id == t["applicant_id"] or msg.author.id == t.get("referrer_id"):
                            continue
                        # Any message from a staff member / other user counts as an admin response
                        admin_has_replied = True
                        break
                except (discord.HTTPException, discord.Forbidden):
                    pass

            if admin_has_replied:
                # Admin actively communicating — reset timer without spamming other staff
                await self.db.update_guest_ticket_escalation(
                    ticket_id, t.get("pinged_admin_ids") or "", now_formatted()
                )
                continue

            # Parse already pinged admin IDs
            raw_pinged = t.get("pinged_admin_ids") or ""
            already_pinged_ids = {int(x) for x in raw_pinged.split(",") if x.strip().isdigit()}

            # Exclude already pinged admins, applicant, referrer, and bot
            exclude_ids = already_pinged_ids | {t["applicant_id"]}
            if t.get("referrer_id"):
                exclude_ids.add(t["referrer_id"])

            next_mentions, next_ids = await self.get_target_admin_mentions_batch(
                guild, count=2, exclude_ids=exclude_ids
            )

            if next_ids:
                try:
                    await thread.send(
                        content=(
                            f"⏰ **Ticket Escalation (Pending for 1 hour without admin response):**\n"
                            f"{next_mentions} — Please review this guest verification request!"
                        ),
                        allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
                    )
                except (discord.HTTPException, discord.Forbidden) as e:
                    logger.warning(f"Could not send escalation message in thread #{channel_id}: {e}")
                    continue

                updated_pinged_ids = {
                    int(x) for x in already_pinged_ids | set(next_ids)
                    if isinstance(x, int) or str(x).isdigit()
                }
                pinged_str = ",".join(str(i) for i in sorted(updated_pinged_ids))
                await self.db.update_guest_ticket_escalation(ticket_id, pinged_str, now_formatted())
                await self.db.log(
                    "INFO",
                    "GUEST_TICKET_ESCALATED",
                    f"Ticket #{t.get('ticket_seq', ticket_id)} escalated to admins: {next_ids} after 1hr inactivity",
                    guild=guild,
                    user_id=t["applicant_id"],
                )
                escalated_count += 1
            else:
                # All candidate admins were already tagged — send a reminder ping to admin role
                admin_role = await self.get_admin_role_or_fallback(guild)
                reminder_tag = admin_role.mention if admin_role else "@Staff"
                try:
                    await thread.send(
                        content=(
                            f"⏰ **Ticket Escalation Reminder (Pending > 1 hour):**\n"
                            f"{reminder_tag} — All initial staff were notified. Please assist with this guest verification!"
                        ),
                        allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=False),
                    )
                except (discord.HTTPException, discord.Forbidden):
                    pass
                await self.db.update_guest_ticket_escalation(ticket_id, raw_pinged, now_formatted())

        return escalated_count

    def start_escalation_task(
        self, check_interval_seconds: float = 60.0, escalation_delay_seconds: float = 3600.0
    ) -> None:
        """Starts the background task that checks and escalates overdue open review tickets."""
        if self._escalation_task is None or self._escalation_task.done():
            self._escalation_task = asyncio.create_task(
                self._escalation_loop(check_interval_seconds, escalation_delay_seconds),
                name="tarveri_ticket_escalation",
            )
            logger.info("Ticket escalation background task started.")

    def stop_escalation_task(self) -> None:
        """Cancels the ticket escalation background task."""
        if self._escalation_task and not self._escalation_task.done():
            self._escalation_task.cancel()
            self._escalation_task = None
            logger.info("Ticket escalation background task stopped.")

    async def _escalation_loop(
        self, check_interval_seconds: float, escalation_delay_seconds: float
    ) -> None:
        """Background loop executing check_and_escalate_tickets periodically."""
        try:
            while True:
                await asyncio.sleep(check_interval_seconds)
                try:
                    if self.db and self.db.is_connected:
                        await self.check_and_escalate_tickets(
                            interval_seconds=escalation_delay_seconds
                        )
                except Exception as e:
                    logger.error(f"Error in ticket escalation loop: {e}", exc_info=True)
        except asyncio.CancelledError:
            pass

