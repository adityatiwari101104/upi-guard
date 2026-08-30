"""
UPI Guard — ML Fraud Detection Engine

Hybrid approach: ML risk scoring + existing rule-based checks.
- Isolation Forest: unsupervised anomaly detection (catches novel fraud patterns)
- Gradient Boosting: supervised classifier (trained on synthetic labeled data)
- Combined risk score 0-100 fed alongside existing rules
"""

import os
import time
import pickle
import warnings
from datetime import datetime
from collections import defaultdict

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest, GradientBoostingClassifier
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import classification_report, confusion_matrix
from sklearn.pipeline import Pipeline

warnings.filterwarnings("ignore", category=UserWarning)

MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ml_models")
MODEL_PATH = os.path.join(MODEL_DIR, "fraud_model.pkl")

FEATURE_NAMES = [
    "hour_of_day",
    "day_of_week",
    "amount_ratio",
    "is_exact_match",
    "amount_deviation",
    "upi_txn_count",
    "upi_fraud_rate",
    "is_new_upi_id",
    "time_since_last_txn_from_upi",
    "upi_velocity_120s",
    "merchant_txn_count",
]


# ─────────────────────────────────────────────
# FEATURE ENGINEERING
# ─────────────────────────────────────────────

class FeatureEngineer:
    """Extracts ML features from a transaction and its context."""

    def __init__(self):
        self.upi_stats = defaultdict(lambda: {
            "total_txns": 0,
            "fraud_txns": 0,
            "last_txn_time": -1,
            "recent_timestamps": [],
        })
        self.merchant_txn_counts = defaultdict(int)

    def extract(self, paid_amount, expected_amount, upi_id, merchant_id, timestamp=None):
        """Extract feature vector from a single transaction."""
        if timestamp is None:
            timestamp = time.time()

        dt = datetime.fromtimestamp(timestamp)

        # Amount features
        amount_ratio = paid_amount / expected_amount if expected_amount > 0 else 0.0
        is_exact_match = 1.0 if abs(paid_amount - expected_amount) < 0.01 else 0.0
        amount_deviation = abs(paid_amount - expected_amount) / expected_amount if expected_amount > 0 else 0.0

        # UPI behavior features
        stats = self.upi_stats[upi_id]
        upi_txn_count = stats["total_txns"]
        upi_fraud_rate = stats["fraud_txns"] / max(upi_txn_count, 1)
        is_new_upi_id = 1.0 if upi_txn_count == 0 else 0.0

        time_since_last = -1.0
        if stats["last_txn_time"] > 0:
            time_since_last = timestamp - stats["last_txn_time"]

        # Velocity: count transactions in last 120 seconds
        cutoff = timestamp - 120
        stats["recent_timestamps"] = [t for t in stats["recent_timestamps"] if t > cutoff]
        upi_velocity_120s = len(stats["recent_timestamps"])

        # Merchant feature
        merchant_txn_count = self.merchant_txn_counts[merchant_id]

        features = {
            "hour_of_day": dt.hour + dt.minute / 60.0,
            "day_of_week": dt.weekday(),
            "amount_ratio": amount_ratio,
            "is_exact_match": is_exact_match,
            "amount_deviation": amount_deviation,
            "upi_txn_count": upi_txn_count,
            "upi_fraud_rate": upi_fraud_rate,
            "is_new_upi_id": is_new_upi_id,
            "time_since_last_txn_from_upi": time_since_last,
            "upi_velocity_120s": upi_velocity_120s,
            "merchant_txn_count": merchant_txn_count,
        }

        return features

    def update_stats(self, upi_id, merchant_id, is_fraud, timestamp=None):
        """Update running stats after a transaction is processed."""
        if timestamp is None:
            timestamp = time.time()

        stats = self.upi_stats[upi_id]
        stats["total_txns"] += 1
        if is_fraud:
            stats["fraud_txns"] += 1
        stats["last_txn_time"] = timestamp
        stats["recent_timestamps"].append(timestamp)

        self.merchant_txn_counts[merchant_id] += 1

    def features_to_vector(self, features_dict):
        """Convert feature dict to ordered numpy array."""
        return np.array([features_dict[f] for f in FEATURE_NAMES]).reshape(1, -1)


