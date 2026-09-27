from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tarveri.database import Database
from tarveri.services.update_checker import UpdateCheckerService


@pytest.mark.asyncio
async def test_update_checker_no_update():
    bot = MagicMock()
    db = MagicMock(spec=Database)
    db.is_connected = True
    db.log = AsyncMock()

    checker = UpdateCheckerService(bot, db, hoster_discord_id=None, update_stream="auto")

    with patch("subprocess.run") as mock_run:
        # Mock git returns same hash for HEAD and upstream
        mock_run.side_effect = [
            MagicMock(returncode=0),  # fetch
            MagicMock(stdout="origin/main\n", returncode=0),  # upstream branch @{u}
            MagicMock(stdout="commit123\n", returncode=0),  # HEAD
            MagicMock(stdout="commit123\n", returncode=0),  # remote HEAD
        ]

        is_avail, count, local_h, remote_h, target_stream = await checker.check_for_updates()
        assert is_avail is False
        assert count == 0
        assert target_stream == "origin/main"


@pytest.mark.asyncio
async def test_update_checker_update_available():
    bot = MagicMock()
    db = MagicMock(spec=Database)
    db.is_connected = True
    db.log = AsyncMock()

    hoster_user = AsyncMock()
    bot.get_user.return_value = hoster_user

    checker = UpdateCheckerService(
        bot,
        db,
        hoster_discord_id=987654321,
        update_stream="refactor/modular-optimization",
    )

    with patch("subprocess.run") as mock_run:
        # Mock git returns 2 commits ahead on target stream
        mock_run.side_effect = [
            MagicMock(returncode=0),  # fetch origin refactor/modular-optimization
            MagicMock(stdout="oldcommit123\n", returncode=0),  # HEAD
            MagicMock(stdout="newcommit456\n", returncode=0),  # remote HEAD
            MagicMock(stdout="2\n", returncode=0),  # count
        ]

        is_avail, count, local_h, remote_h, target_stream = await checker.check_for_updates()
        assert is_avail is True
        assert count == 2
        assert local_h == "oldcommit123"
        assert remote_h == "newcommit456"
        assert target_stream == "origin/refactor/modular-optimization"

        # Check that DB log and DM were triggered
        db.log.assert_awaited_once()
        hoster_user.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_checker_custom_stream_override():
    bot = MagicMock()
    db = MagicMock(spec=Database)
    db.is_connected = False

    checker = UpdateCheckerService(bot, db, update_stream="main")

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # fetch origin beta
            MagicMock(stdout="commitAAA\n", returncode=0),  # HEAD
            MagicMock(stdout="commitBBB\n", returncode=0),  # remote HEAD
            MagicMock(stdout="1\n", returncode=0),  # count
        ]

        is_avail, count, local_h, remote_h, target_stream = await checker.check_for_updates(custom_stream="beta")
        assert is_avail is True
        assert count == 1
        assert target_stream == "origin/beta"


@pytest.mark.asyncio
async def test_update_checker_subprocess_exception():
    bot = MagicMock()
    db = MagicMock(spec=Database)
    db.is_connected = False

    checker = UpdateCheckerService(bot, db, update_stream="main")

    with patch("subprocess.run", side_effect=Exception("git binary not found / timeout")):
        is_avail, count, local_h, remote_h, target_stream = await checker.check_for_updates()
        assert is_avail is False
        assert count == 0
        assert local_h == ""
        assert remote_h == ""


@pytest.mark.asyncio
async def test_update_checker_remote_unresolvable():
    bot = MagicMock()
    db = MagicMock(spec=Database)
    db.is_connected = False

    checker = UpdateCheckerService(bot, db, update_stream="non-existent-branch")

    with patch("subprocess.run") as mock_run:
        # Remote rev-parse returns non-zero returncode for non-existent-branch and fallback
        mock_run.side_effect = [
            MagicMock(returncode=0),  # fetch --prune
            MagicMock(stdout="commit123\n", returncode=0),  # local HEAD
            MagicMock(stdout="", returncode=128),  # remote rev-parse target_branch fails
            MagicMock(stdout="", returncode=128),  # fallback origin/main rev-parse fails
        ]

        is_avail, count, local_h, remote_h, target_stream = await checker.check_for_updates()
        assert is_avail is False
        assert count == 0
        assert local_h == "commit123"
        assert remote_h == ""


@pytest.mark.asyncio
async def test_update_checker_dm_forbidden_exception():
    import discord

    bot = MagicMock()
    db = MagicMock(spec=Database)
    db.is_connected = True
    db.log = AsyncMock()

    hoster_user = AsyncMock()
    mock_resp = MagicMock()
    mock_resp.status = 403
    mock_resp.reason = "Forbidden"
    hoster_user.send.side_effect = discord.Forbidden(mock_resp, "Cannot send messages to this user")
    bot.get_user.return_value = hoster_user

    checker = UpdateCheckerService(
        bot,
        db,
        hoster_discord_id=999888777,
        update_stream="main",
    )

    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            MagicMock(returncode=0),  # fetch origin main
            MagicMock(stdout="commitOLD\n", returncode=0),  # local HEAD
            MagicMock(stdout="commitNEW\n", returncode=0),  # remote HEAD
            MagicMock(stdout="3\n", returncode=0),  # count
        ]

        # Should complete successfully and catch the Forbidden exception without failing
        is_avail, count, local_h, remote_h, target_stream = await checker.check_for_updates()
        assert is_avail is True
        assert count == 3


@pytest.mark.asyncio
async def test_update_checker_service_lifecycle():
    import asyncio

    bot = MagicMock()
    db = MagicMock(spec=Database)

    checker = UpdateCheckerService(bot, db, interval_hours=12)
    assert checker.interval_seconds == 12 * 3600

    # Start service
    checker.start()
    assert checker._task is not None
    assert not checker._stop_event.is_set()

    # Stop service
    checker.stop()
    assert checker._stop_event.is_set()
    try:
        await checker._task
    except asyncio.CancelledError:
        pass
    assert checker._task.cancelled() or checker._task.done()
