# UPI Guard — Merchant Payment Verification System

Protects shopkeepers from UPI fraud by generating **amount-locked QR codes** and verifying
the actual payment received vs. what was expected — with a clear green/red full-screen alert.

---

## How It Works

1. Merchant types the bill amount (e.g. ₹450)
2. App generates a UPI QR locked to exactly ₹450
3. Customer scans and pays
4. Razorpay webhook fires → server verifies amount
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
- `RAZORPAY_KEY_ID` — from Razorpay Dashboard → Settings → API Keys
- `RAZORPAY_KEY_SECRET` — same place
- `RAZORPAY_WEBHOOK_SECRET` — set when creating the webhook (next step)

### 3. Run Locally

```bash
python app.py
```

Open `http://localhost:5000`

> **Note:** Without Razorpay keys, the app runs in **Demo Mode** automatically.
> You can simulate real and fraudulent payments using the on-screen buttons.

### 4. Deploy & Configure Webhook (for real payments)

You need a public HTTPS URL for Razorpay to call your server.

**Deploy to Render (free):**
1. Push this repo to GitHub
2. Create new Web Service on [render.com](https://render.com)
3. Set environment variables in Render dashboard
4. Your URL will be: `https://your-app.onrender.com`

**Configure Razorpay Webhook:**
1. Go to Razorpay Dashboard → Settings → Webhooks
2. Add webhook URL: `https://your-app.onrender.com/webhook`
3. Select event: `qr_code.credited`
4. Copy the webhook secret into your `.env`

---

## Demo Mode

If Razorpay keys aren't set, the app runs in demo mode:
- QR codes are generated locally (standard UPI format)
- Two buttons appear: "Pay Correct Amount" and "Pay ₹1 (Fraud)"
- Full green/red flow works exactly as in production

---

## Project Structure

```
upi-guard/
├── app.py              # Flask backend + WebSocket server
├── templates/
│   └── index.html      # Merchant terminal UI
├── requirements.txt
├── .env.example
└── README.md
```

## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Merchant UI |
| `/api/create-qr` | POST | Generate amount-locked UPI QR |
| `/api/simulate-payment` | POST | Demo: simulate a payment |
| `/webhook` | POST | Razorpay payment webhook |
| `/api/session/:qr_id` | GET | Polling fallback for payment status |

---

## Next Steps / Improvements

- **Multi-merchant support** — auth system, each merchant gets their own dashboard
- **Transaction history** — store all payments in PostgreSQL
- **Physical device** — run on a cheap Android tablet mounted at counter
- **Bluetooth buzzer** — trigger a physical buzzer via BLE on payment result
- **Offline resilience** — queue payments and sync when internet restores
