"""
UPI Guard - Backend Server
Run: python app.py
Webhook URL (optional): https://your-domain.com/webhook
"""

import os
import hmac
import hashlib
import json
import time
import io
import base64
import sqlite3
import tempfile
import segno
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from collections import deque
from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
try:
    import razorpay
except Exception:
    razorpay = None

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-prod')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

PAYMENT_MODE_DEFAULT = os.getenv('PAYMENT_MODE_DEFAULT', 'live').strip().lower()

# PhonePe config
PHONEPE_CLIENT_ID = os.getenv('PHONEPE_CLIENT_ID', '').strip()
PHONEPE_CLIENT_SECRET = os.getenv('PHONEPE_CLIENT_SECRET', '').strip()
PHONEPE_CLIENT_VERSION = os.getenv('PHONEPE_CLIENT_VERSION', '1').strip()
PHONEPE_ENVIRONMENT = os.getenv('PHONEPE_ENVIRONMENT', 'sandbox').strip().lower()

# Merchant UPI VPA fallback for direct QR mode
MERCHANT_UPI_VPA = os.getenv('MERCHANT_UPI_VPA', '').strip()
MOCK_WEBHOOK_SECRET = os.getenv('MOCK_WEBHOOK_SECRET', 'mock_webhook_secret').strip()
DB_PATH = os.getenv('DB_PATH', os.path.join(tempfile.gettempdir(), 'upi_guard_audit.db')).strip()
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID', '').strip()
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET', '').strip()
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '').strip()

# In-memory session store
qr_sessions = {}

# In-memory transaction history store
transaction_history = {}

# ─────────────────────────────────────────────
# FRAUD DETECTION STATE
# ─────────────────────────────────────────────
upi_history = {}
blocked_upi_ids = set()