# ─────────────────────────────────────────────
# SYNTHETIC DATA GENERATOR
# ─────────────────────────────────────────────

def generate_synthetic_data(n_samples=5000, fraud_ratio=0.30, seed=42):
    """Generate labeled synthetic transaction data for training."""
    rng = np.random.RandomState(seed)
    n_fraud = int(n_samples * fraud_ratio)
    n_legit = n_samples - n_fraud

    rows = []

    # --- Legitimate transactions ---
    for _ in range(n_legit):
        hour = rng.choice(range(24), p=_business_hour_probs())
        expected = rng.uniform(20, 5000)
        paid = expected * rng.uniform(0.98, 1.02)  # slight variation
        upi_id = rng.choice([f"user{rng.randint(1,200)}@upi"])
        is_new = 0.0
        velocity = rng.choice([0, 1, 2], p=[0.5, 0.35, 0.15])
        txn_count = rng.randint(1, 50)
        fraud_rate = 0.0
        time_since = rng.uniform(5, 86400)

        rows.append({
            "hour_of_day": hour + rng.uniform(0, 0.99),
            "day_of_week": rng.randint(0, 7),
            "amount_ratio": paid / expected,
            "is_exact_match": 1.0 if abs(paid - expected) < 0.01 else 0.0,
            "amount_deviation": abs(paid - expected) / expected,
            "upi_txn_count": txn_count,
            "upi_fraud_rate": fraud_rate,
            "is_new_upi_id": is_new,
            "time_since_last_txn_from_upi": time_since,
            "upi_velocity_120s": velocity,
            "merchant_txn_count": rng.randint(10, 500),
            "label": 0,  # legit
        })

    # --- Fraud transactions ---
    fraud_patterns = [
        _gen_probe_attack,
        _gen_amount_mismatch,
        _gen_velocity_abuse,
        _gen_new_upi_fraud,
        _gen_unusual_hour,
        _gen_round_amount_probe,
    ]

    for _ in range(n_fraud):
        pattern = rng.choice(fraud_patterns)
        row = pattern(rng)
        row["label"] = 1  # fraud
        rows.append(row)

    df = pd.DataFrame(rows)
    df = df.sample(frac=1, random_state=seed).reset_index(drop=True)
    return df


def _business_hour_probs():
    """Probability distribution favoring business hours."""
    probs = np.ones(24)
    probs[0:7] = 0.1   # late night: low
    probs[7:10] = 1.5   # morning ramp
    probs[10:14] = 2.5  # peak
    probs[14:18] = 2.0  # afternoon
    probs[18:22] = 1.5  # evening
    probs[22:24] = 0.3  # late night
    return probs / probs.sum()


def _gen_probe_attack(rng):
    """Small amount sent against a large expected amount."""
    expected = rng.uniform(200, 5000)
    paid = rng.choice([0.01, 0.50, 1.0, 2.0])
    upi_id = f"probe{rng.randint(1,50)}@upi"
    return {
        "hour_of_day": rng.uniform(0, 24),
        "day_of_week": rng.randint(0, 7),
        "amount_ratio": paid / expected,
        "is_exact_match": 0.0,
        "amount_deviation": (expected - paid) / expected,
        "upi_txn_count": rng.randint(0, 5),
        "upi_fraud_rate": rng.uniform(0.3, 1.0),
        "is_new_upi_id": rng.choice([0.0, 1.0], p=[0.3, 0.7]),
        "time_since_last_txn_from_upi": rng.uniform(0.5, 30),
        "upi_velocity_120s": rng.randint(1, 6),
        "merchant_txn_count": rng.randint(1, 100),
    }


