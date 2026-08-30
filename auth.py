"""
UPI Guard — Merchant Authentication Module

Provides JWT-based auth for merchants and API key auth for webhooks.
"""

import os
import uuid
import time
import secrets
import hashlib
from functools import wraps

import bcrypt
import jwt
from flask import request, jsonify, g


JWT_SECRET = os.getenv("JWT_SECRET", "change-this-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_HOURS = 72


def hash_password(password):
    """Hash a password with bcrypt."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(password, hashed):
    """Verify a password against its bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def generate_token(merchant_id, merchant_name, email):
    """Generate a JWT token for a merchant."""
    payload = {
        "merchant_id": merchant_id,
        "merchant_name": merchant_name,
        "email": email,
        "iat": time.time(),
        "exp": time.time() + (JWT_EXPIRY_HOURS * 3600),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token):
    """Decode and validate a JWT token. Returns payload or None."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def generate_api_key():
    """Generate a random API key for SMS webhook auth."""
    return f"upiguard_{secrets.token_hex(24)}"


def generate_merchant_id():
    """Generate a unique merchant ID."""
    return f"merchant_{uuid.uuid4().hex[:12]}"


# ─────────────────────────────────────────────
# FLASK DECORATORS
# ─────────────────────────────────────────────

def require_auth(f):
    """Decorator: require a valid JWT in Authorization header.
    
    Sets g.merchant_id, g.merchant_name, g.merchant_email on success.
    Returns 401 on failure.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or invalid Authorization header"}), 401

        token = auth_header.split(" ", 1)[1].strip()
        payload = decode_token(token)
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401

        g.merchant_id = payload["merchant_id"]
        g.merchant_name = payload.get("merchant_name", "")
        g.merchant_email = payload.get("email", "")
        return f(*args, **kwargs)
    return decorated


def require_api_key(f):
    """Decorator: require a valid X-API-Key header.
    
    Validates against the api_keys table. Sets g.merchant_id on success.
    Returns 401 on failure.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get("X-API-Key", "").strip()
        if not api_key:
            return jsonify({"error": "Missing X-API-Key header"}), 401

        # Import here to avoid circular imports
        from db import validate_api_key
        merchant_id = validate_api_key(api_key)
        if not merchant_id:
            return jsonify({"error": "Invalid API key"}), 401

        g.merchant_id = merchant_id
        return f(*args, **kwargs)
    return decorated
