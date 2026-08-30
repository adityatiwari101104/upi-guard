"""
UPI Guard — PostgreSQL Database Layer

Replaces SQLite with PostgreSQL via psycopg2.
Provides the same function interface that app.py already uses.
"""

import os
import json
import time
import psycopg2
import psycopg2.pool
import psycopg2.extras


_pool = None


def _get_pool():
    """Lazy-initialize the connection pool."""
    global _pool
    if _pool is None:
        dsn = os.getenv("DATABASE_URL", "postgresql://upiguard:upiguard@localhost:5432/upiguard")
        # Railway gives postgres:// but psycopg2 needs postgresql://
        if dsn.startswith("postgres://"):
            dsn = dsn.replace("postgres://", "postgresql://", 1)
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            dsn=dsn,
        )
    return _pool


def get_db_connection():
    """Get a connection from the pool. Caller must close() it when done."""
    pool = _get_pool()
    conn = pool.getconn()
    conn.autocommit = False
    return conn


def put_db_connection(conn):
    """Return a connection to the pool."""
    pool = _get_pool()
    pool.putconn(conn)


def init_db():
    """Create tables if they don't exist."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                timestamp DOUBLE PRECISION,
                merchant_id TEXT,
                action TEXT,
                amount DOUBLE PRECISION,
                upi_id TEXT,
                status TEXT,
                details TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                merchant_id TEXT NOT NULL,
                merchant_name TEXT NOT NULL,
                amount_expected DOUBLE PRECISION NOT NULL,
                upi_uri TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                expires_at DOUBLE PRECISION NOT NULL
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                payment_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                amount_paid DOUBLE PRECISION NOT NULL,
                upi_id TEXT,
                status TEXT NOT NULL,
                created_at DOUBLE PRECISION NOT NULL,
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS webhook_events (
                event_id TEXT PRIMARY KEY,
                order_id TEXT,
                payment_id TEXT,
                event_type TEXT NOT NULL,
                payload_json TEXT,
                signature TEXT,
                delivery_status TEXT NOT NULL,
                attempt INTEGER NOT NULL DEFAULT 1,
                created_at DOUBLE PRECISION NOT NULL
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS merchants (
                merchant_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                api_key TEXT UNIQUE,
                upi_vpa TEXT,
                created_at DOUBLE PRECISION NOT NULL
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS api_keys (
                key_id SERIAL PRIMARY KEY,
                merchant_id TEXT NOT NULL REFERENCES merchants(merchant_id),
                api_key TEXT UNIQUE NOT NULL,
                label TEXT,
                active BOOLEAN DEFAULT TRUE,
                created_at DOUBLE PRECISION NOT NULL
            )
        ''')
        conn.commit()
        print("[DB] PostgreSQL tables created/verified")

        # Migration: add upi_vpa column if missing
        try:
            cur2 = conn.cursor()
            cur2.execute("ALTER TABLE merchants ADD COLUMN IF NOT EXISTS upi_vpa TEXT")
            conn.commit()
        except Exception:
            pass  # column already exists or table doesn't exist yet
    except Exception as e:
        conn.rollback()
        print(f"[DB] Table creation error: {e}")
    finally:
        put_db_connection(conn)


# ─────────────────────────────────────────────
# ORDER OPERATIONS
# ─────────────────────────────────────────────

def create_order_record(order_id, merchant_id, merchant_name, amount_expected, upi_uri, expires_in=300):
    created_at = time.time()
    expires_at = created_at + int(expires_in)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO orders (order_id, merchant_id, merchant_name, amount_expected, upi_uri, status, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ''', (order_id, merchant_id, merchant_name, float(amount_expected), upi_uri, 'created', created_at, expires_at))
        conn.commit()
        return created_at, expires_at
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        put_db_connection(conn)


def get_order_record(order_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM orders WHERE order_id = %s', (order_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        put_db_connection(conn)


def update_order_status(order_id, status):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('UPDATE orders SET status = %s WHERE order_id = %s', (status, order_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        put_db_connection(conn)


# ─────────────────────────────────────────────
# PAYMENT OPERATIONS
# ─────────────────────────────────────────────

def create_payment_record(payment_id, order_id, amount_paid, upi_id, status):
    created_at = time.time()
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO payments (payment_id, order_id, amount_paid, upi_id, status, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
        ''', (payment_id, order_id, float(amount_paid), upi_id, status, created_at))
        conn.commit()
        return created_at
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        put_db_connection(conn)


def get_latest_payment_for_order(order_id):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM payments WHERE order_id = %s ORDER BY created_at DESC LIMIT 1', (order_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        put_db_connection(conn)


# ─────────────────────────────────────────────
# WEBHOOK EVENTS
# ─────────────────────────────────────────────

def log_webhook_event(event_id, order_id, payment_id, event_type, payload_json, signature, delivery_status, attempt=1):
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO webhook_events (event_id, order_id, payment_id, event_type, payload_json, signature, delivery_status, attempt, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO UPDATE SET
                order_id = EXCLUDED.order_id,
                payment_id = EXCLUDED.payment_id,
                event_type = EXCLUDED.event_type,
                payload_json = EXCLUDED.payload_json,
                signature = EXCLUDED.signature,
                delivery_status = EXCLUDED.delivery_status,
                attempt = EXCLUDED.attempt
        ''', (event_id, order_id, payment_id, event_type, json.dumps(payload_json), signature, delivery_status, int(attempt), time.time()))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        put_db_connection(conn)


# ─────────────────────────────────────────────
# AUDIT LOG
# ─────────────────────────────────────────────

def log_audit_event(merchant_id, action, amount=0.0, upi_id="", status="INFO", details=""):
    try:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute('''
                INSERT INTO audit_log (timestamp, merchant_id, action, amount, upi_id, status, details)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            ''', (time.time(), merchant_id, action, amount, upi_id, status, json.dumps(details) if isinstance(details, dict) else details))
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"Audit log error: {e}")
        finally:
            put_db_connection(conn)
    except Exception as e:
        print(f"Audit log connection error: {e}")


def get_audit_logs(merchant_id, action_filter=None, status_filter=None):
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        query = "SELECT timestamp, action, amount, upi_id, status, details FROM audit_log WHERE merchant_id = %s"
        params = [merchant_id]

        if action_filter and action_filter != 'ALL':
            query += " AND action = %s"
            params.append(action_filter)

        if status_filter and status_filter != 'ALL':
            query += " AND status = %s"
            params.append(status_filter)

        query += " ORDER BY timestamp DESC LIMIT 100"

        cur.execute(query, params)
        rows = cur.fetchall()

        logs = []
        for row in rows:
            d = dict(row)
            if d.get('details') and isinstance(d['details'], str):
                try:
                    d['details'] = json.loads(d['details'])
                except (json.JSONDecodeError, TypeError):
                    pass
            logs.append(d)
        return logs
    finally:
        put_db_connection(conn)


# ─────────────────────────────────────────────
# MERCHANT / AUTH OPERATIONS
# ─────────────────────────────────────────────

def create_merchant(merchant_id, name, email, password_hash, api_key, upi_vpa=""):
    """Insert a new merchant record."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('''
            INSERT INTO merchants (merchant_id, name, email, password_hash, api_key, upi_vpa, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        ''', (merchant_id, name, email, password_hash, api_key, upi_vpa, time.time()))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        put_db_connection(conn)


def get_merchant_by_email(email):
    """Look up a merchant by email."""
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM merchants WHERE email = %s', (email,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        put_db_connection(conn)


def get_merchant_by_id(merchant_id):
    """Look up a merchant by ID."""
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute('SELECT * FROM merchants WHERE merchant_id = %s', (merchant_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        put_db_connection(conn)


def update_merchant_api_key(merchant_id, new_api_key):
    """Update a merchant's API key."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('UPDATE merchants SET api_key = %s WHERE merchant_id = %s', (new_api_key, merchant_id))
        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        put_db_connection(conn)


def validate_api_key(api_key):
    """Validate an API key. Returns merchant_id if valid, None otherwise."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute('SELECT merchant_id FROM merchants WHERE api_key = %s', (api_key,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        put_db_connection(conn)
