"""
02_enrich_topics.py
-------------------
One-time pre-flight enrichment for the chat agentic workflow.

For every row in `non_purchasers_feedback`, this script calls Claude Haiku 4.5
(via aisuite) to classify the verbose `reason_for_non_purchase` text into:

    inferred_topic     : one of 10 canonical topics
    topic_confidence   : float in [0.0, 1.0]
    non_purchase_type     : the kind of attribute the customer asked about
    attribute_value    : the specific attribute value, normalized
                         (e.g. "star design", "size 8", "rose gold")

The four columns are written back to the same table. Once this runs, the
Coder agent in the chat workflow can write clean SQL against structured
columns instead of fighting with free-text LIKE patterns.

Run
---
    python3 02_enrich_topics.py --dry-run     # preview 5 rows, no DB writes
    python3 02_enrich_topics.py --limit 20    # process first 20 rows
    python3 02_enrich_topics.py               # full run (~25-35 min serial)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


# -----------------------------------------------------------------------------
# ## Bootstrap — load environment variables from .env
#
# This block runs first so that downstream code can read MYSQL_PASSWORD and
# ANTHROPIC_API_KEY from os.environ without the user having to `export` them
# in the shell. python-dotenv is optional — if it isn't installed, we
# silently fall back to whatever the shell already has.
# -----------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    # .env lives at the project root (one level above data_prep/)
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass


# -----------------------------------------------------------------------------
# ## Imports that depend on optional packages
#
# We check for each third-party package separately and print a helpful
# install hint if it's missing. This avoids cryptic ImportError tracebacks
# the first time someone runs the script on a fresh machine.
# -----------------------------------------------------------------------------
try:
    import mysql.connector
except ImportError:
    print("ERROR: mysql-connector-python is not installed.\n"
          "Run:   pip3 install mysql-connector-python")
    sys.exit(1)

try:
    import aisuite as ai
except ImportError:
    print("ERROR: aisuite is not installed.\n"
          "Run:   pip3 install 'aisuite[anthropic]'")
    sys.exit(1)


# -----------------------------------------------------------------------------
# ## Configuration
#
# Everything user-tunable lives here. The model is Haiku 4.5 (cheap and fast,
# good enough for classification). MySQL config is read from .env so the
# same script can be pointed at different databases without code changes.
# -----------------------------------------------------------------------------
MODEL = "anthropic:claude-haiku-4-5-20251001"

MYSQL_CONFIG = {
    "host":     os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port":     int(os.environ.get("MYSQL_PORT", "3306")),
    "user":     os.environ.get("MYSQL_USER", "ram"),
    "password": os.environ.get("MYSQL_PASSWORD", ""),
    "database": os.environ.get("MYSQL_DB", "merchandising"),
}


# -----------------------------------------------------------------------------
# ## Closed enums — the only allowed values for topic & non_purchase_type
#
# The validator below rejects any LLM output that uses a value outside these
# lists. Keeping the lists in code (not just in the prompt) gives us a hard
# guard against silent vocabulary drift in the inferred_topic column.
# -----------------------------------------------------------------------------
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

ALLOWED_NON_PURCHASE_TYPES = [
    "design",        # specific design / pattern the customer wanted
    "size",          # specific size
    "color",         # color or finish (rose gold, white gold, etc.)
    "weight",        # weight preference
    "price",         # price band / budget
    "customization", # engraving, made-to-order, etc.
    "service",       # service-related issues (no specific attribute)
    "stock",         # item simply out of stock (no specific attribute)
    "none",          # no specific attribute can be extracted
]


# -----------------------------------------------------------------------------
# ## System prompt — instructions + few-shot examples
#
# Three things are baked into this prompt so the model produces a
# consistent, queryable `attribute_value`:
#
#   1. The 10 allowed topics and 9 allowed non_purchase_types (closed enums).
#   2. The strict topic-to-non_purchase_type mapping so the two columns never
#      contradict each other (e.g. topic="Design Unavailable" + non_purchase_type="size" is invalid).
#   3. Normalization rules per non_purchase_type, anchored by 8 worked
#      examples covering the most common patterns. Without these, Haiku
#      would freelance the format (sometimes "8", sometimes "size 8",
#      sometimes "size eight") and break SQL grouping later.
# -----------------------------------------------------------------------------
SYSTEM_PROMPT = f"""You are a topic classifier for jewelry-store non-purchase feedback.

You will be given a customer's free-text reason for not buying, along with the
product they were looking at. Output a JSON object with exactly these four
fields:

  "inferred_topic":    one of {ALLOWED_TOPICS}
  "topic_confidence":  a float in [0.0, 1.0]
  "non_purchase_type":    one of {ALLOWED_NON_PURCHASE_TYPES}
  "attribute_value":   a short normalized string, OR null (see rules below).

