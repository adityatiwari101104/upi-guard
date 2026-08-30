"""
UPI Guard — Redis-backed Session Store

Replaces in-memory Python dicts with Redis-backed objects
that have the same dict-like interface, so app.py needs
minimal changes.
"""

import os
import json
import time
import redis


class RedisDict:
    """Redis-backed dict with optional TTL. Stores values as JSON."""

    def __init__(self, client, name, ttl=None):
        self._r = client
        self._name = name
        self._ttl = ttl

    def __getitem__(self, key):
        raw = self._r.hget(self._name, key)
        if raw is None:
            raise KeyError(key)
        return json.loads(raw)

    def __setitem__(self, key, value):
        self._r.hset(self._name, key, json.dumps(value, default=str))
        if self._ttl:
            self._r.expire(self._name, self._ttl)

    def __delitem__(self, key):
        if not self._r.hdel(self._name, key):
            raise KeyError(key)

    def get(self, key, default=None):
        raw = self._r.hget(self._name, key)
        if raw is None:
            return default
        return json.loads(raw)

    def pop(self, key, default=None):
        raw = self._r.hget(self._name, key)
        if raw is None:
            return default
        self._r.hdel(self._name, key)
        return json.loads(raw)

    def __contains__(self, key):
        return self._r.hexists(self._name, key)

    def __len__(self):
        return self._r.hlen(self._name)

    def keys(self):
        return [k.decode() if isinstance(k, bytes) else k for k in self._r.hkeys(self._name)]

    def values(self):
        return [json.loads(v) for v in self._r.hvals(self._name)]

    def items(self):
        raw = self._r.hgetall(self._name)
        return [
            (k.decode() if isinstance(k, bytes) else k, json.loads(v))
            for k, v in raw.items()
        ]

    def clear(self):
        self._r.delete(self._name)


class RedisList:
    """Redis-backed list. Newest items at index 0 (lpush), trimmed to max_len."""

    def __init__(self, client, name, max_len=200, ttl=None):
        self._r = client
        self._name = name
        self._max_len = max_len
        self._ttl = ttl

    def append(self, value):
        self._r.lpush(self._name, json.dumps(value, default=str))
        self._r.ltrim(self._name, 0, self._max_len - 1)
        if self._ttl:
            self._r.expire(self._name, self._ttl)

    def __iter__(self):
        items = self._r.lrange(self._name, 0, -1)
        return iter([json.loads(i) for i in reversed(items)])

    def __len__(self):
        return self._r.llen(self._name)

    def to_list(self):
        """Return all items as a Python list (newest last)."""
        items = self._r.lrange(self._name, 0, -1)
        return [json.loads(i) for i in reversed(items)]

    def clear(self):
        self._r.delete(self._name)


class RedisSet:
    """Redis-backed set for blocking lists, etc."""

    def __init__(self, client, name):
        self._r = client
        self._name = name

    def add(self, value):
        self._r.sadd(self._name, value)

    def __contains__(self, value):
        return self._r.sismember(self._name, value)

    def remove(self, value):
        self._r.srem(self._name, value)

    def __len__(self):
        return self._r.scard(self._name)

    def members(self):
        raw = self._r.smembers(self._name)
        return [m.decode() if isinstance(m, bytes) else m for m in raw]

    def clear(self):
        self._r.delete(self._name)


class RedisDeque:
    """Redis-backed fixed-length list that behaves like collections.deque(maxlen=N)
    with optional TTL. Used for upi_history velocity tracking."""

    def __init__(self, client, name, maxlen=10, ttl=None):
        self._r = client
        self._name = name
        self._maxlen = maxlen
        self._ttl = ttl

    def append(self, value):
        self._r.rpush(self._name, json.dumps(value))
        self._r.ltrim(self._name, -self._maxlen, -1)
        if self._ttl:
            self._r.expire(self._name, self._ttl)

    def popleft(self):
        """Remove and return the leftmost (oldest) item."""
        raw = self._r.lpop(self._name)
        if raw is None:
            raise IndexError("pop from empty RedisDeque")
        return json.loads(raw)

    def __getitem__(self, index):
        raw = self._r.lindex(self._name, index)
        if raw is None:
            raise IndexError("index out of range")
        return json.loads(raw)

    def __len__(self):
        return self._r.llen(self._name)

    def __iter__(self):
        items = self._r.lrange(self._name, 0, -1)
        return iter([json.loads(i) for i in items])

    def to_list(self):
        items = self._r.lrange(self._name, 0, -1)
        return [json.loads(i) for i in items]

    def clear(self):
        self._r.delete(self._name)


class RedisTimeWindow:
    """Redis-backed sliding time window using sorted sets.
    Tracks timestamps and supports counting entries within a time range."""

    def __init__(self, client, name, window_seconds=120):
        self._r = client
        self._name = name
        self._window = window_seconds

    def add(self, timestamp):
        """Add a timestamp to the window. Auto-evicts entries older than window."""
        self._r.zadd(self._name, {json.dumps(timestamp): timestamp})
        cutoff = timestamp - self._window
        self._r.zremrangebyscore(self._name, "-inf", cutoff)
        self._r.expire(self._name, self._window * 2)

    def count_in_window(self, now=None):
        """Count entries within the time window."""
        if now is None:
            now = time.time()
        cutoff = now - self._window
        return self._r.zcount(self._name, cutoff, "+inf")

    def oldest(self):
        """Return the oldest timestamp in the window, or None."""
        items = self._r.zrange(self._name, 0, 0, withscores=True)
        if items:
            return items[0][1]
        return None

    def __len__(self):
        return self._r.zcard(self._name)

    def clear(self):
        self._r.delete(self._name)


def create_store(redis_url=None):
    """Factory: create all session store objects connected to Redis.

    Returns a dict with keys: qr_sessions, pending_payments,
    transaction_history_fn, upi_history_fn, blocked_upi_ids.
    """
    raw_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    url = redis_url or raw_url
    # Upstash requires SSL — convert redis:// to rediss://
    if url and "upstash.io" in url and url.startswith("redis://"):
        url = url.replace("redis://", "rediss://", 1)
        print(f"[Redis] Converted to SSL (rediss://) for Upstash")
    print(f"[Redis] Raw URL: {url[:80]}")
    # Handle various Redis URL formats
    if url and not url.startswith(("redis://", "rediss://", "unix://")):
        # Upstash uses SSL, everything else likely plain redis
        url = f"rediss://{url}"
        print(f"[Redis] Added rediss:// prefix: {url[:80]}")
    if not url:
        url = "redis://localhost:6379/0"
    print(f"[Redis] Client configured for {url[:60]}...")
    client = redis.from_url(
        url,
        decode_responses=False,
        socket_connect_timeout=5,
        socket_timeout=5,
        retry_on_timeout=True,
    )
    client.ping()
    print(f"[Redis] Connected successfully")

    qr_sessions = RedisDict(client, "upiguard:qr_sessions")
    pending_payments = RedisDict(client, "upiguard:pending_payments")
    blocked_upi_ids = RedisSet(client, "upiguard:blocked_upis")

    def transaction_history(mid):
        return RedisList(client, f"upiguard:txn_history:{mid}", max_len=200)

    def upi_history(uid):
        return RedisTimeWindow(client, f"upiguard:upi_history:{uid}", window_seconds=120)

    return {
        "client": client,
        "qr_sessions": qr_sessions,
        "pending_payments": pending_payments,
        "transaction_history": transaction_history,
        "upi_history": upi_history,
        "blocked_upi_ids": blocked_upi_ids,
    }
