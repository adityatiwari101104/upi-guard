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
import sqlite3
from collections import deque
from flask import Flask, request, jsonify, render_template
from flask_socketio import SocketIO, emit, join_room
from dotenv import load_dotenv
import razorpay

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-prod')
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# Initialize Razorpay client
client = razorpay.Client(auth=(
    os.getenv('RAZORPAY_KEY_ID', 'rzp_test_YOUR_KEY_ID'),
    os.getenv('RAZORPAY_KEY_SECRET', 'YOUR_KEY_SECRET')
))

RAZORPAY_WEBHOOK_SECRET = os.getenv('RAZORPAY_WEBHOOK_SECRET', 'YOUR_WEBHOOK_SECRET')

# In-memory session store (use Redis/DB in production)
# Structure: { qr_id: { merchant_id, expected_amount, status, created_at, history: [...] } }
qr_sessions = {}

# Processed webhook events cache for idempotency
processed_events = set()

# In-memory transaction history store (for demo purposes)
# Structure: { merchant_id: [ { transaction_id, amount, status, timestamp, ... }, ... ] }
transaction_history = {}

# ─────────────────────────────────────────────
# FRAUD DETECTION STATE
# ─────────────────────────────────────────────
# Track payment frequency: { "upi_id": [timestamp1, timestamp2, ...] }
upi_history = {}

