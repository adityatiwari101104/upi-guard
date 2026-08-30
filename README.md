# UPI Guard — AI-Powered Merchant Payment Verification

Protects shopkeepers from UPI fraud by generating amount-locked QR codes, auto-verifying payments via SMS, and running ML-based fraud detection — all at zero monthly cost.

## How It Works

```
1. Merchant enters bill amount on terminal
2. App generates fixed-amount UPI QR code
3. Customer scans QR and pays via UPI
4. Android app intercepts bank SMS notification
5. SMS forwarded to backend → amount + ref parsed
6. ML fraud detection runs (rules + model)
7. Terminal flashes GREEN (success) or RED (fraud)
```

## Quick Start (Docker)

```bash
# 1. Clone and configure
cp .env.example .env
# Edit .env with your secrets

# 2. Start everything
docker-compose up -d

# 3. Open terminal
open http://localhost:5000
```

This starts 5 services: Flask API, PostgreSQL, Redis, Celery worker, and Celery beat.

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Frontend   │────▶│  Flask API   │────▶│  PostgreSQL  │
│  (React JSX) │     │  (Socket.IO) │     │  (persistent)│
└──────────────┘     └──────┬───────┘     └──────────────┘
                            │
                     ┌──────┴───────┐
                     │    Redis     │
                     │  (sessions)  │
                     └──────┬───────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
       ┌──────┴──────┐ ┌───┴────┐ ┌─────┴─────┐
       │Celery Worker│ │Celery  │ │  Android  │
       │  (tasks)    │ │  Beat  │ │    App    │
       └─────────────┘ └────────┘ └───────────┘
```

## Features

### Payment Modes
- **UPI Direct** — Real UPI QR with merchant VPA, verified via SMS
- **Demo** — Local simulation for testing
- **Mock** — Simulated gateway webhook flow
- **Razorpay Test** — Razorpay test environment
- **Live** — PhonePe checkout integration

### ML Fraud Detection (Hybrid)
- **Rule-based checks**: blocked UPI IDs, unusual amounts, velocity abuse, amount mismatch
- **ML model**: Isolation Forest + Gradient Boosting trained on synthetic data
- **Risk score**: 0-100 combined score shown on every transaction
- **Auto-retrain**: Model can be retrained via API with real transaction data

### Merchant Authentication
- JWT-based login (email + password)
- Each merchant sees only their own data
- API key authentication for SMS webhook
- WebSocket rooms validated per merchant

### Background Processing (Celery)
- Auto-expires stale QR sessions (every 60s)
- Retries failed webhook deliveries (every 30s)
- Generates daily analytics reports (every 24h)

## API Endpoints

### Public
| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Landing page |
| `/terminal` | GET | Merchant terminal |
| `/api/auth/register` | POST | Register new merchant |
| `/api/auth/login` | POST | Login, returns JWT |
| `/api/model-status` | GET | ML model metadata |

### JWT Protected (Authorization: Bearer `<token>`)
| Endpoint | Method | Description |
|---|---|---|
| `/api/auth/me` | GET | Current merchant info |
| `/api/auth/regenerate-api-key` | POST | Rotate API key |
| `/api/create-qr` | POST | Create amount-locked QR |
| `/api/orders` | POST | Create order |
| `/api/orders/<id>` | GET | Get order (own only) |
| `/api/session/<qr_id>` | GET | Session status (own only) |
| `/api/history` | GET | Transaction history |
| `/api/audit-logs` | GET | Audit logs |
| `/api/analytics` | GET | Analytics summary |
| `/api/receipt/<qr_id>` | GET | Payment receipt (own only) |
| `/api/simulate-payment` | POST | Demo payment simulation |
| `/api/retrain-model` | POST | Retrain ML model |
| `/api/check-payment/<qr_id>` | GET | Live payment status check |

### API Key Protected (X-API-Key header)
| Endpoint | Method | Description |
|---|---|---|
| `/sms-webhook` | POST | Forward UPI SMS for verification |

### Webhook (Signature Verified)
| Endpoint | Method | Description |
|---|---|---|
| `/webhook` | POST | Live Razorpay webhook |
| `/webhook/razorpay-mock` | POST | Mock Razorpay webhook |

## Tech Stack

| Component | Technology |
|---|---|
| Backend | Flask + Socket.IO (gthread workers) |
| Database | PostgreSQL 15 |
| Cache/Sessions | Redis 7 |
| Background Tasks | Celery + Redis |
| ML | scikit-learn (Isolation Forest + Gradient Boosting) |
| Auth | JWT (PyJWT) + bcrypt |
| Frontend | React (Babel standalone) + Chart.js |
| QR Generation | segno |
| Containerization | Docker Compose |

## Project Structure

```
upi-guard/
├── app.py                  # Flask backend + routes
├── auth.py                 # JWT authentication module
├── db.py                   # PostgreSQL abstraction layer
├── session_store.py        # Redis-backed session objects
├── fraud_detector.py       # ML fraud detection engine
├── train_fraud_model.py    # Model training script
├── celery_app.py           # Celery configuration
├── tasks.py                # Background tasks
├── ml_models/              # Trained model files
├── static/
│   ├── js/terminal.jsx     # React terminal UI
│   └── css/                # Stylesheets
├── templates/
│   ├── index.html          # Landing page
│   ├── terminal.html       # Terminal shell
│   └── merchant_confirm.html  # Manual payment confirmation
├── docker-compose.yml      # 5-service infrastructure
├── Dockerfile              # App container
├── requirements.txt        # Python dependencies
└── .env.example            # Environment config template
```

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `SECRET_KEY` | Yes | Flask secret key |
| `JWT_SECRET` | Yes | JWT signing secret |
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `REDIS_URL` | Yes | Redis connection string |
| `PAYMENT_MODE_DEFAULT` | No | Default mode: `demo`, `upi_direct`, `live`, `razorpay_test` |
| `MERCHANT_UPI_VPA` | For UPI Direct | Merchant's UPI VPA |
| `RAZORPAY_KEY_ID` | For Razorpay | Razorpay test/live key |
| `RAZORPAY_KEY_SECRET` | For Razorpay | Razorpay secret |
| `PHONEPE_CLIENT_ID` | For Live | PhonePe client ID |
| `PHONEPE_CLIENT_SECRET` | For Live | PhonePe client secret |

## Development

```bash
# Start only infrastructure (no app)
docker-compose up -d db redis

# Run app locally
pip install -r requirements.txt
python app.py

# Train/retrain ML model
python train_fraud_model.py

# Run Celery worker locally
celery -A celery_app worker --loglevel=info

# Run Celery beat locally
celery -A celery_app beat --loglevel=info
```

## License

Academic project — UPI Guard.
