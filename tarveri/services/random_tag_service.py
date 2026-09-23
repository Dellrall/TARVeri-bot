"""
Random Tagging Service for periodic, highly-random user mentions in #general.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from tarveri.bot import TARVeriBot
    from tarveri.database import Database

logger = logging.getLogger("tarveri")


def calculate_next_random_delay(min_minutes: int = 15, max_minutes: int = 180) -> float:
    """
    Calculates a highly randomized next tick interval (in seconds).
    Includes sub-minute jitter (random seconds and milliseconds) for true unpredictability.
    """
    if min_minutes > max_minutes:
        min_minutes, max_minutes = max_minutes, min_minutes

    random_minutes = random.randint(min_minutes, max_minutes)
    random_seconds = random.randint(0, 59)
    random_ms = random.random()

    total_seconds = (random_minutes * 60) + random_seconds + random_ms
    return max(10.0, total_seconds)


class RandomTagService:
    """
    Service managing randomized user mentions restricted to #general across active guilds.
    """

    def __init__(self, bot: TARVeriBot, db: Database):
        self.bot = bot
        self.db = db
        self._guild_tasks: dict[int, asyncio.Task[None]] = {}
        self._is_running = False

    async def start(self) -> None:
        """Initializes database schema and starts workers for enabled guilds."""
        if self._is_running:
            return
        self._is_running = True
        await self._ensure_schema()
        await self.refresh_all_workers()
        logger.info("🎯 Random Tagging Service initialized and background workers started.")

    def stop(self) -> None:
        """Stops all running background guild tasks."""
        self._is_running = False
        for guild_id, task in list(self._guild_tasks.items()):
            if not task.done():
                task.cancel()
        self._guild_tasks.clear()
        logger.info("Random Tagging Service stopped.")

    async def _ensure_schema(self) -> None:
        """Ensures the random_tag_config table exists in the database."""
        schema_sql = """
        CREATE TABLE IF NOT EXISTS random_tag_config (
            guild_id TEXT PRIMARY KEY,
            is_enabled INTEGER DEFAULT 0,
            target_channel_id TEXT,
            max_daily_runs INTEGER DEFAULT 5,
            current_daily_runs INTEGER DEFAULT 0,
            last_reset_date TEXT,
            chance_denominator INTEGER DEFAULT 5,
            min_interval_minutes INTEGER DEFAULT 15,
            max_interval_minutes INTEGER DEFAULT 180,
            words_list TEXT DEFAULT '["Hey", "Cheers to", "Shoutout to", "Greetings"]',
            word_rotation_mode TEXT DEFAULT 'random',
            last_word_index INTEGER DEFAULT 0,
            last_triggered_at TEXT
        );
        """
        async with self.db._conn.cursor() as cursor:
            await cursor.execute(schema_sql)
        await self.db._conn.commit()

    async def get_config(self, guild_id: int) -> dict[str, Any]:
        """Retrieves random tag configuration for a specific guild."""
        async with self.db._conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT guild_id, is_enabled, target_channel_id, max_daily_runs, current_daily_runs,
                       last_reset_date, chance_denominator, min_interval_minutes, max_interval_minutes,
                       words_list, word_rotation_mode, last_word_index, last_triggered_at
                FROM random_tag_config WHERE guild_id = ?
                """,
                (str(guild_id),),
            )
            row = await cursor.fetchone()

        if not row:
            default_words = ["Hey", "Cheers to", "Shoutout to", "Greetings"]
            return {
                "guild_id": str(guild_id),
                "is_enabled": False,
                "target_channel_id": None,
                "max_daily_runs": 5,
                "current_daily_runs": 0,
                "last_reset_date": datetime.now(UTC).strftime("%Y-%m-%d"),
                "chance_denominator": 5,
                "min_interval_minutes": 15,
                "max_interval_minutes": 180,
                "words_list": default_words,
                "word_rotation_mode": "random",
                "last_word_index": 0,
                "last_triggered_at": None,
            }

        try:
            words = json.loads(row[9])
        except Exception:
            words = ["Hey", "Cheers to"]

        return {
            "guild_id": row[0],
            "is_enabled": bool(row[1]),
            "target_channel_id": row[2],
            "max_daily_runs": row[3],
            "current_daily_runs": row[4],
            "last_reset_date": row[5],
            "chance_denominator": row[6],
            "min_interval_minutes": row[7],
            "max_interval_minutes": row[8],
            "words_list": words,
            "word_rotation_mode": row[10] or "random",
            "last_word_index": row[11] or 0,
            "last_triggered_at": row[12],
        }

    async def update_config(self, guild_id: int, **kwargs: Any) -> None:
        """Updates guild config and restarts its worker if needed."""
        current = await self.get_config(guild_id)
        current.update(kwargs)

        words_json = json.dumps(current["words_list"])

        async with self.db._conn.cursor() as cursor:
            await cursor.execute(
                """
                INSERT INTO random_tag_config (
                    guild_id, is_enabled, target_channel_id, max_daily_runs, current_daily_runs,
                    last_reset_date, chance_denominator, min_interval_minutes, max_interval_minutes,
                    words_list, word_rotation_mode, last_word_index, last_triggered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    is_enabled=excluded.is_enabled,
                    target_channel_id=excluded.target_channel_id,
                    max_daily_runs=excluded.max_daily_runs,
                    current_daily_runs=excluded.current_daily_runs,
                    last_reset_date=excluded.last_reset_date,
                    chance_denominator=excluded.chance_denominator,
                    min_interval_minutes=excluded.min_interval_minutes,
                    max_interval_minutes=excluded.max_interval_minutes,
                    words_list=excluded.words_list,
                    word_rotation_mode=excluded.word_rotation_mode,
                    last_word_index=excluded.last_word_index,
                    last_triggered_at=excluded.last_triggered_at
                """,
                (
                    str(guild_id),
                    1 if current["is_enabled"] else 0,
                    current["target_channel_id"],
                    current["max_daily_runs"],
                    current["current_daily_runs"],
                    current["last_reset_date"],
                    current["chance_denominator"],
                    current["min_interval_minutes"],
                    current["max_interval_minutes"],
                    words_json,
                    current["word_rotation_mode"],
                    current["last_word_index"],
                    current["last_triggered_at"],
                ),
            )
        await self.db._conn.commit()
        await self.restart_worker(guild_id)

    async def refresh_all_workers(self) -> None:
        """Starts workers for all enabled guilds."""
        for guild in self.bot.guilds:
            config = await self.get_config(guild.id)
            if config["is_enabled"]:
                await self.restart_worker(guild.id)

    async def restart_worker(self, guild_id: int) -> None:
        """Restarts the background loop for a specific guild."""
        if guild_id in self._guild_tasks:
            task = self._guild_tasks.pop(guild_id)
            if not task.done():
                task.cancel()

        config = await self.get_config(guild_id)
        if config["is_enabled"] and self._is_running:
            self._guild_tasks[guild_id] = asyncio.create_task(
                self._guild_loop(guild_id), name=f"random_tag_guild_{guild_id}"
            )

    async def _resolve_general_channel(
        self, guild: discord.Guild, configured_channel_id: str | None
    ) -> discord.TextChannel | None:
        """Finds #general text channel by configured ID or by name."""
        if configured_channel_id:
            try:
                ch = guild.get_channel(int(configured_channel_id)) or await guild.fetch_channel(int(configured_channel_id))
                if isinstance(ch, discord.TextChannel):
                    return ch
            except Exception as e:
                logger.debug("Could not fetch configured general channel %s: %s", configured_channel_id, e)

        # Find by name "general"
        for ch in guild.text_channels:
            if ch.name.lower() == "general":
                return ch
        return None

    async def _guild_loop(self, guild_id: int) -> None:
        """Highly randomized scheduling loop for a single guild."""
        try:
            while self._is_running:
                config = await self.get_config(guild_id)
                if not config["is_enabled"]:
                    break

                # Calculate highly random delay with sub-minute jitter
                delay_sec = calculate_next_random_delay(
                    config["min_interval_minutes"], config["max_interval_minutes"]
                )
                logger.debug(
                    f"[RandomTag Guild {guild_id}] Next random tick in {delay_sec / 60:.2f} minutes."
                )

                await asyncio.sleep(delay_sec)

                # Process tick
                await self._process_tick(guild_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[RandomTag Guild {guild_id}] Worker loop encountered error: {e}", exc_info=True)

    async def _process_tick(self, guild_id: int) -> None:
        """Evaluates daily cap, roll probability, and triggers user mention."""
        guild = self.bot.get_guild(guild_id)
        if not guild:
            return

        config = await self.get_config(guild_id)
        if not config["is_enabled"]:
            return

        now = datetime.now(UTC)
        today_str = now.strftime("%Y-%m-%d")

        # 1. Reset daily count at midnight
        if config["last_reset_date"] != today_str:
            await self.update_config(
                guild_id, current_daily_runs=0, last_reset_date=today_str
            )
            config["current_daily_runs"] = 0

        # 2. Check Daily Quota
        if config["current_daily_runs"] >= config["max_daily_runs"]:
            return

        # 3. Probability Check (1 in N chance)
        chance_denom = max(1, config["chance_denominator"])
        roll = random.randint(1, chance_denom)
        if roll != 1:
            logger.debug(f"[RandomTag Guild {guild_id}] Roll failed ({roll}/{chance_denom}).")
            return

        # 4. Resolve #general channel
        general_channel = await self._resolve_general_channel(
            guild, config.get("target_channel_id")
        )
        if not general_channel:
            logger.warning(
                f"[RandomTag Guild {guild_id}] #general channel could not be resolved."
            )
            return

        # 5. Fetch members and pick any non-bot user
        try:
            if not guild.chunked:
                await guild.chunk()
            members = [m for m in guild.members if not m.bot]
            if not members:
                return

            random_member = random.choice(members)
        except Exception as e:
            logger.error(f"[RandomTag Guild {guild_id}] Member selection failed: {e}")
            return

        # 6. Word selection and rotation
        words = config["words_list"] or ["Hello"]
        rotation_mode = config["word_rotation_mode"]
        last_idx = config["last_word_index"]

        if rotation_mode == "round_robin":
            new_idx = (last_idx + 1) % len(words)
            selected_word = words[new_idx]
            await self.update_config(guild_id, last_word_index=new_idx)
        else:
            selected_word = random.choice(words)

        message_content = f"{selected_word} {random_member.mention}"

        # 7. Send message into #general
        try:
            await general_channel.send(
                content=message_content,
                allowed_mentions=discord.AllowedMentions(users=[random_member]),
            )
            logger.info(
                f"[RandomTag Guild {guild_id}] Tagged {random_member} in #{general_channel.name} with '{selected_word}'."
            )

            # Update daily run count & timestamp
            await self.update_config(
                guild_id,
                current_daily_runs=config["current_daily_runs"] + 1,
                last_triggered_at=now.isoformat(),
            )
        except Exception as e:
            logger.error(
                f"[RandomTag Guild {guild_id}] Failed to send tag message in #{general_channel.name}: {e}"
            )
