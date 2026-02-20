"""
UPI Guard - Backend Server
Run: python app.py
Webhook URL for Razorpay: https://your-domain.com/webhook
"""

import os
import hmac
import hashlib
import json
import time
import qrcode
import io
import base64
from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
import razorpay

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-prod')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Initialize Razorpay client
client = razorpay.Client(auth=(
    os.getenv('RAZORPAY_KEY_ID', 'rzp_test_YOUR_KEY_ID'),
    os.getenv('RAZORPAY_KEY_SECRET', 'YOUR_KEY_SECRET')
))

RAZORPAY_WEBHOOK_SECRET = os.getenv('RAZORPAY_WEBHOOK_SECRET', 'YOUR_WEBHOOK_SECRET')

# In-memory session store (use Redis/DB in production)
# Structure: { qr_id: { merchant_id, expected_amount, status, created_at } }
qr_sessions = {}


# ─────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/create-qr', methods=['POST'])
def create_qr():
    """
    Merchant enters bill amount → generate a locked UPI QR via Razorpay
    Body: { amount: 450, merchant_id: "shop123", merchant_name: "Sharma Kirana" }
    """
    data = request.json
    amount = data.get('amount')
    merchant_id = data.get('merchant_id', 'default_merchant')
    merchant_name = data.get('merchant_name', 'Merchant')

    if not amount or amount <= 0:
        return jsonify({'error': 'Invalid amount'}), 400

    amount_paise = int(float(amount) * 100)  # Razorpay uses paise

    try:
        # Create UPI QR via Razorpay
        qr_response = client.qrcode.create({
            "type": "upi_qr",
            "name": merchant_name,
            "usage": "single_use",       # Expires after one payment
            "fixed_amount": True,
            "payment_amount": amount_paise,
            "description": f"Bill payment - ₹{amount}",
            "close_by": int(time.time()) + 300,  # Expires in 5 minutes
        })

        qr_id = qr_response['id']
        image_url = qr_response.get('image_url', '')

        # Store session
        qr_sessions[qr_id] = {
            'merchant_id': merchant_id,
            'merchant_name': merchant_name,
            'expected_amount': amount_paise,
            'expected_amount_rupees': amount,
            'status': 'pending',
            'created_at': time.time()
        }

        # Generate QR code image from the UPI string as fallback
        # (Razorpay also returns image_url directly)
        upi_string = qr_response.get('image_url', '')

        return jsonify({
            'success': True,
            'qr_id': qr_id,
            'image_url': image_url,
            'amount': amount,
            'expires_in': 300
        })

    except Exception as e:
        # ── DEMO MODE: If Razorpay keys not configured, generate a mock QR ──
        qr_id = f"demo_qr_{int(time.time())}"
        upi_string = f"upi://pay?pa=merchant@okaxis&pn={merchant_name}&am={amount}&cu=INR&tn=Bill+Payment"

        # Generate QR image locally
        qr_img = qrcode.QRCode(version=1, box_size=10, border=4)
        qr_img.add_data(upi_string)
        qr_img.make(fit=True)
        img = qr_img.make_image(fill_color="black", back_color="white")

        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        img_b64 = base64.b64encode(buffer.getvalue()).decode()

        qr_sessions[qr_id] = {
            'merchant_id': merchant_id,
            'merchant_name': merchant_name,
            'expected_amount': amount_paise,
            'expected_amount_rupees': float(amount),
            'status': 'pending',
            'created_at': time.time(),
            'demo': True
        }

        return jsonify({
            'success': True,
            'qr_id': qr_id,
            'image_b64': f"data:image/png;base64,{img_b64}",
            'amount': amount,
            'expires_in': 300,
            'demo_mode': True,
            'upi_string': upi_string
        })


@app.route('/api/simulate-payment', methods=['POST'])
def simulate_payment():
    """
    DEMO ONLY — simulates a payment coming in.
    Body: { qr_id: "demo_qr_123", amount: 450 }  ← real payment
    Body: { qr_id: "demo_qr_123", amount: 1 }    ← fraud attempt
    """
    data = request.json
    qr_id = data.get('qr_id')
    paid_amount = float(data.get('amount', 0))
    paid_paise = int(paid_amount * 100)

    session = qr_sessions.get(qr_id)
    if not session:
        return jsonify({'error': 'QR session not found'}), 404

    expected_paise = session['expected_amount']
    merchant_id = session['merchant_id']

    if paid_paise == expected_paise:
        result = {
            'status': 'SUCCESS',
            'paid': paid_amount,
            'expected': session['expected_amount_rupees'],
            'message': f"₹{paid_amount:.0f} Received ✓"
        }
        session['status'] = 'paid'
    else:
        result = {
            'status': 'MISMATCH',
            'paid': paid_amount,
            'expected': session['expected_amount_rupees'],
            'message': f"FRAUD ALERT! Expected ₹{session['expected_amount_rupees']:.0f}, Got ₹{paid_amount:.0f}"
        }
        session['status'] = 'mismatch'

    # Push to merchant's browser via WebSocket
    socketio.emit('payment_result', result, room=merchant_id)

    return jsonify({'success': True, 'result': result})


@app.route('/webhook', methods=['POST'])
def razorpay_webhook():
    """
    Razorpay calls this URL when a payment is made.
    Configure this in Razorpay Dashboard → Webhooks
    """
    payload = request.get_data(as_text=True)
    signature = request.headers.get('X-Razorpay-Signature', '')

    # Verify webhook signature
    try:
        expected_sig = hmac.new(
            RAZORPAY_WEBHOOK_SECRET.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_sig, signature):
            return jsonify({'error': 'Invalid signature'}), 400
    except Exception:
        pass  # Skip verification in demo mode

    data = json.loads(payload)
    event = data.get('event')

    if event == 'qr_code.credited':
        # Extract payment info
        qr_entity = data['payload']['qr_code']['entity']
        payment_entity = data['payload']['payment']['entity']

        qr_id = qr_entity['id']
        amount_received = payment_entity['amount']  # in paise

        session = qr_sessions.get(qr_id)
        if not session:
            return jsonify({'error': 'Session not found'}), 404

        expected = session['expected_amount']
        merchant_id = session['merchant_id']
        paid_rupees = amount_received / 100
        expected_rupees = expected / 100

        if amount_received == expected:
            result = {
                'status': 'SUCCESS',
                'paid': paid_rupees,
                'expected': expected_rupees,
                'message': f"₹{paid_rupees:.0f} Received ✓",
                'transaction_id': payment_entity.get('id', '')
            }
            session['status'] = 'paid'
        else:
            result = {
                'status': 'MISMATCH',
                'paid': paid_rupees,
                'expected': expected_rupees,
                'message': f"FRAUD ALERT! Expected ₹{expected_rupees:.0f}, Got ₹{paid_rupees:.0f}",
                'transaction_id': payment_entity.get('id', '')
            }
            session['status'] = 'mismatch'

        # Push real-time to merchant screen
        socketio.emit('payment_result', result, room=merchant_id)

    return jsonify({'status': 'ok'})


@app.route('/api/session/<qr_id>', methods=['GET'])
def get_session(qr_id):
    """Polling fallback if WebSocket drops"""
    session = qr_sessions.get(qr_id)
    if not session:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(session)


# ─────────────────────────────────────────────
# WEBSOCKET EVENTS
# ─────────────────────────────────────────────

@socketio.on('join')
def on_join(data):
    """Merchant joins their own room to receive payment notifications"""
    merchant_id = data.get('merchant_id', 'default_merchant')
    join_room(merchant_id)
    emit('joined', {'room': merchant_id})


if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
