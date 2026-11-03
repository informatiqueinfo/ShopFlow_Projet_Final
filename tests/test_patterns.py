import time
import unittest
from shopflow.patterns import CircuitBreaker, LoadShedder, RateLimiter, StaleCache, TokenBucket


class PatternsTests(unittest.TestCase):
    def test_token_bucket(self):
        bucket = TokenBucket(2, 1)
        self.assertTrue(bucket.allow()[0])
        self.assertTrue(bucket.allow()[0])
        ok, retry_after = bucket.allow()
        self.assertFalse(ok)
        self.assertGreaterEqual(retry_after, 1)

    def test_isolation_clients(self):
        limiter = RateLimiter(1, 1)
        self.assertTrue(limiter.allow("attacker")[0])
        self.assertFalse(limiter.allow("attacker")[0])
        self.assertTrue(limiter.allow("witness")[0])

    def test_shedder_priority(self):
        shedder = LoadShedder(4, 1)
        for _ in range(3):
            self.assertTrue(shedder.acquire(False))
        self.assertFalse(shedder.acquire(False))
        self.assertTrue(shedder.acquire(True))
        self.assertFalse(shedder.acquire(True))
        shedder.release(True)
        for _ in range(3):
            shedder.release(False)

    def test_breaker_open_halfopen(self):
        breaker = CircuitBreaker(2, 0.02)
        breaker.failure()
        self.assertTrue(breaker.allowed())
        breaker.failure()
        self.assertFalse(breaker.allowed())
        time.sleep(0.03)
        self.assertTrue(breaker.allowed())
        self.assertFalse(breaker.allowed())
        breaker.success()
        self.assertTrue(breaker.allowed())

    def test_cache_stale(self):
        cache = StaleCache(0.01, 1)
        cache.write("A1", {"quantity": 2})
        self.assertIsNotNone(cache.read("A1"))
        time.sleep(0.02)
        self.assertIsNone(cache.read("A1"))
        self.assertEqual(cache.read("A1", allow_stale=True)[0]["quantity"], 2)


if __name__ == "__main__":
    unittest.main()