# Blocked UPI IDs
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

        # --- AUDIT LOG ---
        log_audit_event(
            merchant_id=merchant_id, 
            action="QR_GENERATED", 
            amount=float(amount), 
            status="SUCCESS",
            details={"qr_id": qr_id, "gateway": "razorpay"}
        )

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
        import segno
        qr_gen = segno.make(upi_string)
        buffer = io.BytesIO()
        qr_gen.save(buffer, kind='png', scale=10)
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

        # --- AUDIT LOG ---
        log_audit_event(
            merchant_id=merchant_id, 
            action="QR_GENERATED", 
            amount=float(amount), 
            status="SUCCESS",
            details={"qr_id": qr_id, "gateway": "demo_fallback"}
        )

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
    data = request.json
    qr_id = data.get('qr_id')
    paid_amount = float(data.get('amount', 0))
    paid_paise = int(paid_amount * 100)
    
    # In demo mode, we allow the client to specify a UPI ID, default to testing
    upi_id = data.get('upi_id', 'demo@upi')

    session = qr_sessions.get(qr_id)
    if not session:
        return jsonify({'error': 'QR session not found'}), 404

    expected_paise = session['expected_amount']
    merchant_id = session['merchant_id']

    # --- AI Fraud Detection Checks ---
    now = time.time()
    fraud_reasons = []

    # 1. Blocklist Check
    if upi_id in blocked_upi_ids:
        fraud_reasons.append("Blocked UPI ID")

    # 2. Unusual Amount Check
    if paid_amount < 2.0 or paid_amount in [1.0, 0.5, 0.01]:
        fraud_reasons.append("Unusual Attempt Value")

    # 3. Frequency Check (Velocity)
    if upi_id not in upi_history:
        upi_history[upi_id] = deque(maxlen=10)
    
    # Clean up old timestamps (> 2 minutes old)
    while upi_history[upi_id] and now - upi_history[upi_id][0] > 120:
        upi_history[upi_id].popleft()
        
    # If 3 or more payments in the last 2 minutes, flag it!
    if len(upi_history[upi_id]) >= 2:
        fraud_reasons.append("High Frequency (Suspected Bot)")
        blocked_upi_ids.add(upi_id) # Auto-block for future attempts
        
    # Record this attempt
    upi_history[upi_id].append(now)

    # --- Standard Amount Matching ---
    if paid_paise != expected_paise:
        fraud_reasons.append(f"Amount Mismatch Expected ₹{session['expected_amount_rupees']:.0f}, Got ₹{paid_amount:.0f}")

    if not fraud_reasons:
        result = {
            'status': 'SUCCESS',
            'paid': paid_amount,
            'expected': session['expected_amount_rupees'],
            'upi_id': upi_id,
            'message': f"₹{paid_amount:.0f} Received ✓"
        }
        session['status'] = 'paid'
        
        # --- AUDIT LOG ---
        log_audit_event(merchant_id, "PAYMENT_RECEIVED", paid_amount, upi_id, "SUCCESS")
        
    else:
        result = {
            'status': 'MISMATCH',  # Keep 'MISMATCH' label for frontend compatibility
            'paid': paid_amount,
            'expected': session['expected_amount_rupees'],
            'upi_id': upi_id,
            'message': f"FRAUD ALERT! { ' | '.join(fraud_reasons) }",
            'fraud_reasons': fraud_reasons
        }
        session['status'] = 'mismatch'

        # --- AUDIT LOG ---
        log_audit_event(merchant_id, "FRAUD_FLAGGED", paid_amount, upi_id, "SUSPICIOUS", {"reasons": fraud_reasons, "expected": session['expected_amount_rupees']})

    socketio.emit('payment_result', result, room=merchant_id)
    
    # Record demo transaction to history
    if merchant_id not in transaction_history:
        transaction_history[merchant_id] = []
    
    # Add timestamp and transaction_id for demo history
    result_copy = result.copy()
    result_copy['timestamp'] = time.time()
    result_copy['transaction_id'] = f"demo_txn_{int(time.time())}"
    result_copy['qr_id'] = qr_id
    transaction_history[merchant_id].append(result_copy)
    
    # Also return result directly so frontend can handle if WebSocket drops
    return jsonify({'success': True, 'result': result_copy})


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
        # In production this should be strictly enforced
        pass  # Skip verification in demo mode

    data = json.loads(payload)
    event = data.get('event')
    
    # Idempotency Check: Don't process the same webhook event twice
    event_id = data.get('event_id') if 'event_id' in data else f"evt_{hashlib.md5(payload.encode()).hexdigest()}"
    if event_id in processed_events:
        return jsonify({'status': 'ok', 'message': 'Duplicate event ignored'})
    processed_events.add(event_id)

    if event == 'qr_code.credited':
        # Extract payment info
        qr_entity = data['payload']['qr_code']['entity']
        payment_entity = data['payload']['payment']['entity']

        qr_id = qr_entity['id']
        amount_received = payment_entity['amount']  # in paise
        transaction_id = payment_entity.get('id', '')

        session = qr_sessions.get(qr_id)
        if not session:
            # Optionally record unmatched payments to an orphan ledger
            return jsonify({'error': 'Session not found'}), 404

        # If already paid, ignore (Secondary idempotency backup)
        if session['status'] == 'paid':
            return jsonify({'status': 'ok', 'message': 'Already processed'})

        expected = session['expected_amount']
        merchant_id = session['merchant_id']
        paid_rupees = float(amount_received) / 100.0
        expected_rupees = float(expected) / 100.0
        
        # In real webhooks, we try to extract payer tracking info if provided 
        # (Razorpay doesn't always expose customer UPI ID in this specific event payload format directly without the Payment entity details, 
        # so for demo purposes, we'll assign a static or hashed ID if missing)
        upi_id = payment_entity.get('vpa', f"vpa_{transaction_id[-6:]}@rzp")

        # --- AI Fraud Detection Checks ---
        now = time.time()
        fraud_reasons = []

        if upi_id in blocked_upi_ids:
            fraud_reasons.append("Blocked UPI ID")

        if paid_rupees < 2.0 or paid_rupees in [1.0, 0.5, 0.01]:
            fraud_reasons.append("Unusual Attempt Value")

        if upi_id not in upi_history:
            upi_history[upi_id] = deque(maxlen=10)
        
        while upi_history[upi_id] and now - upi_history[upi_id][0] > 120:
            upi_history[upi_id].popleft()
            
        if len(upi_history[upi_id]) >= 2:
            fraud_reasons.append("High Frequency (Suspected Bot)")
            blocked_upi_ids.add(upi_id)
            
        upi_history[upi_id].append(now)

        if amount_received != expected:
            fraud_reasons.append(f"Amount Mismatch: Expected ₹{expected_rupees:.0f}, Got ₹{paid_rupees:.0f}")

        # --- Resolve Check ---
        if not fraud_reasons:
            result = {
                'status': 'SUCCESS',
                'paid': paid_rupees,
                'expected': expected_rupees,
                'upi_id': upi_id,
                'message': f"₹{paid_rupees:.0f} Received ✓",
                'transaction_id': transaction_id,
                'timestamp': time.time()
            }
            session['status'] = 'paid'
            # --- AUDIT LOG ---
            log_audit_event(merchant_id, "PAYMENT_RECEIVED", paid_rupees, upi_id, "SUCCESS")
        else:
            result = {
                'status': 'MISMATCH',
                'paid': paid_rupees,
                'expected': expected_rupees,
                'upi_id': upi_id,
                'message': f"FRAUD ALERT! { ' | '.join(fraud_reasons) }",
                'fraud_reasons': fraud_reasons,
                'transaction_id': transaction_id,
                'timestamp': time.time()
            }
            session['status'] = 'mismatch'
            # --- AUDIT LOG ---
            log_audit_event(merchant_id, "FRAUD_FLAGGED", paid_rupees, upi_id, "SUSPICIOUS", {"reasons": fraud_reasons, "expected": expected_rupees})

        # Record to transaction history
        if merchant_id not in transaction_history:
            transaction_history[merchant_id] = []
        transaction_history[merchant_id].append(result)

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


