"""
Verification business logic, concurrency control, and cross-guild role synchronization.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import aiosqlite
import discord

from tarveri.config import (
    FACULTY_ROLES,
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
    resolve_faculty_role,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.role_manager import RoleManager
from tarveri.services.verification import (
    AcademicLifecycleMixin,
    ModerationAndRevocationMixin,
    ReconciliationMixin,
    RoleManagementMixin,
)

logger = logging.getLogger("tarveri")


@dataclass(slots=True)
class RoleSyncResult:
    verified_in: list[tuple[int, str, str]] = field(default_factory=list)  # (guild_id, guild_name, role_name)
    already_had_role_in: list[tuple[int, str, str]] = field(default_factory=list)  # (guild_id, guild_name, role_name)
    missing_role_in: list[str] = field(default_factory=list)
    failed_in: list[str] = field(default_factory=list)
    requires_email_in: list[str] = field(default_factory=list)


class VerificationService(
    RoleManagementMixin,
    AcademicLifecycleMixin,
    ReconciliationMixin,
    ModerationAndRevocationMixin,
):
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
                    alert_embed.add_field(
                        name="⚡ Action Taken",
                        value="Verification blocked immediately. No roles assigned.",
                        inline=False,
                    )
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
                final_email_hash = email_hash or (
                    existing_details.get("student_email_hash") if existing_details else None
                )
                is_user_email_verified = bool(final_email_hash)

                mutual_guilds = await self.get_mutual_guilds_for_user(user.id)
                assigned_faculty_role = resolve_faculty_role(stored_faculty) or role_name
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
                        programme_code=info.programme_code,
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
                            programme_code=info.programme_code,
                        )
                        active_servers = [f"{g_name} ({r_name})" for _, g_name, r_name in sync_result.verified_in]
                        await self.db.log(
                            "INFO",
                            "VERIFIED",
                            f"Verified {user} (ID: {user.id}) as {role_name} "
                            f"(Campus: {campus_role_name or 'KL Main Campus'}, Level: {level_role_name or 'Degree'}) "
                            f"in {len(sync_result.verified_in)} server(s): {', '.join(active_servers)}",
                            user_id=user.id,
                            guild=guild_ctx,
                        )
                except (sqlite3.IntegrityError, aiosqlite.IntegrityError) as exc:
                    logger.critical("Database collision on record_verification: %s", exc)
                    # Rollback assigned roles
                    target_names = {role_name, campus_role_name, level_role_name}
                    target_names.discard(None)
                    for g_id, _, r_label in sync_result.verified_in:
                        guild_obj = self.bot.get_guild(g_id) if hasattr(self.bot, "get_guild") else None
                        if not guild_obj and hasattr(self.bot, "guilds"):
                            for g in self.bot.guilds:
                                if getattr(g, "id", None) == g_id:
                                    guild_obj = g
                                    break
                        if guild_obj:
                            member_obj = await self.get_or_fetch_member(guild_obj, user.id)
                            if member_obj:
                                roles_to_remove = [
                                    r
                                    for r in getattr(member_obj, "roles", [])
                                    if getattr(r, "name", "") in target_names
                                    or (isinstance(getattr(r, "name", None), str) and getattr(r, "name", "") in r_label)
                                ]
                                if roles_to_remove:
                                    try:
                                        await member_obj.remove_roles(
                                            *roles_to_remove, reason="TARVeri: Database collision rollback"
                                        )
                                    except Exception as exc:
                                        logger.debug("Failed removing roles during rollback: %s", exc)
                                else:
                                    try:
                                        await member_obj.remove_roles(reason="TARVeri: Database collision rollback")
                                    except Exception as exc:
                                        logger.debug("Failed removing roles during rollback fallback: %s", exc)
                    return "⚠️ Verification failed due to a collision. Please try again or contact an admin."
                except Exception as exc:
                    logger.critical("Failed to record verification in DB: %s", exc, exc_info=True)
                    return "⚠️ Verification recorded locally, but a database error occurred. Contact an admin."

                summary = self.format_role_summary(sync_result) or f"✅ You've been verified as **{role_name}**!"

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
            elif sync_result.requires_email_in:
                servers_str = ", ".join(f"**{g_name}**" for g_name in sync_result.requires_email_in)
                return (
                    f"📧 Institutional email verification is required in {servers_str}.\n"
                    "Please provide your institutional email (`@student.tarc.edu.my`) to complete verification."
                )
            else:
                if sync_result.missing_role_in:
                    failed_names = ", ".join(f"**{name}**" for name in sync_result.missing_role_in)
                    return (
                        f"⚠️ Verified your student ID as **{role_name}**, but I couldn't create/find the required role in {failed_names}. "
                        "Please contact a server administrator to check role permissions."
                    )
                return (
                    f"⚠️ Verified your student ID as **{role_name}**, but could not assign roles. "
                    "Please contact a server administrator to check role permissions."
                )
        finally:
            async with self._lock:
                self._in_flight_users.discard(user.id)
