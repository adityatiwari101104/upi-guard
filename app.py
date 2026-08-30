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
import re
import io
import base64
import segno
from datetime import datetime
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from flask import Flask, request, jsonify, render_template, g
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
try:
    import razorpay
except Exception:
    razorpay = None

from fraud_detector import FraudDetector
from auth import (
    hash_password, check_password, generate_token, generate_api_key,
    generate_merchant_id, require_auth, require_api_key,
)
from db import (
    init_db as pg_init_db,
    create_order_record,
    get_order_record,
    update_order_status,
    create_payment_record,
    get_latest_payment_for_order,
    log_webhook_event,
    log_audit_event,
    create_merchant,
    get_merchant_by_email,
    get_merchant_by_id,
    update_merchant_api_key,
)
from session_store import create_store

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
MERCHANT_UPI_VPA = os.getenv('MERCHANT_UPI_VPA', 'adityavictory124-1@oksbi').strip()
MOCK_WEBHOOK_SECRET = os.getenv('MOCK_WEBHOOK_SECRET', 'mock_webhook_secret').strip()
RAZORPAY_KEY_ID = os.getenv('RAZORPAY_KEY_ID', '').strip()
RAZORPAY_KEY_SECRET = os.getenv('RAZORPAY_KEY_SECRET', '').strip()
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '').strip()

# ─────────────────────────────────────────────
# DATABASE & SESSION STORE INIT
# ─────────────────────────────────────────────
pg_init_db()
_store = create_store()
qr_sessions = _store["qr_sessions"]
pending_payments = _store["pending_payments"]
transaction_history = _store["transaction_history"]
upi_history = _store["upi_history"]
blocked_upi_ids = _store["blocked_upi_ids"]


def get_razorpay_client():
    if razorpay is None:
        raise RuntimeError("razorpay package not installed. Add razorpay to requirements.")
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError("Razorpay keys are missing in .env")
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

# Initialize ML fraud detector
fraud_detector = FraudDetector()


