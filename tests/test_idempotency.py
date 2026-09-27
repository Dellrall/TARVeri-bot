import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.cogs.admin_cog import AdminCog
from tarveri.config import SRC_ROLES
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.guest_service import GuestService
from tarveri.services.verification_service import VerificationService


@pytest.mark.asyncio
async def test_concurrent_faculty_role_creation_idempotency(tmp_path):
    """Simulate 10 concurrent coroutines requesting a faculty role; exactly 1 role creation occurs."""
    db = Database(str(tmp_path / "idempotency_faculty.db"))
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = VerificationService(bot, db, "secret", RateLimiter())

    guild = MagicMock(spec=discord.Guild)
    guild.id = 123456789
    guild.name = "Test Campus"
    guild.roles = []
    guild.fetch_roles = AsyncMock(return_value=[])
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True

    created_role = MagicMock(spec=discord.Role)
    created_role.id = 999001
    created_role.name = "FOAS"
    created_role.position = 1

    async def mock_create_role(**kwargs):
        # Simulate network latency in Discord API
        await asyncio.sleep(0.01)
        guild.roles.append(created_role)
        return created_role

    guild.create_role = AsyncMock(side_effect=mock_create_role)

    # Launch 10 concurrent tasks trying to create the FOAS role
    tasks = [service.get_or_create_faculty_role(guild, "FOAS") for _ in range(10)]
    results = await asyncio.gather(*tasks)

    # All returned roles should be the single created role instance
    for r in results:
        assert r == created_role

    # create_role should have been invoked exactly ONCE
    assert guild.create_role.call_count == 1

    await db.close()


@pytest.mark.asyncio
async def test_concurrent_guest_role_creation_idempotency(tmp_path):
    """Simulate 10 concurrent coroutines requesting a guest role; exactly 1 role creation occurs."""
    db = Database(str(tmp_path / "idempotency_guest.db"))
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 987654321
    guild.name = "Test Campus"
    guild.roles = []
    guild.fetch_roles = AsyncMock(return_value=[])
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True

    created_guest_role = MagicMock(spec=discord.Role)
    created_guest_role.id = 888001
    created_guest_role.name = "Guest(Approved)"
    created_guest_role.position = 1

    async def mock_create_role(**kwargs):
        await asyncio.sleep(0.01)
        guild.roles.append(created_guest_role)
        return created_guest_role

    guild.create_role = AsyncMock(side_effect=mock_create_role)

    # Launch 10 concurrent tasks
    tasks = [service.get_or_create_guest_role(guild) for _ in range(10)]
    results = await asyncio.gather(*tasks)

    for r in results:
        assert r == created_guest_role

    assert guild.create_role.call_count == 1

    await db.close()


@pytest.mark.asyncio
async def test_concurrent_src_roles_restoration_idempotency(tmp_path):
    """Simulate concurrent restore_src_roles calls; no duplicated SRC roles created."""
    db = Database(str(tmp_path / "idempotency_src.db"))
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = VerificationService(bot, db, "secret", RateLimiter())

    guild = MagicMock(spec=discord.Guild)
    guild.id = 555444333
    guild.name = "SRC Test Campus"
    guild.roles = []
    guild.fetch_roles = AsyncMock(side_effect=lambda: list(guild.roles))
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True

    role_counter = 1000

    async def mock_create_role(**kwargs):
        nonlocal role_counter
        await asyncio.sleep(0.005)
        role_counter += 1
        r = MagicMock(spec=discord.Role)
        r.id = role_counter
        r.name = kwargs["name"]
        guild.roles.append(r)
        return r

    guild.create_role = AsyncMock(side_effect=mock_create_role)

    # Run 5 concurrent restorations
    tasks = [service.restore_src_roles(guild) for _ in range(5)]
    await asyncio.gather(*tasks)

    # Total created across all tasks should equal number of SRC roles (8)
    assert guild.create_role.call_count == len(SRC_ROLES)
    assert len(guild.roles) == len(SRC_ROLES)

    await db.close()


