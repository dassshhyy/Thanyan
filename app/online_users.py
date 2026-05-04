import time

from redis.asyncio import Redis


class OnlineUsersTracker:
    def __init__(self, redis_client: Redis, key: str, ttl_seconds: int) -> None:
        self.redis = redis_client
        self.key = key
        self.ttl_seconds = ttl_seconds

    async def heartbeat(self, visitor_id: str) -> int:
        now = int(time.time())
        min_alive = now - self.ttl_seconds
        pipeline = self.redis.pipeline()
        pipeline.zadd(self.key, {visitor_id: now})
        pipeline.zremrangebyscore(self.key, 0, min_alive)
        pipeline.zcard(self.key)
        _, _, count = await pipeline.execute()
        return int(count)

    async def count(self) -> int:
        now = int(time.time())
        min_alive = now - self.ttl_seconds
        pipeline = self.redis.pipeline()
        pipeline.zremrangebyscore(self.key, 0, min_alive)
        pipeline.zcard(self.key)
        _, count = await pipeline.execute()
        return int(count)

    async def active_ids(self) -> set[str]:
        now = int(time.time())
        min_alive = now - self.ttl_seconds
        pipeline = self.redis.pipeline()
        pipeline.zremrangebyscore(self.key, 0, min_alive)
        pipeline.zrange(self.key, 0, -1)
        _, visitor_ids = await pipeline.execute()
        return {
            visitor_id.decode("utf-8") if isinstance(visitor_id, bytes) else str(visitor_id)
            for visitor_id in visitor_ids
            if visitor_id not in {None, ""}
        }
