"""
03_eval_topic_classifier.py
---------------------------
Offline eval comparing the LLM-classified `inferred_topic` against the
synthetic `ground_truth_topic` anchor for every enriched row in
`non_purchasers_feedback`.

Prints:
  - Overall accuracy
  - Per-topic precision / recall / F1 + row support
  - Confusion matrix (truth × predicted)
  - Average topic_confidence on correct vs incorrect predictions
  - Lowest-confidence mistakes for spot-checking

Run after every enrichment (or after a Topic Classifier prompt change)
to catch regressions.

Usage
-----
    python3 data_prep/03_eval_topic_classifier.py
    python3 data_prep/03_eval_topic_classifier.py --csv eval_report.csv
    python3 data_prep/03_eval_topic_classifier.py --show-mistakes 25
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path


try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

try:
    import mysql.connector
except ImportError:
    print("ERROR: mysql-connector-python is not installed.\n"
          "Run:   pip3 install mysql-connector-python")
    sys.exit(1)


# Mirror the canonical topic list in 02_enrich_topics.py so the eval
# stays in lockstep with the classifier's allowed vocabulary.
ALLOWED_TOPICS = [
    "Design Unavailable",
    "Size Unavailable",
    "Stock Unavailable",
    "Price Too High",
    "Quality Concerns",
    "Weight Concerns",
    "Color/Finish Mismatch",
    "Customization Not Offered",
    "Sales Service",
    "Others",
]


MYSQL_CONFIG = {
    "host":     os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port":     int(os.environ.get("MYSQL_PORT", "3306")),
    "user":     os.environ.get("MYSQL_USER", "ram"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DB", "merchandising"),
}


def fetch_enriched(conn) -> list[dict]:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        """
        SELECT feedback_id, ground_truth_topic, inferred_topic,
               topic_confidence, reason_for_non_purchase
          FROM non_purchasers_feedback
         WHERE inferred_topic IS NOT NULL
        """
    )
    rows = cur.fetchall()
    cur.close()
    return rows


def build_confusion(rows: list[dict], topics: list[str]) -> dict:
    """Return cm[truth][pred] = count, ignoring rows with out-of-vocab values."""
    cm = {t: {p: 0 for p in topics} for t in topics}
    for r in rows:
        gt = r["ground_truth_topic"]
        pred = r["inferred_topic"]
        if gt in cm and pred in cm[gt]:
            cm[gt][pred] += 1
    return cm


def per_topic_metrics(cm: dict, topics: list[str]) -> dict:
    out = {}
    for t in topics:
        tp = cm[t][t]
        fp = sum(cm[gt][t] for gt in topics if gt != t)
        fn = sum(cm[t][p] for p in topics if p != t)
        support = sum(cm[t][p] for p in topics)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall    = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0 else 0.0
        )
        out[t] = {
            "precision": precision,
            "recall":    recall,
            "f1":        f1,
            "support":   support,
        }
    return out


def print_confusion(cm: dict, topics: list[str]) -> None:
    """Compact confusion matrix — truth rows, predicted columns."""
    # 3-letter column headers, e.g. "Design Unavailable" -> "Des"
    cols = [t[:3] for t in topics]
    header = " " * 30 + "  ".join(f"{c:>3}" for c in cols)
    print(header)
    for gt in topics:
        cells = "  ".join(f"{cm[gt][p]:>3}" for p in topics)
        print(f"{gt[:28]:<30}{cells}")


def main():
    parser = argparse.ArgumentParser(description="Eval the Topic Classifier output.")
    parser.add_argument("--csv", help="Optional path to write per-row results CSV")
    parser.add_argument(
        "--show-mistakes", type=int, default=10,
        help="Show top-N lowest-confidence mistakes (default 10)",
    )
    args = parser.parse_args()

    if not MYSQL_CONFIG["password"]:
        print("ERROR: MYSQL_PASSWORD not set in environment / .env.")
        sys.exit(1)

    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
    except mysql.connector.Error as err:
        print(f"ERROR connecting to MySQL: {err}")
        sys.exit(1)

    try:
        rows = fetch_enriched(conn)
    finally:
        conn.close()

    if not rows:
        print("No enriched rows found. Run 02_enrich_topics.py first.")
        return

    total = len(rows)
    correct = sum(1 for r in rows if r["ground_truth_topic"] == r["inferred_topic"])
    accuracy = correct / total

    cm = build_confusion(rows, ALLOWED_TOPICS)
    metrics = per_topic_metrics(cm, ALLOWED_TOPICS)

    conf_correct = [
        float(r["topic_confidence"] or 0)
        for r in rows if r["ground_truth_topic"] == r["inferred_topic"]
    ]
    conf_wrong = [
        float(r["topic_confidence"] or 0)
        for r in rows if r["ground_truth_topic"] != r["inferred_topic"]
    ]
    avg_conf_correct = sum(conf_correct) / len(conf_correct) if conf_correct else 0.0
    avg_conf_wrong = sum(conf_wrong) / len(conf_wrong) if conf_wrong else 0.0

    print("\n=== Topic Classifier Eval ===")
    print(f"Total enriched rows:        {total}")
    print(f"Overall accuracy:           {accuracy * 100:.2f}% ({correct}/{total})")
    print(f"Avg confidence (correct):   {avg_conf_correct:.3f}")
    print(f"Avg confidence (incorrect): {avg_conf_wrong:.3f}")

    print("\n--- Per-topic metrics ---")
    print(f"{'Topic':<28} {'Prec':>7} {'Rec':>7} {'F1':>7} {'Support':>8}")
    for t in ALLOWED_TOPICS:
        m = metrics[t]
        print(
            f"{t:<28} "
            f"{m['precision'] * 100:>6.1f}% "
            f"{m['recall']    * 100:>6.1f}% "
            f"{m['f1']        * 100:>6.1f}% "
            f"{m['support']:>8}"
        )

    print("\n--- Confusion matrix (truth ↓ / predicted →; 3-letter headers) ---")
    print_confusion(cm, ALLOWED_TOPICS)

    print(f"\n--- Top {args.show_mistakes} lowest-confidence mistakes ---")
    mistakes = sorted(
        [r for r in rows if r["ground_truth_topic"] != r["inferred_topic"]],
        key=lambda r: float(r["topic_confidence"] or 0),
    )[: args.show_mistakes]
    if not mistakes:
        print("  (none — every row classified correctly)")
    for m in mistakes:
        reason = (m["reason_for_non_purchase"] or "")[:90]
        print(
            f"  id={m['feedback_id']:<5} conf={float(m['topic_confidence'] or 0):.2f}  "
            f"truth={m['ground_truth_topic']:<28} pred={m['inferred_topic']}"
        )
        print(f"      reason: {reason}...")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "feedback_id", "ground_truth_topic", "inferred_topic",
                "topic_confidence", "correct",
            ])
            for r in rows:
                w.writerow([
                    r["feedback_id"], r["ground_truth_topic"], r["inferred_topic"],
                    r["topic_confidence"],
                    int(r["ground_truth_topic"] == r["inferred_topic"]),
                ])
        print(f"\nWrote per-row CSV to {args.csv}")


if __name__ == "__main__":
    main()
