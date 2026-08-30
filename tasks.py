"""
UPI Guard — Celery Background Tasks

Handles QR session expiry, webhook retries, and daily analytics.
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta

from celery_app import celery

# Add project root to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger("upiguard.tasks")


def _get_session_store():
    """Lazy import of session store (Redis)."""
    from session_store import create_store
    return create_store()


def _get_db():
    """Lazy import of DB module."""
    import db
    return db


# ─────────────────────────────────────────────
# TASK 1: Expire stale QR sessions
# ─────────────────────────────────────────────

@celery.task(name="tasks.expire_stale_qr_sessions")
def expire_stale_qr_sessions():
    """Scan QR sessions and mark any older than 5 minutes as expired."""
    try:
        store = _get_session_store()
        qr_sessions = store["qr_sessions"]
        now = time.time()
        expired_count = 0

        for key in qr_sessions.keys():
            session = qr_sessions.get(key)
            if not session:
                continue

            created_at = session.get("created_at", 0)
            if now - created_at > 300:  # 5 minutes
                session["status"] = "expired"
                qr_sessions[key] = session
                expired_count += 1

        if expired_count > 0:
            logger.info(f"[TASK] Expired {expired_count} stale QR sessions")

        return {"expired": expired_count}
    except Exception as e:
        logger.error(f"[TASK] QR expiry error: {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────────
# TASK 2: Retry failed webhook deliveries
# ─────────────────────────────────────────────

@celery.task(name="tasks.retry_failed_webhooks")
def retry_failed_webhooks():
    """Find webhook events with 'pending' delivery status and retry them."""
    try:
        db = _get_db()
        conn = db.get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT event_id, order_id, payment_id, event_type, payload_json, attempt
                FROM webhook_events
                WHERE delivery_status = 'pending' AND attempt < 3
                ORDER BY created_at ASC
                LIMIT 10
            """)
            rows = cur.fetchall()

            if not rows:
                return {"retried": 0}

            for row in rows:
                event_id, order_id, payment_id, event_type, payload_json, attempt = row
                logger.info(f"[TASK] Retrying webhook {event_id} (attempt {attempt + 1})")

                # Mark as delivered (in a real system, you'd actually POST to the webhook URL)
                cur.execute("""
                    UPDATE webhook_events
                    SET delivery_status = 'delivered', attempt = %s
                    WHERE event_id = %s
                """, (attempt + 1, event_id))

            conn.commit()
            return {"retried": len(rows)}
        finally:
            db.put_db_connection(conn)
    except Exception as e:
        logger.error(f"[TASK] Webhook retry error: {e}")
        return {"error": str(e)}


# ─────────────────────────────────────────────
# TASK 3: Generate daily analytics report
# ─────────────────────────────────────────────

@celery.task(name="tasks.generate_daily_analytics")
def generate_daily_analytics():
    """Aggregate yesterday's transactions into a summary and store in Redis."""
    try:
        store = _get_session_store()
        db = _get_db()

        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        # Get all audit events from yesterday
        conn = db.get_db_connection()
        try:
            from psycopg2.extras import RealDictCursor
            cur = conn.cursor(cursor_factory=RealDictCursor)
            cur.execute("""
                SELECT merchant_id, action, amount, status
                FROM audit_log
                WHERE timestamp >= %s AND timestamp < %s
            """, (
                datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() - 86400,
                datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp(),
            ))
            rows = cur.fetchall()
        finally:
            db.put_db_connection(conn)

        # Aggregate per merchant
        merchant_stats = {}
        for row in rows:
            mid = row["merchant_id"]
            if mid not in merchant_stats:
                merchant_stats[mid] = {
                    "total_txns": 0,
                    "success": 0,
                    "fraud": 0,
                    "revenue": 0.0,
                }
            stats = merchant_stats[mid]
            stats["total_txns"] += 1
            if row["status"] == "SUCCESS":
                stats["success"] += 1
                stats["revenue"] += float(row["amount"] or 0)
            elif row["status"] == "SUSPICIOUS":
                stats["fraud"] += 1

        # Store in Redis
        r = store["client"]
        for mid, stats in merchant_stats.items():
            stats["date"] = yesterday
            stats["generated_at"] = datetime.now().isoformat()
            r.set(
                f"upiguard:daily_report:{mid}:{yesterday}",
                json.dumps(stats, default=str),
                ex=86400 * 7,  # keep for 7 days
            )

        logger.info(f"[TASK] Generated daily analytics for {len(merchant_stats)} merchants ({yesterday})")
        return {"merchants": len(merchant_stats), "date": yesterday}
    except Exception as e:
        logger.error(f"[TASK] Analytics error: {e}")
        return {"error": str(e)}
