# UPI Guard - Merchant Payment Verification System

Protects shopkeepers from UPI fraud by generating amount-locked QR codes and verifying
received payment vs expected amount with clear green/red alerts.

---

## How It Works

1. Merchant enters bill amount (example: Rs 450)
2. App generates fixed-amount QR
3. Customer scans and pays using PhonePe checkout
4. App checks payment status from PhonePe
5. Merchant screen flashes GREEN (success) or RED (mismatch/fraud)

---

## Setup

### 1) Install dependencies

```bash
pip install -r requirements.txt
```

### 2) Configure environment

```bash
cp .env.example .env
```

Fill these in `.env`:
- `PHONEPE_CLIENT_ID`
- `PHONEPE_CLIENT_SECRET`
- `PHONEPE_CLIENT_VERSION` (usually `1`)
- `PHONEPE_ENVIRONMENT` (`sandbox` or `production`)
- `PAYMENT_MODE_DEFAULT` (`live` or `demo`)

### 3) Run locally

```bash
python app.py
```

Open `http://localhost:5000` then go to `http://localhost:5000/terminal`.

---

## Modes

### Demo mode
- Uses local simulation only
- Shows quick test buttons
- No gateway credentials required

### Live mode (PhonePe)
- Creates PhonePe checkout payment URL and QR
- Polls payment status via backend endpoint
- Applies fraud rules and pushes real-time result to terminal

---

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Public landing page |
| `/terminal` | GET | Merchant terminal |
| `/api/create-qr` | POST | Create amount-locked QR |
| `/api/simulate-payment` | POST | Demo-mode simulated payment |
| `/api/check-payment/<qr_id>` | GET | Live-mode PhonePe status check |
| `/api/session/<qr_id>` | GET | Session fallback status |
| `/api/history` | GET | Merchant transaction history |
| `/api/audit-logs` | GET | Merchant audit logs |
| `/api/analytics` | GET | Merchant analytics summary/charts |
| `/webhook` | POST | Reserved webhook endpoint |

---

## Project Structure

- `app.py` - Flask backend + Socket.IO
- `static/js/terminal.jsx` - React terminal frontend
- `templates/index.html` - public landing page
- `templates/terminal.html` - terminal shell
- `static/css/*` - styles

---

## Notes

- Live gateway behavior depends on your PhonePe account permissions and enabled products.
- For production, always use HTTPS hosting and production credentials.
