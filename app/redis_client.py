"""Redis client factory, shared by the guard chain (app/services/guards.py)
and the Celery worker (app/workers/tasks.py) — mirrors app/db.py's role for
SQLAlchemy: one client built from settings, imported everywhere instead of
each caller constructing its own connection.
"""

from __future__ import annotations

import redis

from app.config import settings

redis_client: redis.Redis = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
