import time

from tarveri.rate_limiter import RateLimiter


def test_rate_limiter_allows_under_limit():
    limiter = RateLimiter(max_attempts=3, window_seconds=60)
    user_id = 12345

    assert not limiter.is_rate_limited(user_id)
    limiter.record_attempt(user_id)
    assert not limiter.is_rate_limited(user_id)
    limiter.record_attempt(user_id)
    assert not limiter.is_rate_limited(user_id)
    limiter.record_attempt(user_id)

    # Reached limit (3 attempts recorded)
    assert limiter.is_rate_limited(user_id)


def test_rate_limiter_reset():
    limiter = RateLimiter(max_attempts=2, window_seconds=60)
    user_id = 999

    limiter.record_attempt(user_id)
    limiter.record_attempt(user_id)
    assert limiter.is_rate_limited(user_id)

    limiter.reset(user_id)
    assert not limiter.is_rate_limited(user_id)


def test_rate_limiter_window_expiry():
    # 0.1 second window for fast expiry testing
    limiter = RateLimiter(max_attempts=1, window_seconds=0.1)
    user_id = 456

    limiter.record_attempt(user_id)
    assert limiter.is_rate_limited(user_id)

    time.sleep(0.15)
    assert not limiter.is_rate_limited(user_id)


def test_rate_limiter_global_reset():
    limiter = RateLimiter(max_attempts=1, window_seconds=60)
    limiter.record_attempt(111)
    limiter.record_attempt(222)
    assert limiter.is_rate_limited(111)
    assert limiter.is_rate_limited(222)

    # Global reset clears all users
    limiter.reset()
    assert not limiter.is_rate_limited(111)
    assert not limiter.is_rate_limited(222)


def test_rate_limiter_multi_user_isolation():
    limiter = RateLimiter(max_attempts=2, window_seconds=60)
    user_a = 1001
    user_b = 1002

    limiter.record_attempt(user_a)
    limiter.record_attempt(user_a)
    assert limiter.is_rate_limited(user_a)
    assert not limiter.is_rate_limited(user_b)

    limiter.record_attempt(user_b)
    assert not limiter.is_rate_limited(user_b)
    limiter.record_attempt(user_b)
    assert limiter.is_rate_limited(user_b)


def test_rate_limiter_capacity_cleanup_trigger():
    # max_tracked_users=5 to trigger cleanup immediately when capacity exceeded
    limiter = RateLimiter(max_attempts=1, window_seconds=0.05, max_tracked_users=5)
    for i in range(10):
        limiter.record_attempt(i)

    assert len(limiter._attempts) >= 5

    # Wait for timestamps to expire
    time.sleep(0.08)

    # Calling is_rate_limited triggers _maybe_cleanup()
    assert not limiter.is_rate_limited(999)
    # Expired keys should have been purged
    assert len(limiter._attempts) < 5
