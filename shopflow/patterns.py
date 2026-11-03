
import math
import random
import threading
import time


class TokenBucket:
    def __init__(self, capacity=10, refill_rate=5):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def allow(self):
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.refill_rate)
            self.updated = now
            if self.tokens >= 1:
                self.tokens -= 1
                return True, 0
            return False, max(1, math.ceil((1 - self.tokens) / self.refill_rate))


class RateLimiter:
    def __init__(self, capacity=10, rate=5):
        self.capacity, self.rate = capacity, rate
        self.buckets = {}
        self.lock = threading.Lock()

    def allow(self, client):
        with self.lock:
            bucket = self.buckets.setdefault(client, TokenBucket(self.capacity, self.rate))
        return bucket.allow()


class LoadShedder:
    """Réserve deux des huit places aux requêtes prioritaires (checkout)."""
    def __init__(self, capacity=8, reserved=2):
        self.capacity = capacity
        self.general = capacity - reserved
        self.active = 0
        self.nonpriority = 0
        self.lock = threading.Lock()

    def acquire(self, priority=False):
        with self.lock:
            if self.active >= self.capacity or (not priority and self.nonpriority >= self.general):
                return False
            self.active += 1
            if not priority:
                self.nonpriority += 1
            return True

    def release(self, priority=False):
        with self.lock:
            self.active -= 1
            if not priority:
                self.nonpriority -= 1


class CircuitBreaker:
    def __init__(self, failures=3, reset_after=2.0):
        self.max_failures = failures
        self.reset_after = reset_after
        self.failure_count = 0
        self.opened_at = None
        self.probe_in_flight = False
        self.lock = threading.Lock()

    def allowed(self):
        with self.lock:
            if self.opened_at is None:
                return True
            if time.monotonic() - self.opened_at < self.reset_after or self.probe_in_flight:
                return False
            self.probe_in_flight = True
            return True

    def success(self):
        with self.lock:
            self.failure_count = 0
            self.opened_at = None
            self.probe_in_flight = False

    def failure(self):
        with self.lock:
            self.failure_count += 1
            if self.failure_count >= self.max_failures:
                self.opened_at = time.monotonic()
            self.probe_in_flight = False


class StaleCache:
    def __init__(self, fresh_for=5, stale_for=90):
        self.fresh_for, self.stale_for = fresh_for, stale_for
        self.entries = {}
        self.lock = threading.Lock()

    def read(self, key, allow_stale=False):
        with self.lock:
            entry = self.entries.get(key)
        if not entry:
            return None
        value, timestamp = entry
        age = time.monotonic() - timestamp
        if age <= self.fresh_for or (allow_stale and age <= self.stale_for):
            return value, age > self.fresh_for
        return None

    def write(self, key, value):
        with self.lock:
            self.entries[key] = (value, time.monotonic())


def backoff(attempt):
    return min(0.025 * (2 ** attempt), 0.15) + random.uniform(0, 0.01)
