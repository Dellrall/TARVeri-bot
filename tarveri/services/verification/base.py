"""
Base mixin defining the verification service contract and dependencies.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord
    from discord.ext import commands

    from tarveri.config import Settings
    from tarveri.database import Database
    from tarveri.rate_limiter import RateLimiter
    from tarveri.services.email_service import EmailService


@dataclass(slots=True)
class RoleSyncResult:
    verified_in: list[tuple[int, str, str]] = field(default_factory=list)  # (guild_id, guild_name, role_name)
    already_had_role_in: list[tuple[int, str, str]] = field(default_factory=list)  # (guild_id, guild_name, role_name)
    missing_role_in: list[str] = field(default_factory=list)
    failed_in: list[str] = field(default_factory=list)
    requires_email_in: list[str] = field(default_factory=list)


class BaseVerificationMixin:
    """Base mixin providing type annotations and lock access for verification mixins."""

    bot: commands.Bot
    db: Database
    secret: str
    rate_limiter: RateLimiter
    settings: Settings
    email_service: EmailService | None
    _guild_role_locks: dict[int, asyncio.Lock]

    def _get_guild_role_lock(self, guild_id: int) -> asyncio.Lock:
        if guild_id not in self._guild_role_locks:
            self._guild_role_locks[guild_id] = asyncio.Lock()
        return self._guild_role_locks[guild_id]

    async def get_or_fetch_member(self, guild: discord.Guild, user_id: int) -> discord.Member | None: ...

    async def get_mutual_guilds_for_user(self, user_id: int) -> list[discord.Guild]: ...