def _gen_amount_mismatch(rng):
    """Paid amount differs from expected by 10-50%."""
    expected = rng.uniform(50, 3000)
    factor = rng.uniform(0.5, 0.9)
    paid = expected * factor
    return {
        "hour_of_day": rng.uniform(0, 24),
        "day_of_week": rng.randint(0, 7),
        "amount_ratio": paid / expected,
        "is_exact_match": 0.0,
        "amount_deviation": (expected - paid) / expected,
        "upi_txn_count": rng.randint(1, 20),
        "upi_fraud_rate": rng.uniform(0.0, 0.5),
        "is_new_upi_id": 0.0,
        "time_since_last_txn_from_upi": rng.uniform(1, 3600),
        "upi_velocity_120s": rng.choice([0, 1, 2]),
        "merchant_txn_count": rng.randint(5, 200),
    }


def _gen_velocity_abuse(rng):
    """Rapid-fire transactions from same UPI ID."""
    expected = rng.uniform(100, 2000)
    paid = expected * rng.uniform(0.9, 1.1)
    return {
        "hour_of_day": rng.uniform(0, 24),
        "day_of_week": rng.randint(0, 7),
        "amount_ratio": paid / expected,
        "is_exact_match": 1.0 if abs(paid - expected) < 0.01 else 0.0,
        "amount_deviation": abs(paid - expected) / expected,
        "upi_txn_count": rng.randint(3, 15),
        "upi_fraud_rate": rng.uniform(0.1, 0.8),
        "is_new_upi_id": 0.0,
        "time_since_last_txn_from_upi": rng.uniform(0.5, 15),
        "upi_velocity_120s": rng.randint(3, 10),
        "merchant_txn_count": rng.randint(1, 50),
    }


def _gen_new_upi_fraud(rng):
    """Brand new UPI ID with suspicious behavior."""
    expected = rng.uniform(100, 5000)
    paid = rng.choice([1, 2, 5, 9, 99]) if rng.random() < 0.6 else expected * rng.uniform(0.6, 0.9)
    return {
        "hour_of_day": rng.uniform(0, 24),
        "day_of_week": rng.randint(0, 7),
        "amount_ratio": paid / expected,
        "is_exact_match": 1.0 if abs(paid - expected) < 0.01 else 0.0,
        "amount_deviation": abs(paid - expected) / expected,
        "upi_txn_count": 0,
        "upi_fraud_rate": 0.0,
        "is_new_upi_id": 1.0,
        "time_since_last_txn_from_upi": -1.0,
        "upi_velocity_120s": rng.randint(0, 3),
        "merchant_txn_count": rng.randint(0, 10),
    }


def _gen_unusual_hour(rng):
    """Transactions at unusual hours (2am-5am)."""
    expected = rng.uniform(50, 1000)
    paid = expected * rng.uniform(0.7, 1.3)
    return {
        "hour_of_day": rng.uniform(2, 5),
        "day_of_week": rng.randint(0, 7),
        "amount_ratio": paid / expected,
        "is_exact_match": 1.0 if abs(paid - expected) < 0.01 else 0.0,
        "amount_deviation": abs(paid - expected) / expected,
        "upi_txn_count": rng.randint(0, 10),
        "upi_fraud_rate": rng.uniform(0.0, 0.4),
        "is_new_upi_id": rng.choice([0.0, 1.0], p=[0.4, 0.6]),
        "time_since_last_txn_from_upi": rng.uniform(1, 7200),
        "upi_velocity_120s": rng.randint(0, 3),
        "merchant_txn_count": rng.randint(1, 100),
    }


def _gen_round_amount_probe(rng):
    """Round number probing (₹9, ₹99, ₹999)."""
    expected = rng.uniform(100, 5000)
    paid = rng.choice([9, 99, 999, 1999])
    if paid >= expected:
        paid = expected * rng.uniform(0.5, 0.9)
    return {
        "hour_of_day": rng.uniform(0, 24),
        "day_of_week": rng.randint(0, 7),
        "amount_ratio": paid / expected,
        "is_exact_match": 0.0,
        "amount_deviation": (expected - paid) / expected,
        "upi_txn_count": rng.randint(0, 8),
        "upi_fraud_rate": rng.uniform(0.2, 0.9),
        "is_new_upi_id": rng.choice([0.0, 1.0], p=[0.5, 0.5]),
        "time_since_last_txn_from_upi": rng.uniform(0.5, 120),
        "upi_velocity_120s": rng.randint(1, 5),
        "merchant_txn_count": rng.randint(1, 50),
    }


