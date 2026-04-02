# UPI Guard — Merchant Payment Verification System

Protects shopkeepers from UPI fraud by generating **amount-locked QR codes** and verifying
the actual payment received vs. what was expected — with a clear green/red full-screen alert.

---

## How It Works

1. Merchant types the bill amount (e.g. ₹450)
2. App generates a UPI QR locked to exactly ₹450
3. Customer scans and pays
4. Cashfree webhook fires → server verifies amount
5. Merchant screen flashes **GREEN ✓** (correct) or **RED ✗** (mismatch/fraud)

---

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `CASHFREE_CLIENT_ID` — from Cashfree Merchant Dashboard → Developers → API Keys
- `CASHFREE_CLIENT_SECRET` — same place
- `CASHFREE_WEBHOOK_SECRET` — set when creating the webhook (next step)
- `CASHFREE_ENVIRONMENT` — `sandbox` for testing, `production` for real payments

### 3. Run Locally

```bash
python app.py
```

Open `http://localhost:5000` for the public landing page, then click **Open Terminal**.
Direct terminal URL: `http://localhost:5000/terminal`

Use the mode selector on the terminal screen:
- **Live**: creates real Cashfree UPI QR and waits for webhook confirmation.
- **Demo**: creates local test QR and enables simulation buttons.

### 4. Deploy & Configure Webhook (for real payments)

You need a public HTTPS URL for Cashfree to call your server.

**Deploy to Render (free):**
1. Push this repo to GitHub
2. Create new Web Service on [render.com](https://render.com)
3. Set environment variables in Render dashboard
4. Your URL will be: `https://your-app.onrender.com`

**Configure Cashfree Webhook:**
1. Go to Cashfree Merchant Dashboard → Payment Gateway → Developers → Webhook
2. Add webhook URL: `https://your-app.onrender.com/webhook`
3. Select event: `PAYMENT_SUCCESS_WEBHOOK`
4. Copy the webhook secret into your `.env`

---

## Demo Mode

If you select Demo mode, the app runs with local simulation:
- QR codes are generated locally (standard UPI format)
- Two buttons appear: "Pay Correct Amount" and "Pay ₹1 (Fraud)"
- Full green/red flow works exactly as in production

## Live Mode

If you select Live mode:
- Cashfree keys and webhook secret must be configured in `.env`
- QR generation failure returns an error (no silent fallback)
- Payment success/fraud is decided from real Cashfree webhook events

---

## Project Structure

```
upi-guard/
├── app.py              # Flask backend + WebSocket server
├── static/
│   ├── css/
│   │   ├── landing.css     # Public landing page styles
│   │   └── terminal.css    # Terminal app styles
│   └── js/
│       └── terminal.jsx    # React terminal frontend
├── templates/
│   ├── index.html       # Public landing page
│   └── terminal.html    # React mount shell for terminal
├── requirements.txt
├── .env.example
└── README.md
```

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Public landing page |
| `/terminal` | GET | Merchant terminal (React frontend) |
| `/api/create-qr` | POST | Generate amount-locked UPI QR |
| `/api/simulate-payment` | POST | Demo: simulate a payment |
| `/webhook` | POST | Cashfree payment webhook |
| `/api/session/:qr_id` | GET | Polling fallback for payment status |

---

## Frontend Stack

- Public marketing/entry page at `/`.
- Merchant dashboard at `/terminal` built in **React**.
- Styles and scripts split into static files for easier maintenance.

---

## Next Steps / Improvements

- **Multi-merchant support** — auth system, each merchant gets their own dashboard
- **Transaction history** — store all payments in PostgreSQL
- **Physical device** — run on a cheap Android tablet mounted at counter
- **Bluetooth buzzer** — trigger a physical buzzer via BLE on payment result
- **Offline resilience** — queue payments and sync when internet restores
