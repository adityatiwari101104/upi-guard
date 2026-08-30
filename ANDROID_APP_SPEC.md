# UPI Guard — Android App Spec

## What This App Does
Intercepts UPI credit SMS on the merchant's phone and forwards them to the UPI Guard backend for automatic payment verification. This replaces expensive POS machines — zero monthly cost.

## How It Works
```
Customer pays → Bank sends SMS to merchant's phone → This app intercepts it →
POSTs to backend → Backend verifies amount → Terminal shows GREEN/RED alert
```

## What You Need to Build

### 1. SMS BroadcastReceiver
- Listen for incoming SMS (`android.provider.Telephony.SMS_RECEIVED`)
- Extract message body from PDU
- Filter: only forward if message contains UPI keywords ("credited", "received", "UPI", "Rs.")
- Ignore non-UPI SMS

### 2. POST to Backend
```
POST https://upi-guard-production-6782.up.railway.app/sms-webhook
Headers:
  Content-Type: application/json
  X-API-Key: <merchant's API key>
Body:
  {"message": "<full SMS text>"}
```

The backend automatically:
- Parses amount and UPI ref from SMS
- Matches against merchant's pending payment
- Runs fraud detection
- Pushes result to merchant's terminal via WebSocket

### 3. Foreground Service
- Keep the app alive in background (Android kills background apps otherwise)
- Show persistent notification: "UPI Guard — Payment verification active"
- Use `START_STICKY` so service restarts if killed

### 4. Simple Config UI
One screen with:
- **Server URL** input (e.g., `https://upi-guard-production-6782.up.railway.app`)
- **API Key** input ( merchant gets this after registering)
- **Save** button (stores in SharedPreferences)
- **Status** indicator (green dot = connected, red = not)

### 5. Permissions Needed
```xml
<uses-permission android:name="android.permission.RECEIVE_SMS" />
<uses-permission android:name="android.permission.READ_SMS" />
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
```

## Example SMS That Should Be Forwarded
```
Rs.250 credited to your account. UPI Ref No. 512345678901.
- State Bank of India
```

## Example SMS That Should Be IGNORED
```
Your OTP is 123456. Do not share.
Your account balance is Rs 5000.
```

## API Key
The merchant gets their API key when they register on the terminal:
1. Open terminal → Register with email + password + UPI VPA
2. After registration, API key is shown on screen
3. Copy that key into the Android app's config

## Tech Stack (Your Choice)
- Kotlin + Jetpack Compose (recommended)
- Or Java + XML
- OkHttp for HTTP requests
- Minimum SDK: 26 (Android 8.0)

## Testing
1. Install app on phone
2. Enter server URL + API key
3. Open terminal on laptop → create QR → note the amount
4. Have someone pay that amount via UPI
5. SMS arrives → app forwards → terminal shows result

## Server Details
- **Base URL**: `https://upi-guard-production-6782.up.railway.app`
- **SMS Endpoint**: `POST /sms-webhook`
- **Auth Header**: `X-API-Key: <key>`
- **Content-Type**: `application/json`
- **Body**: `{"message": "full SMS text here"}`

## What Happens on Success
Backend returns:
```json
{"status": "ok", "amount": 250.0, "transaction_ref": "512345678901"}
```
Merchant's terminal automatically shows GREEN "Rs 250 Received" alert.

## What Happens on Mismatch
Backend returns:
```json
{"status": "mismatch", "message": "FRAUD ALERT! Amount Mismatch"}
```
Merchant's terminal shows RED "Fraud Alert" with details.
