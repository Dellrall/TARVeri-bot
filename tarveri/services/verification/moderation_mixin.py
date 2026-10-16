"""
Student Unverification, Alumni Revocation, Blacklist, Security Alerts, and Mass Action mixin.
"""

from __future__ import annotations

import inspect
import logging
import re
from datetime import datetime
from typing import Any

import discord

from tarveri.config import (
    CAMPUS_ROLE_NAMES,
    FACULTY_ROLE_NAMES,
    FACULTY_ROLES,
    GUEST_ROLE_PATTERN,
    STUDY_LEVEL_ROLE_NAMES,
    get_configured_tz,
    hash_email,
    hash_student_id,
    mask_email,
    mask_student_id,
)
from tarveri.services.verification.base import BaseVerificationMixin

logger = logging.getLogger("tarveri")


class ModerationAndRevocationMixin(BaseVerificationMixin):
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
                if can_manage and isinstance(bot_pos, int) and isinstance(role_pos, int) and role_pos < bot_pos:
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
            r
            for r in roles_to_remove
            if can_manage
            and isinstance(bot_pos, int)
            and isinstance(getattr(r, "position", 0), int)
            and getattr(r, "position", 0) < bot_pos
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

        strip_msg = (
            f" Stripped {len(stripped_roles)} role(s) from {target_member.mention}."
            if stripped_roles and target_member
            else ""
        )
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
            if guild_perms and (
                getattr(guild_perms, "manage_channels", False) or getattr(guild_perms, "administrator", False)
            ):
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
                if role_perms and (
                    getattr(role_perms, "administrator", False) or getattr(role_perms, "manage_guild", False)
                ):
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
            dlq = getattr(self.bot, "dead_letter_service", None)
            if dlq and hasattr(dlq, "enqueue_security_alert"):
                try:
                    res = dlq.enqueue_security_alert(
                        guild_id=guild.id,
                        embed_data=embed.to_dict(),
                        error_reason=str(exc),
                    )
                    if inspect.isawaitable(res):
                        await res
                except Exception as dlq_err:
                    logger.debug("Failed enqueueing security alert to DLQ: %s", dlq_err)
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
                r
                for r in roles_to_strip
                if not (
                    isinstance(bot_pos, int)
                    and isinstance(getattr(r, "position", 0), int)
                    and getattr(r, "position", 0) >= bot_pos
                )
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

        return (
            True,
            f"✅ Successfully executed mass revocation `{action_id}` ({removed_count} roles stripped from {len(user_ids)} members).",
        )

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
