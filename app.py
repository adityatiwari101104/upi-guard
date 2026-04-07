"""
UPI Guard - Backend Server
Run: python app.py
Webhook URL for Cashfree: https://your-domain.com/webhook
"""

import eventlet
eventlet.monkey_patch(os=True, select=True, socket=False, thread=True, time=True)

import os
import hmac
import hashlib
import json
import time
import io
import base64
import sqlite3
import segno
from urllib.parse import quote
from collections import deque
from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv

# Cashfree SDK (only needed for live mode order creation)
try:
    from cashfree_pg.api_client import Cashfree
    from cashfree_pg.models.create_order_request import CreateOrderRequest
    from cashfree_pg.models.customer_details import CustomerDetails
    from cashfree_pg.models.order_meta import OrderMeta
    CASHFREE_SDK_AVAILABLE = True
except ImportError:
    CASHFREE_SDK_AVAILABLE = False

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-prod')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Cashfree config
CASHFREE_CLIENT_ID = os.getenv('CASHFREE_CLIENT_ID', '').strip()
CASHFREE_CLIENT_SECRET = os.getenv('CASHFREE_CLIENT_SECRET', '').strip()
CASHFREE_WEBHOOK_SECRET = os.getenv('CASHFREE_WEBHOOK_SECRET', '').strip()
CASHFREE_ENVIRONMENT = os.getenv('CASHFREE_ENVIRONMENT', 'sandbox').strip().lower()
PAYMENT_MODE_DEFAULT = os.getenv('PAYMENT_MODE_DEFAULT', 'live').strip().lower()
CASHFREE_API_VERSION = "2023-08-01"

# Merchant UPI VPA fallback for direct QR mode
MERCHANT_UPI_VPA = os.getenv('MERCHANT_UPI_VPA', '').strip()

# Initialize Cashfree client
cashfree_client = None
if CASHFREE_SDK_AVAILABLE and CASHFREE_CLIENT_ID and CASHFREE_CLIENT_SECRET:
    cf_env = Cashfree.XProduction if CASHFREE_ENVIRONMENT == 'production' else Cashfree.XSandbox
    cashfree_client = Cashfree(
        XEnvironment=cf_env,
        XClientId=CASHFREE_CLIENT_ID,
        XClientSecret=CASHFREE_CLIENT_SECRET
    )
    cashfree_client.XRequestTimeout = 30

# In-memory session store
qr_sessions = {}

# Processed webhook events cache for idempotency
processed_events = set()

# In-memory transaction history store
transaction_history = {}

# ─────────────────────────────────────────────
# FRAUD DETECTION STATE
# ─────────────────────────────────────────────
upi_history = {}
blocked_upi_ids = set()

# ─────────────────────────────────────────────
# DATABASE SETUP (AUDIT TRAIL)
# ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect('audit.db')
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
    conn.commit()
    conn.close()

init_db()


def log_audit_event(merchant_id, action, amount=0.0, upi_id="", status="INFO", details=""):
    try:
        conn = sqlite3.connect('audit.db')
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
    """Live mode requires Cashfree credentials for automatic verification"""
    placeholder_values = {'your_client_id', 'your_client_secret', 'your_webhook_secret'}
    return (
        bool(cashfree_client)
        and CASHFREE_CLIENT_ID not in placeholder_values
        and CASHFREE_CLIENT_SECRET not in placeholder_values
        and CASHFREE_WEBHOOK_SECRET not in placeholder_values
    )


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


