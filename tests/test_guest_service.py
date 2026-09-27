from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.database import Database
from tarveri.services.guest_service import GuestService, generate_code_string


@pytest.mark.asyncio
async def test_generate_code_format():
    code = generate_code_string()
    assert code.startswith("TAR-")
    assert len(code) == 10  # "TAR-" + 6 chars
    # Ensure no ambiguous characters
    for ch in "0O1I":
        assert ch not in code


@pytest.mark.asyncio
async def test_create_and_validate_referral_code(tmp_path):
    db_path = str(tmp_path / "guest_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    user = MagicMock(spec=discord.Member)
    user.id = 12345
    user.__str__.return_value = "Student#1234"

    guild_id = 999888

    # 1. Create first referral code
    success, code = await service.create_referral_code(guild_id, user, ttl_hours=24, max_active=2)
    assert success is True
    assert code.startswith("TAR-")

    # 2. Validate the referral code
    is_valid, err, record = await service.validate_referral_code(guild_id, code)
    assert is_valid is True
    assert err == ""
    assert record["referrer_discord_id"] == 12345
    assert record["status"] == "ACTIVE"

    # 3. Create second referral code
    success2, code2 = await service.create_referral_code(guild_id, user, ttl_hours=24, max_active=2)
    assert success2 is True

    # 4. Third referral code should exceed rate limit (max_active=2)
    success3, err3 = await service.create_referral_code(guild_id, user, ttl_hours=24, max_active=2)
    assert success3 is False
    assert "maximum allowed" in err3

    # 5. Invalid code validation
    is_valid_fake, err_fake, _ = await service.validate_referral_code(guild_id, "TAR-INVALID")
    assert is_valid_fake is False
    assert "Invalid, expired, or already used" in err_fake

    # 6. Wrong guild code validation
    is_valid_wrong_guild, err_wg, _ = await service.validate_referral_code(111222, code)
    assert is_valid_wrong_guild is False

    await db.close()


@pytest.mark.asyncio
async def test_guest_ticket_approval_and_rejection_lifecycle(tmp_path):
    db_path = str(tmp_path / "ticket_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 555666
    guild.name = "Test Server"
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.guild_permissions.kick_members = True
    guild.me.top_role = MagicMock()

    guest_role = MagicMock(spec=discord.Role)
    guest_role.name = "Guest"
    guild.roles = [guest_role]

    admin_user = MagicMock(spec=discord.Member)
    admin_user.id = 999
    admin_user.mention = "<@999>"

    applicant = MagicMock(spec=discord.Member)
    applicant.id = 888
    applicant.name = "Friend"
    applicant.display_name = "Friend"
    applicant.top_role = MagicMock()
    applicant.top_role.__lt__.return_value = True  # applicant role < bot top role
    applicant.add_roles = AsyncMock()
    applicant.send = AsyncMock()
    applicant.kick = AsyncMock()

    guild.get_member.side_effect = lambda uid: applicant if uid == 888 else None
    guild.fetch_member = AsyncMock(return_value=applicant)

    # 1. Create a referral code
    referrer = MagicMock(spec=discord.Member)
    referrer.id = 777
    referrer.__str__.return_value = "Referrer#0001"
    _, code = await service.create_referral_code(guild.id, referrer, ttl_hours=48)

    # Mock parent review channel and thread creation
    parent_channel = MagicMock(spec=discord.TextChannel)
    parent_channel.name = "approvals"
    perms = MagicMock()
    perms.view_channel = True
    perms.create_private_threads = True
    perms.send_messages_in_threads = True
    parent_channel.permissions_for.return_value = perms
    parent_channel.set_permissions = AsyncMock()
    parent_channel.overwrites_for = MagicMock(return_value=MagicMock())

    thread = MagicMock(spec=discord.Thread)
    thread.id = 444111
    thread.mention = "<#444111>"
    thread.parent = parent_channel
    thread.add_user = AsyncMock()
    parent_channel.create_thread = AsyncMock(return_value=thread)
    guild.text_channels = [parent_channel]
    guild.get_thread = MagicMock(return_value=thread)

    # Open review ticket with referral code
    success, msg, created_thread = await service.open_guest_review_ticket(
        guild=guild,
        applicant=applicant,
        referral_code=code,
    )
    assert success is True
    assert created_thread == thread
    assert "#A0001" in msg
    assert parent_channel.create_thread.call_args[1]["name"].startswith("guest-a0001-")
    thread.add_user.assert_called()
    parent_channel.set_permissions.assert_called()

    # Code should now be PENDING_APPROVAL
    ref_record = await db.get_referral_code(code, guild.id)
    assert ref_record["status"] == "PENDING_APPROVAL"

    # Ticket in DB
    ticket = await db.get_guest_ticket_by_channel(thread.id)
    assert ticket is not None
    assert ticket["status"] == "OPEN"
    assert ticket["applicant_id"] == 888
    assert ticket["referrer_id"] == 777

    # Vouch
    await db.update_guest_ticket_vouch(ticket["ticket_id"], "Verified friend from college")
    ticket_vouched = await db.get_guest_ticket_by_id(ticket["ticket_id"])
    assert ticket_vouched["vouch_note"] == "Verified friend from college"

    # Approve application
    parent_channel.set_permissions.reset_mock()
    app_success, app_msg = await service.approve_guest_application(ticket, guild, admin_user)
    assert app_success is True
    applicant.add_roles.assert_called_once_with(guest_role, reason=f"TARVeri: Guest approved by {admin_user}")
    applicant.send.assert_called_once()
    assert "approved" in applicant.send.call_args[0][0].lower()
    parent_channel.set_permissions.assert_any_call(applicant, overwrite=None, reason="TARVeri: Review ticket closed")

    # Code and ticket should now be marked USED / APPROVED
    ref_record_after = await db.get_referral_code(code, guild.id)
    assert ref_record_after["status"] == "USED"

    ticket_after = await db.get_guest_ticket_by_id(ticket["ticket_id"])
    assert ticket_after["status"] == "APPROVED"
    assert ticket_after["closed_by_admin_id"] == 999

    # Test Rejection flow on a second ticket
    _, code_rej = await service.create_referral_code(guild.id, referrer, ttl_hours=48)
    applicant2 = MagicMock(spec=discord.Member)
    applicant2.id = 666
    applicant2.display_name = "Spammer"
    applicant2.top_role = MagicMock()
    applicant2.top_role.__lt__.return_value = True
    applicant2.add_roles = AsyncMock()
    applicant2.send = AsyncMock()
    applicant2.kick = AsyncMock()
    guild.get_member.side_effect = lambda uid: applicant2 if uid == 666 else None
    guild.fetch_member = AsyncMock(return_value=applicant2)

    thread2 = MagicMock(spec=discord.Thread)
    thread2.id = 444222
    parent_channel.create_thread = AsyncMock(return_value=thread2)

    await service.open_guest_review_ticket(guild=guild, applicant=applicant2, referral_code=code_rej)
    ticket2 = await db.get_guest_ticket_by_channel(thread2.id)

    rej_success, rej_msg = await service.reject_guest_application(
        ticket2, guild, admin_user, reason="Suspicious account"
    )
    assert rej_success is True
    applicant2.send.assert_called_once()
    assert "Suspicious account" in applicant2.send.call_args[0][0]
    applicant2.kick.assert_called_once()

    ticket2_after = await db.get_guest_ticket_by_id(ticket2["ticket_id"])
    assert ticket2_after["status"] == "REJECTED"

    await db.close()


@pytest.mark.asyncio
async def test_get_or_create_guest_role_finds_existing(tmp_path):
    db_path = str(tmp_path / "role_reuse_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 112233
    guild.name = "Role Test Guild"

    # Server already has an existing "Guest(Approved)" role
    existing_guest_role = MagicMock(spec=discord.Role)
    existing_guest_role.name = "Guest(Approved)"
    guild.roles = [existing_guest_role]
    guild.create_role = AsyncMock()

    found_role = await service.get_or_create_guest_role(guild)
    assert found_role == existing_guest_role
    # Should not have called create_role because existing role was found
    guild.create_role.assert_not_called()

    # If role doesn't exist, create_role is called
    guild.roles = []
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    new_role = MagicMock(spec=discord.Role)
    new_role.name = "Guest(Approved)"
    guild.create_role = AsyncMock(return_value=new_role)

    created = await service.get_or_create_guest_role(guild)
    assert created == new_role
    guild.create_role.assert_called_once()
    assert guild.create_role.call_args[1]["name"] == "Guest(Approved)"

    await db.close()


@pytest.mark.asyncio
async def test_handle_member_leave_or_ban_revokes_guest_access(tmp_path):
    db_path = str(tmp_path / "guest_leave_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 777888
    guild.name = "Leave Test Guild"

    member = MagicMock(spec=discord.Member)
    member.id = 554433
    member.__str__.return_value = "GuestUser#1234"

    thread = MagicMock(spec=discord.Thread)
    thread.id = 999111
    thread.archived = False
    thread.locked = False
    thread.send = AsyncMock()
    thread.edit = AsyncMock()
    thread.starter_message = MagicMock()
    thread.starter_message.edit = AsyncMock()
    guild.get_thread.return_value = thread

    # Create ticket and referral code
    ticket_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=member.id,
        channel_id=999111,
        reason="Attending workshop",
    )
    await db.create_referral_code("TAR-LEAVE1", guild.id, member.id, "2099-01-01 00:00:00")

    ticket_before = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_before["status"] == "OPEN"

    # 1. User leaves server
    await service.handle_member_leave_or_ban(guild, member, is_ban=False)

    ticket_after = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_after["status"] == "LEFT_SERVER"

    ref_code = await db.get_referral_code("TAR-LEAVE1", guild.id)
    assert ref_code["status"] == "LEFT_SERVER"
    thread.send.assert_called_once()
    assert "left the server" in thread.send.call_args[0][0]
    thread.edit.assert_called_once_with(archived=True, locked=True, reason="TARVeri: Applicant left server")

    # 2. User gets banned
    thread2 = MagicMock(spec=discord.Thread)
    thread2.id = 999222
    thread2.archived = False
    thread2.locked = False
    thread2.send = AsyncMock()
    thread2.edit = AsyncMock()
    guild.get_thread.return_value = thread2

    ticket_id2 = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=member.id,
        channel_id=999222,
        reason="Attending workshop 2",
    )
    await service.handle_member_leave_or_ban(guild, member, is_ban=True)

    ticket_after_ban = await db.get_guest_ticket_by_id(ticket_id2)
    assert ticket_after_ban["status"] == "BANNED"
    thread2.send.assert_called_once()
    assert "banned from the server" in thread2.send.call_args[0][0]
    thread2.edit.assert_called_once_with(archived=True, locked=True, reason="TARVeri: Applicant banned from server")

    await db.close()


@pytest.mark.asyncio
async def test_referral_edge_scenarios(tmp_path):
    """Tests all edge scenarios for referral codes and guest review tickets."""
    db_path = str(tmp_path / "edge_scenarios.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock(spec=discord.Client)
    service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 111999
    guild.name = "Edge Guild"
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.guild_permissions.kick_members = True

    guest_role = MagicMock(spec=discord.Role)
    guest_role.name = "Guest"
    guild.roles = [guest_role]

    parent_channel = MagicMock(spec=discord.TextChannel)
    parent_channel.name = "guest-review"
    perms = MagicMock()
    perms.view_channel = True
    perms.create_private_threads = True
    parent_channel.permissions_for.return_value = perms
    guild.text_channels = [parent_channel]

    student_referrer = MagicMock(spec=discord.Member)
    student_referrer.id = 10001
    student_referrer.roles = []
    student_referrer.display_name = "StudentReferrer"
    student_referrer.__str__.return_value = "StudentReferrer#0001"

    # Edge Scenario 1: Code normalization (spaces, lowercase, missing TAR- prefix)
    _, ref_code = await service.create_referral_code(guild.id, student_referrer, ttl_hours=24)
    # Extract raw 6-char part
    raw_part = ref_code.replace("TAR-", "")

    # Test lowercase with whitespace
    is_valid1, _, rec1 = await service.validate_referral_code(guild.id, f"  {ref_code.lower()}  ")
    assert is_valid1 is True
    assert rec1["code"] == ref_code

    # Test without TAR- prefix
    is_valid2, _, rec2 = await service.validate_referral_code(guild.id, raw_part.lower())
    assert is_valid2 is True
    assert rec2["code"] == ref_code

    # Edge Scenario 2: Self-referral prevention
    success_self, msg_self, _ = await service.open_guest_review_ticket(
        guild=guild,
        applicant=student_referrer,
        referral_code=ref_code,
    )
    assert success_self is False
    assert "cannot use your own referral code" in msg_self.lower()

    # Edge Scenario 3: Already-verified student applying for guest
    # Record verification for student in DB
    await db.record_verification(10001, "hash10001", "M")
    success_verif, msg_verif, _ = await service.open_guest_review_ticket(
        guild=guild,
        applicant=student_referrer,
        reason="I want guest role",
    )
    assert success_verif is False
    assert "already verified as a tarumt student" in msg_verif.lower()

    # Edge Scenario 4: User already has Guest role
    existing_guest = MagicMock(spec=discord.Member)
    existing_guest.id = 20002
    existing_guest.roles = [guest_role]
    existing_guest.display_name = "AlreadyGuest"

    success_guest, msg_guest, _ = await service.open_guest_review_ticket(
        guild=guild,
        applicant=existing_guest,
        referral_code=ref_code,
    )
    assert success_guest is False
    assert "already have the" in msg_guest.lower()

    # Edge Scenario 5: Referrer leaves or gets banned -> cancels open tickets referred by them
    applicant_friend = MagicMock(spec=discord.Member)
    applicant_friend.id = 30003
    applicant_friend.roles = []
    applicant_friend.display_name = "FriendGuest"

    thread = MagicMock(spec=discord.Thread)
    thread.id = 888111
    thread.mention = "<#888111>"
    thread.add_user = AsyncMock()
    parent_channel.create_thread = AsyncMock(return_value=thread)

    # Valid friend opens review ticket
    success_friend, _, friend_thread = await service.open_guest_review_ticket(
        guild=guild,
        applicant=applicant_friend,
        referral_code=ref_code,
    )
    assert success_friend is True

    ticket_friend = await db.get_guest_ticket_by_channel(thread.id)
    assert ticket_friend["status"] == "OPEN"
    assert ticket_friend["referrer_id"] == student_referrer.id

    # Referrer gets banned
    await service.handle_member_leave_or_ban(guild, student_referrer, is_ban=True)

    # Friend's ticket should now be cancelled/revoked
    ticket_friend_after = await db.get_guest_ticket_by_channel(thread.id)
    assert ticket_friend_after["status"] == "REVOKED"
    assert "banned" in ticket_friend_after["close_reason"].lower()

    # Referral code should also be revoked
    code_record = await db.get_referral_code(ref_code, guild.id)
    assert code_record["status"] in ("BANNED", "REVOKED", "PENDING_APPROVAL")

    # Edge Scenario 6: Expired referral code
    _, exp_code = await service.create_referral_code(guild.id, student_referrer, ttl_hours=24)
    # Manually set expired timestamp
    await db._conn.execute("UPDATE referral_codes SET expires_at = '2020-01-01 00:00:00' WHERE code = ?", (exp_code,))
    await db._conn.commit()

    await db.close()


@pytest.mark.asyncio
async def test_get_admin_role_or_fallback_and_auto_invite(tmp_path):
    db_path = str(tmp_path / "admin_discovery_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 9988
    guild.name = "Discovery Guild"

    # 1. Test standard alias discovery ("Staff")
    staff_role = MagicMock(spec=discord.Role)
    staff_role.name = "Staff"
    staff_admin_member = MagicMock(spec=discord.Member)
    staff_admin_member.id = 7001
    staff_role.is_default.return_value = False
    guild.roles = [staff_role]

    discovered = await service.get_admin_role_or_fallback(guild)
    assert discovered == staff_role

    # Test DB configured custom admin role overrides alias
    custom_role = MagicMock(spec=discord.Role)
    custom_role.name = "Custom Reviewers"
    custom_admin_member = MagicMock(spec=discord.Member)
    custom_admin_member.id = 7002
    custom_role.members = [custom_admin_member]
    guild.roles = [staff_role, custom_role]

    await db.set_guild_admin_role(guild.id, "Custom Reviewers")
    discovered_custom = await service.get_admin_role_or_fallback(guild)
    assert discovered_custom == custom_role

    # 2. Test auto-inviting multiple admin members (role member + admin permission + owner) to private review thread
    parent_channel = MagicMock(spec=discord.TextChannel)
    parent_channel.name = "guest-tickets"
    perms = MagicMock()
    perms.view_channel = True
    perms.create_private_threads = True
    parent_channel.permissions_for.return_value = perms
    guild.text_channels = [parent_channel]

    thread = MagicMock(spec=discord.Thread)
    thread.id = 888777
    thread.add_user = AsyncMock()
    parent_channel.create_thread = AsyncMock(return_value=thread)

    applicant = MagicMock(spec=discord.Member)
    applicant.id = 6001
    applicant.roles = []
    applicant.display_name = "NewGuest"

    # Server owner
    owner_member = MagicMock(spec=discord.Member)
    owner_member.id = 9001
    guild.owner = owner_member

    # Administrator permission member
    admin_perm_member = MagicMock(spec=discord.Member)
    admin_perm_member.id = 9002
    admin_perm_member.bot = False
    admin_perm_perms = MagicMock()
    admin_perm_perms.administrator = True
    admin_perm_perms.manage_guild = False
    admin_perm_perms.manage_threads = False
    admin_perm_member.guild_permissions = admin_perm_perms
    admin_perm_member.roles = []

    guild.members = [applicant, custom_admin_member, owner_member, admin_perm_member]

    success, msg, created_thread = await service.open_guest_review_ticket(
        guild=guild,
        applicant=applicant,
        reason="Testing admin auto-invite",
    )
    assert success is True

    # Check that applicant, custom_admin_member, owner_member, and admin_perm_member were all added to thread
    added_user_ids = [call.args[0].id for call in thread.add_user.call_args_list]
    assert applicant.id in added_user_ids
    assert custom_admin_member.id in added_user_ids
    assert owner_member.id in added_user_ids
    assert admin_perm_member.id in added_user_ids

    await db.close()


@pytest.mark.asyncio
async def test_admin_online_detection_and_authority_tagging(tmp_path):
    db_path = str(tmp_path / "online_tag_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 7788
    guild.name = "Online Test Guild"
    guild.owner_id = 9991

    # 1. Setup owner (Offline)
    owner = MagicMock(spec=discord.Member)
    owner.id = 9991
    owner.bot = False
    owner.mention = "<@9991>"
    owner.status = discord.Status.offline
    guild.owner = owner

    # 2. Setup Senior Admin (Offline)
    senior_admin = MagicMock(spec=discord.Member)
    senior_admin.id = 9992
    senior_admin.bot = False
    senior_admin.mention = "<@9992>"
    senior_admin.status = discord.Status.offline
    senior_perms = MagicMock()
    senior_perms.administrator = True
    senior_perms.manage_guild = True
    senior_admin.guild_permissions = senior_perms
    senior_role = MagicMock(spec=discord.Role)
    senior_role.position = 50
    senior_admin.top_role = senior_role
    senior_admin.roles = []

    # 3. Setup Moderator / Junior Admin (Online / DND)
    online_mod = MagicMock(spec=discord.Member)
    online_mod.id = 9993
    online_mod.bot = False
    online_mod.mention = "<@9993>"
    online_mod.status = discord.Status.online
    mod_perms = MagicMock()
    mod_perms.administrator = False
    mod_perms.manage_guild = True
    mod_perms.manage_threads = True
    online_mod.guild_permissions = mod_perms
    mod_role = MagicMock(spec=discord.Role)
    mod_role.position = 20
    online_mod.top_role = mod_role
    online_mod.roles = []

    guild.members = [owner, senior_admin, online_mod]
    guild.roles = [senior_role, mod_role]

    # Test 1: When online_mod is online, prioritizing active staff in batch of 2
    tag_1 = await service.get_target_admin_mention(guild, count=1)
    assert tag_1 == "<@9993>"
    tag_2 = await service.get_target_admin_mention(guild, count=2)
    assert tag_2 == "<@9993>, <@9991>"

    # Test 2: When NO admin is online (online_mod goes offline), tag top 2 highest authority (Owner: 9991, Senior: 9992)
    online_mod.status = discord.Status.offline
    tag_all_offline_single = await service.get_target_admin_mention(guild, count=1)
    assert tag_all_offline_single == "<@9991>"
    tag_all_offline = await service.get_target_admin_mention(guild, count=2)
    assert tag_all_offline == "<@9991>, <@9992>"

    # Test 3: If owner is excluded, tag highest authority among remaining (Senior Admin: 9992, Mod: 9993)
    tag_excluded_owner = await service.get_target_admin_mention(guild, exclude_ids={9991}, count=2)
    assert tag_excluded_owner == "<@9992>, <@9993>"

    # Test 4: Multiple online admins (both senior_admin and online_mod online)
    senior_admin.status = discord.Status.idle
    online_mod.status = discord.Status.dnd
    tag_multiple_online = await service.get_target_admin_mention(guild, exclude_ids={9991}, count=2)
    # Ordered by authority: senior_admin first, then online_mod
    assert tag_multiple_online == "<@9992>, <@9993>"

    await db.close()


@pytest.mark.asyncio
async def test_ticket_escalation_lifecycle(tmp_path):
    db_path = str(tmp_path / "escalation_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1122
    guild.name = "Escalation Guild"
    guild.owner = None
    guild.owner_id = None
    bot.get_guild.return_value = guild

    # Setup 4 admins
    # Admin 1 & Admin 2 (Batch 1)
    adm1 = MagicMock(spec=discord.Member)
    adm1.id = 101
    adm1.bot = False
    adm1.mention = "<@101>"
    adm1.status = discord.Status.online
    adm1_perms = MagicMock()
    adm1_perms.administrator = True
    adm1.guild_permissions = adm1_perms
    adm1.top_role = MagicMock(position=100)
    adm1.roles = []

    adm2 = MagicMock(spec=discord.Member)
    adm2.id = 102
    adm2.bot = False
    adm2.mention = "<@102>"
    adm2.status = discord.Status.online
    adm2_perms = MagicMock()
    adm2_perms.administrator = True
    adm2.guild_permissions = adm2_perms
    adm2.top_role = MagicMock(position=90)
    adm2.roles = []

    # Admin 3 & Admin 4 (Batch 2)
    adm3 = MagicMock(spec=discord.Member)
    adm3.id = 103
    adm3.bot = False
    adm3.mention = "<@103>"
    adm3.status = discord.Status.idle
    adm3_perms = MagicMock()
    adm3_perms.administrator = True
    adm3.guild_permissions = adm3_perms
    adm3.top_role = MagicMock(position=80)
    adm3.roles = []

    adm4 = MagicMock(spec=discord.Member)
    adm4.id = 104
    adm4.bot = False
    adm4.mention = "<@104>"
    adm4.status = discord.Status.offline
    adm4_perms = MagicMock()
    adm4_perms.administrator = True
    adm4.guild_permissions = adm4_perms
    adm4.top_role = MagicMock(position=70)
    adm4.roles = []

    applicant = MagicMock(spec=discord.Member)
    applicant.id = 501
    applicant.bot = False
    applicant.display_name = "EscApplicant"
    applicant.roles = []

    guild.members = [adm1, adm2, adm3, adm4, applicant]
    guild.roles = []

    # 1. Test get_target_admin_mentions_batch returns batch of 2
    mentions, ids = await service.get_target_admin_mentions_batch(guild, count=2, exclude_ids={501})
    assert ids == [101, 102]
    assert mentions == "<@101>, <@102>"

    # 2. Setup review thread and open ticket
    parent_channel = MagicMock(spec=discord.TextChannel)
    parent_channel.name = "guest-tickets"
    perms = MagicMock()
    perms.view_channel = True
    perms.create_private_threads = True
    parent_channel.permissions_for.return_value = perms
    guild.text_channels = [parent_channel]

    thread = MagicMock(spec=discord.Thread)
    thread.id = 9901
    thread.archived = False
    thread.locked = False
    thread.send = AsyncMock()
    thread.add_user = AsyncMock()
    parent_channel.create_thread = AsyncMock(return_value=thread)
    guild.get_thread.return_value = thread

    # History generator helper with no admin messages
    async def empty_history(*args, **kwargs):
        if False:
            yield None

    thread.history = empty_history

    success, msg, _ = await service.open_guest_review_ticket(
        guild=guild,
        applicant=applicant,
        reason="Needs escalation test",
    )
    assert success is True

    # Check ticket in DB has initial pinged_admin_ids = "101,102"
    ticket = await db.get_guest_ticket_by_channel(thread.id)
    assert ticket is not None
    assert ticket["pinged_admin_ids"] == "101,102"

    # 3. Running escalation check with interval_seconds=3600 when ticket was JUST created -> 0 escalated
    escalated = await service.check_and_escalate_tickets(interval_seconds=3600.0)
    assert escalated == 0

    # 4. Running escalation with interval_seconds=0 (simulate 1 hr passed) -> Escalates to adm3 & adm4
    escalated = await service.check_and_escalate_tickets(interval_seconds=0.0)
    assert escalated == 1
    thread.send.assert_called_once()
    escalation_msg = thread.send.call_args[1]["content"]
    assert "<@103>" in escalation_msg
    assert "<@104>" in escalation_msg

    # Verify DB now contains all 4 admin IDs in pinged_admin_ids
    ticket_after = await db.get_guest_ticket_by_channel(thread.id)
    assert "101" in ticket_after["pinged_admin_ids"]
    assert "102" in ticket_after["pinged_admin_ids"]
    assert "103" in ticket_after["pinged_admin_ids"]
    assert "104" in ticket_after["pinged_admin_ids"]

    # 5. Test background task lifecycle
    service.start_escalation_task(check_interval_seconds=0.1, escalation_delay_seconds=3600.0)
    assert service._escalation_task is not None
    service.stop_escalation_task()
    assert service._escalation_task is None

    await db.close()


@pytest.mark.asyncio
async def test_reconcile_downtime_state(tmp_path):
    db_path = str(tmp_path / "downtime_sync_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 5566
    guild.name = "Downtime Guild"
    guild.chunked = True
    guild.roles = []
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot.get_guild.return_value = guild

    # 1. Open Ticket 1: Applicant 8001 left during maintenance
    ticket1_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=8001,
        channel_id=9001,
        reason="Left during downtime",
    )
    thread1 = MagicMock(spec=discord.Thread)
    thread1.id = 9001
    thread1.archived = False
    thread1.send = AsyncMock()
    thread1.edit = AsyncMock()

    # 2. Open Ticket 2: Referrer 8002 left during maintenance
    ticket2_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=8003,
        referrer_id=8002,
        channel_id=9002,
        referral_code="TAR-DOWNTIME",
    )
    thread2 = MagicMock(spec=discord.Thread)
    thread2.id = 9002
    thread2.archived = False
    thread2.send = AsyncMock()
    thread2.edit = AsyncMock()

    # 3. Open Ticket 3: Thread deleted during maintenance
    ticket3_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=8004,
        channel_id=9003,
        reason="Thread was deleted",
    )

    # 4. Active referral code from student 8005 who left during maintenance
    await db.create_referral_code("TAR-ORPHANED", guild.id, 8005, "2099-01-01 00:00:00")

    # 5. Expired referral code
    await db.create_referral_code("TAR-EXPIRED", guild.id, 8006, "2020-01-01 00:00:00")

    # Present members in guild: Only applicant 8003 and 8004
    applicant8003 = MagicMock(spec=discord.Member)
    applicant8003.id = 8003
    applicant8003.roles = []
    applicant8004 = MagicMock(spec=discord.Member)
    applicant8004.id = 8004
    applicant8004.roles = []

    guild.get_member.side_effect = lambda user_id: {
        8003: applicant8003,
        8004: applicant8004,
    }.get(user_id, None)

    guild.get_thread.side_effect = lambda thread_id: {
        9001: thread1,
        9002: thread2,
    }.get(thread_id, None)
    guild.fetch_channel = AsyncMock(side_effect=discord.NotFound(MagicMock(), "Unknown Channel"))
    guild.fetch_member = AsyncMock(side_effect=discord.NotFound(MagicMock(), "Unknown Member"))
    guild.fetch_ban = AsyncMock(side_effect=discord.NotFound(MagicMock(), "Unknown Ban"))

    # Execute downtime reconciliation
    summary = await service.reconcile_downtime_state()

    assert summary["expired_referrals"] == 1
    assert (
        summary["reconciled_tickets"] == 3
    )  # ticket1 (applicant left), ticket2 (referrer left), ticket3 (thread deleted)
    assert summary["reconciled_referrals"] == 1  # TAR-ORPHANED

    # Verify DB statuses
    t1 = await db.get_guest_ticket_by_id(ticket1_id)
    assert t1["status"] == "LEFT_SERVER"

    t2 = await db.get_guest_ticket_by_id(ticket2_id)
    assert t2["status"] == "REVOKED"

    t3 = await db.get_guest_ticket_by_id(ticket3_id)
    assert t3["status"] == "EXPIRED"

    ref_orphan = await db.get_referral_code("TAR-ORPHANED", guild.id)
    assert ref_orphan["status"] == "LEFT_SERVER"

    ref_exp = await db.get_referral_code("TAR-EXPIRED", guild.id)
    assert ref_exp["status"] == "EXPIRED"

    await db.close()


@pytest.mark.asyncio
async def test_find_parent_review_channel_heals_stale_review_channel(tmp_path):
    db_path = str(tmp_path / "stale_review_ch.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 5566
    guild.name = "Self Healing Guild"

    # Set stale review channel ID in DB
    stale_review_id = 999123
    await db.set_guild_review_channel(guild.id, stale_review_id)

    # Setup fallback text channel with keyword "review"
    fallback_ch = MagicMock(spec=discord.TextChannel)
    fallback_ch.id = 888123
    fallback_ch.name = "guest-review"
    perms = MagicMock()
    perms.view_channel = True
    perms.create_private_threads = True
    perms.manage_threads = True
    fallback_ch.permissions_for.return_value = perms

    guild.text_channels = [fallback_ch]
    # get_channel returns None for stale ID
    guild.get_channel.side_effect = lambda cid: fallback_ch if cid == fallback_ch.id else None
    guild.me = MagicMock()

    resolved_ch = await service.find_parent_review_channel(guild)
    assert resolved_ch == fallback_ch

    # Verify stale setting in DB was cleared
    settings = await db.get_guild_settings(guild.id)
    assert settings[3] is None

    await db.close()


@pytest.mark.asyncio
async def test_reconcile_downtime_state_handles_manual_guest_grant(tmp_path):
    db_path = str(tmp_path / "manual_grant_reconcile.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 7711
    guild.name = "Manual Grant Guild"
    bot.get_guild.return_value = guild

    # Setup guest role
    guest_role = MagicMock(spec=discord.Role)
    guest_role.name = "Guest(Approved)"
    guest_role.id = 4411
    guild.roles = [guest_role]

    # Setup applicant member who holds the guest role (manually granted by admin)
    applicant = MagicMock(spec=discord.Member)
    applicant.id = 6611
    applicant.bot = False
    applicant.roles = [guest_role]

    # Create open ticket and referral code
    ref_code = "TAR-MGRANT"
    await db.create_referral_code(ref_code, guild.id, 9999, "2099-01-01 00:00:00")
    ticket_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=applicant.id,
        channel_id=8811,
        referral_code=ref_code,
    )

    thread = AsyncMock(spec=discord.Thread)
    thread.id = 8811
    thread.archived = False
    thread.locked = False

    guild.get_member.side_effect = lambda uid: applicant if uid == applicant.id else None
    guild.get_thread.side_effect = lambda tid: thread if tid == 8811 else None

    # Reconcile downtime state
    summary = await service.reconcile_downtime_state()
    assert summary["reconciled_tickets"] == 1

    # Check ticket is approved
    ticket = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket["status"] == "APPROVED"
    assert "manually granted" in ticket["close_reason"]

    # Check referral code is marked USED
    ref = await db.get_referral_code(ref_code, guild.id)
    assert ref["status"] == "USED"
    assert ref["used_by_discord_id"] == applicant.id

    # Check thread was closed and archived
    thread.send.assert_awaited_once()
    thread.edit.assert_awaited_once_with(archived=True, locked=True)

    await db.close()


@pytest.mark.asyncio
async def test_find_parent_review_channel_skips_admin_locked_channels(tmp_path):
    db_path = str(tmp_path / "admin_skip.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1122
    guild.name = "Locked Guild"
    guild.roles = []
    guild.get_channel.return_value = None

    default_role = MagicMock(spec=discord.Role)
    default_role.id = guild.id
    guild.default_role = default_role

    bot_member = MagicMock(spec=discord.Member)
    bot_member.id = 999000
    guild.me = bot_member

    # Admin only channel
    admin_ch = MagicMock(spec=discord.TextChannel)
    admin_ch.id = 1001
    admin_ch.name = "admin-secret"
    admin_perms = MagicMock()
    admin_perms.view_channel = True
    admin_perms.create_private_threads = True
    admin_perms.manage_threads = True

    # Everyone perms: view_channel is False
    everyone_locked = MagicMock()
    everyone_locked.view_channel = False

    def admin_perms_for(target):
        if target == bot_member:
            return admin_perms
        return everyone_locked

    admin_ch.permissions_for.side_effect = admin_perms_for

    # Public help channel
    help_ch = MagicMock(spec=discord.TextChannel)
    help_ch.id = 1002
    help_ch.name = "ask-for-help"
    help_bot_perms = MagicMock()
    help_bot_perms.view_channel = True
    help_bot_perms.create_private_threads = True
    help_bot_perms.manage_threads = True

    everyone_open = MagicMock()
    everyone_open.view_channel = True

    def help_perms_for(target):
        if target == bot_member:
            return help_bot_perms
        return everyone_open

    help_ch.permissions_for.side_effect = help_perms_for

    guild.text_channels = [admin_ch, help_ch]

    resolved_ch = await service.find_parent_review_channel(guild)
    # Must pick help_ch, NOT admin_ch
    assert resolved_ch == help_ch

    await db.close()


@pytest.mark.asyncio
async def test_find_parent_review_channel_auto_creates_ask_for_help_channel(tmp_path):
    db_path = str(tmp_path / "auto_create_ch.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 3344
    guild.name = "No Help Channel Guild"
    guild.roles = []
    guild.get_channel.return_value = None

    default_role = MagicMock(spec=discord.Role)
    default_role.id = guild.id
    guild.default_role = default_role

    bot_member = MagicMock(spec=discord.Member)
    bot_member.id = 999000
    bot_member.guild_permissions.manage_channels = True
    guild.me = bot_member

    # Guild has no text channels currently accessible to users
    guild.text_channels = []

    # Mock create_text_channel
    created_ch = MagicMock(spec=discord.TextChannel)
    created_ch.id = 778899
    created_ch.name = "ask-for-help"
    created_ch.send = AsyncMock()
    guild.create_text_channel = AsyncMock(return_value=created_ch)

    resolved_ch = await service.find_parent_review_channel(guild)
    assert resolved_ch == created_ch
    guild.create_text_channel.assert_called_once()
    assert guild.create_text_channel.call_args[1]["name"] == "ask-for-help"

    # Verify new channel ID was tied to guild help channel in DB
    settings = await db.get_guild_settings(guild.id)
    assert settings[1] == created_ch.id

    # Verify welcome message was sent to the new channel
    created_ch.send.assert_called_once()

    await db.close()


@pytest.mark.asyncio
async def test_close_guest_ticket_manually_without_kicking(tmp_path):
    db_path = str(tmp_path / "manual_close.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 7788
    guild.name = "Manual Close Guild"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.id = 9001
    admin_user.mention = "<@9001>"

    applicant = MagicMock(spec=discord.Member)
    applicant.id = 5001
    applicant.send = AsyncMock()
    applicant.kick = AsyncMock()
    applicant.add_roles = AsyncMock()
    guild.get_member.return_value = applicant

    # Create referral code and open ticket
    await db.create_referral_code("TAR-MANUAL", guild.id, 4001, "2099-01-01 00:00:00")
    ticket_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=applicant.id,
        referrer_id=4001,
        channel_id=8888,
        referral_code="TAR-MANUAL",
        reason="Testing manual close",
        ticket_seq=42,
    )

    ticket = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket["status"] == "OPEN"

    success, msg = await service.close_guest_ticket_manually(
        ticket=ticket,
        guild=guild,
        admin_user=admin_user,
        reason="Spam dismissal / Manual review without action",
    )

    assert success is True
    assert "manually closed" in msg

    # Verify applicant was NEVER kicked and NEVER assigned roles
    applicant.kick.assert_not_called()
    applicant.add_roles.assert_not_called()

    # Verify DB record updated
    closed_ticket = await db.get_guest_ticket_by_id(ticket_id)
    assert closed_ticket["status"] == "CLOSED"
    assert closed_ticket["closed_by_admin_id"] == admin_user.id
    assert closed_ticket["close_reason"] == "Spam dismissal / Manual review without action"

    # Verify referral code was marked CLOSED
    ref = await db.get_referral_code("TAR-MANUAL", guild.id)
    assert ref["status"] == "CLOSED"

    await db.close()
