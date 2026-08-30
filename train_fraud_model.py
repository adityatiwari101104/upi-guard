"""
UPI Guard — Fraud Model Training Script

Run this to train (or retrain) the ML fraud detection model:
    python train_fraud_model.py

Optional: pass a CSV of real labeled transactions to merge with synthetic data:
    python train_fraud_model.py --data real_transactions.csv

The CSV must have columns matching FEATURE_NAMES + 'label' (0=legit, 1=fraud).
"""

import os
import sys
import argparse

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fraud_detector import (
    FraudDetector,
    generate_synthetic_data,
    FEATURE_NAMES,
)


def main():
    parser = argparse.ArgumentParser(description="Train UPI Guard fraud detection model")
    parser.add_argument("--data", type=str, default=None, help="Path to CSV with real labeled transactions")
    parser.add_argument("--samples", type=int, default=5000, help="Number of synthetic samples (default: 5000)")
    parser.add_argument("--fraud-ratio", type=float, default=0.30, help="Fraction of fraud in synthetic data (default: 0.30)")
    args = parser.parse_args()

    print("=" * 60)
    print("  UPI Guard — Fraud Model Training")
    print("=" * 60)

    # Load real data if provided
    extra_data = None
    if args.data:
        import pandas as pd
        if not os.path.exists(args.data):
            print(f"Error: {args.data} not found")
            sys.exit(1)
        extra_data = pd.read_csv(args.data)
        print(f"\nLoaded {len(extra_data)} real transactions from {args.data}")
        print(f"Columns: {list(extra_data.columns)}")

        # Validate columns
        missing = [f for f in FEATURE_NAMES + ["label"] if f not in extra_data.columns]
        if missing:
            print(f"Warning: Missing columns in CSV: {missing}")
            print(f"Required columns: {FEATURE_NAMES + ['label']}")

    # Generate synthetic data
    print(f"\nGenerating {args.samples} synthetic transactions ({args.fraud_ratio:.0%} fraud)...")
    df = generate_synthetic_data(n_samples=args.samples, fraud_ratio=args.fraud_ratio)
    print(f"Generated {len(df)} samples: {int(df['label'].sum())} fraud, {int((df['label'] == 0).sum())} legit")

    # Show class distribution
    print(f"\nFeature statistics:")
    for feat in FEATURE_NAMES:
        mean_val = df[feat].mean()
        std_val = df[feat].std()
        print(f"  {feat:35s}  mean={mean_val:8.3f}  std={std_val:8.3f}")

    # Merge with real data
    if extra_data is not None:
        # Ensure only valid columns are used
        valid_cols = [c for c in FEATURE_NAMES + ["label"] if c in extra_data.columns]
        extra_subset = extra_data[valid_cols].copy()
        df = __import__("pandas").concat([df, extra_subset], ignore_index=True)
        print(f"\nMerged dataset: {len(df)} total samples")

    # Train
    print("\nTraining models...")
    detector = FraudDetector()
    metadata = detector.retrain(extra_data=extra_data)

    # Print final results
    print("\n" + "=" * 60)
    print("  Training Complete!")
    print("=" * 60)

    if metadata.get("classifier_report"):
        r = metadata["classifier_report"]
        print(f"\n  Classifier Performance:")
        print(f"    Precision:  {r['precision']:.2%}")
        print(f"    Recall:     {r['recall']:.2%}")
        print(f"    F1 Score:   {r['f1']:.2%}")
        print(f"    Accuracy:   {r['accuracy']:.2%}")

        cm = r.get("confusion_matrix", [[0, 0], [0, 0]])
        print(f"\n  Confusion Matrix:")
        print(f"                  Predicted")
        print(f"                  Legit  Fraud")
        print(f"    Actual Legit  {cm[0][0]:5d}  {cm[0][1]:5d}")
        print(f"    Actual Fraud  {cm[1][0]:5d}  {cm[1][1]:5d}")

    print(f"\n  Model saved to: ml_models/fraud_model.pkl")
    print(f"  Training samples: {metadata.get('n_samples', '?')}")
    print(f"  Trained at: {metadata.get('trained_at', '?')}")

    # Quick test predictions
    print("\n  Quick test predictions:")
    test_cases = [
        {"desc": "Normal payment", "paid": 250, "expected": 250, "upi": "user1@upi", "merchant": "m1"},
        {"desc": "Probe attack", "paid": 1, "expected": 500, "upi": "probe1@upi", "merchant": "m1"},
        {"desc": "Amount mismatch", "paid": 150, "expected": 250, "upi": "user2@upi", "merchant": "m1"},
        {"desc": "Exact match, known UPI", "paid": 1000, "expected": 1000, "upi": "user1@upi", "merchant": "m2"},
    ]

    for tc in test_cases:
        result = detector.predict(tc["paid"], tc["expected"], tc["upi"], tc["merchant"])
        print(f"    {tc['desc']:30s}  ->  risk={result['risk_score']:3d}/100  verdict={result['ml_verdict']}")
        detector.update_after_decision(tc["upi"], tc["merchant"], is_fraud=(result["risk_score"] >= 70))


if __name__ == "__main__":
    main()
