from unittest.mock import AsyncMock, MagicMock

import pytest

from tarveri.services.random_tag_service import RandomTagService, calculate_next_random_delay


@pytest.mark.asyncio
async def test_calculate_next_random_delay():
    delay = calculate_next_random_delay(15, 60)
    assert 15 * 60 <= delay <= 61 * 60


@pytest.mark.asyncio
async def test_random_tag_service_config(tmp_path):
    from tarveri.database import Database

    db_path = str(tmp_path / "test_random_tag.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    bot.guilds = []

    service = RandomTagService(bot, db)
    await service.start()

    # Initial default config
    config = await service.get_config(123456789)
    assert config["is_enabled"] is False
    assert config["max_daily_runs"] == 5
    assert config["chance_denominator"] == 5
    assert config["word_rotation_mode"] == "random"

    # Update config
    await service.update_config(
        123456789,
        is_enabled=True,
        max_daily_runs=8,
        chance_denominator=3,
        words_list=["Hello", "Sup", "Cheers"],
        word_rotation_mode="round_robin",
    )

    updated = await service.get_config(123456789)
    assert updated["is_enabled"] is True
    assert updated["max_daily_runs"] == 8
    assert updated["chance_denominator"] == 3
    assert updated["words_list"] == ["Hello", "Sup", "Cheers"]
    assert updated["word_rotation_mode"] == "round_robin"

    service.stop()
    await db.close()


@pytest.mark.asyncio
async def test_random_tag_execution_strictly_general(tmp_path):
    from tarveri.database import Database

    db_path = str(tmp_path / "test_tag_exec.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    guild = MagicMock()
    guild.id = 123456789
    guild.chunked = True

    # General channel mock
    general_channel = MagicMock()
    general_channel.name = "general"
    general_channel.send = AsyncMock()

    other_channel = MagicMock()
    other_channel.name = "announcements"

    guild.text_channels = [other_channel, general_channel]
    guild.get_channel = MagicMock(return_value=None)

    # Human member and bot member
    human_member = MagicMock()
    human_member.bot = False
    human_member.mention = "<@111222333>"

    bot_member = MagicMock()
    bot_member.bot = True

    guild.members = [bot_member, human_member]
    bot.get_guild = MagicMock(return_value=guild)

    service = RandomTagService(bot, db)
    await service.start()

    await service.update_config(
        123456789,
        is_enabled=True,
        max_daily_runs=5,
        chance_denominator=1, # 100% chance for test
        words_list=["Yo"],
    )

    await service._process_tick(123456789)

    # Verify message sent only to general channel
    general_channel.send.assert_awaited_once()
    sent_args = general_channel.send.call_args[1]
    assert sent_args["content"] == "Yo <@111222333>"

    service.stop()
    await db.close()