@app.route('/api/history', methods=['GET'])
def get_history():
    """Retrieve transaction history for a specific merchant"""
    merchant_id = request.args.get('merchant_id')
    if not merchant_id:
        return jsonify({'error': 'merchant_id required'}), 400
        
    history = transaction_history.get(merchant_id, [])
    return jsonify({'success': True, 'history': history[-50:]}) # return last 50


@app.route('/api/audit-logs', methods=['GET'])
def get_audit_logs():
    """Retrieve database-backed audit trailing logs for a merchant"""
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
    """Returns analytics data for a specific merchant"""
    merchant_id = request.args.get('merchant_id')
    if not merchant_id:
        return jsonify({'error': 'merchant_id required'}), 400
        
    history = transaction_history.get(merchant_id, [])
    
    total_txns = len(history)
    success_count = sum(1 for t in history if t.get('status') == 'SUCCESS')
    fraud_count = total_txns - success_count
    success_rate = round((success_count / total_txns * 100) if total_txns > 0 else 0, 1)

    # Calculate last 7 days revenue
    from datetime import datetime, timedelta
    today = datetime.now().date()
    
    daily_revenue = { (today - timedelta(days=i)).strftime('%b %d'): 0 for i in range(6, -1, -1) }
    hourly_distribution = { f"{i:02d}:00": 0 for i in range(24) }

    for t in history:
        if 'timestamp' in t:
            dt = datetime.fromtimestamp(t['timestamp'])
            
            # Daily revenue (only successful transactions)
            if t.get('status') == 'SUCCESS':
                date_str = dt.strftime('%b %d')
                if date_str in daily_revenue:
                    daily_revenue[date_str] += t.get('paid', 0)
            
            # Hourly distribution (all transactions)
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
    """Generate a printable HTML receipt for a successful payment"""
    session = qr_sessions.get(qr_id)
    if not session or session.get('status') != 'paid':
        return "Receipt not found or payment not completed.", 404
        
    # In a real app, we'd query the DB/transaction_history for the exact matching transaction.
    # We can reconstruct it from the session details for the demo.
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
            <div class="row"><span>Status:</span> <span>SUCCESS ✓</span></div>
            
            <div class="row total-row">
                <span>TOTAL PAID</span>
                <span>₹{paid_amount}</span>
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
    """Merchant joins their own room to receive payment notifications"""
    merchant_id = data.get('merchant_id', 'default_merchant')
    join_room(merchant_id)
    emit('joined', {'room': merchant_id})


if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
