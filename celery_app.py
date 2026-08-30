"""
UPI Guard — Celery Configuration

Uses Redis as broker and backend.
"""

import os
import sys
from celery import Celery

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
# Handle various Redis URL formats
if REDIS_URL and not REDIS_URL.startswith(("redis://", "rediss://", "unix://")):
    if "upstash.io" in REDIS_URL or ":6379" in REDIS_URL:
        REDIS_URL = f"rediss://{REDIS_URL}" if "upstash.io" in REDIS_URL else f"redis://{REDIS_URL}"
    else:
        REDIS_URL = f"redis://{REDIS_URL}"

celery = Celery(
    "upiguard",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks"],
)

celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Kolkata",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "expire-stale-qr-sessions": {
            "task": "tasks.expire_stale_qr_sessions",
            "schedule": 60.0,
        },
        "retry-failed-webhooks": {
            "task": "tasks.retry_failed_webhooks",
            "schedule": 30.0,
        },
        "generate-daily-analytics": {
            "task": "tasks.generate_daily_analytics",
            "schedule": 86400.0,
        },
    },
)