@pytest.mark.asyncio
async def test_guest_ticket_resolution_cas_idempotency(tmp_path):
    """Simulate two racing admins approving/rejecting the same ticket at the same moment."""
    db = Database(str(tmp_path / "idempotency_ticket.db"))
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 112233
    guild.name = "Campus"
    guild.roles = []
    guild.fetch_roles = AsyncMock(return_value=[])
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.guild_permissions.kick_members = True

    guest_role = MagicMock(spec=discord.Role)
    guest_role.name = "Guest(Approved)"
    guest_role.position = 1
    guild.roles.append(guest_role)

    applicant = MagicMock(spec=discord.Member)
    applicant.id = 5001
    applicant.top_role.position = 0
    applicant.add_roles = AsyncMock()
    applicant.send = AsyncMock()
    applicant.kick = AsyncMock()
    guild.get_member = MagicMock(return_value=applicant)

    admin_a = MagicMock(spec=discord.Member)
    admin_a.id = 9001
    admin_a.mention = "<@9001>"

    admin_b = MagicMock(spec=discord.Member)
    admin_b.id = 9002
    admin_b.mention = "<@9002>"

    # Create ticket in DB
    ticket_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=applicant.id,
        referrer_id=7001,
        channel_id=8001,
        reason="Study buddy",
    )
    ticket = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket is not None
    assert ticket["status"] == "OPEN"

    # Simulate race: Admin A approves and Admin B rejects simultaneously
    res_a, res_b = await asyncio.gather(
        service.approve_guest_application(ticket, guild, admin_a, reason="Approved by Admin A"),
        service.reject_guest_application(ticket, guild, admin_b, reason="Rejected by Admin B"),
    )

    successes = [r for r in [res_a, res_b] if r[0] is True]
    failures = [r for r in [res_a, res_b] if r[0] is False]

    # Exactly ONE admin operation succeeded
    assert len(successes) == 1
    assert len(failures) == 1

    # The failing operation notified that the ticket was already resolved
    assert "already resolved" in failures[0][1]

    # Check DB status is finalized
    final_ticket = await db.get_guest_ticket_by_id(ticket_id)
    assert final_ticket["status"] in ("APPROVED", "REJECTED")

    await db.close()


@pytest.mark.asyncio
async def test_admin_unverify_idempotent_multiple_runs(tmp_path):
    """Admin unverify can be run repeatedly without error on any state."""
    db = Database(str(tmp_path / "idempotency_unverify.db"))
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    service.get_mutual_guilds_for_user = AsyncMock(return_value=[])
    service.get_or_fetch_member = AsyncMock(return_value=None)
    rate_limiter = RateLimiter(max_attempts=3, window_seconds=60)
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.name = "Campus Guild"
    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    target_user = MagicMock(spec=discord.User)
    target_user.id = 999888
    target_user.mention = "<@999888>"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1st call on unverified user
    await cog.unverify.callback(cog, interaction, user=target_user)
    assert "is not verified" in interaction.followup.send.call_args[0][0]

    # Now verify user
    await db.record_verification(target_user.id, "hash_idem", "M")
    member = MagicMock(spec=discord.Member)
    faculty_role = MagicMock(spec=discord.Role)
    faculty_role.name = "FOCS"
    member.roles = [faculty_role]
    member.remove_roles = AsyncMock()
    service.get_mutual_guilds_for_user = AsyncMock(return_value=[guild])
    service.get_or_fetch_member = AsyncMock(return_value=member)

    # 2nd call: unverified with active roles
    interaction.followup.send.reset_mock()
    await cog.unverify.callback(cog, interaction, user=target_user)
    assert "Successfully unverified" in interaction.followup.send.call_args[0][0]
    assert await db.get_verification_by_user(target_user.id) is None

    # 3rd call immediately after (idempotent repeat on cleared user)
    member.roles = []
    interaction.followup.send.reset_mock()
    await cog.unverify.callback(cog, interaction, user=target_user)
    assert "is not verified" in interaction.followup.send.call_args[0][0]

    await db.close()
