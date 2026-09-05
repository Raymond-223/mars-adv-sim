"""Low-level Redis adapter.

Only this adapter imports the Redis client.  It wraps connection-pool backed
JSON/string, Hash, Set, Sorted-Set, batch/pipeline and TTL operations so upper
layers do not directly depend on redis-py.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence


class RedisStore:
    def __init__(self, redis_url: str, *, max_connections: int = 32, socket_timeout: float = 2.0):
        try:
            import redis  # type: ignore
        except ImportError as exc:
            raise RuntimeError("redis package is not installed. Install with: pip install redis>=5") from exc
        self.pool = redis.ConnectionPool.from_url(
            redis_url,
            decode_responses=True,
            max_connections=max_connections,
            socket_timeout=socket_timeout,
            socket_connect_timeout=socket_timeout,
            health_check_interval=30,
        )
        self.client = redis.Redis(connection_pool=self.pool)

    def ping(self) -> bool:
        return bool(self.client.ping())

    def set_json(self, key: str, value: Mapping[str, Any], ttl_seconds: Optional[int] = None) -> None:
        payload = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))
        if ttl_seconds and ttl_seconds > 0:
            self.client.setex(key, int(ttl_seconds), payload)
        else:
            self.client.set(key, payload)

    def get_json(self, key: str) -> Optional[Dict[str, Any]]:
        raw = self.client.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    def mget_json(self, keys: Sequence[str]) -> List[Optional[Dict[str, Any]]]:
        if not keys:
            return []
        values = self.client.mget(list(keys))
        return [json.loads(v) if v is not None else None for v in values]

    def batch_set_json(self, items: Mapping[str, Mapping[str, Any]], ttl_seconds: Optional[int] = None) -> None:
        if not items:
            return
        with self.client.pipeline(transaction=False) as pipe:
            for key, value in items.items():
                payload = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))
                if ttl_seconds and ttl_seconds > 0:
                    pipe.setex(key, int(ttl_seconds), payload)
                else:
                    pipe.set(key, payload)
            pipe.execute()

    def delete(self, *keys: str) -> int:
        return int(self.client.delete(*keys)) if keys else 0

    def exists(self, key: str) -> bool:
        return bool(self.client.exists(key))

    def expire(self, key: str, ttl_seconds: int) -> None:
        self.client.expire(key, int(ttl_seconds))

    def ttl(self, key: str) -> int:
        return int(self.client.ttl(key))

    def hset_json(self, key: str, mapping: Mapping[str, Any], ttl_seconds: Optional[int] = None) -> None:
        flat = {k: json.dumps(v, ensure_ascii=False) for k, v in mapping.items()}
        if flat:
            self.client.hset(key, mapping=flat)
        if ttl_seconds and ttl_seconds > 0:
            self.client.expire(key, int(ttl_seconds))

    def hgetall_json(self, key: str) -> Dict[str, Any]:
        data = self.client.hgetall(key)
        result: Dict[str, Any] = {}
        for k, v in data.items():
            try:
                result[k] = json.loads(v)
            except Exception:
                result[k] = v
        return result

    def sadd(self, key: str, values: Iterable[str]) -> None:
        vals = list(dict.fromkeys(values))
        if vals:
            self.client.sadd(key, *vals)

    def srem(self, key: str, values: Iterable[str]) -> None:
        vals = list(dict.fromkeys(values))
        if vals:
            self.client.srem(key, *vals)

    def smembers(self, key: str) -> List[str]:
        return list(self.client.smembers(key))

    def zadd(self, key: str, mapping: Mapping[str, float]) -> None:
        if mapping:
            self.client.zadd(key, mapping)

    def zrem(self, key: str, values: Iterable[str]) -> None:
        vals = list(values)
        if vals:
            self.client.zrem(key, *vals)

    def zrevrange(self, key: str, start: int = 0, end: int = -1) -> List[str]:
        return list(self.client.zrevrange(key, start, end))

    def scan_keys(self, pattern: str) -> List[str]:
        keys: List[str] = []
        for key in self.client.scan_iter(match=pattern, count=500):
            keys.append(key)
        return keys
