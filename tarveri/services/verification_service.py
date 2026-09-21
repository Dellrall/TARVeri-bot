"""
Verification business logic, concurrency control, and cross-guild role synchronization.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiosqlite
import discord

from tarveri.config import (
    ALUMNI_ALIASES,
    ALUMNI_ROLE_COLOR,
    ALUMNI_ROLE_NAME,
    ALUMNI_ROLE_PATTERN,
    CAMPUS_ALIASES,
    CAMPUS_COLORS,
    CAMPUS_ROLE_NAMES,
    CAMPUS_ROLES,
    FACULTY_ALIASES,
    FACULTY_COLORS,
    FACULTY_ROLE_NAMES,
    FACULTY_ROLES,
    GUEST_ROLE_COLOR,
    GUEST_ROLE_PATTERN,
    ROLE_QUALIFIER_PATTERN,
    SRC_ROLES,
    STUDY_LEVEL_ALIASES,
    STUDY_LEVEL_COLORS,
    STUDY_LEVEL_ROLE_NAMES,
    STUDY_LEVEL_ROLES,
    StudentIdInfo,
    encrypt_email,
    estimate_student_card_expiry,
    format_card_expiry_display,
    get_configured_tz,
    hash_email,
    hash_student_id,
    is_valid_student_email,
    mask_email,
    mask_student_id,
    parse_card_expiry_date,
    parse_student_id,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.role_manager import RoleManager

logger = logging.getLogger("tarveri")


@dataclass(slots=True)
class RoleSyncResult:
    verified_in: list[tuple[int, str, str]] = field(default_factory=list)  # (guild_id, guild_name, role_name)
    already_had_role_in: list[tuple[int, str, str]] = field(default_factory=list)  # (guild_id, guild_name, role_name)
    missing_role_in: list[str] = field(default_factory=list)
    failed_in: list[str] = field(default_factory=list)
    requires_email_in: list[str] = field(default_factory=list)


class VerificationService:
    def __init__(
        self,
        bot: discord.Client,
        db: Database,
        secret: str,
        rate_limiter: RateLimiter,
        settings: Any = None,
        email_service: Any = None,
    ):
        self.bot = bot
        self.db = db
        self.secret = secret
        self.rate_limiter = rate_limiter
        self.settings = settings
        self.email_service = email_service
        self.role_manager = RoleManager(db)
        self._in_flight_users: set[int] = set()
        self._lock = asyncio.Lock()
        self._role_locks: dict[int, asyncio.Lock] = {}

    def _get_guild_role_lock(self, guild_id: int) -> asyncio.Lock:
        return self.role_manager.get_guild_role_lock(guild_id)

    async def get_or_fetch_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None:
        """Retrieves a member from cache (O(1)), or fetches from Discord API on cache miss."""
        member = guild.get_member(user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(user_id)
        except (discord.NotFound, discord.HTTPException):
            return None

    async def get_mutual_guilds_for_user(self, user_id: int) -> list[discord.Guild]:
        """
        Finds all mutual guilds where the user is a member.
        Optimized with fast cache-check first and concurrent API fetch on cache misses.
        """
        mutual: list[discord.Guild] = []
        missing_in_cache: list[discord.Guild] = []

        for guild in self.bot.guilds:
            member = guild.get_member(user_id)
            if member is not None:
                mutual.append(guild)
            else:
                missing_in_cache.append(guild)

        if missing_in_cache:
            async def _check_guild(g: discord.Guild) -> discord.Guild | None:
                try:
                    m = await g.fetch_member(user_id)
                    return g if m is not None else None
                except (discord.NotFound, discord.HTTPException):
                    return None

            results = await asyncio.gather(*[_check_guild(g) for g in missing_in_cache], return_exceptions=True)
            for res in results:
                if isinstance(res, discord.Guild):
                    mutual.append(res)

        return mutual

    @classmethod
    def _match_faculty_role_in_list(cls, roles: Sequence[discord.Role], target_name: str) -> discord.Role | None:
        """
        Multi-tier dynamic matcher to find an existing faculty role in a list/sequence of roles:
        Tier 1: Exact match (name == target_name)
        Tier 2: Case-insensitive & whitespace-trimmed match (name.strip().upper() == target_name.upper())
        Tier 3: Normalized alphanumeric match (e.g. '[FOCS]' or '🎓 FOCS')
        Tier 4: Prefix / word-boundary / bracket match (e.g. 'FOCS - Computing' or 'Faculty of Computing (FOCS)')
        Tier 5: Full faculty name & aliases match from FACULTY_ALIASES (e.g. 'Faculty of Computing and Information Technology')
        Tier 6: Normalized alphanumeric alias match (stripping punctuation/brackets from aliases)

        CRITICAL GUARD: Excludes roles containing committee/council/staff qualifiers (e.g. 'FOCS SRC', 'FOCS Council', 'FOCS Exco').
        Prioritizes the role with the highest position if multiple matches exist.
        """
        if not roles:
            return None

        target_upper = target_name.strip().upper()
        target_alnum = re.sub(r"[^A-Za-z0-9]", "", target_upper)
        target_has_qualifier = bool(ROLE_QUALIFIER_PATTERN.search(target_name))
        aliases = FACULTY_ALIASES.get(target_name, [target_name])

        def _is_safe_role(r_name: Any) -> bool:
            r_str = str(r_name or "")
            if not target_has_qualifier and ROLE_QUALIFIER_PATTERN.search(r_str):
                return False
            # Cross-domain guard: Never match study level, campus, or alumni roles as faculty roles
            r_upper = r_str.strip().upper()
            if target_name in FACULTY_ROLE_NAMES:
                if any(r_upper == lvl.upper() for lvl in STUDY_LEVEL_ROLE_NAMES):
                    return False
                if any(r_upper == c.upper() for c in CAMPUS_ROLE_NAMES):
                    return False
                if r_upper == ALUMNI_ROLE_NAME.upper():
                    return False
            return True

        # Tier 1: Exact match
        exact_matches = [r for r in roles if getattr(r, "name", None) == target_name]
        if exact_matches:
            return max(exact_matches, key=lambda r: getattr(r, "position", 0))

        # Filter candidates for Tiers 2-6 to avoid matching SRC / Council / Exco roles
        safe_roles = [r for r in roles if _is_safe_role(getattr(r, "name", ""))]

        # Tier 2: Case-insensitive & trimmed match
        ci_matches = [
            r for r in safe_roles if getattr(r, "name", "").strip().upper() == target_upper
        ]
        if ci_matches:
            return max(ci_matches, key=lambda r: getattr(r, "position", 0))

        # Tier 3: Normalized alphanumeric match
        alnum_matches = [
            r
            for r in safe_roles
            if re.sub(r"[^A-Za-z0-9]", "", getattr(r, "name", "")).upper() == target_alnum
        ]
        if alnum_matches:
            return max(alnum_matches, key=lambda r: getattr(r, "position", 0))

        # Tier 4: Prefix or word boundary or bracket match of acronym
        fuzzy_matches = []
        for r in safe_roles:
            r_name = getattr(r, "name", "").strip().upper()
            if not r_name:
                continue
            if (
                r_name.startswith(f"{target_upper} ")
                or r_name.startswith(f"{target_upper}-")
                or r_name.startswith(f"{target_upper}:")
                or f"({target_upper})" in r_name
                or f"[{target_upper}]" in r_name
                or bool(re.search(rf"\b{re.escape(target_upper)}\b", r_name))
            ):
                fuzzy_matches.append(r)

        if fuzzy_matches:
            return max(fuzzy_matches, key=lambda r: getattr(r, "position", 0))

        # Tier 5 & 6: Full faculty name & dynamic aliases match
        alias_matches = []
        for r in safe_roles:
            r_name = getattr(r, "name", "").strip()
            if not r_name:
                continue
            r_lower = r_name.lower()
            r_clean = re.sub(r"[^A-Za-z0-9]", "", r_lower)
            for alias in aliases:
                a_lower = alias.lower()
                a_clean = re.sub(r"[^A-Za-z0-9]", "", a_lower)
                # Exact alias match or stripped alphanumeric match
                if r_lower == a_lower or (a_clean and r_clean == a_clean):
                    alias_matches.append(r)
                    break
                # Word boundary match for full alias (if length > 3 to avoid acronym collisions)
                if len(alias) > 3 and re.search(rf"\b{re.escape(alias)}\b", r_name, re.IGNORECASE):
                    alias_matches.append(r)
                    break

        if alias_matches:
            return max(alias_matches, key=lambda r: getattr(r, "position", 0))

        return None

    async def restore_src_roles(self, guild: discord.Guild) -> dict[str, int]:
        """
        Restores / creates the faculty SRC (Student Representative Council) roles if missing.
        FAFB SRC, CPUS SRC, FOCS SRC, FCCI SRC, FOAS SRC, FOBE SRC, FSSH SRC, FOET SRC.
        Guarantees idempotency via per-guild role lock.
        """
        stats = {"created": 0, "existing": 0, "failed": 0}
        if not guild or not hasattr(guild, "roles"):
            return stats

        can_manage = (
            getattr(guild.me.guild_permissions, "manage_roles", False)
            if hasattr(guild, "me") and hasattr(guild.me, "guild_permissions")
            else False
        )
        if not can_manage:
            stats["failed"] = len(SRC_ROLES)
            return stats

        lock = self._get_guild_role_lock(guild.id)
        async with lock:
            guild_roles = list(getattr(guild, "roles", []))
            if hasattr(guild, "fetch_roles") and callable(guild.fetch_roles):
                try:
                    live_roles = await guild.fetch_roles()
                    if isinstance(live_roles, (list, tuple)):
                        guild_roles = list(live_roles)
                except (discord.HTTPException, discord.Forbidden):
                    pass

            for fac_code, src_name in SRC_ROLES.items():
                # Check if role already exists (exact or case-insensitive)
                exists = any(getattr(r, "name", "").strip().lower() == src_name.lower() for r in guild_roles)
                if exists:
                    stats["existing"] += 1
                    continue

                # Role missing, create it
                color_val = FACULTY_COLORS.get(fac_code, 0x3498DB)
                try:
                    role = await guild.create_role(
                        name=src_name,
                        colour=discord.Colour(color_val),
                        mentionable=True,
                        reason="TARVeri: restore missing faculty SRC role",
                    )
                    guild_roles.append(role)
                    try:
                        await self.db.record_bot_created_role(guild.id, role.id, src_name)
                    except Exception as e:
                        logger.warning("Could not record bot created SRC role: %s", e, exc_info=True)
                    stats["created"] += 1
                    await self.db.log(
                        "INFO",
                        "SRC_ROLE_RESTORED",
                        f"Restored SRC role '{src_name}' in '{guild.name}' (Guild ID: {guild.id})",
                        guild=guild,
                    )
                except discord.HTTPException as e:
                    stats["failed"] += 1
                    logger.warning(f"Failed to restore SRC role '{src_name}' in '{guild.name}': {e}")

        if stats["created"] > 0:
            logger.info(f"[{guild.name}] Restored {stats['created']} missing SRC role(s).")

        return stats

    async def find_faculty_role(self, guild: discord.Guild, role_name: str) -> discord.Role | None:
        """
        Finds an existing faculty role in a guild by searching in-memory cache first,
        and querying Discord REST API (fetch_roles) as a fallback to guarantee no duplicates.
        """
        return await self.role_manager.find_role_in_guild(
            guild, lambda roles: self._match_faculty_role_in_list(roles, role_name)
        )

    async def get_or_create_faculty_role(self, guild: discord.Guild, role_name: str) -> discord.Role | None:
        """
        Finds an existing faculty role. ONLY creates a new role if the role absolutely does not exist.
        Guarantees idempotency via double-checked locking across concurrent tasks.
        """
        color_val = FACULTY_COLORS.get(role_name, 0x3498DB)
        return await self.role_manager.get_or_create_role(
            guild,
            role_name=role_name,
            matcher=lambda roles: self._match_faculty_role_in_list(roles, role_name),
            colour=color_val,
            mentionable=True,
            reason="TARVeri: auto-created missing faculty role for verification",
        )

    @classmethod
    def _match_alumni_role_in_list(cls, roles: Sequence[discord.Role]) -> discord.Role | None:
        """
        Dynamic multi-tier matcher to find an existing alumni role in a list of roles.
        Checks:
        1. Exact match with ALUMNI_ROLE_NAME ('TARUMT Alumni')
        2. Exact alias match (e.g. 'Alumni', 'TARUC Alumni', 'Graduated', 'TAR UMT Alumni')
        3. Alphanumeric normalized match (e.g. '[TARUMT] Alumni' or 'TARUMT-Alumni')
        4. Regex pattern search with ALUMNI_ROLE_PATTERN (e.g. 'Alumni🎓', 'Graduated Students', '2024 Alumni')
        """
        if not roles:
            return None

        # Tier 1: Exact target name match
        for r in roles:
            if getattr(r, "name", None) == ALUMNI_ROLE_NAME:
                return r

        # Tier 2: Exact alias match (case-insensitive)
        alias_set = {a.strip().upper() for a in ALUMNI_ALIASES}
        for r in roles:
            r_name = getattr(r, "name", "").strip().upper()
            if r_name in alias_set:
                return r

        # Tier 3: Alphanumeric normalized match
        target_alnum = re.sub(r"[^A-Za-z0-9]", "", ALUMNI_ROLE_NAME.upper())
        alias_alnums = {re.sub(r"[^A-Za-z0-9]", "", a.upper()) for a in ALUMNI_ALIASES}
        for r in roles:
            r_alnum = re.sub(r"[^A-Za-z0-9]", "", getattr(r, "name", "").upper())
            if r_alnum and (r_alnum == target_alnum or r_alnum in alias_alnums):
                return r

        # Tier 4: Regex pattern search (any role containing alumni/graduate/alumnus/alumna)
        for r in roles:
            r_name = getattr(r, "name", "")
            if ALUMNI_ROLE_PATTERN.search(r_name):
                return r

        return None

    async def find_alumni_role(self, guild: discord.Guild) -> discord.Role | None:
        """Finds existing alumni role in guild cache or live API using multi-tier matching."""
        if not guild:
            return None
        return await self.role_manager.find_role_in_guild(guild, self._match_alumni_role_in_list)

    async def get_or_create_alumni_role(self, guild: discord.Guild) -> discord.Role | None:
        """Finds or atomically creates the TARUMT Alumni role."""
        return await self.role_manager.get_or_create_role(
            guild,
            role_name=ALUMNI_ROLE_NAME,
            matcher=self._match_alumni_role_in_list,
            colour=ALUMNI_ROLE_COLOR,
            mentionable=True,
            reason="TARVeri: auto-created missing TARUMT Alumni role",
        )

    async def sync_alumni_role_across_guilds(
        self,
        user_id: int,
        guilds: Sequence[discord.Guild],
        reason: str = "TARVeri: Sync alumni role",
    ) -> list[str]:
        """Ensures the alumni role is assigned to the user across the specified guilds."""
        assigned_guild_names: list[str] = []
        for guild in guilds:
            member = await self.get_or_fetch_member(guild, user_id)
            if not member:
                continue

            alumni_role = await self.get_or_create_alumni_role(guild)
            if not alumni_role:
                continue

            if alumni_role not in getattr(member, "roles", []):
                me = getattr(guild, "me", None)
                can_manage = (
                    getattr(me.guild_permissions, "manage_roles", False)
                    if me and hasattr(me, "guild_permissions")
                    else False
                )
                bot_top = getattr(me, "top_role", None)
                bot_pos = getattr(bot_top, "position", 0) if bot_top else 0
                role_pos = getattr(alumni_role, "position", 0)
                if can_manage and role_pos < bot_pos:
                    try:
                        await member.add_roles(alumni_role, reason=reason)
                        assigned_guild_names.append(guild.name)
                    except discord.HTTPException as e:
                        logger.warning(f"Could not assign alumni role to {member} in {guild.name}: {e}")
        return assigned_guild_names

    async def find_guest_role(
        self, guild: discord.Guild, configured_name: str | None = None
    ) -> discord.Role | None:
        """Finds the guest role in guild matching configured name or regex pattern."""
        def _match_guest_in_list(roles: Sequence[discord.Role]) -> discord.Role | None:
            if configured_name and configured_name.strip():
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

        return await self.role_manager.find_role_in_guild(guild, _match_guest_in_list)

    async def get_or_create_guest_role(self, guild: discord.Guild) -> discord.Role | None:
        """Retrieves or atomically creates the Guest(Approved) role for the guild."""
        settings = await self.db.get_guild_settings(guild.id)
        configured_name = settings[2].strip() if settings and settings[2] else None

        def _match_guest_in_list(roles: Sequence[discord.Role]) -> discord.Role | None:
            if configured_name and configured_name.strip():
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
            reason="TARVeri: Auto-created Guest(Approved) role",
        )

    @classmethod
    def _match_campus_role_in_list(cls, roles: Sequence[discord.Role], target_name: str) -> discord.Role | None:
        """Dynamic matcher to find an existing branch campus role in a list of roles."""
        if not roles or not target_name:
            return None

        target_upper = target_name.strip().upper()
        target_alnum = re.sub(r"[^A-Za-z0-9]", "", target_upper)
        aliases = CAMPUS_ALIASES.get(target_name, [target_name])

        # Tier 1: Exact match
        for r in roles:
            if getattr(r, "name", None) == target_name:
                return r

        def _is_safe_campus_role(r_name: Any) -> bool:
            r_str = str(r_name or "")
            if ROLE_QUALIFIER_PATTERN.search(r_str):
                return False
            r_upper = r_str.strip().upper()
            if target_name in CAMPUS_ROLE_NAMES:
                if any(r_upper == fac.upper() for fac in FACULTY_ROLE_NAMES):
                    return False
                if any(r_upper == lvl.upper() for lvl in STUDY_LEVEL_ROLE_NAMES):
                    return False
                if r_upper == ALUMNI_ROLE_NAME.upper():
                    return False
            return True

        safe_roles = [r for r in roles if _is_safe_campus_role(getattr(r, "name", ""))]

        for r in safe_roles:
            if getattr(r, "name", "").strip().upper() == target_upper:
                return r

        for alias in aliases:
            alias_upper = alias.strip().upper()
            for r in safe_roles:
                if getattr(r, "name", "").strip().upper() == alias_upper:
                    return r

        for r in safe_roles:
            r_alnum = re.sub(r"[^A-Za-z0-9]", "", getattr(r, "name", "").upper())
            if r_alnum == target_alnum and r_alnum:
                return r

        return None

    async def find_campus_role(self, guild: discord.Guild, campus_name: str) -> discord.Role | None:
        """Finds existing campus role in guild cache or live API."""
        if not guild or not campus_name:
            return None
        return await self.role_manager.find_role_in_guild(
            guild, lambda roles: self._match_campus_role_in_list(roles, campus_name)
        )

    async def get_or_create_campus_role(self, guild: discord.Guild, campus_name: str) -> discord.Role | None:
        """Finds or atomically creates a branch campus role."""
        if not guild or not campus_name:
            return None
        color_val = CAMPUS_COLORS.get(campus_name, 0x3498DB)
        return await self.role_manager.get_or_create_role(
            guild,
            role_name=campus_name,
            matcher=lambda roles: self._match_campus_role_in_list(roles, campus_name),
            colour=color_val,
            mentionable=True,
            reason="TARVeri: auto-created campus branch role for student verification",
        )

    @classmethod
    def _match_study_level_role_in_list(cls, roles: Sequence[discord.Role], target_name: str) -> discord.Role | None:
        """Dynamic matcher to find an existing study level role in a list of roles."""
        if not roles or not target_name:
            return None

        target_upper = target_name.strip().upper()
        target_alnum = re.sub(r"[^A-Za-z0-9]", "", target_upper)
        aliases = STUDY_LEVEL_ALIASES.get(target_name, [target_name])

        # Tier 1: Exact match
        for r in roles:
            if getattr(r, "name", None) == target_name:
                return r

        def _is_safe_level_role(r_name: Any) -> bool:
            r_str = str(r_name or "")
            if ROLE_QUALIFIER_PATTERN.search(r_str):
                return False
            r_upper = r_str.strip().upper()
            if target_name in STUDY_LEVEL_ROLE_NAMES:
                if any(r_upper == fac.upper() for fac in FACULTY_ROLE_NAMES):
                    return False
                if any(r_upper == c.upper() for c in CAMPUS_ROLE_NAMES):
                    return False
                if r_upper == ALUMNI_ROLE_NAME.upper():
                    return False
            return True

        safe_roles = [r for r in roles if _is_safe_level_role(getattr(r, "name", ""))]

        for r in safe_roles:
            if getattr(r, "name", "").strip().upper() == target_upper:
                return r

        for alias in aliases:
            alias_upper = alias.strip().upper()
            for r in safe_roles:
                if getattr(r, "name", "").strip().upper() == alias_upper:
                    return r

        for r in safe_roles:
            r_alnum = re.sub(r"[^A-Za-z0-9]", "", getattr(r, "name", "").upper())
            if r_alnum == target_alnum and r_alnum:
                return r

        return None

    async def find_study_level_role(self, guild: discord.Guild, level_name: str) -> discord.Role | None:
        """Finds existing study level role in guild cache or live API."""
        if not guild or not level_name:
            return None
        return await self.role_manager.find_role_in_guild(
            guild, lambda roles: self._match_study_level_role_in_list(roles, level_name)
        )

    async def get_or_create_study_level_role(self, guild: discord.Guild, level_name: str) -> discord.Role | None:
        """Finds or atomically creates a study level role."""
        if not guild or not level_name:
            return None
        color_val = STUDY_LEVEL_COLORS.get(level_name, 0x2980B9)
        return await self.role_manager.get_or_create_role(
            guild,
            role_name=level_name,
            matcher=lambda roles: self._match_study_level_role_in_list(roles, level_name),
            colour=color_val,
            mentionable=True,
            reason="TARVeri: auto-created study level role for student verification",
        )

    async def _assign_role_in_guild(
        self,
        guild: discord.Guild,
        user_id: int,
        role_name: str,
        result: RoleSyncResult,
        campus_role_name: str | None = None,
        level_role_name: str | None = None,
        is_email_verified: bool = False,
        student_id_hash: str | None = None,
        email_hash: str | None = None,
    ) -> None:
        """Process role assignment in a single guild (faculty role + campus role + study level role)."""
        member = await self.get_or_fetch_member(guild, user_id)
        if member is None:
            return

        # Check if user, student_id_hash, or email_hash is blacklisted in this guild
        if self.db and hasattr(guild, "id"):
            try:
                is_bl, _ = await self.db.is_blacklisted(
                    guild.id,
                    user_id=user_id,
                    student_id_hash=student_id_hash,
                    email_hash=email_hash,
                )
                if is_bl:
                    return
            except Exception as exc:
                logger.debug("Failed checking blacklist for %s in %s: %s", user_id, guild.id, exc)

        # Check if the guild mandates institutional email verification (Opt-In)
        if self.db and hasattr(guild, "id"):
            try:
                is_guild_email_required = await self.db.is_guild_email_verification_enabled(guild.id)
                if is_guild_email_required and not is_email_verified:
                    result.requires_email_in.append(guild.name)
                    return
            except Exception as exc:
                logger.debug("Failed checking guild email policy for %s: %s", guild.id, exc)

        member_roles = getattr(member, "roles", [])
        if not isinstance(member_roles, (list, tuple)):
            member_roles = []

        # Check if member already holds the target faculty role
        has_target_faculty = self._match_faculty_role_in_list(member_roles, role_name) is not None

        # Check for any conflicting faculty roles (e.g. manually selected a different faculty role prior)
        conflicting_faculty_roles = [
            r for r in member_roles
            if any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES if fac != role_name)
        ]

        me = getattr(guild, "me", None)
        can_manage = (
            getattr(me.guild_permissions, "manage_roles", False)
            if me and hasattr(me, "guild_permissions")
            else False
        )
        bot_top_role = getattr(me, "top_role", None) if me else None
        bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0

        roles_to_add: list[discord.Role] = []
        roles_to_remove: list[discord.Role] = []
        assigned_names: list[str] = []

        # 1. Primary faculty role
        if not has_target_faculty:
            fac_role = await self.get_or_create_faculty_role(guild, role_name)
            if not fac_role:
                result.missing_role_in.append(guild.name)
                return
            fac_pos = getattr(fac_role, "position", 0)
            if not can_manage or (isinstance(bot_pos, int) and isinstance(fac_pos, int) and fac_pos >= bot_pos):
                result.failed_in.append(guild.name)
                return
            roles_to_add.append(fac_role)
            fac_name = getattr(fac_role, "name", None)
            assigned_names.append(fac_name if isinstance(fac_name, str) and fac_name else role_name)
        else:
            assigned_names.append(role_name)

        # Queue removal of conflicting faculty roles if bot has permission
        for conf_r in conflicting_faculty_roles:
            conf_pos = getattr(conf_r, "position", 0)
            if can_manage and not (isinstance(bot_pos, int) and isinstance(conf_pos, int) and conf_pos >= bot_pos):
                roles_to_remove.append(conf_r)

        # 2. Branch Campus role (if provided)
        if campus_role_name:
            has_campus = any(self._match_campus_role_in_list([r], campus_role_name) is not None for r in member_roles)
            conflicting_campus_roles = [
                r for r in member_roles
                if any(self._match_campus_role_in_list([r], c) is not None for c in CAMPUS_ROLE_NAMES if c != campus_role_name)
            ]
            for conf_c in conflicting_campus_roles:
                conf_c_pos = getattr(conf_c, "position", 0)
                if can_manage and not (isinstance(bot_pos, int) and isinstance(conf_c_pos, int) and conf_c_pos >= bot_pos):
                    roles_to_remove.append(conf_c)

            if not has_campus:
                camp_role = await self.get_or_create_campus_role(guild, campus_role_name)
                if camp_role:
                    camp_pos = getattr(camp_role, "position", 0)
                    if can_manage and not (isinstance(bot_pos, int) and isinstance(camp_pos, int) and camp_pos >= bot_pos):
                        roles_to_add.append(camp_role)
                        camp_name = getattr(camp_role, "name", None)
                        assigned_names.append(camp_name if isinstance(camp_name, str) and camp_name else campus_role_name)
            else:
                assigned_names.append(campus_role_name)

        # 3. Study Level role (if provided)
        if level_role_name:
            has_level = any(self._match_study_level_role_in_list([r], level_role_name) is not None for r in member_roles)
            conflicting_level_roles = [
                r for r in member_roles
                if any(self._match_study_level_role_in_list([r], lvl_name) is not None for lvl_name in STUDY_LEVEL_ROLE_NAMES if lvl_name != level_role_name)
            ]
            for conf_l in conflicting_level_roles:
                conf_l_pos = getattr(conf_l, "position", 0)
                if can_manage and not (isinstance(bot_pos, int) and isinstance(conf_l_pos, int) and conf_l_pos >= bot_pos):
                    roles_to_remove.append(conf_l)

            if not has_level:
                lvl_role = await self.get_or_create_study_level_role(guild, level_role_name)
                if lvl_role:
                    lvl_pos = getattr(lvl_role, "position", 0)
                    if can_manage and not (isinstance(bot_pos, int) and isinstance(lvl_pos, int) and lvl_pos >= bot_pos):
                        roles_to_add.append(lvl_role)
                        lvl_name = getattr(lvl_role, "name", None)
                        assigned_names.append(lvl_name if isinstance(lvl_name, str) and lvl_name else level_role_name)
            else:
                assigned_names.append(level_role_name)

        # Perform role modifications
        roles_modified = False
        if roles_to_remove:
            try:
                await member.remove_roles(*roles_to_remove, reason="TARVeri: Reconcile faculty/campus/level role mismatch")
                roles_modified = True
            except discord.HTTPException as e:
                logger.warning(f"Failed to remove conflicting roles for user {user_id} in '{guild.name}': {e}")

        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="TARVeri: Student verification role assignment")
                roles_modified = True
            except discord.HTTPException as e:
                result.failed_in.append(guild.name)
                await self.db.log(
                    "ERROR",
                    "ROLE_ASSIGN_FAILED",
                    f"Failed to assign roles to user {user_id} in '{guild.name}': {e}",
                    guild=guild,
                    user_id=user_id,
                )
                return

        summary_label = ", ".join(dict.fromkeys(assigned_names))
        if roles_modified:
            result.verified_in.append((guild.id, guild.name, summary_label))
        elif has_target_faculty:
            result.already_had_role_in.append((guild.id, guild.name, summary_label))

    async def assign_role_across_guilds(
        self,
        user_id: int,
        role_name: str,
        guilds: Sequence[discord.Guild],
        campus_role_name: str | None = None,
        level_role_name: str | None = None,
        is_email_verified: bool = False,
        student_id_hash: str | None = None,
        email_hash: str | None = None,
    ) -> RoleSyncResult:
        """
        Ensures the given user holds `role_name` (and optional campus & level roles) in all specified guilds concurrently.
        Respects per-guild institutional email verification and blacklist policies.
        """
        result = RoleSyncResult()
        if not guilds:
            return result

        tasks = [
            self._assign_role_in_guild(
                g,
                user_id,
                role_name,
                result,
                campus_role_name=campus_role_name,
                level_role_name=level_role_name,
                is_email_verified=is_email_verified,
                student_id_hash=student_id_hash,
                email_hash=email_hash,
            )
            for g in guilds
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        return result

    def format_role_summary(self, result: RoleSyncResult) -> str:
        """Formats a human-readable summary of role assignments."""
        lines: list[str] = []
        if result.verified_in:
            lines.append("✅ You've been given the following role(s):")
            lines.extend(
                f"   • **{item[1] if len(item) == 3 else item[0]}** → {item[2] if len(item) == 3 else item[1]}"
                for item in result.verified_in
            )
        if result.already_had_role_in:
            lines.append("ℹ️ You already had a faculty role in:")
            lines.extend(
                f"   • **{item[1] if len(item) == 3 else item[0]}** → {item[2] if len(item) == 3 else item[1]} (unchanged)"
                for item in result.already_had_role_in
            )
            if not result.verified_in:
                lines.append("✅ Your student status is now officially verified in our database.")
        if result.requires_email_in:
            lines.append("📧 The following server(s) mandate institutional email OTP verification:")
            lines.extend(
                f"   • **{g}** (Verify your institutional email in that server to unlock roles)"
                for g in result.requires_email_in
            )
        if result.missing_role_in:
            lines.append("⚠️ I couldn't create/find the required role (contact an admin) in:")
            lines.extend(f"   • **{g}** (I likely need 'Manage Roles' permission there)" for g in result.missing_role_in)
        if result.failed_in:
            lines.append("⚠️ I don't have permission to assign roles in:")
            lines.extend(f"   • **{g}** (my role needs to be moved above the faculty roles)" for g in result.failed_in)
        return "\n".join(lines)

    async def transition_student_level(
        self,
        user: discord.User | discord.Member,
        info: StudentIdInfo,
        new_id_hash: str,
        raw_expiry_date: str | None = None,
    ) -> str:
        """
        Executes an academic level progression for an already-verified student
        (e.g., Foundation/CPUS -> Degree, Diploma -> Degree, Degree -> Postgraduate).
        Archives previous academic profile into verification_transitions, updates verifications,
        and atomically swaps roles across mutual guilds.
        """
        guild_ctx = getattr(user, "guild", None)
        current_details = await self.db.get_verification_details(user.id)
        if not current_details:
            return "❌ Could not retrieve your current verification record to perform transition."

        from_hash = current_details.get("student_id_hash", "")
        from_faculty = current_details.get("faculty_code", "M")
        from_campus = current_details.get("campus_code") or "W"
        from_level = current_details.get("level_code") or "R"

        to_student_id = info.student_id
        to_faculty = info.faculty_code or "M"
        to_campus = info.campus_code or "W"
        to_level = info.level_code or "R"
        to_faculty_role = info.faculty_role or FACULTY_ROLES.get(to_faculty, "FOCS")
        to_campus_role = info.campus_role or CAMPUS_ROLES.get(to_campus, "KL Main Campus")
        to_level_role = info.level_role or STUDY_LEVEL_ROLES.get(to_level, "Degree")
        if raw_expiry_date and raw_expiry_date.strip():
            to_expiry_date = parse_card_expiry_date(raw_expiry_date)
            if not to_expiry_date:
                return (
                    "❌ Invalid new student card expiry date format. Please use `MM/YY` (e.g. `10/28`) "
                    "or leave it blank to auto-calculate."
                )
        else:
            to_expiry_date = estimate_student_card_expiry(to_student_id, to_level)

        from_faculty_name = FACULTY_ROLES.get(from_faculty, from_faculty)
        from_level_name = STUDY_LEVEL_ROLES.get(from_level, from_level)
        CAMPUS_ROLES.get(from_campus, from_campus)

        # 1. Archive transition in verification_transitions
        await self.db.record_academic_transition(
            discord_user_id=user.id,
            from_id_hash=from_hash,
            from_faculty_code=from_faculty,
            from_campus_code=from_campus,
            from_level_code=from_level,
            to_id_hash=new_id_hash,
            to_faculty_code=to_faculty,
            to_campus_code=to_campus,
            to_level_code=to_level,
            notes=f"Academic level progression: {from_level_name} ({from_faculty_name}) -> {to_level_role} ({to_faculty_role})",
        )

        # 2. Update primary verification profile
        await self.db.update_verification_profile(
            discord_user_id=user.id,
            student_id_hash=new_id_hash,
            faculty_code=to_faculty,
            campus_code=to_campus,
            level_code=to_level,
            card_expiry_date=to_expiry_date,
        )

        # 3. Strip old alumni role if member held it
        mutual_guilds = await self.get_mutual_guilds_for_user(user.id)
        for g in mutual_guilds:
            member = await self.get_or_fetch_member(g, user.id)
            if member:
                for r in getattr(member, "roles", []):
                    if self._match_alumni_role_in_list([r]) is not None:
                        try:
                            await member.remove_roles(r, reason="TARVeri: Transitioned back to active student status")
                        except discord.HTTPException:
                            pass

        # 4. Sync new roles across all mutual guilds (auto removes conflicting faculty/campus/level roles)
        sync_result = await self.assign_role_across_guilds(
            user.id,
            to_faculty_role,
            mutual_guilds,
            campus_role_name=to_campus_role,
            level_role_name=to_level_role,
        )

        await self.db.log(
            "INFO",
            "ACADEMIC_TRANSITION",
            f"{user} (ID: {user.id}) transitioned from [{from_faculty_name} • {from_level_name}] to [{to_faculty_role} • {to_level_role}] (new masked ID: {mask_student_id(to_student_id)})",
            user_id=user.id,
            guild=guild_ctx,
        )

        lines = [
            "🎉 **Academic Level Progression Successful!**",
            f"🎓 Your status has been transitioned to **{to_faculty_role}** • **{to_level_role}** ({to_campus_role}).",
        ]
        if sync_result.verified_in:
            lines.append(f"🏷️ Updated roles in: {', '.join([f'**{e[1] if len(e) == 3 else e[0]}**' for e in sync_result.verified_in])}")
        lines.append("🪪 Your Digital Campus Card (`/card`) has been updated to reflect your new study level.")
        return "\n".join(lines)

    async def process_student_dropout(
        self,
        user: discord.User | discord.Member,
        reason: str = "Self-reported dropout",
    ) -> dict[str, Any]:
        """
        Processes a student self-initiated dropout / withdrawal:
        1. Checks existing verification record in database.
        2. Records dropout event in audit log and deletes verification record.
        3. Strips all faculty, campus, study level, and alumni roles across mutual guilds.
        """
        user_id = user.id
        verif = await self.db.get_verification_by_user(user_id)
        if not verif:
            return {
                "success": False,
                "error_message": "You do not have an active student verification record in the database.",
            }

        details = await self.db.get_verification_details(user_id)
        details.get("student_id_hash") if details else verif[0]
        stored_faculty = details.get("faculty_code") if details else verif[1]
        stored_campus = (details.get("campus_code") if details else None) or "W"
        stored_level = (details.get("level_code") if details else None) or "R"

        faculty_name = FACULTY_ROLES.get(stored_faculty, stored_faculty)
        campus_name = CAMPUS_ROLES.get(stored_campus, "KL Main Campus")
        level_name = STUDY_LEVEL_ROLES.get(stored_level, "Degree")

        # 1. Delete verification from DB
        await self.db.delete_verification(user_id)

        # 2. Reset rate limit if available
        if hasattr(self, "rate_limiter") and self.rate_limiter:
            self.rate_limiter.reset(user_id)

        # 3. Strip faculty, campus, study level, and alumni roles across mutual guilds, and grant Guest(Approved)
        mutual_guilds = await self.get_mutual_guilds_for_user(user_id)
        roles_removed_servers: list[str] = []
        guest_roles_granted_servers: list[str] = []

        for guild in mutual_guilds:
            member = await self.get_or_fetch_member(guild, user_id)
            if not member:
                continue

            roles_to_remove = [
                r
                for r in getattr(member, "roles", [])
                if any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES)
                or any(self._match_campus_role_in_list([r], camp) is not None for camp in CAMPUS_ROLE_NAMES)
                or any(self._match_study_level_role_in_list([r], lvl) is not None for lvl in STUDY_LEVEL_ROLE_NAMES)
                or self._match_alumni_role_in_list([r]) is not None
            ]

            me = getattr(guild, "me", None)
            can_manage = (
                getattr(me.guild_permissions, "manage_roles", False)
                if me and hasattr(me, "guild_permissions")
                else False
            )
            bot_top = getattr(me, "top_role", None)
            bot_pos = getattr(bot_top, "position", 0) if bot_top else 0

            # Remove student-specific roles
            for role in roles_to_remove:
                role_pos = getattr(role, "position", 0)
                if can_manage and isinstance(bot_pos, int) and isinstance(role_pos, int) and role_pos < bot_pos:
                    try:
                        await member.remove_roles(
                            role,
                            reason=f"TARVeri: Student discontinuation/dropout ({reason})",
                        )
                        if guild.name not in roles_removed_servers:
                            roles_removed_servers.append(guild.name)
                    except discord.HTTPException as e:
                        logger.warning(f"Could not remove role {role.name} from {member} in {guild.name}: {e}")

            # Assign Guest(Approved) role so they maintain guest permissions & channel access
            guest_role = await self.get_or_create_guest_role(guild)
            if guest_role and guest_role not in getattr(member, "roles", []):
                role_pos = getattr(guest_role, "position", 0)
                if can_manage and isinstance(bot_pos, int) and isinstance(role_pos, int) and role_pos < bot_pos:
                    try:
                        await member.add_roles(
                            guest_role,
                            reason=f"TARVeri: Granted Guest(Approved) upon study discontinuation ({reason})",
                        )
                        guest_roles_granted_servers.append(guild.name)
                    except discord.HTTPException as e:
                        logger.warning(f"Could not assign guest role to {member} in {guild.name}: {e}")

        # 4. Log audit event
        guild_ctx = getattr(user, "guild", None)
        await self.db.log(
            "INFO",
            "STUDENT_DROPOUT",
            f"Student {user} (ID: {user_id}) confirmed dropout / withdrawal from [{faculty_name} • {campus_name} • {level_name}]. Reason: '{reason}'. Granted Guest role across {len(guest_roles_granted_servers)} servers.",
            user_id=user_id,
            guild=guild_ctx,
        )

        return {
            "success": True,
            "faculty_name": faculty_name,
            "campus_name": campus_name,
            "level_name": level_name,
            "guilds_updated": len(roles_removed_servers),
            "guest_guilds_granted": len(guest_roles_granted_servers),
        }

    async def validate_preflight_for_otp(
        self,
        user_id: int,
        raw_student_id: str,
        raw_email: str | None,
        guild: discord.Guild | None = None,
    ) -> tuple[bool, str | None]:
        """
        Validates rate limiting, Student ID format, duplicate Student ID,
        and duplicate Email BEFORE any OTP transmission to conserve SMTP quota.
        Returns (is_valid, error_message).
        """
        if self.rate_limiter.is_rate_limited(user_id):
            return (
                False,
                "⏳ You've made too many verification attempts. Please wait a few minutes and try again.",
            )

        info = parse_student_id(raw_student_id)
        if not info.is_valid or not info.faculty_code or not info.faculty_role:
            if not info.student_id:
                return False, "❌ Please provide a valid student ID (e.g., `23WMD09867`)."
            if info.faculty_code and info.faculty_code not in FACULTY_ROLES:
                return False, "❌ Student ID does not match any known faculty. Please check and try again."
            return False, "❌ Invalid student ID format. Please use the format like `23WMD09867`."

        student_id = info.student_id
        id_hash = hash_student_id(student_id, self.secret)

        existing_id = await self.db.get_verification_by_id_hash(id_hash)
        if existing_id and existing_id[0] != user_id:
            await self.db.log(
                "WARNING",
                "DUPLICATE_ID_ATTEMPT",
                f"User ID {user_id} tried to verify student ID (masked: {mask_student_id(student_id)}) already bound to account ID {existing_id[0]}",
                user_id=user_id,
                guild=guild,
            )
            return (
                False,
                "❌ This student ID has already been used to verify a different Discord account. If that wasn't you, contact an admin immediately.",
            )

        if raw_email and raw_email.strip():
            clean_email = raw_email.strip().lower()
            allowed_domains = (
                getattr(self.settings, "email_allowed_domains", ("student.tarc.edu.my", "tarc.edu.my"))
                if self.settings
                else ("student.tarc.edu.my", "tarc.edu.my")
            )
            if not is_valid_student_email(clean_email, allowed_domains=allowed_domains):
                allowed_str = ", ".join(f"@{d}" for d in allowed_domains)
                return (
                    False,
                    f"❌ Invalid student email address. Please use your official institutional email ({allowed_str}).",
                )

            email_hash = hash_email(clean_email, self.secret)
            existing_email = await self.db.get_verification_by_email_hash(email_hash)
            if existing_email and existing_email[0] != user_id:
                await self.db.log(
                    "WARNING",
                    "DUPLICATE_EMAIL_ATTEMPT",
                    f"User ID {user_id} tried to use student email (masked: {mask_email(clean_email)}) already bound to account ID {existing_email[0]}",
                    user_id=user_id,
                    guild=guild,
                )
                return (
                    False,
                    "❌ This student email has already been used to verify a different Discord account. If that wasn't you, contact an admin immediately.",
                )

        return True, None

    async def perform_verification(
        self,
        user: discord.User | discord.Member,
        raw_student_id: str,
        raw_expiry_date: str | None = None,
        raw_email: str | None = None,
    ) -> str:
        """
        Core verification pipeline:
        1. Rate limit validation
        2. In-flight race condition check
        3. Student ID format and faculty/campus/level code extraction
        4. Account / duplicate ID / duplicate email verification checks
        5. Academic level progression / transition for existing students
        6. Role assignment across mutual guilds
        7. Atomic database recording with rollback on collision
        """
        guild_ctx = getattr(user, "guild", None)
        if self.rate_limiter.is_rate_limited(user.id):
            await self.db.log(
                "WARNING",
                "RATE_LIMITED",
                f"{user} hit the attempt limit",
                user_id=user.id,
                guild=guild_ctx,
            )
            return (
                "⏳ You've made too many verification attempts. Please wait a few minutes "
                "and try again, or contact an admin if this is a mistake."
            )

        async with self._lock:
            if user.id in self._in_flight_users:
                return "⏳ Your verification is already being processed. Please wait a moment."
            self._in_flight_users.add(user.id)

        self.rate_limiter.record_attempt(user.id)

        try:
            info = parse_student_id(raw_student_id)
            if not info.is_valid or not info.faculty_code or not info.faculty_role:
                if not info.student_id:
                    return "❌ Please provide a valid student ID (e.g., `23WMD09867`)."
                if info.faculty_code and info.faculty_code not in FACULTY_ROLES:
                    return "❌ Student ID does not match any known faculty. Please check and try again."
                return "❌ Invalid student ID format. Please use the format like `23WMD09867`."

            student_id = info.student_id
            faculty_code = info.faculty_code
            role_name = info.faculty_role
            campus_code = info.campus_code
            campus_role_name = info.campus_role
            level_code = info.level_code
            level_role_name = info.level_role
            if raw_expiry_date and raw_expiry_date.strip():
                iso_expiry_date = parse_card_expiry_date(raw_expiry_date)
                if not iso_expiry_date:
                    return (
                        "❌ Invalid student card expiry date format. Please use `MM/YY` (e.g. `10/26`) "
                        "or leave it blank to auto-calculate."
                    )
            else:
                iso_expiry_date = estimate_student_card_expiry(student_id, level_code)

            id_hash = hash_student_id(student_id, self.secret)

            email_hash = None
            email_encrypted = None
            if raw_email and raw_email.strip():
                clean_email = raw_email.strip().lower()
                allowed_domains = (
                    getattr(self.settings, "email_allowed_domains", ("student.tarc.edu.my", "tarc.edu.my"))
                    if self.settings
                    else ("student.tarc.edu.my", "tarc.edu.my")
                )
                if not is_valid_student_email(clean_email, allowed_domains=allowed_domains):
                    return "❌ Invalid student email address. Please use your official institutional email (e.g. `@student.tarc.edu.my`)."
                email_hash = hash_email(clean_email, self.secret)
                existing_for_email = await self.db.get_verification_by_email_hash(email_hash)
                if existing_for_email and existing_for_email[0] != user.id:
                    await self.db.log(
                        "WARNING",
                        "DUPLICATE_EMAIL_ATTEMPT",
                        f"{user} (ID: {user.id}) tried to use student email (masked: {mask_email(clean_email)}) already bound to account ID {existing_for_email[0]}",
                        user_id=user.id,
                        guild=guild_ctx,
                    )
                    return (
                        "❌ This student email has already been used to verify a different Discord "
                        "account. If that wasn't you, contact an admin immediately."
                    )
                enc_key = getattr(self.settings, "email_encryption_key", "") if self.settings else ""
                if enc_key:
                    try:
                        email_encrypted = encrypt_email(clean_email, enc_key)
                    except Exception as e:
                        logger.warning(f"Could not encrypt email for {user}: {e}")

            # Check if user, student ID, or email is blacklisted in current guild context
            if guild_ctx and hasattr(guild_ctx, "id"):
                match = await self.db.get_blacklist_match(
                    guild_ctx.id,
                    user_id=user.id,
                    student_id_hash=id_hash,
                    email_hash=email_hash,
                )
                if match:
                    bl_reason = match.get("reason")
                    target_type = match.get("target_type", "UNKNOWN")
                    display_mask = match.get("display_mask", "N/A")
                    await self.db.log(
                        "WARNING",
                        "BLACKLIST_ATTEMPT_BLOCKED",
                        f"{user} (ID: {user.id}) verification blocked due to guild blacklist in '{guild_ctx.name}'. Type: {target_type}, Mask: {display_mask}, Reason: {bl_reason}",
                        user_id=user.id,
                        guild=guild_ctx,
                    )

                    # Dispatch real-time security alert embed to private #tarveri-log
                    alert_embed = discord.Embed(
                        title="🚫 [Security Alert] Blacklisted Verification Blocked",
                        description=f"A verification attempt was blocked because the target matches an active entry on the **{guild_ctx.name}** blacklist.",
                        color=discord.Color.red(),
                        timestamp=datetime.now(get_configured_tz()),
                    )
                    user_mention = getattr(user, "mention", f"<@{user.id}>")
                    alert_embed.add_field(
                        name="👤 Discord User",
                        value=f"{user_mention} (`{user}` • ID: `{user.id}`)",
                        inline=False,
                    )
                    alert_embed.add_field(name="🛡️ Blacklist Vector", value=f"`{target_type}`", inline=True)
                    alert_embed.add_field(name="🔍 Matched Target", value=f"`{display_mask}`", inline=True)
                    alert_embed.add_field(name="📝 Reason", value=bl_reason or "*No reason specified*", inline=False)
                    alert_embed.add_field(name="⚡ Action Taken", value="Verification blocked immediately. No roles assigned.", inline=False)
                    alert_embed.set_footer(text="TARVeri Security Guard • Guild Blacklist")

                    try:
                        await self.send_admin_security_alert(guild_ctx, alert_embed)
                    except Exception as alert_exc:
                        logger.warning(f"Failed sending blacklist verification security alert: {alert_exc}")

                    reason_suffix = f" Reason: {bl_reason}" if bl_reason else ""
                    return f"⛔ You are blacklisted from verifying in **{guild_ctx.name}**.{reason_suffix}"

            # Check if user is already verified
            existing_for_user = await self.db.get_verification_by_user(user.id)
            if existing_for_user:
                stored_hash, stored_faculty, _ = existing_for_user
                if stored_hash != id_hash:
                    # Academic Level Transition: check if new ID is claimed by another account
                    existing_for_id = await self.db.get_verification_by_id_hash(id_hash)
                    if existing_for_id and existing_for_id[0] != user.id:
                        await self.db.log(
                            "WARNING",
                            "DUPLICATE_ID_ATTEMPT",
                            f"{user} (ID: {user.id}) tried to transition to student ID (masked: {mask_student_id(student_id)}) already bound to account ID {existing_for_id[0]}",
                            user_id=user.id,
                            guild=guild_ctx,
                        )
                        return (
                            "❌ This student ID has already been used to verify a different Discord "
                            "account. If that wasn't you, contact an admin immediately."
                        )

                    return await self.transition_student_level(
                        user=user,
                        info=info,
                        new_id_hash=id_hash,
                        raw_expiry_date=raw_expiry_date,
                    )

                existing_details = await self.db.get_verification_details(user.id)
                final_email_hash = email_hash or (existing_details.get("student_email_hash") if existing_details else None)
                is_user_email_verified = bool(final_email_hash)

                mutual_guilds = await self.get_mutual_guilds_for_user(user.id)
                assigned_faculty_role = FACULTY_ROLES.get(stored_faculty, role_name)
                sync_result = await self.assign_role_across_guilds(
                    user.id,
                    assigned_faculty_role,
                    mutual_guilds,
                    campus_role_name=campus_role_name,
                    level_role_name=level_role_name,
                    is_email_verified=is_user_email_verified,
                    student_id_hash=id_hash,
                    email_hash=final_email_hash,
                )
                try:
                    await self.db.update_verification_details(
                        user.id,
                        campus_code=campus_code,
                        level_code=level_code,
                        card_expiry_date=iso_expiry_date,
                        student_email_encrypted=email_encrypted,
                        student_email_hash=email_hash,
                    )
                except Exception as exc:
                    logger.warning("Failed updating verification details during refresh: %s", exc, exc_info=True)
                summary = self.format_role_summary(sync_result)
                return summary or "ℹ️ You're already verified and up to date in every server I share with you."

            # Check if student ID is already bound to another Discord account
            existing_for_id = await self.db.get_verification_by_id_hash(id_hash)
            if existing_for_id:
                await self.db.log(
                    "WARNING",
                    "DUPLICATE_ID_ATTEMPT",
                    f"{user} (ID: {user.id}) tried to reuse a student ID "
                    f"(masked: {mask_student_id(student_id)}) already bound to account ID {existing_for_id[0]}",
                    user_id=user.id,
                    guild=guild_ctx,
                )
                return (
                    "❌ This student ID has already been used to verify a different Discord "
                    "account. If that wasn't you, contact an admin immediately."
                )

            mutual_guilds = await self.get_mutual_guilds_for_user(user.id)
            if not mutual_guilds:
                return "⚠️ I couldn't find you in any server I'm in. Please join the server first, then try again."

            is_user_email_verified = bool(email_hash)
            sync_result = await self.assign_role_across_guilds(
                user.id,
                role_name,
                mutual_guilds,
                campus_role_name=campus_role_name,
                level_role_name=level_role_name,
                is_email_verified=is_user_email_verified,
                student_id_hash=id_hash,
                email_hash=email_hash,
            )

            # Persist if role was granted or user already held the role in at least one server
            if sync_result.verified_in or sync_result.already_had_role_in:
                try:
                    async with self.db.transaction():
                        await self.db.record_verification(
                            user.id,
                            id_hash,
                            faculty_code,
                            campus_code=campus_code,
                            level_code=level_code,
                            card_expiry_date=iso_expiry_date,
                            student_email_encrypted=email_encrypted,
                            student_email_hash=email_hash,
                        )
                        active_servers = [
                            entry[1] if len(entry) == 3 else entry[0]
                            for entry in (sync_result.verified_in + sync_result.already_had_role_in)
                        ]
                        email_log_str = f", email: {mask_email(clean_email)}" if email_hash and raw_email else ""
                        await self.db.log(
                            "INFO",
                            "VERIFIED",
                            f"{user} (ID: {user.id}) verified (student ID masked: {mask_student_id(student_id)}{email_log_str}) "
                            f"→ active in {active_servers}",
                            user_id=user.id,
                            guild=guild_ctx,
                        )
                except (sqlite3.IntegrityError, aiosqlite.IntegrityError) as e:
                    # Rollback assigned roles if database collision occurs
                    for entry in sync_result.verified_in:
                        if len(entry) == 3:
                            g_id, _, r_names = entry
                            guild = self.bot.get_guild(g_id)
                        else:
                            g_name, r_names = entry
                            guild = discord.utils.get(self.bot.guilds, name=g_name)

                        if guild:
                            member = await self.get_or_fetch_member(guild, user.id)
                            if member:
                                for single_r in r_names.split(", "):
                                    r = discord.utils.get(guild.roles, name=single_r.strip())
                                    if r and r in member.roles:
                                        try:
                                            await member.remove_roles(
                                                r, reason="TARVeri: Rollback due to database collision"
                                            )
                                        except discord.HTTPException as exc:
                                            logger.debug("Failed removing role during rollback: %s", exc)
                    await self.db.log(
                        "ERROR",
                        "INTEGRITY_CONFLICT",
                        f"Verification collision for {user} (ID: {user.id}): {e}",
                        user_id=user.id,
                        guild=guild_ctx,
                    )
                    return (
                        "❌ Verification failed due to a collision (the student ID or your account was just verified elsewhere). "
                        "Please contact an admin if this persists."
                    )

            summary = self.format_role_summary(sync_result) or "⚠️ Verification completed, but no roles could be assigned."

            # Automatically detect if the student ID intake or expiry has already passed
            today_iso = datetime.now(get_configured_tz()).strftime("%Y-%m-%d")
            if iso_expiry_date and iso_expiry_date < today_iso:
                expiry_disp = format_card_expiry_display(iso_expiry_date)
                summary += (
                    f"\n\n🎓 **Alumni / Academic Status Notice**:\n"
                    f"Based on your student ID (study validity ended **{expiry_disp}**), you may have already graduated!\n"
                    f"• Click **I have Graduated** below or run `/graduate` to claim your official **TARUMT Alumni** role & card badge.\n"
                    f"• If you continued your studies (e.g. Diploma ➔ Degree), click **Further Studies** or run `/verify <new_id>`.\n"
                    f"• If you are still completing your programme, click **Still Studying / Extension**."
                )

            return summary
        finally:
            async with self._lock:
                self._in_flight_users.discard(user.id)

    async def reconcile_verified_members(
        self,
        guild: discord.Guild,
        default_campus: str = "W",
        default_level: str | None = None,
    ) -> dict[str, int]:
        """
        Self-healing: cross-references current guild members against the verifications table.
        1. If a student verified in DB is missing faculty/campus/study-level roles in this guild,
           restores and synchronizes them (subject to guild email verification policy).
        2. If a student is in an email-mandated server without email verification, strips unauthorized roles.
        3. If an unverified member holds verified roles, cleans up stray roles.
        """
        summary = {"checked": 0, "restored": 0, "failed": 0, "unauthorized_cleaned": 0, "unverified_cleaned": 0}
        if not guild:
            return summary

        # Ensure guild member cache is populated if chunk method exists
        if hasattr(guild, "chunk") and not getattr(guild, "chunked", True):
            try:
                await guild.chunk()
            except Exception as exc:
                logger.debug("Guild chunking skipped during reconciliation: %s", exc)

        all_verifications = await self.db.get_all_verifications()
        if not all_verifications:
            all_verifications = []

        me = getattr(guild, "me", None)
        can_manage = (
            getattr(me.guild_permissions, "manage_roles", False)
            if me and hasattr(me, "guild_permissions")
            else False
        )
        bot_top_role = getattr(me, "top_role", None) if me else None
        bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0

        guild_email_required = False
        guild_email_enforced = False
        if self.db and hasattr(guild, "id"):
            try:
                guild_email_required = await self.db.is_guild_email_verification_enabled(guild.id)
                guild_email_enforced = await self.db.is_guild_email_enforcement_enabled(guild.id)
            except Exception as exc:
                logger.debug("Failed checking guild email policy during reconciliation: %s", exc)

        if not guild_email_enforced and self.settings:
            guild_email_enforced = getattr(self.settings, "enable_email_role_enforcement", False)

        verified_user_ids: set[int] = set()
        email_strip_candidates: list[tuple[discord.Member, list[discord.Role]]] = []

        for discord_user_id, _, faculty_code, _ in all_verifications:
            verified_user_ids.add(discord_user_id)
            member = await self.get_or_fetch_member(guild, discord_user_id)
            if not member:
                continue

            summary["checked"] += 1
            member_roles = getattr(member, "roles", [])
            if not isinstance(member_roles, (list, tuple)):
                member_roles = []

            # Check eligibility if guild mandates email verification (Opt-In)
            details = await self.db.get_verification_details(discord_user_id)
            is_email_verified = bool(details.get("student_email_hash")) if details else False

            # Check if member is blacklisted in this guild
            is_bl, bl_reason = await self.db.is_blacklisted(
                guild.id,
                user_id=discord_user_id,
                student_id_hash=details.get("student_id_hash") if details else None,
                email_hash=details.get("student_email_hash") if details else None,
            )
            if is_bl:
                stripped_roles = await self.strip_all_roles_in_guild(
                    guild,
                    member,
                    reason=f"TARVeri Self-Healing: Stripped verified roles from blacklisted member ({bl_reason})",
                )
                if stripped_roles:
                    summary["unauthorized_cleaned"] += len(stripped_roles)
                    await self.db.log(
                        "WARNING",
                        "BLACKLIST_ROLE_STRIPPED",
                        f"Self-healing: Stripped {len(stripped_roles)} role(s) from blacklisted member {member} (ID: {discord_user_id}) in '{guild.name}'. Reason: {bl_reason}",
                        guild=guild,
                        user_id=discord_user_id,
                    )
                continue

            if guild_email_required and not is_email_verified and guild_email_enforced:
                # When retroactive enforcement is explicitly enabled by admin, collect candidates for revocation
                roles_to_strip = [
                    r
                    for r in member_roles
                    if any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES)
                    or any(self._match_campus_role_in_list([r], camp) is not None for camp in CAMPUS_ROLE_NAMES)
                    or any(self._match_study_level_role_in_list([r], lvl) is not None for lvl in STUDY_LEVEL_ROLE_NAMES)
                    or self._match_alumni_role_in_list([r]) is not None
                ]
                strip_manageable = [
                    r for r in roles_to_strip
                    if not (isinstance(bot_pos, int) and isinstance(getattr(r, "position", 0), int) and getattr(r, "position", 0) >= bot_pos)
                ]
                if strip_manageable and can_manage:
                    email_strip_candidates.append((member, strip_manageable))
                continue

            roles_to_add: list[discord.Role] = []

            # 1. Primary faculty role
            target_role_name = FACULTY_ROLES.get(faculty_code)
            has_faculty_role = False
            if target_role_name and isinstance(member_roles, (list, tuple)):
                has_faculty_role = self._match_faculty_role_in_list(member_roles, target_role_name) is not None

                # Clean up any obsolete/conflicting other faculty roles
                conflicting_fac_roles = [
                    r for r in member_roles
                    if any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES if fac != target_role_name)
                ]
                for conf_r in conflicting_fac_roles:
                    conf_pos = getattr(conf_r, "position", 0)
                    if can_manage and not (isinstance(bot_pos, int) and isinstance(conf_pos, int) and conf_pos >= bot_pos):
                        try:
                            await member.remove_roles(conf_r, reason="TARVeri: Self-healing faculty role mismatch")
                        except discord.HTTPException:
                            pass

            if not has_faculty_role and target_role_name:
                target_role = await self.get_or_create_faculty_role(guild, target_role_name)
                if target_role:
                    role_pos = getattr(target_role, "position", 0)
                    if can_manage and not (isinstance(bot_pos, int) and isinstance(role_pos, int) and role_pos >= bot_pos):
                        roles_to_add.append(target_role)
                    else:
                        summary["failed"] += 1
                else:
                    summary["failed"] += 1

            # 2. Campus branch role
            c_code = details.get("campus_code") if details else None

            # Detect if member already holds a campus role in Discord
            existing_campus_code = None
            has_campus_role = False
            if isinstance(member_roles, (list, tuple)):
                for r in member_roles:
                    for code_k, name_v in CAMPUS_ROLES.items():
                        if self._match_campus_role_in_list([r], name_v) is not None:
                            has_campus_role = True
                            existing_campus_code = code_k
                            break
                    if has_campus_role:
                        break

            if has_campus_role and existing_campus_code and (not c_code or c_code != existing_campus_code):
                try:
                    await self.db.update_verification_details(discord_user_id, campus_code=existing_campus_code)
                    c_code = existing_campus_code
                except Exception as exc:
                    logger.warning("Failed updating campus code in reconciliation: %s", exc, exc_info=True)

            if not c_code:
                c_code = default_campus

            target_campus_name = CAMPUS_ROLES.get(c_code, "KL Main Campus")
            if not has_campus_role and target_campus_name:
                target_camp = await self.get_or_create_campus_role(guild, target_campus_name)
                if target_camp:
                    camp_pos = getattr(target_camp, "position", 0)
                    if can_manage and not (isinstance(bot_pos, int) and isinstance(camp_pos, int) and camp_pos >= bot_pos):
                        roles_to_add.append(target_camp)

            # 3. Study level role
            l_code = details.get("level_code") if details else None

            # Detect if member already holds a study level role in Discord
            existing_level_code = None
            has_level_role = False
            if isinstance(member_roles, (list, tuple)):
                for r in member_roles:
                    for code_k, name_v in STUDY_LEVEL_ROLES.items():
                        if self._match_study_level_role_in_list([r], name_v) is not None:
                            has_level_role = True
                            existing_level_code = code_k
                            break
                    if has_level_role:
                        break

            if has_level_role and existing_level_code and (not l_code or l_code != existing_level_code):
                try:
                    await self.db.update_verification_details(discord_user_id, level_code=existing_level_code)
                    l_code = existing_level_code
                except Exception as exc:
                    logger.warning("Failed updating study level code in reconciliation: %s", exc, exc_info=True)

            if not l_code and default_level:
                l_code = default_level

            if l_code:
                target_level_name = STUDY_LEVEL_ROLES.get(l_code)
                if target_level_name and not has_level_role:
                    target_lvl = await self.get_or_create_study_level_role(guild, target_level_name)
                    if target_lvl:
                        lvl_pos = getattr(target_lvl, "position", 0)
                        if can_manage and not (isinstance(bot_pos, int) and isinstance(lvl_pos, int) and lvl_pos >= bot_pos):
                            roles_to_add.append(target_lvl)

            if roles_to_add:
                try:
                    await member.add_roles(
                        *roles_to_add,
                        reason="TARVeri: Self-healing automatic role restoration for verified student",
                    )
                    summary["restored"] += len(roles_to_add)
                    assigned_labels = ", ".join(
                        [getattr(r, "name", "Role") for r in roles_to_add if isinstance(getattr(r, "name", None), str)]
                    ) or "roles"
                    await self.db.log(
                        "INFO",
                        "ROLE_RESTORED",
                        f"Self-healing: Restored missing role(s) [{assigned_labels}] to verified student {member} (ID: {discord_user_id})",
                        guild=guild,
                        user_id=discord_user_id,
                    )
                except discord.HTTPException as e:
                    summary["failed"] += len(roles_to_add)
                    logger.warning(
                        f"Failed to restore roles for {member} in '{guild.name}': {e}"
                    )

        # Process email policy candidates with Circuit Breaker (Threshold >= 5)
        threshold = getattr(self.settings, "mass_revocation_threshold", 5) if self.settings else 5
        if len(email_strip_candidates) >= threshold:
            action_id = await self.stage_mass_revocation(
                guild=guild,
                action_type="EMAIL_POLICY_REVOCATION",
                candidates=email_strip_candidates,
                reason="Mandatory institutional email verification policy enforcement",
            )
            summary["mass_revocation_suspended"] = len(email_strip_candidates)
            await self.db.log(
                "WARNING",
                "MASS_REVOCATION_INTERCEPTED",
                f"Mass role revocation intercepted for {len(email_strip_candidates)} member(s) in '{guild.name}'. Suspended pending admin approval (Action ID: {action_id}).",
                guild=guild,
            )
        elif email_strip_candidates:
            for cand_member, cand_roles in email_strip_candidates:
                try:
                    await cand_member.remove_roles(
                        *cand_roles,
                        reason="TARVeri Self-Healing: Stripped verified roles from non-email-verified member in email-enforced server",
                    )
                    summary["unauthorized_cleaned"] += len(cand_roles)
                    await self.db.log(
                        "INFO",
                        "ROLE_POLICY_ENFORCED",
                        f"Self-healing: Removed {len(cand_roles)} role(s) from {cand_member} (ID: {cand_member.id}) - requires institutional email verification in '{guild.name}'",
                        guild=guild,
                        user_id=cand_member.id,
                    )
                except discord.HTTPException as exc:
                    logger.warning(
                        f"Could not strip unauthorized roles from {cand_member} in {guild.name}: {exc}",
                        exc_info=True,
                    )

        # 4. Clean up unverified members who erroneously hold verified roles (with Circuit Breaker)
        unverified_stray_candidates: list[tuple[discord.Member, list[discord.Role]]] = []
        guild_members = getattr(guild, "members", [])
        if isinstance(guild_members, (list, tuple)):
            for member in guild_members:
                if member.id not in verified_user_ids and not getattr(member, "bot", False):
                    stray_roles = [
                        r
                        for r in getattr(member, "roles", [])
                        if any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES)
                        or any(self._match_campus_role_in_list([r], camp) is not None for camp in CAMPUS_ROLE_NAMES)
                        or any(self._match_study_level_role_in_list([r], lvl) is not None for lvl in STUDY_LEVEL_ROLE_NAMES)
                        or self._match_alumni_role_in_list([r]) is not None
                    ]
                    stray_manageable = [
                        r for r in stray_roles
                        if not (isinstance(bot_pos, int) and isinstance(getattr(r, "position", 0), int) and getattr(r, "position", 0) >= bot_pos)
                    ]
                    if stray_manageable and can_manage:
                        unverified_stray_candidates.append((member, stray_manageable))

        if len(unverified_stray_candidates) >= threshold:
            action_id = await self.stage_mass_revocation(
                guild=guild,
                action_type="STRAY_UNVERIFIED_CLEANUP",
                candidates=unverified_stray_candidates,
                reason="Unverified members holding student verification roles",
            )
            summary["mass_unverified_suspended"] = len(unverified_stray_candidates)
            await self.db.log(
                "WARNING",
                "MASS_REVOCATION_INTERCEPTED",
                f"Mass stray role cleanup intercepted for {len(unverified_stray_candidates)} unverified member(s) in '{guild.name}'. Suspended pending admin approval (Action ID: {action_id}).",
                guild=guild,
            )
        elif unverified_stray_candidates:
            for member, stray_manageable in unverified_stray_candidates:
                try:
                    await member.remove_roles(
                        *stray_manageable,
                        reason="TARVeri Self-Healing: Removed verified roles from unverified member",
                    )
                    summary["unverified_cleaned"] += len(stray_manageable)
                    await self.db.log(
                        "INFO",
                        "UNVERIFIED_ROLES_CLEANED",
                        f"Self-healing: Removed {len(stray_manageable)} stray verified role(s) from unverified member {member} (ID: {member.id}) in '{guild.name}'",
                        guild=guild,
                        user_id=member.id,
                    )
                except discord.HTTPException as exc:
                    logger.warning(
                        f"Could not remove stray verified roles from unverified member {member} in {guild.name}: {exc}",
                        exc_info=True,
                    )

        if summary["restored"] > 0 or summary["unauthorized_cleaned"] > 0 or summary["unverified_cleaned"] > 0:
            logger.info(
                f"[{guild.name}] Self-healing verified member reconciliation: "
                f"Checked {summary['checked']}, Restored {summary['restored']}, "
                f"Unauthorized Cleaned {summary['unauthorized_cleaned']}, Unverified Cleaned {summary['unverified_cleaned']}, Failed {summary['failed']}"
            )

        return summary

    async def backfill_branch_roles(
        self,
        guild: discord.Guild | None = None,
        default_campus_code: str = "W",
        default_level_code: str | None = None,
    ) -> dict[str, int]:
        """
        One-time migration helper:
        1. Backfills legacy DB records where campus_code is NULL to default_campus_code.
        2. If default_level_code is given, backfills DB records where level_code is NULL.
        3. Iterates over specified guild (or all shared guilds) and assigns missing branch campus
           and study level roles to verified students.
        """
        stats = {
            "guilds_scanned": 0,
            "members_checked": 0,
            "roles_assigned": 0,
            "db_migrated": 0,
            "failed": 0,
        }
        stats["db_migrated"] = await self.db.backfill_legacy_verifications(default_campus=default_campus_code)
        if default_level_code and self.db._conn:
            cursor = await self.db._conn.execute(
                "UPDATE verifications SET level_code = ? WHERE level_code IS NULL",
                (default_level_code,),
            )
            await self.db._conn.commit()
            stats["db_migrated"] += cursor.rowcount

        target_guilds = [guild] if guild else list(self.bot.guilds)
        for g in target_guilds:
            if not g:
                continue
            stats["guilds_scanned"] += 1
            g_summary = await self.reconcile_verified_members(
                g,
                default_campus=default_campus_code,
                default_level=default_level_code,
            )
            stats["members_checked"] += g_summary.get("checked", 0)
            stats["roles_assigned"] += g_summary.get("restored", 0)
            stats["failed"] += g_summary.get("failed", 0)

        return stats

    async def claim_alumni_status(
        self,
        user_id: int,
        user_display_name: str,
        graduated_year: int,
        programme: str | None = None,
        current_guild: discord.Guild | None = None,
    ) -> dict[str, Any]:
        """Processes instant alumni transition for an already-verified student."""
        verif = await self.db.get_verification_by_user(user_id)
        if not verif:
            return {
                "success": False,
                "error": "NOT_VERIFIED",
                "message": "You must be a verified TARUMT student before claiming Alumni status. Please run `/verify` first.",
            }

        # Validate year (from 1969 TAR College founding to realistic graduation window)
        from datetime import datetime
        current_year = datetime.now().year
        if graduated_year < 1969 or graduated_year > current_year + 5:
            return {
                "success": False,
                "error": "INVALID_YEAR",
                "message": f"Please provide a valid graduation year (1969–{current_year + 5}).",
            }

        # Record in database
        clean_prog = programme.strip() if programme and programme.strip() else None
        await self.db.record_alumni_claim(user_id, graduated_year, clean_prog)

        # Assign role across mutual guilds
        mutual_guilds = await self.get_mutual_guilds_for_user(user_id)
        roles_assigned = await self.sync_alumni_role_across_guilds(
            user_id,
            mutual_guilds,
            reason=f"TARVeri: Claimed Alumni status (Class of {graduated_year})",
        )

        stored_faculty = verif[1]
        faculty_name = FACULTY_ROLES.get(stored_faculty, stored_faculty)

        await self.db.log(
            "INFO",
            "ALUMNI_CLAIMED",
            f"Student {user_display_name} (ID: {user_id}) claimed Alumni status: Class of {graduated_year} • {clean_prog or 'N/A'} (Assigned in {len(roles_assigned)} servers)",
            guild=current_guild,
            user_id=user_id,
        )

        return {
            "success": True,
            "graduated_year": graduated_year,
            "programme": clean_prog,
            "faculty_code": stored_faculty,
            "faculty_name": faculty_name,
            "guilds_updated": len(roles_assigned),
        }

    async def revoke_alumni_status(
        self,
        target_user: discord.User | discord.Member,
        admin: discord.User | discord.Member,
        current_guild: discord.Guild | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Revokes alumni status from a user and removes alumni roles."""
        alumni_info = await self.db.get_alumni_info_by_user(target_user.id)
        if not alumni_info or not alumni_info.get("is_alumni"):
            return {
                "success": False,
                "error": "NOT_ALUMNI",
                "message": f"{target_user.mention} is not currently registered as an Alumni.",
            }

        # Remove role in mutual guilds
        mutual_guilds = await self.get_mutual_guilds_for_user(target_user.id)
        roles_removed: list[str] = []

        for guild in mutual_guilds:
            member = await self.get_or_fetch_member(guild, target_user.id)
            if not member:
                continue

            alumni_role = await self.find_alumni_role(guild)
            if alumni_role and alumni_role in getattr(member, "roles", []):
                me = getattr(guild, "me", None)
                can_manage = (
                    getattr(me.guild_permissions, "manage_roles", False)
                    if me and hasattr(me, "guild_permissions")
                    else False
                )
                bot_top = getattr(me, "top_role", None)
                bot_pos = getattr(bot_top, "position", 0) if bot_top else 0
                role_pos = getattr(alumni_role, "position", 0)
                if can_manage and role_pos < bot_pos:
                    try:
                        await member.remove_roles(
                            alumni_role,
                            reason=f"TARVeri: Alumni status revoked by {admin}. Reason: {reason or 'None'}",
                        )
                        roles_removed.append(guild.name)
                    except discord.HTTPException as e:
                        logger.warning(f"Could not remove alumni role from {member} in {guild.name}: {e}")

        await self.db.revoke_alumni_status(target_user.id)
        await self.db.log(
            "WARNING",
            "ALUMNI_REVOKED",
            f"Alumni status for {target_user} (ID: {target_user.id}) revoked by {admin}. Reason: {reason or 'No reason provided'}",
            guild=current_guild,
            user_id=target_user.id,
        )

        return {
            "success": True,
            "target_id": target_user.id,
            "roles_removed_count": len(roles_removed),
        }

    async def unverify_member(
        self,
        user_id: int,
        admin: discord.User | discord.Member | None = None,
        current_guild: discord.Guild | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """
        Unlinks verification from DB, resets rate limit, and strips faculty, campus, study level,
        and alumni roles across all mutual servers.
        """
        verif = await self.db.get_verification_by_user(user_id)
        if not verif:
            return {
                "success": False,
                "message": f"User ID `{user_id}` is not currently verified in the database.",
                "roles_removed": [],
                "guilds_count": 0,
            }

        mutual_guilds = await self.get_mutual_guilds_for_user(user_id)
        roles_removed: list[str] = []
        guilds_affected: set[int] = set()
        clean_reason = reason or "No reason provided"

        for guild in mutual_guilds:
            member = await self.get_or_fetch_member(guild, user_id)
            if not member:
                continue

            roles_to_remove = [
                r
                for r in getattr(member, "roles", [])
                if any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES)
                or any(self._match_campus_role_in_list([r], camp) is not None for camp in CAMPUS_ROLE_NAMES)
                or any(self._match_study_level_role_in_list([r], lvl) is not None for lvl in STUDY_LEVEL_ROLE_NAMES)
                or self._match_alumni_role_in_list([r]) is not None
            ]

            me = getattr(guild, "me", None)
            can_manage = (
                getattr(me.guild_permissions, "manage_roles", False)
                if me and hasattr(me, "guild_permissions")
                else False
            )
            bot_top = getattr(me, "top_role", None)
            bot_pos = getattr(bot_top, "position", 0) if bot_top else 0

            for role in roles_to_remove:
                role_pos = getattr(role, "position", 0)
                if (
                    can_manage
                    and isinstance(bot_pos, int)
                    and isinstance(role_pos, int)
                    and role_pos < bot_pos
                ):
                    try:
                        admin_str = str(admin) if admin else "Admin"
                        await member.remove_roles(
                            role,
                            reason=f"TARVeri: Verification unlinked by {admin_str}. Reason: {clean_reason}",
                        )
                        roles_removed.append(f"{guild.name} ({role.name})")
                        guilds_affected.add(guild.id)
                    except discord.HTTPException as e:
                        logger.warning(
                            f"Could not remove role {role.name} from {member} in {guild.name}: {e}",
                            exc_info=True,
                        )

        await self.db.delete_verification(user_id)
        self.rate_limiter.reset(user_id)

        admin_desc = f"Admin {admin}" if admin else "Admin"
        await self.db.log(
            "WARNING",
            "MEMBER_UNVERIFIED",
            f"{admin_desc} unverified member ID {user_id}. Reason: {clean_reason}",
            guild=current_guild,
            user_id=user_id,
        )

        return {
            "success": True,
            "message": f"Successfully unverified user ID `{user_id}`.",
            "roles_removed": roles_removed,
            "guilds_count": len(guilds_affected),
        }

    async def reconcile_alumni_members(self, guild: discord.Guild) -> dict[str, int]:
        """Self-healing: Ensures all registered alumni in the guild have the TARUMT Alumni role."""
        summary = {"checked": 0, "restored": 0, "failed": 0}
        if not guild:
            return summary

        alumni_ids = await self.db.get_all_alumni_user_ids()
        if not alumni_ids:
            return summary

        alumni_role = await self.get_or_create_alumni_role(guild)
        if not alumni_role:
            summary["failed"] = len(alumni_ids)
            return summary

        me = getattr(guild, "me", None)
        can_manage = (
            getattr(me.guild_permissions, "manage_roles", False)
            if me and hasattr(me, "guild_permissions")
            else False
        )
        bot_top = getattr(me, "top_role", None)
        bot_pos = getattr(bot_top, "position", 0) if bot_top else 0
        role_pos = getattr(alumni_role, "position", 0)
        if not can_manage or (isinstance(bot_pos, int) and isinstance(role_pos, int) and role_pos >= bot_pos):
            summary["failed"] = len(alumni_ids)
            return summary

        for user_id in alumni_ids:
            member = await self.get_or_fetch_member(guild, user_id)
            if not member:
                continue

            summary["checked"] += 1
            if alumni_role not in getattr(member, "roles", []):
                try:
                    await member.add_roles(
                        alumni_role,
                        reason="TARVeri: Self-healing automatic Alumni role restoration",
                    )
                    summary["restored"] += 1
                    await self.db.log(
                        "INFO",
                        "ROLE_RESTORED",
                        f"Self-healing: Restored missing Alumni role to {member} (ID: {user_id}) in '{guild.name}'",
                        guild=guild,
                        user_id=user_id,
                    )
                except discord.HTTPException as e:
                    summary["failed"] += 1
                    logger.warning(f"Could not restore alumni role for {member} in {guild.name}: {e}")

        if summary["restored"] > 0:
            logger.info(
                f"[{guild.name}] Self-healing alumni member reconciliation: "
                f"Checked {summary['checked']}, Restored {summary['restored']}, Failed {summary['failed']}"
            )

        return summary

    async def is_role_created_by_bot(
        self,
        guild: discord.Guild,
        role: discord.Role,
        bot_created_ids: set[int] | None = None,
    ) -> bool:
        """
        Checks whether a role was created by TARVeri (tracked in SQLite bot_created_roles
        or verified via Discord audit logs). Admin-created roles return False.
        """
        if not role or not guild:
            return False

        # 1. Fast in-memory / pre-fetched set check
        if bot_created_ids is not None and getattr(role, "id", None) in bot_created_ids:
            return True

        # 2. SQLite database lookup
        try:
            db_ids = await self.db.get_bot_created_role_ids(guild.id)
            if getattr(role, "id", None) in db_ids:
                return True
        except Exception as e:
            logger.debug(f"Could not query bot_created_role_ids for guild {guild.id}: {e}")

        # 3. Discord Audit Logs fallback if bot has View Audit Log permission
        me = getattr(guild, "me", None)
        can_view_audit = getattr(getattr(me, "guild_permissions", None), "view_audit_log", False)
        if can_view_audit and hasattr(guild, "audit_logs") and callable(guild.audit_logs):
            try:
                async for entry in guild.audit_logs(action=discord.AuditLogAction.role_create, limit=100):
                    if entry.target and entry.target.id == getattr(role, "id", None):
                        if entry.user and me and entry.user.id == me.id:
                            await self.db.record_bot_created_role(guild.id, role.id, getattr(role, "name", "unknown"))
                            return True
                        else:
                            return False
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass

        return False

    async def reconcile_duplicate_roles(self, guild: discord.Guild) -> dict[str, Any]:
        """
        Self-healing: scans guild for duplicate faculty and guest roles matching the same category.
        Identifies the primary role (highest position in hierarchy / highest member count),
        migrates all members on redundant duplicate role(s) to the primary role, and ONLY deletes
        redundant duplicate role(s) that were created by the bot (admin-created roles are preserved).
        """
        stats: dict[str, Any] = {
            "checked_categories": 0,
            "migrated_members": 0,
            "deleted_roles": 0,
            "failed": 0,
            "details": [],
        }
        if not guild:
            return stats

        # Ensure guild member cache is populated if chunk method exists
        if hasattr(guild, "chunk") and not getattr(guild, "chunked", True):
            try:
                await guild.chunk()
            except Exception as exc:
                logger.debug("Guild chunking skipped during duplicate role reconciliation: %s", exc)

        me = getattr(guild, "me", None)
        can_manage = (
            getattr(me.guild_permissions, "manage_roles", False)
            if me and hasattr(me, "guild_permissions")
            else False
        )
        bot_top_role = getattr(me, "top_role", None) if me else None
        bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0

        # Pre-fetch bot-created role IDs from DB for this guild
        try:
            bot_created_ids = await self.db.get_bot_created_role_ids(guild.id)
        except Exception as exc:
            logger.warning("Failed to fetch bot-created role IDs for guild %s: %s", guild.id, exc, exc_info=True)
            bot_created_ids = set()

        guild_roles = list(getattr(guild, "roles", []))

        def _role_rank(role: discord.Role, target_name: str) -> tuple[int, int, int]:
            pos = getattr(role, "position", 0) if isinstance(getattr(role, "position", 0), int) else 0
            member_count = len(getattr(role, "members", []))
            exact_match = 1 if getattr(role, "name", "").strip().lower() == target_name.strip().lower() else 0
            return (exact_match, pos, member_count)

        # 1. Group by faculty (strictly ignoring SRC, Council, Committee, study level, campus, and staff roles)
        category_roles: dict[str, list[discord.Role]] = {}
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if not r_name or ROLE_QUALIFIER_PATTERN.search(r_name):
                continue
            r_upper = r_name.strip().upper()
            if any(r_upper == lvl.upper() for lvl in STUDY_LEVEL_ROLE_NAMES):
                continue
            if any(r_upper == c.upper() for c in CAMPUS_ROLE_NAMES):
                continue
            if r_upper == ALUMNI_ROLE_NAME.upper():
                continue
            for fac in FACULTY_ROLE_NAMES:
                if self._match_faculty_role_in_list([r], fac) is not None:
                    category_roles.setdefault(fac, []).append(r)
                    break

        # 2. Group study level roles (strictly partitioned)
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if not r_name or ROLE_QUALIFIER_PATTERN.search(r_name):
                continue
            r_upper = r_name.strip().upper()
            if any(r_upper == fac.upper() for fac in FACULTY_ROLE_NAMES):
                continue
            for lvl in STUDY_LEVEL_ROLE_NAMES:
                if self._match_study_level_role_in_list([r], lvl) is not None:
                    category_roles.setdefault(lvl, []).append(r)
                    break

        # 3. Group campus roles (strictly partitioned)
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if not r_name or ROLE_QUALIFIER_PATTERN.search(r_name):
                continue
            r_upper = r_name.strip().upper()
            if any(r_upper == fac.upper() for fac in FACULTY_ROLE_NAMES):
                continue
            for camp in CAMPUS_ROLE_NAMES:
                if self._match_campus_role_in_list([r], camp) is not None:
                    category_roles.setdefault(camp, []).append(r)
                    break

        # 4. Group guest roles
        settings = await self.db.get_guild_settings(guild.id)
        configured_guest_name = settings[2].strip() if settings and len(settings) > 2 and settings[2] else None
        guest_roles: list[discord.Role] = []
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if not r_name or ROLE_QUALIFIER_PATTERN.search(r_name):
                continue
            if configured_guest_name and r_name.strip().lower() == configured_guest_name.lower() or GUEST_ROLE_PATTERN.search(r_name):
                guest_roles.append(r)

        if guest_roles:
            category_roles["Guest"] = guest_roles

        # 5. Process categories with duplicates
        for cat_name, roles_found in category_roles.items():
            stats["checked_categories"] += 1
            if len(roles_found) <= 1:
                continue

            target_name = cat_name if cat_name != "Guest" else (configured_guest_name or "Guest(Approved)")
            sorted_roles = sorted(roles_found, key=lambda r: _role_rank(r, target_name), reverse=True)
            primary_role = sorted_roles[0]
            redundant_roles = sorted_roles[1:]

            for red_role in redundant_roles:
                is_bot_created = await self.is_role_created_by_bot(guild, red_role, bot_created_ids)

                # Migrate members
                red_members = list(getattr(red_role, "members", []))
                for member in red_members:
                    member_roles = getattr(member, "roles", [])
                    if primary_role not in member_roles:
                        try:
                            await member.add_roles(
                                primary_role,
                                reason=f"TARVeri Self-Healing: Migrate from duplicate role '{red_role.name}' to primary '{primary_role.name}'",
                            )
                            stats["migrated_members"] += 1
                        except (discord.HTTPException, discord.Forbidden) as e:
                            logger.warning(f"Failed to migrate member {member} to {primary_role.name}: {e}")
                            stats["failed"] += 1

                    if is_bot_created and red_role in getattr(member, "roles", []):
                        try:
                            await member.remove_roles(
                                red_role,
                                reason=f"TARVeri Self-Healing: Remove duplicate role '{red_role.name}'",
                            )
                        except (discord.HTTPException, discord.Forbidden):
                            pass

                # If NOT created by bot, preserve the admin-created role (do not delete!)
                if not is_bot_created:
                    detail_msg = f"Preserved admin-created role '{red_role.name}' (ID: {getattr(red_role, 'id', 'N/A')}) — only bot-created roles are deleted"
                    stats["details"].append(detail_msg)
                    logger.info(f"[{guild.name}] {detail_msg}")
                    continue

                # Delete bot-created redundant role
                red_pos = getattr(red_role, "position", 0)
                is_manageable = (
                    can_manage
                    and isinstance(bot_pos, int)
                    and isinstance(red_pos, int)
                    and red_pos < bot_pos
                    and not getattr(red_role, "managed", False)
                    and not (hasattr(red_role, "is_default") and red_role.is_default())
                )

                if is_manageable:
                    try:
                        await red_role.delete(
                            reason=f"TARVeri Self-Healing: Removed bot-created duplicate role '{red_role.name}' (migrated to '{primary_role.name}')"
                        )
                        try:
                            await self.db.delete_bot_created_role(red_role.id)
                        except Exception as exc:
                            logger.warning("Failed deleting bot-created role from DB tracking: %s", exc, exc_info=True)
                        stats["deleted_roles"] += 1
                        detail_msg = f"Deleted bot-created duplicate role '{red_role.name}' (migrated {len(red_members)} member(s) to '{primary_role.name}')"
                        stats["details"].append(detail_msg)
                        await self.db.log(
                            "INFO",
                            "DUPLICATE_ROLE_DELETED",
                            f"Self-Healing: [{guild.name}] {detail_msg}",
                            guild=guild,
                        )
                    except (discord.HTTPException, discord.Forbidden) as e:
                        stats["failed"] += 1
                        logger.warning(f"Failed to delete duplicate role {red_role.name} in {guild.name}: {e}")
                else:
                    stats["failed"] += 1
                    detail_msg = f"Cannot delete bot-created duplicate role '{red_role.name}' due to hierarchy/permissions (pos {red_pos} >= bot {bot_pos})"
                    stats["details"].append(detail_msg)
                    logger.warning(f"[{guild.name}] {detail_msg}")

        if stats["deleted_roles"] > 0 or stats["migrated_members"] > 0:
            logger.info(
                f"[{guild.name}] Self-healing duplicate role reconciliation complete: "
                f"Deleted {stats['deleted_roles']} role(s), Migrated {stats['migrated_members']} member(s), Failed {stats['failed']}"
            )

        return stats

    def diagnose_guild_permissions(self, guild: discord.Guild) -> list[str]:
        """
        Diagnoses permission, hierarchy, and duplicate role issues in a guild.
        Returns a list of warning descriptions (empty if guild setup is fully healthy).
        """
        warnings: list[str] = []
        if not guild or not hasattr(guild, "me") or not guild.me:
            return warnings

        me = guild.me
        bot_perms = getattr(me, "guild_permissions", None)
        bot_top_role = getattr(me, "top_role", None)

        if not bot_perms or not getattr(bot_perms, "manage_roles", False):
            warnings.append("❌ Missing `Manage Roles` permission — cannot create or assign faculty/guest roles.")

        guild_roles = getattr(guild, "roles", [])
        if not isinstance(guild_roles, (list, tuple)):
            guild_roles = []

        # Check for duplicate faculty roles
        seen_faculties: dict[str, list[discord.Role]] = {}
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if not r_name or ROLE_QUALIFIER_PATTERN.search(r_name):
                continue
            r_upper = r_name.strip().upper()
            if any(r_upper == lvl.upper() for lvl in STUDY_LEVEL_ROLE_NAMES):
                continue
            if any(r_upper == c.upper() for c in CAMPUS_ROLE_NAMES):
                continue
            if r_upper == ALUMNI_ROLE_NAME.upper():
                continue
            for fac in FACULTY_ROLE_NAMES:
                if self._match_faculty_role_in_list([r], fac) is not None:
                    seen_faculties.setdefault(fac, []).append(r)
                    break

        for fac, matched_roles in seen_faculties.items():
            if len(matched_roles) > 1:
                role_descs = ", ".join(f"`{r.name}` (pos: {getattr(r, 'position', 0)})" for r in matched_roles)
                warnings.append(
                    f"⚠️ Duplicate faculty roles detected for **{fac}**: {role_descs}. Please delete redundant roles in Server Settings → Roles."
                )

        # Check for duplicate guest roles
        matched_guest_roles: list[discord.Role] = []
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if not r_name:
                continue
            if GUEST_ROLE_PATTERN.search(r_name):
                matched_guest_roles.append(r)

        if len(matched_guest_roles) > 1:
            role_descs = ", ".join(f"`{r.name}` (pos: {getattr(r, 'position', 0)})" for r in matched_guest_roles)
            warnings.append(
                f"⚠️ Duplicate guest roles detected: {role_descs}. Please delete redundant roles in Server Settings → Roles."
            )

        # Check hierarchy against existing faculty and guest roles
        bot_pos = getattr(bot_top_role, "position", 0) if bot_top_role else 0
        for r in guild_roles:
            r_name = getattr(r, "name", "")
            if not r_name:
                continue
            is_managed = (
                any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES)
                or bool(GUEST_ROLE_PATTERN.search(r_name))
            )
            if is_managed:
                r_pos = getattr(r, "position", 0)
                if isinstance(bot_pos, int) and isinstance(r_pos, int) and r_pos >= bot_pos:
                    bot_name = getattr(bot_top_role, "name", "TARVeri")
                    warnings.append(
                        f"⚠️ Role hierarchy conflict: Role **{r_name}** (pos {r_pos}) is higher than or equal to bot top role **{bot_name}** (pos {bot_pos}). Please drag the bot's role above **{r_name}** in Server Settings → Roles."
                    )

        return warnings

    async def strip_all_roles_in_guild(
        self,
        guild: discord.Guild,
        member: discord.Member,
        reason: str = "TARVeri: Blacklist role revocation",
    ) -> list[str]:
        """
        Strips all faculty, campus, study level, alumni, and guest roles from a member in a specific guild.
        Returns the list of role names that were removed.
        """
        if not guild or not member:
            return []

        roles_to_remove = [
            r
            for r in getattr(member, "roles", [])
            if any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES)
            or any(self._match_campus_role_in_list([r], camp) is not None for camp in CAMPUS_ROLE_NAMES)
            or any(self._match_study_level_role_in_list([r], lvl) is not None for lvl in STUDY_LEVEL_ROLE_NAMES)
            or self._match_alumni_role_in_list([r]) is not None
            or bool(GUEST_ROLE_PATTERN.search(getattr(r, "name", "")))
        ]

        me = getattr(guild, "me", None)
        can_manage = getattr(getattr(me, "guild_permissions", None), "manage_roles", False)
        bot_top = getattr(me, "top_role", None)
        bot_pos = getattr(bot_top, "position", 0) if bot_top else 0

        manageable_roles = [
            r for r in roles_to_remove
            if can_manage and isinstance(bot_pos, int) and isinstance(getattr(r, "position", 0), int) and getattr(r, "position", 0) < bot_pos
        ]

        removed_names: list[str] = []
        if manageable_roles:
            try:
                await member.remove_roles(*manageable_roles, reason=reason)
                removed_names = [getattr(r, "name", "Role") for r in manageable_roles]
            except discord.HTTPException as exc:
                logger.warning(f"Failed to strip roles from {member} in {guild.name}: {exc}", exc_info=True)

        return removed_names

    async def blacklist_target(
        self,
        guild: discord.Guild,
        target_type: str,
        raw_value: str,
        reason: str | None = None,
        admin: discord.User | discord.Member | None = None,
    ) -> tuple[bool, str]:
        """
        Blacklists a target (USER, STUDENT_ID, or EMAIL) in the specified guild.
        Atomically strips any active verified or guest roles from the affected member in that guild.
        """
        clean_type = target_type.strip().upper()
        if clean_type not in ("USER", "STUDENT_ID", "EMAIL"):
            return False, f"❌ Invalid target type `{target_type}`. Must be `USER`, `STUDENT_ID`, or `EMAIL`."

        admin_id = getattr(admin, "id", 0) if admin else 0
        clean_reason = reason.strip() if reason and reason.strip() else "No reason specified"
        target_value = ""
        display_mask = ""
        target_member: discord.Member | None = None

        if clean_type == "USER":
            digits_only = re.sub(r"[^\d]", "", raw_value.strip())
            if not digits_only:
                return False, "❌ Invalid Discord User or User ID provided."
            user_id = int(digits_only)
            target_value = str(user_id)
            display_mask = f"User <@{user_id}> ({user_id})"
            target_member = await self.get_or_fetch_member(guild, user_id)

        elif clean_type == "STUDENT_ID":
            clean_id = raw_value.strip().upper()
            if not clean_id:
                return False, "❌ Invalid student ID provided."
            id_hash = hash_student_id(clean_id, self.secret)
            target_value = id_hash
            display_mask = mask_student_id(clean_id)
            verif_row = await self.db.get_verification_by_id_hash(id_hash)
            if verif_row:
                target_member = await self.get_or_fetch_member(guild, verif_row[0])

        elif clean_type == "EMAIL":
            clean_email = raw_value.strip().lower()
            if not clean_email or "@" not in clean_email:
                return False, "❌ Invalid email address provided."
            email_hash = hash_email(clean_email, self.secret)
            target_value = email_hash
            display_mask = mask_email(clean_email)
            verif_row = await self.db.get_verification_by_email_hash(email_hash)
            if verif_row:
                target_member = await self.get_or_fetch_member(guild, verif_row[0])

        # 1. Add to Database
        await self.db.add_to_blacklist(
            guild_id=guild.id,
            target_type=clean_type,
            target_value=target_value,
            display_mask=display_mask,
            reason=clean_reason,
            blacklisted_by=admin_id,
        )

        # 2. If target member is in the guild, strip roles immediately
        stripped_roles: list[str] = []
        if target_member:
            stripped_roles = await self.strip_all_roles_in_guild(
                guild,
                target_member,
                reason=f"TARVeri: Blacklisted by {admin or 'Admin'} ({clean_reason})",
            )

        # 3. Log audit event
        admin_label = f"Admin {admin}" if admin else "Admin"
        await self.db.log(
            "WARNING",
            "BLACKLIST_ADDED",
            f"{admin_label} (ID: {admin_id}) blacklisted [{clean_type}] {display_mask} in '{guild.name}'. Reason: '{clean_reason}'. Stripped roles: {stripped_roles or 'None'}",
            guild=guild,
            user_id=admin_id,
        )

        strip_msg = f" Stripped {len(stripped_roles)} role(s) from {target_member.mention}." if stripped_roles and target_member else ""
        return True, f"✅ Successfully added [{clean_type}] `{display_mask}` to **{guild.name}** blacklist.{strip_msg}"

    async def unblacklist_target(
        self,
        guild: discord.Guild,
        target_type: str,
        raw_value: str,
        admin: discord.User | discord.Member | None = None,
    ) -> tuple[bool, str]:
        """
        Removes a target (USER, STUDENT_ID, or EMAIL) from a guild's blacklist.
        """
        clean_type = target_type.strip().upper()
        if clean_type not in ("USER", "STUDENT_ID", "EMAIL"):
            return False, f"❌ Invalid target type `{target_type}`. Must be `USER`, `STUDENT_ID`, or `EMAIL`."

        admin_id = getattr(admin, "id", 0) if admin else 0
        target_value = ""
        if clean_type == "USER":
            digits_only = re.sub(r"[^\d]", "", raw_value.strip())
            if not digits_only:
                return False, "❌ Invalid Discord User ID provided."
            target_value = digits_only
        elif clean_type == "STUDENT_ID":
            clean_id = raw_value.strip().upper()
            target_value = hash_student_id(clean_id, self.secret)
        elif clean_type == "EMAIL":
            clean_email = raw_value.strip().lower()
            target_value = hash_email(clean_email, self.secret)

        removed = await self.db.remove_from_blacklist(guild.id, clean_type, target_value)
        # Fallback if raw_value was passed as hash directly
        if not removed and target_value != raw_value.strip():
            removed = await self.db.remove_from_blacklist(guild.id, clean_type, raw_value.strip())

        if not removed:
            return False, f"⚠️ No matching [{clean_type}] blacklist entry found in **{guild.name}**."

        admin_label = f"Admin {admin}" if admin else "Admin"
        await self.db.log(
            "INFO",
            "BLACKLIST_REMOVED",
            f"{admin_label} (ID: {admin_id}) removed [{clean_type}] target from '{guild.name}' blacklist.",
            guild=guild,
            user_id=admin_id,
        )

        return True, f"✅ Successfully removed [{clean_type}] target from **{guild.name}** blacklist."

    async def get_or_create_tarveri_log_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """
        Finds or automatically provisions a private #tarveri-log channel visible only to administrators and the bot.
        """
        if not guild:
            return None

        # 1. First search for existing log channel by standard naming conventions
        target_names = {"tarveri-log", "tarveri_log", "tarveri-logs", "tarveri_logs"}
        text_channels = getattr(guild, "text_channels", []) or []
        for ch in text_channels:
            if getattr(ch, "name", "").lower() in target_names:
                # Verify bot has view and send permissions
                bot_member = getattr(guild, "me", None)
                if bot_member and hasattr(ch, "permissions_for"):
                    perms = ch.permissions_for(bot_member)
                    if not (getattr(perms, "view_channel", True) and getattr(perms, "send_messages", True)):
                        continue
                return ch

        # 2. Check if bot has permission to create channels
        bot_member = getattr(guild, "me", None)
        can_create = False
        if bot_member:
            guild_perms = getattr(bot_member, "guild_permissions", None)
            if guild_perms and (getattr(guild_perms, "manage_channels", False) or getattr(guild_perms, "administrator", False)):
                can_create = True

        if not can_create or not hasattr(guild, "create_text_channel"):
            return None

        # 3. Create private #tarveri-log with restricted overwrites
        try:
            overwrites: dict[Any, discord.PermissionOverwrite] = {}
            if hasattr(guild, "default_role") and guild.default_role:
                overwrites[guild.default_role] = discord.PermissionOverwrite(
                    view_channel=False,
                    send_messages=False,
                )
            if bot_member:
                overwrites[bot_member] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    embed_links=True,
                    read_message_history=True,
                    attach_files=True,
                )

            # Explicitly grant view access to admin and manage_guild roles (excluding @everyone / default_role)
            roles = getattr(guild, "roles", []) or []
            default_r = getattr(guild, "default_role", None)
            for role in roles:
                if role == default_r:
                    continue
                role_perms = getattr(role, "permissions", None)
                if role_perms and (getattr(role_perms, "administrator", False) or getattr(role_perms, "manage_guild", False)):
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        read_message_history=True,
                        send_messages=False,
                    )

            new_ch = await guild.create_text_channel(
                name="tarveri-log",
                overwrites=overwrites,
                topic="🔒 TARVeri Bot Security & Audit Log (Admin Only) • Real-time alerts for blacklists and security events.",
                reason="TARVeri: Auto-created private security alert channel for guild administrators",
            )

            welcome_embed = discord.Embed(
                title="🔒 TARVeri Security & Audit Log",
                description=(
                    "This private channel is dedicated to **TARVeri security alerts**, blacklisted user interception notices, "
                    "and automated administrative audit feeds.\n\n"
                    "• **Access**: Only server administrators and TARVeri have view permissions.\n"
                    "• **Security**: Real-time alerts are posted here when blacklisted users join or attempt verification."
                ),
                color=discord.Color.dark_theme() if hasattr(discord.Color, "dark_theme") else discord.Color.blue(),
                timestamp=datetime.now(get_configured_tz()),
            )
            welcome_embed.set_footer(text="TARVeri Security Guard • Private Admin Feed")
            try:
                await new_ch.send(embed=welcome_embed)
            except Exception as e:
                logger.debug(f"Could not send welcome embed in new #{new_ch.name}: {e}")

            return new_ch
        except Exception as exc:
            logger.warning(f"Failed to auto-create #tarveri-log channel in '{guild.name}': {exc}")
            return None

    async def send_admin_security_alert(self, guild: discord.Guild, embed: discord.Embed) -> bool:
        """
        Dispatches a security alert embed to the guild's private #tarveri-log channel.
        Returns True if sent successfully, False otherwise.
        """
        if not guild:
            return False
        try:
            channel = await self.get_or_create_tarveri_log_channel(guild)
            if channel and hasattr(channel, "send"):
                await channel.send(embed=embed)
                return True
        except Exception as exc:
            logger.warning(f"Failed to send admin security alert to #tarveri-log in '{guild.name}': {exc}")
        return False

    async def stage_mass_revocation(
        self,
        guild: discord.Guild,
        action_type: str,
        candidates: list[tuple[discord.Member, list[discord.Role]]],
        reason: str,
    ) -> str:
        """
        Stages a pending mass revocation in SQLite and dispatches an interactive approval embed to #tarveri-log.
        """
        import uuid

        from tarveri.cogs.admin_cog import MassRevocationApprovalView

        short_id = f"MREV-{uuid.uuid4().hex[:6].upper()}"
        user_ids = [m.id for m, _ in candidates]

        # Check if an active PENDING action already exists for this guild to prevent duplicate alert spam
        existing_active = await self.db.get_active_pending_mass_action_for_guild(guild.id, action_type)
        if existing_active:
            return existing_active["action_id"]

        await self.db.create_pending_mass_action(
            action_id=short_id,
            guild_id=guild.id,
            action_type=action_type,
            user_ids=user_ids,
            reason=reason,
        )

        preview_mentions = [
            m.mention if isinstance(getattr(m, "mention", None), str) else f"<@{getattr(m, 'id', 0)}>"
            for m, _ in candidates[:10]
        ]
        more_count = len(candidates) - 10
        preview_str = ", ".join(preview_mentions)
        if more_count > 0:
            preview_str += f" *(+{more_count} more)*"

        alert_embed = discord.Embed(
            title="⚠️ [Security Guard] Mass Role Revocation Intercepted",
            description=(
                f"Self-healing detected that **{len(candidates)} members** would have their verified roles stripped "
                f"in **{guild.name}**.\n\n"
                "🛡️ **Threshold Circuit Breaker**: Any revocation affecting **5 or more members** is automatically paused "
                "to prevent accidental mass role loss. Please review and authorize below."
            ),
            color=discord.Color.gold(),
            timestamp=datetime.now(get_configured_tz()),
        )
        alert_embed.add_field(name="📋 Action ID", value=f"`{short_id}`", inline=True)
        alert_embed.add_field(name="👥 Total Affected", value=f"**{len(candidates)}** members", inline=True)
        alert_embed.add_field(name="📝 Trigger / Reason", value=reason, inline=False)
        alert_embed.add_field(name="👤 Affected Members Preview", value=preview_str, inline=False)
        alert_embed.set_footer(text="TARVeri Security Guard • Administrator Authorization Required")

        try:
            view = MassRevocationApprovalView(service=self)
            log_ch = await self.get_or_create_tarveri_log_channel(guild)
            if log_ch and hasattr(log_ch, "send"):
                await log_ch.send(embed=alert_embed, view=view)
        except Exception as exc:
            logger.warning(f"Failed to dispatch mass revocation approval view to #tarveri-log in '{guild.name}': {exc}")

        return short_id

    async def execute_approved_mass_revocation(
        self,
        guild: discord.Guild,
        action_id: str,
        admin: discord.Member | discord.User | None = None,
    ) -> tuple[bool, str]:
        """
        Executes a previously staged and admin-approved mass role revocation.
        """
        action = await self.db.get_pending_mass_action(action_id)
        if not action:
            return False, f"❌ Mass action `{action_id}` not found."

        if action["status"] != "PENDING":
            return False, f"⚠️ Mass action `{action_id}` has already been decided (`{action['status']}`)."

        if action["guild_id"] != guild.id:
            return False, "❌ Guild mismatch for this mass action."

        user_ids: list[int] = action["user_ids"]
        admin_id = getattr(admin, "id", 0) if admin else 0
        admin_label = f"Admin {admin}" if admin else "Admin"

        removed_count = 0
        me = getattr(guild, "me", None)
        bot_pos = getattr(getattr(me, "top_role", None), "position", 0) if me else 0

        for uid in user_ids:
            member = await self.get_or_fetch_member(guild, uid)
            if not member:
                continue
            member_roles = getattr(member, "roles", []) or []
            roles_to_strip = [
                r
                for r in member_roles
                if any(self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES)
                or any(self._match_campus_role_in_list([r], camp) is not None for camp in CAMPUS_ROLE_NAMES)
                or any(self._match_study_level_role_in_list([r], lvl) is not None for lvl in STUDY_LEVEL_ROLE_NAMES)
                or self._match_alumni_role_in_list([r]) is not None
            ]
            manageable = [
                r for r in roles_to_strip
                if not (isinstance(bot_pos, int) and isinstance(getattr(r, "position", 0), int) and getattr(r, "position", 0) >= bot_pos)
            ]
            if manageable:
                try:
                    await member.remove_roles(
                        *manageable,
                        reason=f"TARVeri: Mass revocation approved by {admin} ({action_id})",
                    )
                    removed_count += len(manageable)
                except discord.HTTPException as exc:
                    logger.warning(f"Could not strip roles from {member} during mass revocation: {exc}")

        await self.db.update_pending_mass_action_status(action_id, status="APPROVED", decided_by_id=admin_id)
        await self.db.log(
            "INFO",
            "MASS_REVOCATION_APPROVED",
            f"{admin_label} (ID: {admin_id}) approved mass revocation {action_id} in '{guild.name}'. Removed {removed_count} role(s) from {len(user_ids)} member(s).",
            guild=guild,
            user_id=admin_id,
        )

        return True, f"✅ Successfully executed mass revocation `{action_id}` ({removed_count} roles stripped from {len(user_ids)} members)."

    async def reject_mass_revocation(
        self,
        guild: discord.Guild,
        action_id: str,
        admin: discord.Member | discord.User | None = None,
    ) -> tuple[bool, str]:
        """
        Rejects/dismisses a staged mass role revocation.
        """
        action = await self.db.get_pending_mass_action(action_id)
        if not action:
            return False, f"❌ Mass action `{action_id}` not found."

        if action["status"] != "PENDING":
            return False, f"⚠️ Mass action `{action_id}` has already been decided (`{action['status']}`)."

        admin_id = getattr(admin, "id", 0) if admin else 0
        admin_label = f"Admin {admin}" if admin else "Admin"

        await self.db.update_pending_mass_action_status(action_id, status="REJECTED", decided_by_id=admin_id)
        await self.db.log(
            "INFO",
            "MASS_REVOCATION_REJECTED",
            f"{admin_label} (ID: {admin_id}) rejected mass revocation {action_id} in '{guild.name}'. All member roles preserved.",
            guild=guild,
            user_id=admin_id,
        )

        return True, f"🛑 Mass revocation `{action_id}` has been rejected. All member roles remain intact."
