"""Rate limiter using Redis sliding window. Author: Akshay Prajapati"""
import time, logging
logger = logging.getLogger(__name__)

class RateLimiter:
    def __init__(self, redis_client, default_limit=100, window_seconds=60):
        self.redis = redis_client; self.default_limit = default_limit; self.window_seconds = window_seconds
    def is_allowed(self, key, limit=None, window=None):
        limit = limit or self.default_limit; window = window or self.window_seconds
        now = time.time()
        pipe = self.redis.pipeline()
        pipe.zremrangebyscore(key, 0, now - window)
        pipe.zadd(key, {str(now): now}); pipe.zcard(key); pipe.expire(key, window)
        return pipe.execute()[2] <= limit
