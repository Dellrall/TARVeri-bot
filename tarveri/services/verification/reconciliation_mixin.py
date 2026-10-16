"""
Guild Membership, Alumni Role, and Duplicate Role Reconciliation mixin for VerificationService.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from tarveri.config import (
    ALUMNI_ROLE_NAME,
    CAMPUS_ROLE_NAMES,
    CAMPUS_ROLES,
    FACULTY_ROLE_NAMES,
    GUEST_ROLE_PATTERN,
    ROLE_QUALIFIER_PATTERN,
    STUDY_LEVEL_ROLE_NAMES,
    STUDY_LEVEL_ROLES,
    resolve_campus_role,
    resolve_faculty_role,
    resolve_study_level_role,
)
from tarveri.services.verification.base import BaseVerificationMixin

logger = logging.getLogger("tarveri")


class ReconciliationMixin(BaseVerificationMixin):
    async def reconcile_verified_members(
        self,
        guild: discord.Guild,
        default_campus: str = "W",
        default_level: str | None = None,
    ) -> dict[str, int]:
        """
        Idempotent Self-Healing reconciler:
        1. Compares SQLite `verifications` table against live guild membership.
        2. Restores missing Faculty, Branch Campus, and Study Level roles to verified members.
        3. Cleans up contradictory faculty roles (e.g. user holds both FOCS and FAFB).
        4. Strips unauthorized roles from members not in `verifications` (rogue role removal).
        5. Protects against race conditions and hierarchy exceptions.
        """
        summary = {
            "checked": 0,
            "restored": 0,
            "unverified_cleaned": 0,
            "unauthorized_cleaned": 0,
            "failed": 0,
        }
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
        if not me and hasattr(guild, "get_member") and hasattr(self.bot, "user") and self.bot.user:
            me = guild.get_member(self.bot.user.id)
        if not me and hasattr(guild, "fetch_member") and hasattr(self.bot, "user") and self.bot.user:
            try:
                me = await guild.fetch_member(self.bot.user.id)
            except Exception:
                me = None
        can_manage = (
            getattr(me.guild_permissions, "manage_roles", False) if me and hasattr(me, "guild_permissions") else False
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
                    r
                    for r in roles_to_strip
                    if not (
                        isinstance(bot_pos, int)
                        and isinstance(getattr(r, "position", 0), int)
                        and getattr(r, "position", 0) >= bot_pos
                    )
                ]
                if strip_manageable and can_manage:
                    email_strip_candidates.append((member, strip_manageable))
                continue

            roles_to_add: list[discord.Role] = []

            # 1. Primary faculty role (robustly resolved from single-letter, 2-letter, or full name)
            target_role_name = resolve_faculty_role(faculty_code)
            has_faculty_role = False
            if target_role_name and isinstance(member_roles, (list, tuple)):
                has_faculty_role = self._match_faculty_role_in_list(member_roles, target_role_name) is not None

                # Clean up any obsolete/conflicting other faculty roles
                conflicting_fac_roles = [
                    r
                    for r in member_roles
                    if any(
                        self._match_faculty_role_in_list([r], fac) is not None
                        for fac in FACULTY_ROLE_NAMES
                        if fac != target_role_name
                    )
                ]
                for conf_r in conflicting_fac_roles:
                    conf_pos = getattr(conf_r, "position", 0)
                    if can_manage and not (
                        isinstance(bot_pos, int) and isinstance(conf_pos, int) and conf_pos >= bot_pos
                    ):
                        try:
                            await member.remove_roles(conf_r, reason="TARVeri: Self-healing faculty role mismatch")
                        except discord.HTTPException:
                            pass

            if not has_faculty_role and target_role_name:
                target_role = await self.get_or_create_faculty_role(guild, target_role_name)
                if target_role:
                    role_pos = getattr(target_role, "position", 0)
                    if can_manage and not (
                        isinstance(bot_pos, int) and isinstance(role_pos, int) and role_pos >= bot_pos
                    ):
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
                c_code = default_campus or "W"

            target_campus_name = resolve_campus_role(c_code)
            if not has_campus_role and target_campus_name:
                target_camp = await self.get_or_create_campus_role(guild, target_campus_name)
                if target_camp:
                    camp_pos = getattr(target_camp, "position", 0)
                    if can_manage and not (
                        isinstance(bot_pos, int) and isinstance(camp_pos, int) and camp_pos >= bot_pos
                    ):
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
                target_level_name = resolve_study_level_role(l_code)
                if target_level_name and not has_level_role:
                    target_lvl = await self.get_or_create_study_level_role(guild, target_level_name)
                    if target_lvl:
                        lvl_pos = getattr(target_lvl, "position", 0)
                        if can_manage and not (
                            isinstance(bot_pos, int) and isinstance(lvl_pos, int) and lvl_pos >= bot_pos
                        ):
                            roles_to_add.append(target_lvl)

            # 4. Alumni role (if marked as alumni)
            if details and details.get("is_alumni"):
                has_alumni_role = self._match_alumni_role_in_list(member_roles) is not None
                if not has_alumni_role:
                    alumni_role = await self.get_or_create_alumni_role(guild)
                    if alumni_role:
                        alumni_pos = getattr(alumni_role, "position", 0)
                        if can_manage and not (
                            isinstance(bot_pos, int) and isinstance(alumni_pos, int) and alumni_pos >= bot_pos
                        ):
                            roles_to_add.append(alumni_role)

            if roles_to_add:
                try:
                    await member.add_roles(
                        *roles_to_add,
                        reason="TARVeri: Self-healing automatic role restoration for verified student",
                    )
                    summary["restored"] += len(roles_to_add)
                    assigned_labels = (
                        ", ".join(
                            [
                                getattr(r, "name", "Role")
                                for r in roles_to_add
                                if isinstance(getattr(r, "name", None), str)
                            ]
                        )
                        or "roles"
                    )
                    await self.db.log(
                        "INFO",
                        "ROLE_RESTORED",
                        f"Self-healing: Restored missing role(s) [{assigned_labels}] to verified student {member} (ID: {discord_user_id})",
                        guild=guild,
                        user_id=discord_user_id,
                    )
                except discord.HTTPException as e:
                    summary["failed"] += len(roles_to_add)
                    logger.warning(f"Failed to restore roles for {member} in '{guild.name}': {e}")

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
                        or any(
                            self._match_study_level_role_in_list([r], lvl) is not None for lvl in STUDY_LEVEL_ROLE_NAMES
                        )
                        or self._match_alumni_role_in_list([r]) is not None
                    ]
                    stray_manageable = [
                        r
                        for r in stray_roles
                        if not (
                            isinstance(bot_pos, int)
                            and isinstance(getattr(r, "position", 0), int)
                            and getattr(r, "position", 0) >= bot_pos
                        )
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
            getattr(me.guild_permissions, "manage_roles", False) if me and hasattr(me, "guild_permissions") else False
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
            getattr(me.guild_permissions, "manage_roles", False) if me and hasattr(me, "guild_permissions") else False
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
            if (
                configured_guest_name
                and r_name.strip().lower() == configured_guest_name.lower()
                or GUEST_ROLE_PATTERN.search(r_name)
            ):
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
        if not bot_perms or not (
            getattr(bot_perms, "manage_threads", False) or getattr(bot_perms, "administrator", False)
        ):
            warnings.append(
                "⚠️ Missing `Manage Threads` permission — cannot lock guest review threads upon approval or dismissal."
            )
        if not bot_perms or not (
            getattr(bot_perms, "create_private_threads", False) or getattr(bot_perms, "administrator", False)
        ):
            warnings.append(
                "❌ Missing `Create Private Threads` permission — cannot open private guest review tickets."
            )

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
            is_managed = any(
                self._match_faculty_role_in_list([r], fac) is not None for fac in FACULTY_ROLE_NAMES
            ) or bool(GUEST_ROLE_PATTERN.search(r_name))
            if is_managed:
                r_pos = getattr(r, "position", 0)
                if isinstance(bot_pos, int) and isinstance(r_pos, int) and r_pos >= bot_pos:
                    bot_name = getattr(bot_top_role, "name", "TARVeri")
                    warnings.append(
                        f"⚠️ Role hierarchy conflict: Role **{r_name}** (pos {r_pos}) is higher than or equal to bot top role **{bot_name}** (pos {bot_pos}). Please drag the bot's role above **{r_name}** in Server Settings → Roles."
                    )

        return warnings