def create_webhook_signature(payload_text):
    return hmac.new(
        MOCK_WEBHOOK_SECRET.encode(),
        payload_text.encode(),
        hashlib.sha256
    ).hexdigest()


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
    # Use minimal UPI URI for maximum app compatibility.
    upi_string = (
        f"upi://pay?pa={upi_vpa}"
        f"&pn={quote(merchant_name)}"
        f"&am={amount}"
        f"&cu=INR"
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
# AUTH ROUTES
# ─────────────────────────────────────────────

@app.route('/api/auth/register', methods=['POST'])
def auth_register():
    data = request.json or {}
    name = str(data.get('name', '')).strip()
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', '')).strip()

    if not name or not email or not password:
        return jsonify({'error': 'Name, email, and password are required'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    if '@' not in email:
        return jsonify({'error': 'Invalid email address'}), 400

    existing = get_merchant_by_email(email)
    if existing:
        return jsonify({'error': 'Email already registered'}), 409

    merchant_id = generate_merchant_id()
    password_hash = hash_password(password)
    api_key = generate_api_key()

    create_merchant(merchant_id, name, email, password_hash, api_key)
    token = generate_token(merchant_id, name, email)

    log_audit_event(merchant_id, "MERCHANT_REGISTERED", 0.0, "", "SUCCESS", {"email": email})

    return jsonify({
        'success': True,
        'token': token,
        'merchant_id': merchant_id,
        'merchant_name': name,
        'api_key': api_key,
    })


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.json or {}
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', '')).strip()

    if not email or not password:
        return jsonify({'error': 'Email and password are required'}), 400

    merchant = get_merchant_by_email(email)
    if not merchant or not check_password(password, merchant['password_hash']):
        return jsonify({'error': 'Invalid email or password'}), 401

    token = generate_token(merchant['merchant_id'], merchant['name'], merchant['email'])

    return jsonify({
        'success': True,
        'token': token,
        'merchant_id': merchant['merchant_id'],
        'merchant_name': merchant['name'],
    })


@app.route('/api/auth/me', methods=['GET'])
@require_auth
def auth_me():
    merchant = get_merchant_by_id(g.merchant_id)
    if not merchant:
        return jsonify({'error': 'Merchant not found'}), 404
    return jsonify({
        'success': True,
        'merchant_id': merchant['merchant_id'],
        'name': merchant['name'],
        'email': merchant['email'],
        'api_key': merchant['api_key'],
        'created_at': merchant['created_at'],
    })


@app.route('/api/auth/regenerate-api-key', methods=['POST'])
@require_auth
def auth_regenerate_key():
    new_key = generate_api_key()
    update_merchant_api_key(g.merchant_id, new_key)
    return jsonify({'success': True, 'api_key': new_key})


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
@require_auth
def create_order():
    data = request.json or {}
    amount = data.get('amount')
    merchant_id = g.merchant_id
    merchant_name = g.merchant_name

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
@require_auth
def get_order(order_id):
    order = get_order_record(order_id)
    if not order:
        return jsonify({'error': 'Order not found'}), 404
    if order.get('merchant_id') != g.merchant_id:
        return jsonify({'error': 'Forbidden'}), 403

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
@require_auth
def create_qr():
    """Merchant enters amount and app creates a locked QR."""
    data = request.json or {}
    amount = data.get('amount')
    merchant_id = g.merchant_id
    merchant_name = g.merchant_name
    mode = str(data.get('mode', PAYMENT_MODE_DEFAULT)).strip().lower()

    if not amount or amount <= 0:
        return jsonify({'error': 'Invalid amount'}), 400

    if mode not in {'live', 'demo', 'razorpay_test', 'upi_direct'}:
        return jsonify({'error': 'Invalid mode. Use live, demo, razorpay_test, or upi_direct.'}), 400

    amount_paise = int(float(amount) * 100)

    if mode == 'demo':
        return create_demo_qr_response(amount, amount_paise, merchant_id, merchant_name)

    if mode == 'upi_direct':
        if not MERCHANT_UPI_VPA:
            return jsonify({'error': 'MERCHANT_UPI_VPA is not configured in .env'}), 400

        order_id = f"upi_direct_{int(time.time())}"
        upi_uri, img_b64 = generate_upi_qr(
            upi_vpa=MERCHANT_UPI_VPA,
            merchant_name=merchant_name,
            amount=f"{float(amount):.2f}",
            txn_ref=order_id
        )

        qr_sessions[order_id] = {
            'merchant_id': merchant_id,
            'merchant_name': merchant_name,
            'expected_amount': amount_paise,
            'expected_amount_rupees': float(amount),
            'status': 'pending',
            'created_at': time.time(),
            'demo': False,
            'gateway': 'upi_direct',
            'mode': 'upi_direct'
        }

        pending_payments[merchant_id] = {
            "amount": float(amount),
            "time": datetime.now().isoformat(),
            "qr_id": order_id,
        }
        print(f"\n[SMS MODE] New payment expected: Rs {float(amount):.2f} | QR: {order_id}\n")

        return jsonify({
            'success': True,
            'qr_id': order_id,
            'image_b64': f"data:image/png;base64,{img_b64}",
            'qr_image': f"data:image/png;base64,{img_b64}",
            'upi_string': upi_uri,
            'amount': float(amount),
            'expires_in': 300,
            'demo_mode': False,
            'mode': 'upi_direct',
            'status': 'pending'
        })

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


def parse_upi_amount(sms):
    patterns = [
        r'Rs\.?\s*(\d+(?:\.\d{1,2})?)\s*credited',
        r'INR\s*(\d+(?:\.\d{1,2})?)\s*credited',
        r'credited.*?Rs\.?\s*(\d+(?:\.\d{1,2})?)',
        r'received\s+Rs\.?\s*(\d+(?:\.\d{1,2})?)',
        r'Rs\.?\s*(\d+(?:\.\d{1,2})?)\s*received',
    ]
    for pattern in patterns:
        match = re.search(pattern, sms, re.IGNORECASE)
        if match:
            return float(match.group(1))
    return None


def parse_upi_ref(sms):
    patterns = [
        r'UPI\s*[Rr]ef\s*[Nn]o\.?\s*(\d+)',
        r'UPI[:/](\d+)',
        r'ref\s*no\.?\s*(\d+)',
        r'transaction\s*id[:\s]*([A-Za-z0-9]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, sms, re.IGNORECASE)
        if match:
            return match.group(1)
    return "N/A"


@app.route("/sms-webhook", methods=["POST"])
@require_api_key
def sms_webhook():
    try:
        data = request.json or {}
        sms = (data.get("message") or data.get("body") or data.get("sms") or "").strip()
        print(f"\n[SMS WEBHOOK] SMS RECEIVED from merchant {g.merchant_id}: {sms}\n")

        amount = parse_upi_amount(sms)
        ref = parse_upi_ref(sms)

        if amount is None:
            print("[SMS WEBHOOK] Not a UPI credit SMS, ignoring.")
            return jsonify({"status": "ignored"}), 200

        expected_entry = pending_payments.get(g.merchant_id, {})
        expected = expected_entry.get("amount")

        if expected is None:
            print("[SMS WEBHOOK] No pending payment found.")
            return jsonify({"status": "no_pending_payment"}), 200

        qr_id = expected_entry.get("qr_id", "")
        session = qr_sessions.get(qr_id)
        if not session:
            return jsonify({"status": "session_not_found"}), 404

        print(f"[SMS WEBHOOK] Expected: Rs {expected:.2f} | Received: Rs {amount:.2f}")

        # Reuse existing verification + fraud/mismatch pipeline
        apply_payment_result(
            session=session,
            paid_amount_rupees=float(amount),
            upi_id=f"sms_ref_{ref}",
            transaction_id=str(ref)
        )
        pending_payments.pop(g.merchant_id, None)

        return jsonify({"status": "ok", "amount": amount, "transaction_ref": ref}), 200

    except Exception as e:
        print(f"[SMS WEBHOOK] Error: {e}")
        return jsonify({"error": str(e)}), 500


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

    hist = upi_history(upi_id)
    if hist.count_in_window(now) >= 2:
        fraud_reasons.append("High Frequency (Suspected Bot)")
        blocked_upi_ids.add(upi_id)

    hist.add(now)

    if paid_paise != expected_paise:
        fraud_reasons.append(f"Amount Mismatch Expected Rs {session['expected_amount_rupees']:.0f}, Got Rs {paid_amount:.0f}")

    # --- ML Risk Scoring ---
    ml_result = fraud_detector.predict(
        paid_amount=paid_amount,
        expected_amount=session['expected_amount_rupees'],
        upi_id=upi_id,
        merchant_id=merchant_id,
    )
    risk_score = ml_result.get("risk_score", 50)
    ml_verdict = ml_result.get("ml_verdict", "unknown")

    # Hybrid decision: rules are hard gates, ML adds soft scoring
    if risk_score >= 70 and not fraud_reasons:
        fraud_reasons.append(f"ML Risk Score: {risk_score}/100 ({ml_verdict})")

    if not fraud_reasons:
        result = {
            'status': 'SUCCESS',
            'paid': paid_amount,
            'expected': session['expected_amount_rupees'],
            'upi_id': upi_id,
            'message': f"Rs {paid_amount:.0f} Received",
            'risk_score': risk_score,
            'ml_verdict': ml_verdict,
        }
        session['status'] = 'paid'
        fraud_detector.update_after_decision(upi_id, merchant_id, is_fraud=False)
        log_audit_event(merchant_id, "PAYMENT_RECEIVED", paid_amount, upi_id, "SUCCESS",
                        {"risk_score": risk_score, "ml_verdict": ml_verdict})
    else:
        result = {
            'status': 'MISMATCH',
            'paid': paid_amount,
            'expected': session['expected_amount_rupees'],
            'upi_id': upi_id,
            'message': f"FRAUD ALERT! {' | '.join(fraud_reasons)}",
            'fraud_reasons': fraud_reasons,
            'risk_score': risk_score,
            'ml_verdict': ml_verdict,
        }
        session['status'] = 'mismatch'
        fraud_detector.update_after_decision(upi_id, merchant_id, is_fraud=True)
        log_audit_event(merchant_id, "FRAUD_FLAGGED", paid_amount, upi_id, "SUSPICIOUS",
                       {"reasons": fraud_reasons, "expected": session['expected_amount_rupees'],
                        "risk_score": risk_score, "ml_verdict": ml_verdict})

    socketio.emit('payment_result', result, room=merchant_id)

    result_copy = result.copy()
    result_copy['timestamp'] = time.time()
    result_copy['transaction_id'] = f"demo_txn_{int(time.time())}"
    result_copy['qr_id'] = qr_id

    transaction_history(merchant_id).append(result_copy)

    return jsonify({'success': True, 'result': result_copy})


def apply_payment_result(session, paid_amount_rupees, upi_id, transaction_id, source="auto"):
    paid_paise = int(float(paid_amount_rupees) * 100)
    expected_paise = session['expected_amount']
    merchant_id = session['merchant_id']
    expected_rupees = float(expected_paise) / 100.0

    now = time.time()
    fraud_reasons = []

    # Manual confirm flow should validate amount match only.
    if source != "manual_confirm":
        if upi_id in blocked_upi_ids:
            fraud_reasons.append("Blocked UPI ID")

        if paid_amount_rupees < 2.0 or paid_amount_rupees in [1.0, 0.5, 0.01]:
            fraud_reasons.append("Unusual Attempt Value")

        hist = upi_history(upi_id)
        if hist.count_in_window(now) >= 2:
            fraud_reasons.append("High Frequency (Suspected Bot)")
            blocked_upi_ids.add(upi_id)

        hist.add(now)

    if paid_paise != expected_paise:
        fraud_reasons.append(f"Amount Mismatch: Expected Rs {expected_rupees:.0f}, Got Rs {paid_amount_rupees:.0f}")

    # --- ML Risk Scoring ---
    ml_result = fraud_detector.predict(
        paid_amount=paid_amount_rupees,
        expected_amount=expected_rupees,
        upi_id=upi_id,
        merchant_id=merchant_id,
    )
    risk_score = ml_result.get("risk_score", 50)
    ml_verdict = ml_result.get("ml_verdict", "unknown")

    # Hybrid decision: rules are hard gates, ML adds soft scoring
    if risk_score >= 70 and not fraud_reasons:
        fraud_reasons.append(f"ML Risk Score: {risk_score}/100 ({ml_verdict})")

    if not fraud_reasons:
        result = {
            'status': 'SUCCESS',
            'paid': paid_amount_rupees,
            'expected': expected_rupees,
            'upi_id': upi_id,
            'message': f"Rs {paid_amount_rupees:.0f} Received",
            'transaction_id': transaction_id,
            'timestamp': time.time(),
            'risk_score': risk_score,
            'ml_verdict': ml_verdict,
        }
        session['status'] = 'paid'
        fraud_detector.update_after_decision(upi_id, merchant_id, is_fraud=False)
        log_audit_event(merchant_id, "PAYMENT_RECEIVED", paid_amount_rupees, upi_id, "SUCCESS",
                        {"risk_score": risk_score, "ml_verdict": ml_verdict})
    else:
        result = {
            'status': 'MISMATCH',
            'paid': paid_amount_rupees,
            'expected': expected_rupees,
            'upi_id': upi_id,
            'message': f"FRAUD ALERT! {' | '.join(fraud_reasons)}",
            'fraud_reasons': fraud_reasons,
            'transaction_id': transaction_id,
            'timestamp': time.time(),
            'risk_score': risk_score,
            'ml_verdict': ml_verdict,
        }
        session['status'] = 'mismatch'
        fraud_detector.update_after_decision(upi_id, merchant_id, is_fraud=True)
        log_audit_event(merchant_id, "FRAUD_FLAGGED", paid_amount_rupees, upi_id, "SUSPICIOUS",
                       {"reasons": fraud_reasons, "expected": expected_rupees,
                        "risk_score": risk_score, "ml_verdict": ml_verdict})

    transaction_history(merchant_id).append(result)

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
@require_auth
def get_session(qr_id):
    """Polling fallback if WebSocket drops"""
    session = qr_sessions.get(qr_id)
    if not session:
        return jsonify({'error': 'Not found'}), 404
    if session.get('merchant_id') != g.merchant_id:
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify(session)


@app.route('/api/history', methods=['GET'])
@require_auth
def get_history():
    history = transaction_history(g.merchant_id).to_list()
    return jsonify({'success': True, 'history': history[-50:]})


@app.route('/api/audit-logs', methods=['GET'])
@require_auth
def get_audit_logs():
    action_filter = request.args.get('action')
    status_filter = request.args.get('status')

    try:
        from db import get_audit_logs as db_get_audit_logs
        logs = db_get_audit_logs(g.merchant_id, action_filter, status_filter)
        return jsonify({'success': True, 'logs': logs})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/analytics', methods=['GET'])
@require_auth
def get_analytics():
    history = transaction_history(g.merchant_id).to_list()

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
@require_auth
def get_receipt(qr_id):
    session = qr_sessions.get(qr_id)
    if not session or session.get('status') != 'paid':
        return "Receipt not found or payment not completed.", 404
    if session.get('merchant_id') != g.merchant_id:
        return "Forbidden", 403

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
# ML MODEL ENDPOINTS
# ─────────────────────────────────────────────

@app.route('/api/model-status', methods=['GET'])
def model_status():
    """Returns ML model metadata and performance metrics."""
    return jsonify(fraud_detector.get_status())


@app.route('/api/retrain-model', methods=['POST'])
@require_auth
def retrain_model():
    """Retrain the ML model with synthetic + any available real data."""
    try:
        metadata = fraud_detector.retrain()
        return jsonify({
            'success': True,
            'metadata': metadata,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ─────────────────────────────────────────────
# WEBSOCKET EVENTS
# ─────────────────────────────────────────────

@app.route('/merchant-confirm')
def merchant_confirm():
    return render_template('merchant_confirm.html')


@app.route('/api/manual-verify', methods=['POST'])
def manual_verify():
    data = request.json or {}
    amount = float(data.get('amount', 0))
    ref = data.get('ref', 'MANUAL-REF')
    merchant_id = data.get('merchant_id', 'default_merchant')

    expected_entry = pending_payments.get(merchant_id, {})
    expected = expected_entry.get('amount')
    qr_id = expected_entry.get('qr_id', '')

    if not expected:
        return jsonify({'error': 'No pending payment found'}), 400

    session = qr_sessions.get(qr_id)
    if not session:
        session = {
            'merchant_id': merchant_id,
            'merchant_name': 'Merchant',
            'expected_amount': int(expected * 100),
            'expected_amount_rupees': float(expected),
            'status': 'pending',
            'created_at': time.time(),
            'demo': False,
            'mode': 'upi_direct'
        }
        qr_sessions[qr_id] = session

    result = apply_payment_result(
        session=session,
        paid_amount_rupees=amount,
        upi_id=f"manual_ref_{ref}",
        transaction_id=str(ref),
        source="manual_confirm"
    )

    pending_payments.pop(merchant_id, None)
    return jsonify({'status': result.get('status'), 'message': result.get('message')})


@socketio.on('join')
def on_join(data):
    from auth import decode_token
    token = data.get('token', '')
    payload = decode_token(token)
    if not payload:
        emit('error', {'message': 'Invalid or missing token'})
        return
    merchant_id = payload['merchant_id']
    join_room(merchant_id)
    emit('joined', {'room': merchant_id, 'merchant_id': merchant_id})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
#                     ↑ must be 0.0.0.0
