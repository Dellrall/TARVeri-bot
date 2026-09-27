import asyncio
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.utils import (
    delete_after_delay,
    format_ticket_seq,
    parse_db_timestamp,
    parse_ticket_seq,
    schedule_ttl_delete,
)


@pytest.mark.asyncio
async def test_delete_after_delay_interaction():
    interaction = MagicMock(spec=discord.Interaction)
    interaction.delete_original_response = AsyncMock()

    await delete_after_delay(interaction, delay=0.01)
    interaction.delete_original_response.assert_called_once()


@pytest.mark.asyncio
async def test_delete_after_delay_message():
    message = MagicMock(spec=discord.Message)
    message.delete = AsyncMock()

    await delete_after_delay(message, delay=0.01)
    message.delete.assert_called_once()


@pytest.mark.asyncio
async def test_schedule_ttl_delete_task():
    interaction = MagicMock(spec=discord.Interaction)
    interaction.delete_original_response = AsyncMock()

    task = schedule_ttl_delete(interaction, delay=0.01)
    assert isinstance(task, asyncio.Task)
    await task
    interaction.delete_original_response.assert_called_once()


@pytest.mark.asyncio
async def test_delete_after_delay_not_found_handled():
    # If the message is already deleted by user, it raises NotFound
    message = MagicMock(spec=discord.Message)
    message.delete = AsyncMock(side_effect=discord.NotFound(MagicMock(), "Unknown Message"))

    # Should not raise exception
    await delete_after_delay(message, delay=0.01)
    message.delete.assert_called_once()


@pytest.mark.asyncio
async def test_delete_after_delay_forbidden_handled():
    # Bot lacks manage_messages permission
    interaction = MagicMock(spec=discord.Interaction)
    interaction.delete_original_response = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "Missing Permissions"))

    # Should not raise exception
    await delete_after_delay(interaction, delay=0.01)
    interaction.delete_original_response.assert_called_once()


@pytest.mark.asyncio
async def test_delete_after_delay_cancellation_handled():
    interaction = MagicMock(spec=discord.Interaction)
    interaction.delete_original_response = AsyncMock()

    task = schedule_ttl_delete(interaction, delay=10.0)
    task.cancel()
    # Awaiting cancelled task should not raise unhandled exception
    try:
        await task
    except asyncio.CancelledError:
        pass
    interaction.delete_original_response.assert_not_called()


def test_format_ticket_seq():
    # Base cases
    assert format_ticket_seq(1) == "A0001"
    assert format_ticket_seq(2) == "A0002"
    assert format_ticket_seq(9999) == "A9999"
    assert format_ticket_seq(10000) == "B0001"
    assert format_ticket_seq(19998) == "B9999"
    assert format_ticket_seq(19999) == "C0001"
    assert format_ticket_seq(26 * 9999 + 1) == "AA0001"

    # Edge cases / fallbacks
    assert format_ticket_seq(None) == "A0001"
    assert format_ticket_seq(0) == "A0001"
    assert format_ticket_seq(-10) == "A0001"
    assert format_ticket_seq("15") == "A0015"
    assert format_ticket_seq("A0042") == "A0042"
    assert format_ticket_seq("b0010") == "B0010"
    assert format_ticket_seq("custom_code") == "CUSTOM_CODE"


def test_parse_db_timestamp():
    # Standard formats
    dt1 = parse_db_timestamp("2026-09-09 12:30:00")
    assert dt1 is not None
    assert dt1.year == 2026
    assert dt1.month == 9
    assert dt1.day == 9
    assert dt1.hour == 12
    assert dt1.minute == 30

    dt_iso = parse_db_timestamp("2026-09-09T14:45:00")
    assert dt_iso is not None
    assert dt_iso.hour == 14
    assert dt_iso.minute == 45

    # None and invalid inputs
    assert parse_db_timestamp(None) is None
    assert parse_db_timestamp("") is None


def test_parse_ticket_seq():
    # Integer inputs
    assert parse_ticket_seq(1) == 1
    assert parse_ticket_seq(42) == 42
    assert parse_ticket_seq(0) is None
    assert parse_ticket_seq(-5) is None
    assert parse_ticket_seq(None) is None

    # Numeric strings
    assert parse_ticket_seq("1") == 1
    assert parse_ticket_seq("42") == 42
    assert parse_ticket_seq("#42") == 42
    assert parse_ticket_seq("0") is None

    # Alphanumeric codes
    assert parse_ticket_seq("A0001") == 1
    assert parse_ticket_seq("#A0001") == 1
    assert parse_ticket_seq("a0001") == 1
    assert parse_ticket_seq("A0042") == 42
    assert parse_ticket_seq("A9999") == 9999
    assert parse_ticket_seq("B0001") == 10000
    assert parse_ticket_seq("B0042") == 10041
    assert parse_ticket_seq("C0001") == 19999
    assert parse_ticket_seq("AA0001") == 26 * 9999 + 1

    # Roundtrip tests
    for seq in (1, 2, 42, 9999, 10000, 19998, 19999, 260000):
        code = format_ticket_seq(seq)
        assert parse_ticket_seq(code) == seq

    # Invalid codes
    assert parse_ticket_seq("") is None
    assert parse_ticket_seq("invalid") is None
    assert parse_ticket_seq("A0000") is None
    assert parse_ticket_seq("A10000") is None


@pytest.mark.asyncio
async def test_async_circuit_breaker_lifecycle():
    from tarveri.utils import AsyncCircuitBreaker, CircuitBreakerError

    cb = AsyncCircuitBreaker(fail_max=3, reset_timeout=0.2, name="TestBreaker")
    assert cb.current_state == "closed"
    assert cb.fail_count == 0

    async def successful_call():
        return "success"

    async def failing_call():
        raise ValueError("simulated network failure")

    # 1. Success remains closed
    res = await cb.call(successful_call)
    assert res == "success"
    assert cb.current_state == "closed"

    # 2. 2 Failures: still closed, counter increments
    with pytest.raises(ValueError):
        await cb.call(failing_call)
    assert cb.fail_count == 1
    assert cb.current_state == "closed"

    with pytest.raises(ValueError):
        await cb.call(failing_call)
    assert cb.fail_count == 2
    assert cb.current_state == "closed"

    # 3. 3rd Failure: trips OPEN
    with pytest.raises(ValueError):
        await cb.call(failing_call)
    assert cb.current_state == "open"

    # 4. Immediate calls raise CircuitBreakerError without executing the function
    with pytest.raises(CircuitBreakerError) as exc_info:
        await cb.call(successful_call)
    assert "is OPEN" in str(exc_info.value)

    # 5. Wait for reset timeout -> transitions to half_open
    await asyncio.sleep(0.25)
    assert cb.current_state == "half_open"

    # 6. Successful probe recovers circuit to closed
    res2 = await cb.call(successful_call)
    assert res2 == "success"
    assert cb.current_state == "closed"
    assert cb.fail_count == 0