## General rules
- Pick the topic that best matches the customer's primary reason.
- If the reason is ambiguous, set `topic_confidence` below 0.6 and prefer "Others".
- If the customer is just postponing, browsing, or wants to consult family,
  classify as "Others".
- `non_purchase_type` must match the topic (see mapping below). Inconsistent
  pairs are invalid output.

## Topic → non_purchase_type mapping (mandatory)
  Design Unavailable        → "design"
  Size Unavailable          → "size"
  Color/Finish Mismatch     → "color"
  Weight Concerns           → "weight"
  Price Too High            → "price"
  Customization Not Offered → "customization"
  Sales Service             → "service"
  Stock Unavailable         → "stock"
  Quality Concerns          → "none"
  Others                    → "none"

## Normalization rules for `attribute_value`
- design: short lowercase noun phrase for the design pattern.
    Examples: "star design", "floral carving", "moon motif", "jhumka",
              "kundan", "temple design", "minimalist", "flower motif".
- size: an explicit size with units when present. Keep the unit; do not
    drop "size" or "inch".
    Examples: "size 8", "size 2.6", "10 inch", "16 inch", "kids size",
              "adjustable".
- color: the metal/finish phrase, lowercase.
    Examples: "rose gold", "white gold", "antique finish", "silver tone".
- weight: a weight phrase with units when present.
    Examples: "under 8g", "around 12g", "lightweight".
- price: a budget phrase, lowercase.
    Examples: "under 10k", "under 25k", "below 1 lakh".
- customization: the kind of customization requested.
    Examples: "engraving", "made-to-order", "design modification".
- service, stock, none: set `attribute_value` to null. There is no specific
  attribute being asked for in these cases.

## Examples

EXAMPLE 1
Product: Bangles
Reason: "I came specifically looking for a star design bangle but the store didn't have it."
Output: {{"inferred_topic": "Design Unavailable", "topic_confidence": 0.95, "non_purchase_type": "design", "attribute_value": "star design"}}

EXAMPLE 2
Product: Finger Rings
Reason: "I needed size 8 but they only had bigger sizes."
Output: {{"inferred_topic": "Size Unavailable", "topic_confidence": 0.95, "non_purchase_type": "size", "attribute_value": "size 8"}}

EXAMPLE 3
Product: Anklets
Reason: "Was looking for a 10 inch anklet; the sizes didn't fit my daughter."
Output: {{"inferred_topic": "Size Unavailable", "topic_confidence": 0.92, "non_purchase_type": "size", "attribute_value": "10 inch"}}

EXAMPLE 4
Product: Ear Rings
Reason: "Wanted a rose gold finish on ear rings; only yellow gold was available."
Output: {{"inferred_topic": "Color/Finish Mismatch", "topic_confidence": 0.95, "non_purchase_type": "color", "attribute_value": "rose gold"}}

EXAMPLE 5
Product: Necklace
Reason: "Found a beautiful necklace but it crossed my under 25k budget by a lot."
Output: {{"inferred_topic": "Price Too High", "topic_confidence": 0.93, "non_purchase_type": "price", "attribute_value": "under 25k"}}

EXAMPLE 6
Product: Necklace
Reason: "Asked if the necklace can be customized with engraving; was told no."
Output: {{"inferred_topic": "Customization Not Offered", "topic_confidence": 0.94, "non_purchase_type": "customization", "attribute_value": "engraving"}}

EXAMPLE 7
Product: Ear Rings
Reason: "The ear rings I wanted to buy is completely out of stock at the moment."
Output: {{"inferred_topic": "Stock Unavailable", "topic_confidence": 0.95, "non_purchase_type": "stock", "attribute_value": null}}

EXAMPLE 8
Product: Bangles
Reason: "Will buy after the upcoming festival sale; postponing the bangle purchase."
Output: {{"inferred_topic": "Others", "topic_confidence": 0.88, "non_purchase_type": "none", "attribute_value": null}}

Output ONLY the JSON object. No markdown fences, no commentary."""


# -----------------------------------------------------------------------------
# ## User-message template
#
# Each row of the table gets injected into this template. Two fields go in:
# the product the customer was looking at (helps the model disambiguate
# product-specific designs like "jhumka" for ear rings) and the verbose
# reason text itself.
# -----------------------------------------------------------------------------
USER_TEMPLATE = """Product looking for: {product}
Reason for non-purchase: {reason}

