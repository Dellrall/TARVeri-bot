"""
Role Discovery, Creation, and Synchronization mixin for VerificationService.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from typing import Any

import discord

from tarveri.config import (
    ALUMNI_ALIASES,
    ALUMNI_ROLE_COLOR,
    ALUMNI_ROLE_NAME,
    ALUMNI_ROLE_PATTERN,
    CAMPUS_ALIASES,
    CAMPUS_COLORS,
    CAMPUS_ROLE_NAMES,
    FACULTY_ALIASES,
    FACULTY_COLORS,
    FACULTY_ROLE_NAMES,
    GUEST_ROLE_COLOR,
    GUEST_ROLE_PATTERN,
    ROLE_QUALIFIER_PATTERN,
    SRC_ROLE_NAMES,
    SRC_ROLES,
    STUDY_LEVEL_ALIASES,
    STUDY_LEVEL_COLORS,
    STUDY_LEVEL_ROLE_NAMES,
)
from tarveri.services.verification.base import BaseVerificationMixin, RoleSyncResult

logger = logging.getLogger("tarveri")


class RoleManagementMixin(BaseVerificationMixin):
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
        ci_matches = [r for r in safe_roles if getattr(r, "name", "").strip().upper() == target_upper]
        if ci_matches:
            return max(ci_matches, key=lambda r: getattr(r, "position", 0))

        # Tier 3: Normalized alphanumeric match
        alnum_matches = [
            r for r in safe_roles if re.sub(r"[^A-Za-z0-9]", "", getattr(r, "name", "")).upper() == target_alnum
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

    async def restore_src_roles(self, guild: discord.Guild, force: bool = False) -> dict[str, Any]:
        """
        Restores / creates the faculty SRC (Student Representative Council) roles if missing.
        FAFB SRC, CPUS SRC, FOCS SRC, FCCI SRC, FOAS SRC, FOBE SRC, FSSH SRC, FOET SRC.
        Guarantees idempotency via per-guild role lock.
        Checks if the server has opted in to SRC roles unless force=True.
        """
        stats: dict[str, Any] = {"created": 0, "existing": 0, "failed": 0, "disabled": False}
        if not guild or not hasattr(guild, "roles"):
            return stats

        # Opt-in guard: skip restoration if guild has not explicitly enabled SRC role management
        if not force and hasattr(self.db, "is_guild_src_roles_enabled"):
            try:
                is_enabled = await self.db.is_guild_src_roles_enabled(guild.id)
                if not is_enabled:
                    guild_roles = list(getattr(guild, "roles", []))
                    existing = sum(
                        1
                        for src_name in SRC_ROLES.values()
                        if any(getattr(r, "name", "").strip().lower() == src_name.lower() for r in guild_roles)
                    )
                    stats["existing"] = existing
                    stats["disabled"] = True
                    return stats
            except Exception as e:
                logger.warning(f"Failed checking SRC role setting for guild {guild.id}: {e}")
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

    async def remove_src_roles(self, guild: discord.Guild) -> dict[str, Any]:
        """
        Deletes all faculty SRC roles from the guild, cleans up bot_created_roles,
        and disables the guild's SRC role setting.
        Guarantees thread-safety via per-guild role lock.
        """
        stats: dict[str, Any] = {"deleted": 0, "failed": 0, "not_found": 0, "roles": []}
        if not guild or not hasattr(guild, "roles"):
            return stats

        can_manage = (
            getattr(guild.me.guild_permissions, "manage_roles", False)
            if hasattr(guild, "me") and hasattr(guild.me, "guild_permissions")
            else False
        )
        if not can_manage:
            stats["failed"] = len(SRC_ROLE_NAMES)
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

            for src_name in SRC_ROLE_NAMES:
                matching_roles = [r for r in guild_roles if getattr(r, "name", "").strip().lower() == src_name.lower()]
                if not matching_roles:
                    stats["not_found"] += 1
                    continue

                for role in matching_roles:
                    try:
                        await role.delete(reason="TARVeri: Administrator removed faculty SRC roles")
                        stats["deleted"] += 1
                        stats["roles"].append(role.name)
                        if hasattr(self.db, "delete_bot_created_role"):
                            try:
                                await self.db.delete_bot_created_role(role.id)
                            except Exception as e:
                                logger.debug(f"Could not remove bot_created_role record {role.id}: {e}")
                    except (discord.Forbidden, discord.HTTPException) as e:
                        stats["failed"] += 1
                        logger.warning(f"Failed to delete SRC role '{role.name}' in '{guild.name}': {e}")

            if hasattr(self.db, "set_guild_src_roles_enabled"):
                await self.db.set_guild_src_roles_enabled(guild.id, False)

            if hasattr(self.db, "log"):
                await self.db.log(
                    "INFO",
                    "SRC_ROLES_REMOVED",
                    f"Removed {stats['deleted']} faculty SRC role(s) from '{guild.name}' (Guild ID: {guild.id})",
                    guild=guild,
                )

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

    async def find_guest_role(self, guild: discord.Guild, configured_name: str | None = None) -> discord.Role | None:
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
                is_guild_email_enforced = await self.db.is_guild_email_enforcement_enabled(guild.id)
                if not is_guild_email_enforced and self.settings:
                    is_guild_email_enforced = getattr(self.settings, "enable_email_role_enforcement", False)

                if is_guild_email_required and not is_email_verified:
                    is_past_verified = bool(await self.db.get_verification_by_user(user_id))
                    if is_guild_email_enforced or not is_past_verified:
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
            r
            for r in member_roles
            if any(
                self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES if fac != role_name
            )
        ]

        me = getattr(guild, "me", None)
        can_manage = (
            getattr(me.guild_permissions, "manage_roles", False) if me and hasattr(me, "guild_permissions") else False
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
                r
                for r in member_roles
                if any(
                    self._match_campus_role_in_list([r], c) is not None
                    for c in CAMPUS_ROLE_NAMES
                    if c != campus_role_name
                )
            ]
            for conf_c in conflicting_campus_roles:
                conf_c_pos = getattr(conf_c, "position", 0)
                if can_manage and not (
                    isinstance(bot_pos, int) and isinstance(conf_c_pos, int) and conf_c_pos >= bot_pos
                ):
                    roles_to_remove.append(conf_c)

            if not has_campus:
                camp_role = await self.get_or_create_campus_role(guild, campus_role_name)
                if camp_role:
                    camp_pos = getattr(camp_role, "position", 0)
                    if can_manage and not (
                        isinstance(bot_pos, int) and isinstance(camp_pos, int) and camp_pos >= bot_pos
                    ):
                        roles_to_add.append(camp_role)
                        camp_name = getattr(camp_role, "name", None)
                        assigned_names.append(
                            camp_name if isinstance(camp_name, str) and camp_name else campus_role_name
                        )
            else:
                assigned_names.append(campus_role_name)

        # 3. Study Level role (if provided)
        if level_role_name:
            has_level = any(
                self._match_study_level_role_in_list([r], level_role_name) is not None for r in member_roles
            )
            conflicting_level_roles = [
                r
                for r in member_roles
                if any(
                    self._match_study_level_role_in_list([r], lvl_name) is not None
                    for lvl_name in STUDY_LEVEL_ROLE_NAMES
                    if lvl_name != level_role_name
                )
            ]
            for conf_l in conflicting_level_roles:
                conf_l_pos = getattr(conf_l, "position", 0)
                if can_manage and not (
                    isinstance(bot_pos, int) and isinstance(conf_l_pos, int) and conf_l_pos >= bot_pos
                ):
                    roles_to_remove.append(conf_l)

            if not has_level:
                lvl_role = await self.get_or_create_study_level_role(guild, level_role_name)
                if lvl_role:
                    lvl_pos = getattr(lvl_role, "position", 0)
                    if can_manage and not (
                        isinstance(bot_pos, int) and isinstance(lvl_pos, int) and lvl_pos >= bot_pos
                    ):
                        roles_to_add.append(lvl_role)
                        lvl_name = getattr(lvl_role, "name", None)
                        assigned_names.append(lvl_name if isinstance(lvl_name, str) and lvl_name else level_role_name)
            else:
                assigned_names.append(level_role_name)

        # Perform role modifications
        roles_modified = False
        if roles_to_remove:
            try:
                await member.remove_roles(
                    *roles_to_remove, reason="TARVeri: Reconcile faculty/campus/level role mismatch"
                )
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