# ─────────────────────────────────────────────
# MODEL TRAINING
# ─────────────────────────────────────────────

class FraudModel:
    """Combined Isolation Forest + Gradient Boosting fraud model."""

    def __init__(self):
        self.scaler = StandardScaler()
        self.classifier = GradientBoostingClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.1,
            subsample=0.8,
            random_state=42,
        )
        self.anomaly_detector = IsolationForest(
            n_estimators=100,
            contamination=0.3,
            random_state=42,
        )
        self.is_trained = False
        self.metadata = {
            "trained_at": None,
            "n_samples": 0,
            "features": FEATURE_NAMES,
            "classifier_report": None,
        }

    def train(self, df):
        """Train both models on a labeled DataFrame."""
        X = df[FEATURE_NAMES].values
        y = df["label"].values

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )

        # Scale features
        X_train_scaled = self.scaler.fit_transform(X_train)
        X_test_scaled = self.scaler.transform(X_test)

        # Train classifier (supervised)
        self.classifier.fit(X_train_scaled, y_train)

        # Train anomaly detector (unsupervised) — trained on all data
        X_all_scaled = self.scaler.transform(X)
        self.anomaly_detector.fit(X_all_scaled)

        # Evaluate
        y_pred = self.classifier.predict(X_test_scaled)
        report = classification_report(y_test, y_pred, output_dict=True)
        cm = confusion_matrix(y_test, y_pred)

        self.is_trained = True
        self.metadata["trained_at"] = datetime.now().isoformat()
        self.metadata["n_samples"] = len(df)
        self.metadata["classifier_report"] = {
            "precision": report["1"]["precision"],
            "recall": report["1"]["recall"],
            "f1": report["1"]["f1-score"],
            "accuracy": report["accuracy"],
            "confusion_matrix": cm.tolist(),
        }

        return self.metadata

    def predict(self, features_dict):
        """Predict fraud risk for a single transaction.

        Returns:
            dict with risk_score (0-100), ml_verdict, confidence, contributions
        """
        if not self.is_trained:
            return {
                "risk_score": 50,
                "ml_verdict": "unknown",
                "confidence": 0.0,
                "contributions": {"classifier": 0.5, "anomaly": 0.5},
                "model_available": False,
            }

        X = np.array([features_dict[f] for f in FEATURE_NAMES]).reshape(1, -1)
        X_scaled = self.scaler.transform(X)

        # Classifier probability of fraud
        clf_prob = self.classifier.predict_proba(X_scaled)[0][1]

        # Anomaly score: IsolationForest returns -1 (anomaly) or 1 (normal)
        # Convert to 0-1 probability: -1 → 0.9 (anomaly), 1 → 0.1 (normal)
        raw_anomaly = self.anomaly_detector.decision_function(X_scaled)[0]
        # decision_function: higher = more normal. Map to 0-1 fraud probability.
        anomaly_fraud_prob = max(0.0, min(1.0, 0.5 - raw_anomaly))
        # Ensure anomalies get high fraud probability
        if raw_anomaly < -0.1:
            anomaly_fraud_prob = max(anomaly_fraud_prob, 0.6)
        elif raw_anomaly < 0.0:
            anomaly_fraud_prob = max(anomaly_fraud_prob, 0.4)

        # Weighted combination: classifier 60%, anomaly detector 40%
        combined_prob = 0.6 * clf_prob + 0.4 * anomaly_fraud_prob

        # Scale to 0-100 risk score
        risk_score = int(round(combined_prob * 100))

        # Verdict
        if risk_score >= 70:
            ml_verdict = "suspicious"
        elif risk_score >= 40:
            ml_verdict = "borderline"
        else:
            ml_verdict = "safe"

        return {
            "risk_score": risk_score,
            "ml_verdict": ml_verdict,
            "confidence": round(max(clf_prob, 1 - clf_prob), 3),
            "contributions": {
                "classifier_fraud_prob": round(clf_prob, 4),
                "anomaly_fraud_prob": round(anomaly_fraud_prob, 4),
                "combined_prob": round(combined_prob, 4),
            },
            "model_available": True,
        }