Return the JSON object."""


# =============================================================================
# ## DB helpers
# =============================================================================

# -----------------------------------------------------------------------------
# ### `ensure_columns` — idempotent schema migration
#
# Adds the four enrichment columns to `non_purchasers_feedback` if they
# don't already exist. Safe to call on every run; columns are added only
# once. We check INFORMATION_SCHEMA instead of catching exceptions so the
# logic is explicit and the user can see in the output which columns the
# script created.
# -----------------------------------------------------------------------------
def ensure_columns(conn) -> None:
    additions = [
        ("inferred_topic",   "VARCHAR(40)"),
        ("topic_confidence", "FLOAT"),
        ("non_purchase_type",   "VARCHAR(40)"),
        ("attribute_value",  "VARCHAR(200)"),
    ]
    cur = conn.cursor()
    cur.execute(
        """SELECT COLUMN_NAME
             FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME   = 'non_purchasers_feedback'""",
        (MYSQL_CONFIG["database"],),
    )
    existing = {r[0] for r in cur.fetchall()}
    for col, ddl in additions:
        if col not in existing:
            print(f"  + adding column {col} ({ddl})")
            cur.execute(
                f"ALTER TABLE non_purchasers_feedback ADD COLUMN {col} {ddl}"
            )
    conn.commit()
    cur.close()


# -----------------------------------------------------------------------------
# ### `fetch_unenriched` — pick up only rows that still need classification
#
# Returns rows where `inferred_topic IS NULL`. This is what makes the script
# resumable — a crashed or partial run can be restarted and it will pick up
# exactly where it stopped, since successfully enriched rows now have a
# non-NULL topic and are skipped.
# -----------------------------------------------------------------------------
def fetch_unenriched(conn, limit: int | None = None) -> list[dict]:
    cur = conn.cursor(dictionary=True)
    sql = (
        "SELECT feedback_id, product_looking_for, reason_for_non_purchase "
        "FROM non_purchasers_feedback "
        "WHERE inferred_topic IS NULL "
        "ORDER BY feedback_id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()
    return rows


# -----------------------------------------------------------------------------
# ### `fetch_sample` — used only in --dry-run mode
#
# Pulls the first 5 rows regardless of enrichment state, so dry-run works
# even before the columns have been added. Read-only by design.
# -----------------------------------------------------------------------------
def fetch_sample(conn, n: int = 5) -> list[dict]:
    cur = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT feedback_id, product_looking_for, reason_for_non_purchase "
        "FROM non_purchasers_feedback "
        f"ORDER BY feedback_id LIMIT {int(n)}"
    )
    rows = cur.fetchall()
    cur.close()
    return rows


# -----------------------------------------------------------------------------
# ### `update_row` — write one classified row back to MySQL
#
# Called per-row after the LLM returns a validated result. We don't batch
# writes in this serial version because the bottleneck is the LLM API
# call, not the DB. Each commit isolates one row, so a mid-run crash never
# corrupts anything.
# -----------------------------------------------------------------------------
def update_row(conn, feedback_id: int, result: dict) -> None:
    cur = conn.cursor()
    cur.execute(
        """UPDATE non_purchasers_feedback
              SET inferred_topic   = %s,
                  topic_confidence = %s,
                  non_purchase_type   = %s,
                  attribute_value  = %s
            WHERE feedback_id = %s""",
        (
            result["inferred_topic"],
            float(result["topic_confidence"]),
            result["non_purchase_type"],
            result.get("attribute_value"),
            feedback_id,
        ),
    )
    conn.commit()
    cur.close()


# =============================================================================
# ## LLM call + validation
# =============================================================================

# -----------------------------------------------------------------------------
# ### `classify` — single LLM call per row
#
# Sends the system prompt (with all 8 few-shot examples) and the user
# message (this row's product + reason) to Claude Haiku via aisuite.
# Temperature is 0 for determinism — same input always produces the same
# classification, which keeps re-runs stable. max_tokens is capped at 200
# because the output is a tiny JSON object; this also protects against
# runaway generations if the model misbehaves.
#
# We strip an optional ```json ... ``` markdown fence defensively, even
# though the prompt forbids fences. Claude usually respects the rule, but
# the strip is cheap insurance.
# -----------------------------------------------------------------------------
def classify(client, product: str, reason: str) -> dict:
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": USER_TEMPLATE.format(product=product, reason=reason)},
        ],
        temperature=0,
        max_tokens=200,
    )
    text = resp.choices[0].message.content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


# -----------------------------------------------------------------------------
# ### `validate` — hard guard against bad model output
#
# Even with a tight prompt, a model can occasionally drift: misspell a
# topic, return a number as a string, hallucinate a new non_purchase_type.
# This function checks the four invariants:
#   1. inferred_topic is in the allowed enum
#   2. non_purchase_type is in the allowed enum
#   3. topic_confidence is a float in [0, 1]
#   4. attribute_value is either null or a string
# Failures are surfaced to main(), which logs the row as failed and leaves
# inferred_topic NULL so the next run retries it.
# -----------------------------------------------------------------------------
def validate(result: dict) -> tuple[bool, str]:
    if result.get("inferred_topic") not in ALLOWED_TOPICS:
        return False, f"unknown topic {result.get('inferred_topic')!r}"
    if result.get("non_purchase_type") not in ALLOWED_NON_PURCHASE_TYPES:
        return False, f"unknown non_purchase_type {result.get('non_purchase_type')!r}"
    try:
        c = float(result.get("topic_confidence"))
        if not (0.0 <= c <= 1.0):
            return False, f"confidence out of range: {c}"
    except (TypeError, ValueError):
        return False, "confidence is not a number"
    av = result.get("attribute_value")
    if av is not None and not isinstance(av, str):
        return False, f"attribute_value is not a string or null: {type(av).__name__}"
    return True, "ok"


# =============================================================================
# ## Main entry point
# =============================================================================

# -----------------------------------------------------------------------------
# ### `main` — orchestrates the run
#
# Three modes:
#   --dry-run : print classifications for 5 sample rows. No schema change,
#               no DB writes. Use this first to eyeball the prompt's output.
#   --limit N : process only the first N unenriched rows. Use to confirm
#               end-to-end behavior on a small batch before the full run.
#   (no flags): full run. Idempotent; safe to re-run.
#
# Before doing anything we verify that the two required secrets
# (ANTHROPIC_API_KEY, MYSQL_PASSWORD) are present, so the script fails
# fast with a clear message rather than mid-way through.
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=(
            "Enrich non_purchasers_feedback with topic + attribute via "
            "Claude Haiku 4.5."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview classification on 5 rows. No DB schema change, no row updates.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only process the first N unenriched rows (useful for partial runs).",
    )
    args = parser.parse_args()

    # --- Sanity-check the environment -------------------------------------
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not found. Add it to .env.")
        sys.exit(1)
    if not MYSQL_CONFIG["password"]:
        print("ERROR: MYSQL_PASSWORD not found. Add it to .env.")
        sys.exit(1)

    print(f"Model:    {MODEL}")
    print(f"Database: {MYSQL_CONFIG['user']}@{MYSQL_CONFIG['host']}"
          f"/{MYSQL_CONFIG['database']}")
    print()

    client = ai.Client()

    try:
        conn = mysql.connector.connect(**MYSQL_CONFIG)
    except mysql.connector.Error as err:
        print(f"ERROR connecting to MySQL: {err}")
        sys.exit(1)

    # --- DRY RUN branch ----------------------------------------------------
    if args.dry_run:
        print("DRY RUN — fetching 5 sample rows, no DB writes.\n")
        rows = fetch_sample(conn, n=5)
        for i, row in enumerate(rows, 1):
            print(f"[{i}] feedback_id={row['feedback_id']}  "
                  f"product={row['product_looking_for']}")
            print(f"    reason: {row['reason_for_non_purchase']}")
            try:
                result = classify(
                    client,
                    product=row["product_looking_for"],
                    reason=row["reason_for_non_purchase"],
                )
                ok, why = validate(result)
                status = "OK" if ok else f"INVALID ({why})"
                print(f"    -> {status}")
                print(f"       topic={result.get('inferred_topic')!r}  "
                      f"conf={result.get('topic_confidence')}  "
                      f"attr_type={result.get('non_purchase_type')!r}  "
                      f"attr_value={result.get('attribute_value')!r}")
            except Exception as e:
                print(f"    -> FAILED: {e}")
            print()
        conn.close()
        return

    # --- FULL RUN branch ---------------------------------------------------
    print("Checking / adding enrichment columns...")
    ensure_columns(conn)

    rows = fetch_unenriched(conn, limit=args.limit)
    if not rows:
        print("Nothing to enrich — all rows already have inferred_topic.")
        conn.close()
        return

    print(f"Processing {len(rows)} unenriched row(s)...\n")
    n_ok, n_fail = 0, 0
    t0 = time.time()

    for i, row in enumerate(rows, 1):
        try:
            result = classify(
                client,
                product=row["product_looking_for"],
                reason=row["reason_for_non_purchase"],
            )
            ok, why = validate(result)
            if not ok:
                raise ValueError(why)
            update_row(conn, row["feedback_id"], result)
            n_ok += 1
        except Exception as e:
            n_fail += 1
            print(f"  [{i:>4}] id={row['feedback_id']:<5} FAILED: {e}")

        # Progress beacon every 50 rows
        if i % 50 == 0:
            elapsed = time.time() - t0
            rate = i / max(elapsed, 1e-6)
            eta = (len(rows) - i) / max(rate, 1e-6)
            print(f"  ...processed {i}/{len(rows)}  "
                  f"({rate:.1f}/s, ETA {eta/60:.1f} min)")

    conn.close()
    elapsed = time.time() - t0
    print(f"\nDone.  ok={n_ok}  failed={n_fail}  elapsed={elapsed/60:.1f} min")
    if n_fail:
        print("Re-run the script; failed rows still have inferred_topic = NULL "
              "and will be retried.")


if __name__ == "__main__":
    main()