def get_razorpay_client():
    if razorpay is None:
        raise RuntimeError("razorpay package not installed. Add razorpay to requirements.")
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError("Razorpay keys are missing in .env")
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# ─────────────────────────────────────────────
# DATABASE SETUP (AUDIT TRAIL)
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL,
            merchant_id TEXT,
            action TEXT,
            amount REAL,
            upi_id TEXT,
            status TEXT,
            details TEXT
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY,
            merchant_id TEXT NOT NULL,
            merchant_name TEXT NOT NULL,
            amount_expected REAL NOT NULL,
            upi_uri TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            payment_id TEXT PRIMARY KEY,
            order_id TEXT NOT NULL,
            amount_paid REAL NOT NULL,
            upi_id TEXT,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY (order_id) REFERENCES orders(order_id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS webhook_events (
            event_id TEXT PRIMARY KEY,
            order_id TEXT,
            payment_id TEXT,
            event_type TEXT NOT NULL,
            payload_json TEXT,
            signature TEXT,
            delivery_status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

init_db()


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def create_order_record(order_id, merchant_id, merchant_name, amount_expected, upi_uri, expires_in=300):
    created_at = time.time()
    expires_at = created_at + int(expires_in)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO orders (order_id, merchant_id, merchant_name, amount_expected, upi_uri, status, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (order_id, merchant_id, merchant_name, float(amount_expected), upi_uri, 'created', created_at, expires_at))
    conn.commit()
    conn.close()
    return created_at, expires_at


def get_order_record(order_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM orders WHERE order_id = ?', (order_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def update_order_status(order_id, status):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('UPDATE orders SET status = ? WHERE order_id = ?', (status, order_id))
    conn.commit()
    conn.close()


def create_payment_record(payment_id, order_id, amount_paid, upi_id, status):
    created_at = time.time()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT INTO payments (payment_id, order_id, amount_paid, upi_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (payment_id, order_id, float(amount_paid), upi_id, status, created_at))
    conn.commit()
    conn.close()
    return created_at


def get_latest_payment_for_order(order_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM payments WHERE order_id = ? ORDER BY created_at DESC LIMIT 1', (order_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


def create_webhook_signature(payload_text):
    return hmac.new(
        MOCK_WEBHOOK_SECRET.encode(),
        payload_text.encode(),
        hashlib.sha256
    ).hexdigest()


def log_webhook_event(event_id, order_id, payment_id, event_type, payload_json, signature, delivery_status, attempt=1):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO webhook_events (event_id, order_id, payment_id, event_type, payload_json, signature, delivery_status, attempt, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (event_id, order_id, payment_id, event_type, json.dumps(payload_json), signature, delivery_status, int(attempt), time.time()))
    conn.commit()
    conn.close()


def log_audit_event(merchant_id, action, amount=0.0, upi_id="", status="INFO", details=""):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('''
            INSERT INTO audit_log (timestamp, merchant_id, action, amount, upi_id, status, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (time.time(), merchant_id, action, amount, upi_id, status, json.dumps(details)))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Audit log error: {e}")


def is_live_configured():
    """Live mode requires PhonePe client credentials."""
    placeholder_values = {'your_client_id', 'your_client_secret'}
    return (
        PHONEPE_CLIENT_ID
        and PHONEPE_CLIENT_SECRET
        and PHONEPE_CLIENT_ID not in placeholder_values
        and PHONEPE_CLIENT_SECRET not in placeholder_values
    )


def get_phonepe_base_url():
    if PHONEPE_ENVIRONMENT == 'production':
        return "https://api.phonepe.com/apis/hermes"
    return "https://api-preprod.phonepe.com/apis/pg-sandbox"


def phonepe_request(method, path, token=None, body=None, form=False):
    url = f"{get_phonepe_base_url()}{path}"
    headers = {}
    payload = None

    if token:
        headers['Authorization'] = f"O-Bearer {token}"

    if body is not None:
        if form:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            payload = urlencode(body).encode()
        else:
            headers['Content-Type'] = 'application/json'
            payload = json.dumps(body).encode()

    req = Request(url=url, method=method.upper(), headers=headers, data=payload)
    with urlopen(req, timeout=20) as resp:
        raw = resp.read().decode()
        if not raw:
            return {}
        return json.loads(raw)


def get_phonepe_access_token():
    response = phonepe_request(
        method='POST',
        path='/v1/oauth/token',
        body={
            'client_id': PHONEPE_CLIENT_ID,
            'client_secret': PHONEPE_CLIENT_SECRET,
            'client_version': PHONEPE_CLIENT_VERSION,
            'grant_type': 'client_credentials'
        },
        form=True
    )

    token = (
        response.get('access_token')
        or response.get('accessToken')
        or response.get('data', {}).get('accessToken')
    )
    if not token:
        raise RuntimeError(f"PhonePe auth failed: {response}")
    return token


def generate_upi_qr(upi_vpa, merchant_name, amount, txn_ref=""):
    """Generate a UPI QR code image as base64 PNG using segno."""
    upi_string = (
        f"upi://pay?pa={upi_vpa}"
        f"&pn={quote(merchant_name)}"
        f"&am={amount}"
        f"&cu=INR"
        f"&tn=Bill+Payment+Ref+{txn_ref}"
        f"&tr={txn_ref}"
    )
    qr_gen = segno.make(upi_string)
    buffer = io.BytesIO()
    qr_gen.save(buffer, kind='png', scale=10)
    img_b64 = base64.b64encode(buffer.getvalue()).decode()
    return upi_string, img_b64


def generate_url_qr(url):
    """Generate a QR code from a URL as base64 PNG."""
    qr_gen = segno.make(url)
    buffer = io.BytesIO()
    qr_gen.save(buffer, kind='png', scale=10)
    img_b64 = base64.b64encode(buffer.getvalue()).decode()
    return img_b64


def create_demo_qr_response(amount, amount_paise, merchant_id, merchant_name):
    qr_id = f"demo_qr_{int(time.time())}"
    _, img_b64 = generate_upi_qr("merchant@okaxis", merchant_name, amount, qr_id)

    qr_sessions[qr_id] = {
        'merchant_id': merchant_id,
        'merchant_name': merchant_name,
        'expected_amount': amount_paise,
        'expected_amount_rupees': float(amount),
        'status': 'pending',
        'created_at': time.time(),
        'demo': True
    }

    log_audit_event(
        merchant_id=merchant_id,
        action="QR_GENERATED",
        amount=float(amount),
        status="SUCCESS",
        details={"qr_id": qr_id, "gateway": "demo"}
    )

    return jsonify({
        'success': True,
        'qr_id': qr_id,
        'image_b64': f"data:image/png;base64,{img_b64}",
        'amount': amount,
        'expires_in': 300,
        'demo_mode': True,
        'mode': 'demo'
    })


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route('/')
def landing():
    return render_template('index.html')


@app.route('/terminal')
def terminal():
    return render_template('terminal.html')


@app.route('/api/orders', methods=['POST'])
def create_order():
    data = request.json or {}
    amount = data.get('amount')
    merchant_id = str(data.get('merchant_id', 'default_merchant')).strip()
    merchant_name = str(data.get('merchant_name', 'Merchant')).strip()

    try:
        amount_value = float(amount)
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid amount'}), 400

    if amount_value <= 0:
        return jsonify({'error': 'Invalid amount'}), 400

    if not MERCHANT_UPI_VPA:
        return jsonify({'error': 'MERCHANT_UPI_VPA is not configured in .env'}), 400

    order_id = f"order_{merchant_id}_{int(time.time())}"
    upi_uri, img_b64 = generate_upi_qr(
        upi_vpa=MERCHANT_UPI_VPA,
        merchant_name=merchant_name,
        amount=f"{amount_value:.2f}",
        txn_ref=order_id
    )
    created_at, expires_at = create_order_record(
        order_id=order_id,
        merchant_id=merchant_id,
        merchant_name=merchant_name,
        amount_expected=amount_value,
        upi_uri=upi_uri,
        expires_in=300
    )

    qr_sessions[order_id] = {
        'merchant_id': merchant_id,
        'merchant_name': merchant_name,
        'expected_amount': int(amount_value * 100),
        'expected_amount_rupees': amount_value,
        'status': 'pending',
        'created_at': created_at,
        'demo': True,
        'mode': 'mock'
    }

    log_audit_event(
        merchant_id=merchant_id,
        action="ORDER_CREATED",
        amount=amount_value,
        status="SUCCESS",
        details={"order_id": order_id, "gateway": "mock", "upi_vpa": MERCHANT_UPI_VPA}
    )

    return jsonify({
        'success': True,
        'order_id': order_id,
        'qr_id': order_id,
        'upi_uri': upi_uri,
        'image_b64': f"data:image/png;base64,{img_b64}",
        'amount': amount_value,
        'expires_in': int(expires_at - created_at),
        'mode': 'mock'
    })


@app.route('/api/orders/<order_id>', methods=['GET'])
def get_order(order_id):
    order = get_order_record(order_id)
    if not order:
        return jsonify({'error': 'Order not found'}), 404

    payment = get_latest_payment_for_order(order_id)
    return jsonify({
        'success': True,
        'order': order,
        'payment': payment
    })


@app.route('/mock-gateway/pay', methods=['POST'])
def mock_gateway_pay():
    data = request.json or {}
    order_id = str(data.get('order_id', '')).strip()
    upi_id = str(data.get('upi_id', 'payer@upi')).strip()

    if not order_id:
        return jsonify({'error': 'order_id required'}), 400

    order = get_order_record(order_id)
    if not order:
        return jsonify({'error': 'Order not found'}), 404

    try:
        paid_amount = float(data.get('paid_amount', order.get('amount_expected', 0.0)))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid paid_amount'}), 400

    event_type = 'payment.captured' if paid_amount > 0 else 'payment.failed'
    payment_status = 'captured' if event_type == 'payment.captured' else 'failed'
    payment_id = f"pay_mock_{int(time.time() * 1000)}"
    create_payment_record(payment_id, order_id, paid_amount, upi_id, payment_status)
    update_order_status(order_id, payment_status)

    payload = {
        "entity": "event",
        "account_id": "acc_mock_001",
        "event": event_type,
        "contains": ["payment", "order"],
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": int(round(paid_amount * 100)),
                    "currency": "INR",
                    "status": payment_status,
                    "order_id": order_id,
                    "method": "upi",
                    "vpa": upi_id,
                    "captured": True if payment_status == 'captured' else False,
                    "created_at": int(time.time())
                }
            },
            "order": {
                "entity": {
                    "id": order_id,
                    "entity": "order",
                    "amount": int(round(float(order['amount_expected']) * 100)),
                    "currency": "INR",
                    "status": payment_status,
                    "created_at": int(order['created_at'])
                }
            }
        },
        "created_at": int(time.time())
    }

    payload_text = json.dumps(payload, separators=(',', ':'))
    signature = create_webhook_signature(payload_text)

    with app.test_request_context(
        '/webhook/razorpay-mock',
        method='POST',
        data=payload_text,
        headers={'X-Razorpay-Signature': signature, 'Content-Type': 'application/json'}
    ):
        webhook_response = razorpay_mock_webhook()

    event_id = f"evt_{payment_id}"
    log_webhook_event(
        event_id=event_id,
        order_id=order_id,
        payment_id=payment_id,
        event_type=event_type,
        payload_json=payload,
        signature=signature,
        delivery_status='delivered',
        attempt=1
    )

    return jsonify({
        'success': True,
        'event': event_type,
        'payment_id': payment_id,
        'order_id': order_id,
        'signature': signature,
        'webhook_result': webhook_response[0].get_json() if isinstance(webhook_response, tuple) else webhook_response.get_json()
    })


@app.route('/api/create-qr', methods=['POST'])
def create_qr():
    """Merchant enters amount and app creates a locked QR."""
    data = request.json or {}
    amount = data.get('amount')
    merchant_id = data.get('merchant_id', 'default_merchant')
    merchant_name = data.get('merchant_name', 'Merchant')
    mode = str(data.get('mode', PAYMENT_MODE_DEFAULT)).strip().lower()

    if not amount or amount <= 0:
        return jsonify({'error': 'Invalid amount'}), 400

    if mode not in {'live', 'demo', 'razorpay_test'}:
        return jsonify({'error': 'Invalid mode. Use live, demo, or razorpay_test.'}), 400

    amount_paise = int(float(amount) * 100)

    if mode == 'demo':
        return create_demo_qr_response(amount, amount_paise, merchant_id, merchant_name)

    if mode == 'razorpay_test':
        try:
            client = get_razorpay_client()
            order = client.order.create({
                "amount": amount_paise,
                "currency": "INR",
                "payment_capture": 1,
                "notes": {
                    "merchant_id": str(merchant_id),
                    "merchant_name": str(merchant_name)
                }
            })
            order_id = order["id"]

            qr_sessions[order_id] = {
                'merchant_id': merchant_id,
                'merchant_name': merchant_name,
                'expected_amount': amount_paise,
                'expected_amount_rupees': float(amount),
                'status': 'pending',
                'created_at': time.time(),
                'demo': False,
                'gateway': 'razorpay',
                'mode': 'razorpay_test',
                'razorpay_order_id': order_id
            }

            log_audit_event(
                merchant_id=merchant_id,
                action="QR_GENERATED",
                amount=float(amount),
                status="SUCCESS",
                details={"qr_id": order_id, "gateway": "razorpay_test", "order_id": order_id}
            )

            return jsonify({
                'success': True,
                'qr_id': order_id,
                'amount': float(amount),
                'expires_in': 300,
                'demo_mode': False,
                'mode': 'razorpay_test',
                'gateway': 'razorpay',
                'razorpay_order_id': order_id,
                'razorpay_key': RAZORPAY_KEY_ID
            })
        except Exception as e:
            log_audit_event(
                merchant_id=merchant_id,
                action="QR_GENERATION_FAILED",
                amount=float(amount),
                status="ERROR",
                details={"gateway": "razorpay_test", "error": str(e)}
            )
            return jsonify({'error': f'Failed to create Razorpay test order. ({str(e)})'}), 502

    if not is_live_configured():
        return jsonify({'error': 'Live mode is not configured. Set PhonePe keys in .env or switch to Demo mode.'}), 400

    try:
        order_id = f"order_{merchant_id}_{int(time.time())}"
        token = get_phonepe_access_token()
        pay_response = phonepe_request(
            method='POST',
            path='/checkout/v2/pay',
            token=token,
            body={
                "merchantOrderId": order_id,
                "amount": amount_paise,
                "expireAfter": 300,
                "metaInfo": {"udf1": merchant_id, "udf2": merchant_name},
                "paymentFlow": {
                    "type": "PG_CHECKOUT",
                    "merchantUrls": {
                        "redirectUrl": request.host_url.rstrip('/') + f"/terminal?order_id={order_id}"
                    }
                }
            }
        )
        checkout_url = (
            pay_response.get('paymentUrl')
            or pay_response.get('data', {}).get('paymentUrl')
            or pay_response.get('redirectUrl')
            or pay_response.get('data', {}).get('redirectUrl')
        )
        if not checkout_url:
            raise RuntimeError(f"PhonePe create payment failed: {pay_response}")

        img_b64 = generate_url_qr(checkout_url)

        qr_sessions[order_id] = {
            'merchant_id': merchant_id,
            'merchant_name': merchant_name,
            'expected_amount': amount_paise,
            'expected_amount_rupees': float(amount),
            'status': 'pending',
            'created_at': time.time(),
            'demo': False,
            'gateway': 'phonepe',
            'phonepe_order_id': order_id
        }

        log_audit_event(
            merchant_id=merchant_id,
            action="QR_GENERATED",
            amount=float(amount),
            status="SUCCESS",
            details={"qr_id": order_id, "gateway": "phonepe", "checkout_url": checkout_url}
        )

        return jsonify({
            'success': True,
            'qr_id': order_id,
            'image_b64': f"data:image/png;base64,{img_b64}",
            'amount': amount,
            'expires_in': 300,
            'demo_mode': False,
            'mode': 'live'
        })

    except Exception as e:
        log_audit_event(
            merchant_id=merchant_id,
            action="QR_GENERATION_FAILED",
            amount=float(amount),
            status="ERROR",
            details={"gateway": "phonepe", "error": str(e)}
        )
        return jsonify({'error': f'Failed to create payment QR. ({str(e)})'}), 502
@app.route('/api/simulate-payment', methods=['POST'])
def simulate_payment():
    data = request.json
    qr_id = data.get('qr_id')
    paid_amount = float(data.get('amount', 0))
    paid_paise = int(paid_amount * 100)
    upi_id = data.get('upi_id', 'demo@upi')

    session = qr_sessions.get(qr_id)
    if not session:
        return jsonify({'error': 'QR session not found'}), 404

    if not session.get('demo'):
        return jsonify({'error': 'Simulation is allowed only for demo mode transactions.'}), 403

    expected_paise = session['expected_amount']
    merchant_id = session['merchant_id']

    # --- AI Fraud Detection Checks ---
    now = time.time()
    fraud_reasons = []

    if upi_id in blocked_upi_ids:
        fraud_reasons.append("Blocked UPI ID")

    if paid_amount < 2.0 or paid_amount in [1.0, 0.5, 0.01]:
        fraud_reasons.append("Unusual Attempt Value")

    if upi_id not in upi_history:
        upi_history[upi_id] = deque(maxlen=10)

    while upi_history[upi_id] and now - upi_history[upi_id][0] > 120:
        upi_history[upi_id].popleft()

    if len(upi_history[upi_id]) >= 2:
        fraud_reasons.append("High Frequency (Suspected Bot)")
        blocked_upi_ids.add(upi_id)

    upi_history[upi_id].append(now)

    if paid_paise != expected_paise:
        fraud_reasons.append(f"Amount Mismatch Expected Rs {session['expected_amount_rupees']:.0f}, Got Rs {paid_amount:.0f}")

    if not fraud_reasons:
        result = {
            'status': 'SUCCESS',
            'paid': paid_amount,
            'expected': session['expected_amount_rupees'],
            'upi_id': upi_id,
            'message': f"Rs {paid_amount:.0f} Received"
        }
        session['status'] = 'paid'
        log_audit_event(merchant_id, "PAYMENT_RECEIVED", paid_amount, upi_id, "SUCCESS")
    else:
        result = {
            'status': 'MISMATCH',
            'paid': paid_amount,
            'expected': session['expected_amount_rupees'],
            'upi_id': upi_id,
            'message': f"FRAUD ALERT! {' | '.join(fraud_reasons)}",
            'fraud_reasons': fraud_reasons
        }
        session['status'] = 'mismatch'
        log_audit_event(merchant_id, "FRAUD_FLAGGED", paid_amount, upi_id, "SUSPICIOUS",
                       {"reasons": fraud_reasons, "expected": session['expected_amount_rupees']})

    socketio.emit('payment_result', result, room=merchant_id)

    result_copy = result.copy()
    result_copy['timestamp'] = time.time()
    result_copy['transaction_id'] = f"demo_txn_{int(time.time())}"
    result_copy['qr_id'] = qr_id

    if merchant_id not in transaction_history:
        transaction_history[merchant_id] = []
    transaction_history[merchant_id].append(result_copy)

    return jsonify({'success': True, 'result': result_copy})


def apply_payment_result(session, paid_amount_rupees, upi_id, transaction_id):
    paid_paise = int(float(paid_amount_rupees) * 100)
    expected_paise = session['expected_amount']
    merchant_id = session['merchant_id']
    expected_rupees = float(expected_paise) / 100.0

    now = time.time()
    fraud_reasons = []

    if upi_id in blocked_upi_ids:
        fraud_reasons.append("Blocked UPI ID")

    if paid_amount_rupees < 2.0 or paid_amount_rupees in [1.0, 0.5, 0.01]:
        fraud_reasons.append("Unusual Attempt Value")

    if upi_id not in upi_history:
        upi_history[upi_id] = deque(maxlen=10)

    while upi_history[upi_id] and now - upi_history[upi_id][0] > 120:
        upi_history[upi_id].popleft()

    if len(upi_history[upi_id]) >= 2:
        fraud_reasons.append("High Frequency (Suspected Bot)")
        blocked_upi_ids.add(upi_id)

    upi_history[upi_id].append(now)

    if paid_paise != expected_paise:
        fraud_reasons.append(f"Amount Mismatch: Expected Rs {expected_rupees:.0f}, Got Rs {paid_amount_rupees:.0f}")

    if not fraud_reasons:
        result = {
            'status': 'SUCCESS',
            'paid': paid_amount_rupees,
            'expected': expected_rupees,
            'upi_id': upi_id,
            'message': f"Rs {paid_amount_rupees:.0f} Received",
            'transaction_id': transaction_id,
            'timestamp': time.time()
        }
        session['status'] = 'paid'
        log_audit_event(merchant_id, "PAYMENT_RECEIVED", paid_amount_rupees, upi_id, "SUCCESS")
    else:
        result = {
            'status': 'MISMATCH',
            'paid': paid_amount_rupees,
            'expected': expected_rupees,
            'upi_id': upi_id,
            'message': f"FRAUD ALERT! {' | '.join(fraud_reasons)}",
            'fraud_reasons': fraud_reasons,
            'transaction_id': transaction_id,
            'timestamp': time.time()
        }
        session['status'] = 'mismatch'
        log_audit_event(merchant_id, "FRAUD_FLAGGED", paid_amount_rupees, upi_id, "SUSPICIOUS", {"reasons": fraud_reasons, "expected": expected_rupees})

    if merchant_id not in transaction_history:
        transaction_history[merchant_id] = []
    transaction_history[merchant_id].append(result)

    socketio.emit('payment_result', result, room=merchant_id)
    return result


@app.route('/api/check-payment/<qr_id>', methods=['GET'])
def check_payment_status(qr_id):
    session = qr_sessions.get(qr_id)
    if not session:
        return jsonify({'error': 'Session not found'}), 404

    if session.get('demo'):
        return jsonify({'success': True, 'status': session.get('status', 'pending')})

    if session.get('status') in {'paid', 'mismatch'}:
        return jsonify({'success': True, 'status': session.get('status')})

    if session.get('mode') == 'razorpay_test':
        try:
            client = get_razorpay_client()
            payments = client.order.payments(qr_id)
            items = payments.get('items', []) if isinstance(payments, dict) else []
            captured = next((p for p in items if str(p.get('status', '')).lower() == 'captured'), None)
            if captured:
                paid_amount_rupees = float(captured.get('amount', 0)) / 100.0
                upi_id = captured.get('vpa') or 'payer@upi'
                transaction_id = captured.get('id', f"pay_{int(time.time())}")
                apply_payment_result(session, paid_amount_rupees, upi_id, transaction_id)
            return jsonify({'success': True, 'status': session.get('status', 'pending')})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e), 'status': session.get('status', 'pending')}), 502

    try:
        token = get_phonepe_access_token()
        response = phonepe_request(
            method='GET',
            path=f"/checkout/v2/order/{session.get('phonepe_order_id', qr_id)}/status",
            token=token
        )

        state = (
            response.get('state')
            or response.get('status')
            or response.get('orderStatus')
            or response.get('data', {}).get('state')
            or response.get('data', {}).get('status')
            or response.get('data', {}).get('orderStatus')
            or ''
        )
        state_upper = str(state).upper()

        if state_upper in {'COMPLETED', 'SUCCESS', 'PAID'}:
            data = response.get('data', response)
            paid_amount_paise = (
                data.get('amount')
                or data.get('payableAmount')
                or data.get('paymentDetails', {}).get('amount')
                or session['expected_amount']
            )
            paid_amount_rupees = float(paid_amount_paise) / 100.0
            transaction_id = str(
                data.get('transactionId')
                or data.get('paymentTransactionId')
                or data.get('merchantTransactionId')
                or session.get('phonepe_order_id', qr_id)
            )
            upi_id = (
                data.get('upiId')
                or data.get('payerVpa')
                or data.get('paymentDetails', {}).get('upiId')
                or f"payer_{transaction_id[-6:]}@upi"
            )

            apply_payment_result(session, paid_amount_rupees, upi_id, transaction_id)

        return jsonify({'success': True, 'status': session.get('status', 'pending')})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e), 'status': session.get('status', 'pending')}), 502


@app.route('/webhook/razorpay-mock', methods=['POST'])
def razorpay_mock_webhook():
    raw_body = request.get_data(as_text=True)
    signature = request.headers.get('X-Razorpay-Signature', '')
    expected_signature = create_webhook_signature(raw_body)

    if not hmac.compare_digest(signature, expected_signature):
        return jsonify({'success': False, 'error': 'Invalid webhook signature'}), 400

    data = json.loads(raw_body or "{}")
    event_type = data.get('event', '')
    payment_entity = data.get('payload', {}).get('payment', {}).get('entity', {})
    order_entity = data.get('payload', {}).get('order', {}).get('entity', {})

    order_id = str(payment_entity.get('order_id') or order_entity.get('id') or '').strip()
    if not order_id:
        return jsonify({'success': False, 'error': 'order_id missing'}), 400

    order = get_order_record(order_id)
    if not order:
        return jsonify({'success': False, 'error': 'Order not found'}), 404

    if event_type == 'payment.captured':
        paid_amount_rupees = float(payment_entity.get('amount', 0)) / 100.0
        upi_id = payment_entity.get('vpa', 'payer@upi')
        transaction_id = payment_entity.get('id', f"pay_{int(time.time())}")

        session = qr_sessions.get(order_id)
        if not session:
            session = {
                'merchant_id': order['merchant_id'],
                'merchant_name': order['merchant_name'],
                'expected_amount': int(float(order['amount_expected']) * 100),
                'expected_amount_rupees': float(order['amount_expected']),
                'status': 'pending',
                'created_at': order['created_at'],
                'demo': True,
                'mode': 'mock'
            }
            qr_sessions[order_id] = session

        apply_payment_result(session, paid_amount_rupees, upi_id, transaction_id)
        update_order_status(order_id, session.get('status', 'pending'))
        return jsonify({'success': True, 'status': session.get('status', 'pending')})

    if event_type == 'payment.failed':
        update_order_status(order_id, 'failed')
        log_audit_event(order['merchant_id'], "PAYMENT_FAILED", 0.0, "", "ERROR", {"order_id": order_id})
        return jsonify({'success': True, 'status': 'failed'})

    return jsonify({'success': True, 'status': 'ignored', 'event': event_type})


@app.route('/webhook', methods=['POST'])
def webhook_live():
    raw_body = request.get_data(as_text=True)
    signature = request.headers.get('X-Razorpay-Signature', '')

    if not WEBHOOK_SECRET:
        return jsonify({'success': False, 'error': 'WEBHOOK_SECRET missing'}), 500

    expected_signature = hmac.new(
        WEBHOOK_SECRET.encode(),
        raw_body.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        return jsonify({'success': False, 'error': 'Invalid webhook signature'}), 400

    data = json.loads(raw_body or "{}")
    event_type = data.get('event', '')
    payment_entity = data.get('payload', {}).get('payment', {}).get('entity', {})
    order_id = str(payment_entity.get('order_id') or '').strip()

    if event_type != 'payment.captured':
        return jsonify({'success': True, 'status': 'ignored', 'event': event_type})

    if not order_id:
        return jsonify({'success': False, 'error': 'order_id missing'}), 400

    session = qr_sessions.get(order_id)
    if not session:
        return jsonify({'success': False, 'error': 'Session not found'}), 404

    paid_amount_rupees = float(payment_entity.get('amount', 0)) / 100.0
    upi_id = payment_entity.get('vpa', 'payer@upi')
    transaction_id = payment_entity.get('id', f"pay_{int(time.time())}")
    apply_payment_result(session, paid_amount_rupees, upi_id, transaction_id)

    log_audit_event(
        merchant_id=session.get('merchant_id', 'default_merchant'),
        action="WEBHOOK_PAYMENT_CAPTURED",
        amount=paid_amount_rupees,
        upi_id=upi_id,
        status="SUCCESS",
        details={"order_id": order_id, "transaction_id": transaction_id, "gateway": "razorpay"}
    )

    return jsonify({'success': True, 'status': session.get('status', 'pending')})
@app.route('/api/session/<qr_id>', methods=['GET'])
def get_session(qr_id):
    """Polling fallback if WebSocket drops"""
    session = qr_sessions.get(qr_id)
    if not session:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(session)


@app.route('/api/history', methods=['GET'])
def get_history():
    merchant_id = request.args.get('merchant_id')
    if not merchant_id:
        return jsonify({'error': 'merchant_id required'}), 400
    history = transaction_history.get(merchant_id, [])
    return jsonify({'success': True, 'history': history[-50:]})


@app.route('/api/audit-logs', methods=['GET'])
def get_audit_logs():
    merchant_id = request.args.get('merchant_id')
    if not merchant_id:
        return jsonify({'error': 'merchant_id required'}), 400

    action_filter = request.args.get('action')
    status_filter = request.args.get('status')

    query = "SELECT timestamp, action, amount, upi_id, status, details FROM audit_log WHERE merchant_id = ?"
    params = [merchant_id]

    if action_filter and action_filter != 'ALL':
        query += " AND action = ?"
        params.append(action_filter)

    if status_filter and status_filter != 'ALL':
        query += " AND status = ?"
        params.append(status_filter)

    query += " ORDER BY timestamp DESC LIMIT 100"

    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        rows = c.fetchall()

        logs = []
        for row in rows:
            logs.append({
                'timestamp': row['timestamp'],
                'action': row['action'],
                'amount': row['amount'],
                'upi_id': row['upi_id'],
                'status': row['status'],
                'details': json.loads(row['details']) if row['details'] else {}
            })
        conn.close()
        return jsonify({'success': True, 'logs': logs})

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    merchant_id = request.args.get('merchant_id')
    if not merchant_id:
        return jsonify({'error': 'merchant_id required'}), 400

    history = transaction_history.get(merchant_id, [])

    total_txns = len(history)
    success_count = sum(1 for t in history if t.get('status') == 'SUCCESS')
    fraud_count = total_txns - success_count
    success_rate = round((success_count / total_txns * 100) if total_txns > 0 else 0, 1)

    from datetime import datetime, timedelta
    today = datetime.now().date()

    daily_revenue = {(today - timedelta(days=i)).strftime('%b %d'): 0 for i in range(6, -1, -1)}
    hourly_distribution = {f"{i:02d}:00": 0 for i in range(24)}

    for t in history:
        if 'timestamp' in t:
            dt = datetime.fromtimestamp(t['timestamp'])
            if t.get('status') == 'SUCCESS':
                date_str = dt.strftime('%b %d')
                if date_str in daily_revenue:
                    daily_revenue[date_str] += t.get('paid', 0)
            hour_str = f"{dt.hour:02d}:00"
            if hour_str in hourly_distribution:
                hourly_distribution[hour_str] += 1

    return jsonify({
        'success': True,
        'summary': {
            'total_transactions': total_txns,
            'success_rate': success_rate,
            'fraud_count': fraud_count
        },
        'charts': {
            'daily_revenue': {
                'labels': list(daily_revenue.keys()),
                'data': list(daily_revenue.values())
            },
            'hourly_distribution': {
                'labels': list(hourly_distribution.keys()),
                'data': list(hourly_distribution.values())
            }
        }
    })


@app.route('/api/receipt/<qr_id>', methods=['GET'])
def get_receipt(qr_id):
    session = qr_sessions.get(qr_id)
    if not session or session.get('status') != 'paid':
        return "Receipt not found or payment not completed.", 404

    paid_amount = session['expected_amount_rupees']
    merchant_name = session['merchant_name']
    transaction_id = request.args.get('txn_id', 'AUTO-GEN')
    date_str = time.strftime('%d %b %Y, %I:%M %p')

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Receipt - {qr_id}</title>
        <style>
            body {{ font-family: monospace; max-width: 400px; margin: 40px auto; color: #111; }}
            .receipt {{ border: 1px dashed #ccc; padding: 24px; }}
            .header {{ text-align: center; border-bottom: 1px solid #eee; padding-bottom: 16px; margin-bottom: 20px; }}
            .brand {{ font-size: 24px; font-weight: bold; }}
            .row {{ display: flex; justify-content: space-between; margin-bottom: 12px; }}
            .total-row {{ border-top: 1px solid #111; border-bottom: 1px solid #111; padding: 12px 0; font-weight: bold; font-size: 18px; margin-top: 20px; }}
            .footer {{ text-align: center; margin-top: 30px; font-size: 12px; color: #666; }}
            @media print {{ body {{ margin: 0; }} .no-print {{ display: none; }} }}
        </style>
    </head>
    <body>
        <div class="receipt">
            <div class="header">
                <div class="brand">{merchant_name}</div>
                <div>UPI-Guard AI Receipt</div>
            </div>
            <div class="row"><span>Date:</span> <span>{date_str}</span></div>
            <div class="row"><span>QR ID:</span> <span>{qr_id}</span></div>
            <div class="row"><span>TXN ID:</span> <span>{transaction_id}</span></div>
            <div class="row"><span>Status:</span> <span>SUCCESS</span></div>
            <div class="row total-row">
                <span>TOTAL PAID</span>
                <span>Rs {paid_amount}</span>
            </div>
            <div class="footer">
                Thank you for your payment.<br>
                Powered by UPI-Guard AI.
            </div>
        </div>
        <div class="no-print" style="text-align:center; margin-top: 20px;">
            <button onclick="window.print()" style="padding: 10px 20px; font-weight:bold; cursor:pointer;">Print / Download PDF</button>
        </div>
    </body>
    </html>
    """
    return html


# ─────────────────────────────────────────────
# WEBSOCKET EVENTS
# ─────────────────────────────────────────────

@socketio.on('join')
def on_join(data):
    merchant_id = data.get('merchant_id', 'default_merchant')
    join_room(merchant_id)
    emit('joined', {'room': merchant_id})


if __name__ == '__main__':
    print(" * Starting server on http://127.0.0.1:5000")
    socketio.run(app, debug=True, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)


