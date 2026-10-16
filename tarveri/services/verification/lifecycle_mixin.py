"""
Academic Progression, Further Study, and Dropout Lifecycle mixin for VerificationService.
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from tarveri.config import (
    CAMPUS_ROLE_NAMES,
    CAMPUS_ROLES,
    FACULTY_ROLE_NAMES,
    FACULTY_ROLES,
    STUDY_LEVEL_ROLE_NAMES,
    STUDY_LEVEL_ROLES,
    StudentIdInfo,
    estimate_student_card_expiry,
    mask_student_id,
    parse_card_expiry_date,
)
from tarveri.services.verification.base import BaseVerificationMixin, RoleSyncResult

logger = logging.getLogger("tarveri")


class AcademicLifecycleMixin(BaseVerificationMixin):
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
            lines.extend(
                f"   • **{g}** (I likely need 'Manage Roles' permission there)" for g in result.missing_role_in
            )
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
            programme_code=info.programme_code,
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
            lines.append(
                f"🏷️ Updated roles in: {', '.join([f'**{e[1] if len(e) == 3 else e[0]}**' for e in sync_result.verified_in])}"
            )
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
