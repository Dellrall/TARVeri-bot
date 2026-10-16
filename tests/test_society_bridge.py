"""
Unit tests for TARClub society bridge, pairing token generation, and student eligibility queries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tarveri.config import generate_society_pairing_token
from tarveri.database import Database


@pytest.fixture
async def db(tmp_path: Path):
    db_file = tmp_path / "test_tarveri_bridge.db"
    database = Database(str(db_file))
    await database.connect()
    yield database
    await database.close()


def test_pairing_token_format():
    guild_id = 123456789012345678
    token, token_hash = generate_society_pairing_token(guild_id)

    assert token.startswith(f"TARV-{guild_id}-")
    assert len(token_hash) == 64  # SHA-256


@pytest.mark.asyncio
async def test_society_pairing_crud(db: Database):
    guild_id = 987654321098765432
    token, token_hash = generate_society_pairing_token(guild_id)

    # Initial state should be None
    assert await db.get_paired_society(guild_id) is None

    # Create pairing
    await db.create_society_pairing(
        society_guild_id=guild_id,
        token_hash=token_hash,
        paired_by=111222333,
        notes="Computer Science Society",
    )

    paired = await db.get_paired_society(guild_id)
    assert paired is not None
    assert paired["society_guild_id"] == guild_id
    assert paired["is_active"] is True
    assert paired["notes"] == "Computer Science Society"

    # List pairings
    all_paired = await db.list_paired_societies()
    assert len(all_paired) >= 1
    assert any(s["society_guild_id"] == guild_id for s in all_paired)

    # Revoke pairing
    revoked = await db.revoke_society_pairing(guild_id)
    assert revoked is True
    after_revoke = await db.get_paired_society(guild_id)
    assert after_revoke is not None
    assert after_revoke["is_active"] is False


@pytest.mark.asyncio
async def test_student_eligibility_query(db: Database):
    user_id = 888777666555

    # Unverified member returns None
    assert await db.get_student_eligibility_for_society(user_id) is None

    # Record verified student
    await db.record_verification(
        discord_user_id=user_id,
        student_id_hash="dummy_hash_12345",
        faculty_code="M",
        campus_code="W",
        level_code="D",
        programme_code="WMD",
    )

    profile = await db.get_student_eligibility_for_society(user_id)
    assert profile is not None
    assert profile["is_verified"] is True
    assert profile["faculty_code"] == "M"
    assert profile["campus_code"] == "W"
    assert profile["is_alumni"] is False
