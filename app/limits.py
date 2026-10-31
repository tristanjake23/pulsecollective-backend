"""Bounded local limits; atomic Redis limits shared across production workers."""

import hashlib
import hmac
import secrets
import time
from contextlib import contextmanager
from threading import Lock

from fastapi import HTTPException


class Limits:
    def __init__(self, redis_url="", secret=""):
        self.lock = Lock()
        self.windows = {}
        self.leases = {}
        self.revoked = {}
        self.secret = (secret or secrets.token_hex(32)).encode()
        self.redis = None

        if redis_url:
            from redis import Redis

            self.redis = Redis.from_url(
                redis_url,
                socket_timeout=2,
                socket_connect_timeout=2,
            )
            self.redis.ping()

    def key(self, value):
        return (
            "pulse:"
            + hmac.new(
                self.secret,
                value.encode(),
                hashlib.sha256,
            ).hexdigest()
        )

    def hit(self, name, subject, limit, seconds):
        key = self.key(name + ":" + subject)

        if self.redis:
            try:
                count = self.redis.eval(
                    """
                    local n = redis.call('INCR', KEYS[1])
                    if n == 1 then
                        redis.call('EXPIRE', KEYS[1], ARGV[1])
                    end
                    return n
                    """,
                    1,
                    key,
                    seconds,
                )
            except Exception:
                raise HTTPException(
                    503,
                    "Rate-limit service unavailable.",
                ) from None

            if count > limit:
                raise HTTPException(
                    429,
                    "Too many requests. Please try again later.",
                    headers={"Retry-After": str(seconds)},
                )
            return

        now = time.monotonic()

        with self.lock:
            self.windows = {k: v for k, v in self.windows.items() if v[1] > now}

            if key not in self.windows and len(self.windows) >= 10000:
                raise HTTPException(
                    503,
                    "Server busy. Please try again later.",
                )

            count, until = self.windows.get(key, (0, now + seconds))
            self.windows[key] = (count + 1, until)

            if count >= limit:
                raise HTTPException(
                    429,
                    "Too many requests. Please try again later.",
                    headers={
                        "Retry-After": str(max(1, int(until - now))),
                    },
                )

    def revoke(self, token, ttl):
        key = self.key("revoked:" + token)

        if self.redis:
            try:
                self.redis.set(key, "1", ex=ttl)
            except Exception:
                raise HTTPException(
                    503,
                    "Session revocation service unavailable.",
                ) from None
        else:
            with self.lock:
                now = time.time()
                self.revoked = {k: v for k, v in self.revoked.items() if v > now}

                if len(self.revoked) >= 10000:
                    raise HTTPException(
                        503,
                        "Session revocation capacity reached.",
                    )

                self.revoked[key] = now + ttl

    def is_revoked(self, token):
        key = self.key("revoked:" + token)

        if self.redis:
            try:
                return bool(self.redis.exists(key))
            except Exception:
                raise HTTPException(
                    503,
                    "Session validation service unavailable.",
                ) from None

        return self.revoked.get(key, 0) > time.time()

    @contextmanager
    def slot(self, name, capacity):
        key = self.key("slots:" + name)
        token = secrets.token_hex(16)
        now = time.time()

        if self.redis:
            try:
                accepted = self.redis.eval(
                    """
                    redis.call(
                        'ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1]
                    )
                    if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then
                        return 0
                    end
                    redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
                    redis.call('EXPIRE', KEYS[1], 150)
                    return 1
                    """,
                    1,
                    key,
                    now,
                    capacity,
                    now + 120,
                    token,
                )
            except Exception:
                raise HTTPException(
                    503,
                    "AI capacity service unavailable.",
                ) from None
        else:
            with self.lock:
                leases = {
                    t: expiry
                    for t, expiry in self.leases.get(key, {}).items()
                    if expiry > now
                }

                accepted = len(leases) < capacity

                if accepted:
                    leases[token] = now + 120

                self.leases[key] = leases

        if not accepted:
            raise HTTPException(
                429,
                "AI is busy. Please try again shortly.",
                headers={"Retry-After": "10"},
            )

        try:
            yield
        finally:
            if self.redis:
                try:
                    self.redis.zrem(key, token)
                except Exception:
                    pass  # Lease expires even if cleanup fails.
            else:
                with self.lock:
                    self.leases.get(key, {}).pop(token, None)


def throttle(request, name, limit=10, seconds=60, subject=None):
    # Uvicorn accepts forwarding headers only from explicitly trusted proxies.
    host = request.client.host if request.client else "unknown"
    request.app.state.limits.hit(
        name,
        subject or host,
        limit,
        seconds,
    )