@app.route('/api/create-qr', methods=['POST'])
def create_qr():
    """
    Merchant enters bill amount → generate a locked UPI QR.
    Live mode: Creates Cashfree order → generates QR pointing to Cashfree checkout → webhook auto-verifies.
    Demo mode: Generates local UPI QR with simulation buttons.
    """
    data = request.json or {}
    amount = data.get('amount')
    merchant_id = data.get('merchant_id', 'default_merchant')
    merchant_name = data.get('merchant_name', 'Merchant')
    mode = str(data.get('mode', PAYMENT_MODE_DEFAULT)).strip().lower()

    if not amount or amount <= 0:
        return jsonify({'error': 'Invalid amount'}), 400

    if mode not in {'live', 'demo'}:
        return jsonify({'error': 'Invalid mode. Use live or demo.'}), 400

    amount_paise = int(float(amount) * 100)

    if mode == 'demo':
        return create_demo_qr_response(amount, amount_paise, merchant_id, merchant_name)

    # --- LIVE MODE: Cashfree Order + Checkout QR + Webhook auto-verification ---
    if not is_live_configured():
        return jsonify({
            'error': 'Live mode is not configured. Set Cashfree keys in .env or switch to Demo mode.'
        }), 400

    try:
        order_id = f"order_{merchant_id}_{int(time.time())}"
        customer = CustomerDetails(
            customer_id=merchant_id,
            customer_phone="9999999999"
        )
        order_meta = OrderMeta(
            return_url=f"https://upi-guard-568m.onrender.com/terminal?order_id={order_id}"
        )
        order_request = CreateOrderRequest(
            order_id=order_id,
            order_amount=float(amount),
            order_currency="INR",
            customer_details=customer,
            order_meta=order_meta,
            order_note=f"Bill payment - Rs {amount} at {merchant_name}"
        )

        # Step 1: Create Cashfree order (run in native thread to avoid eventlet DNS issues)
        def _create_order():
            return cashfree_client.PGCreateOrder(CASHFREE_API_VERSION, order_request)

        order_response = eventlet.tpool.execute(_create_order)
        order_data = order_response.data
        payment_session_id = order_data.payment_session_id

        # Step 2: Build Cashfree checkout URL and generate QR from it
        # Customer scans QR → opens Cashfree checkout → pays via UPI → webhook fires automatically
        if CASHFREE_ENVIRONMENT == 'production':
            checkout_url = f"https://payments.cashfree.com/order/#/{payment_session_id}"
        else:
            checkout_url = f"https://sandbox.cashfree.com/pg/order/#/{payment_session_id}"

        img_b64 = generate_url_qr(checkout_url)

        qr_sessions[order_id] = {
            'merchant_id': merchant_id,
            'merchant_name': merchant_name,
            'expected_amount': amount_paise,
            'expected_amount_rupees': float(amount),
            'status': 'pending',
            'created_at': time.time(),
            'demo': False,
            'cf_order_id': order_id,
            'payment_session_id': payment_session_id
        }

        log_audit_event(
            merchant_id=merchant_id,
            action="QR_GENERATED",
            amount=float(amount),
            status="SUCCESS",
            details={"qr_id": order_id, "gateway": "cashfree", "checkout_url": checkout_url}
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
            details={"gateway": "cashfree", "error": str(e)}
        )
        return jsonify({
            'error': f'Failed to create payment QR. ({str(e)})'
        }), 502


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


@app.route('/webhook', methods=['POST'])
def cashfree_webhook():
    """
    Cashfree calls this URL when a payment is made.
    This enables AUTOMATIC payment verification — no manual confirmation needed.
    Configure in Cashfree Dashboard → Developers → Webhooks
    """
    raw_body = request.get_data(as_text=True)
    signature = request.headers.get('x-webhook-signature', '')
    timestamp = request.headers.get('x-webhook-timestamp', '')

    if not CASHFREE_WEBHOOK_SECRET or CASHFREE_WEBHOOK_SECRET == 'your_webhook_secret':
        return jsonify({'error': 'Webhook secret not configured'}), 500

    # Verify signature
    message = f"{timestamp}{raw_body}"
    expected_sig = base64.b64encode(
        hmac.new(
            CASHFREE_WEBHOOK_SECRET.encode(),
            message.encode(),
            hashlib.sha256
        ).digest()
    ).decode()

    if not hmac.compare_digest(expected_sig, signature):
        return jsonify({'error': 'Invalid signature'}), 400

    data = json.loads(raw_body)
    event_type = data.get('type', '')

    # Idempotency
    event_id = request.headers.get('x-idempotency-key',
                f"evt_{hashlib.md5(raw_body.encode()).hexdigest()}")
    if event_id in processed_events:
        return jsonify({'status': 'ok', 'message': 'Duplicate event ignored'})
    processed_events.add(event_id)

    if event_type == 'PAYMENT_SUCCESS_WEBHOOK':
        payment_data = data.get('data', {})
        order_info = payment_data.get('order', {})
        payment_info = payment_data.get('payment', {})

        order_id = order_info.get('order_id', '')
        paid_amount_rupees = float(payment_info.get('payment_amount', 0))
        paid_paise = int(paid_amount_rupees * 100)
        transaction_id = str(payment_info.get('cf_payment_id', ''))

        payment_method = payment_info.get('payment_method', {})
        upi_details = payment_method.get('upi', {})
        upi_id = upi_details.get('upi_id', f"payer_{transaction_id[-6:]}@upi")

        session = qr_sessions.get(order_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        if session['status'] == 'paid':
            return jsonify({'status': 'ok', 'message': 'Already processed'})

        expected_paise = session['expected_amount']
        merchant_id = session['merchant_id']
        expected_rupees = float(expected_paise) / 100.0

        # --- AI Fraud Detection Checks ---
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
            log_audit_event(merchant_id, "FRAUD_FLAGGED", paid_amount_rupees, upi_id, "SUSPICIOUS",
                           {"reasons": fraud_reasons, "expected": expected_rupees})

        if merchant_id not in transaction_history:
            transaction_history[merchant_id] = []
        transaction_history[merchant_id].append(result)

        # Push real-time to merchant screen — THIS IS THE AUTOMATIC VERIFICATION
        socketio.emit('payment_result', result, room=merchant_id)

    return jsonify({'status': 'ok'})


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
        conn = sqlite3.connect('audit.db')
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
    socketio.run(app, debug=False, host='127.0.0.1', port=5000)