# ─────────────────────────────────────────────
# MAIN DETECTOR (USED BY APP.PY)
# ─────────────────────────────────────────────

class FraudDetector:
    """High-level interface for the Flask app."""

    def __init__(self, model_path=None):
        self.model_path = model_path or MODEL_PATH
        self.feature_engineer = FeatureEngineer()
        self.model = FraudModel()
        self._load_model()

    def _load_model(self):
        """Load trained model from disk if available."""
        if os.path.exists(self.model_path):
            try:
                with open(self.model_path, "rb") as f:
                    saved = pickle.load(f)
                self.model = saved["model"]

                # Restore upi_stats as defaultdict
                raw_stats = saved.get("upi_stats", {})
                self.feature_engineer.upi_stats = defaultdict(
                    lambda: {"total_txns": 0, "fraud_txns": 0, "last_txn_time": -1, "recent_timestamps": []},
                    raw_stats,
                )
                self.feature_engineer.merchant_txn_counts = defaultdict(int, saved.get("merchant_counts", {}))

                print(f"[ML] Fraud model loaded from {self.model_path}")
                print(f"[ML] Trained on {self.model.metadata.get('n_samples', '?')} samples at {self.model.metadata.get('trained_at', '?')}")
                if self.model.metadata.get("classifier_report"):
                    r = self.model.metadata["classifier_report"]
                    print(f"[ML] Classifier -- Precision: {r['precision']:.2f}  Recall: {r['recall']:.2f}  F1: {r['f1']:.2f}")
                return True
            except Exception as e:
                print(f"[ML] Failed to load model: {e}")
        else:
            print("[ML] No trained model found. Run train_fraud_model.py to train one.")
        return False

    def predict(self, paid_amount, expected_amount, upi_id, merchant_id, timestamp=None):
        """Full prediction pipeline: extract features → predict → update stats."""
        features = self.feature_engineer.extract(
            paid_amount=paid_amount,
            expected_amount=expected_amount,
            upi_id=upi_id,
            merchant_id=merchant_id,
            timestamp=timestamp,
        )
        prediction = self.model.predict(features)
        prediction["features"] = features
        return prediction

    def update_after_decision(self, upi_id, merchant_id, is_fraud, timestamp=None):
        """Update running stats after a transaction is finalized."""
        self.feature_engineer.update_stats(upi_id, merchant_id, is_fraud, timestamp)

    def retrain(self, extra_data=None):
        """Retrain the model. Optionally merge extra real data."""
        print("[ML] Generating synthetic training data...")
        df = generate_synthetic_data(n_samples=5000)

        if extra_data is not None and len(extra_data) > 0:
            print(f"[ML] Merging {len(extra_data)} real transactions...")
            df = pd.concat([df, extra_data], ignore_index=True)

        print(f"[ML] Training on {len(df)} samples...")
        metadata = self.model.train(df)

        # Save model + state
        os.makedirs(MODEL_DIR, exist_ok=True)
        with open(self.model_path, "wb") as f:
            pickle.dump({
                "model": self.model,
                "upi_stats": dict(self.feature_engineer.upi_stats),
                "merchant_counts": dict(self.feature_engineer.merchant_txn_counts),
            }, f)

        print(f"[ML] Model saved to {self.model_path}")
        if metadata.get("classifier_report"):
            r = metadata["classifier_report"]
            print(f"[ML] Results — Precision: {r['precision']:.2f}  Recall: {r['recall']:.2f}  F1: {r['f1']:.2f}")
            print(f"[ML] Confusion matrix: {r['confusion_matrix']}")

        return metadata

    def get_status(self):
        """Return model metadata for the API endpoint."""
        return {
            "model_loaded": self.model.is_trained,
            "model_path": self.model_path,
            "metadata": self.model.metadata,
        }
