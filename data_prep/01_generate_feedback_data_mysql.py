"""
Synthesizes non-purchase feedback data and loads it into a MySQL database.

Prerequisites
-------------
1. MySQL is running locally on port 3306.
2. A database named `merchandising` exists.
3. A user (default: `ram`) has INSERT/CREATE/DROP privileges on that database.
4. Python connector installed:   pip3 install mysql-connector-python

Run
---
    # Option A — pass the password through an env var (recommended)
    export MYSQL_PASSWORD='your_password_here'
    python3 01_generate_feedback_data_mysql.py

    # Option B — let the script prompt you (password won't echo)
    python3 01_generate_feedback_data_mysql.py

What it does
------------
- Connects to MySQL as user `ram` to database `merchandising`.
- Drops the `non_purchasers_feedback` table if it already exists (so you can
  re-run safely).
- Creates the table with the right schema.
- Generates ~200 verbose feedback rows per store across X1..X6 (~1,200 total)
  with built-in store/product/topic biases so downstream agentic analysis has
  something meaningful to discover.
- Inserts them in a single transaction.
- Prints a quick summary so you know it worked.
"""

import os
import random
import sys
from datetime import datetime, timedelta
from getpass import getpass
from pathlib import Path

# Load .env (MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DB).
# Falls back gracefully if python-dotenv is not installed or the file is absent
# — in that case the script will use environment variables already set in the
# shell, or prompt for the password interactively.
try:
    from dotenv import load_dotenv
    # .env lives at the project root (one level above data_prep/)
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

try:
    import mysql.connector
    from mysql.connector import errorcode
except ImportError:
    print(
        "ERROR: mysql-connector-python is not installed.\n"
        "Run:   pip3 install mysql-connector-python\n"
        "Then re-run this script."
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Connection config — change if your MySQL setup differs.
# ---------------------------------------------------------------------------
MYSQL_CONFIG = {
    "host":     os.environ.get("MYSQL_HOST", "127.0.0.1"),
    "port":     int(os.environ.get("MYSQL_PORT", "3306")),
    "user":     os.environ.get("MYSQL_USER", "ram"),
    "database": os.environ.get("MYSQL_DB",   "merchandising"),
}

# Reproducibility — change the seed for fresh data.
random.seed(42)

# ---------------------------------------------------------------------------
# Core enumerations
# ---------------------------------------------------------------------------
STORES = ["X1", "X2", "X3", "X4", "X5", "X6"]
PRODUCTS = ["Ear Rings", "Bangles", "Necklace", "Finger Rings", "Anklets"]
USER_CATEGORIES = [
    "Babies",
    "Teen-age Girls",
    "Office-going Women",
    "Everyday Wear",
    "Wedding",
    "Birthday",
]

FIRST_NAMES = [
    # Indian
    "Priya", "Anjali", "Rohit", "Aarav", "Neha", "Kavya", "Ishita", "Arjun",
    "Sneha", "Riya", "Meera", "Aditi", "Vikram", "Pooja", "Sanjana", "Tara",
    # Western
    "Emma", "Olivia", "Sophia", "Ava", "Mia", "Charlotte", "Amelia", "Harper",
    "Liam", "Noah", "Ethan", "Lucas", "Grace", "Lily", "Chloe", "Zoe",
]
LAST_NAMES = [
    "Sharma", "Verma", "Iyer", "Reddy", "Khan", "Patel", "Nair", "Mehta",
    "Smith", "Johnson", "Brown", "Davis", "Wilson", "Taylor", "Anderson", "Clark",
]
EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"]

# ---------------------------------------------------------------------------
# Verbose-reason templates per topic
# ---------------------------------------------------------------------------
DESIGN_BY_PRODUCT = {
    "Ear Rings":   ["jhumka", "chandbali", "stud", "hoop", "floral", "drop", "kundan"],
    "Bangles":     ["star design", "floral carving", "kada", "bridal kundan", "thin polki", "minimal everyday"],
    "Necklace":    ["choker", "long haar", "temple design", "pendant set", "layered chain", "minimalist"],
    "Finger Rings":["solitaire", "couple ring", "vintage band", "floral ring", "cocktail ring"],
    "Anklets":     ["paayal with ghungroo", "minimal chain anklet", "beaded anklet", "double-layer anklet"],
}
SIZE_BY_PRODUCT = {
    "Ear Rings":   ["small studs", "medium drops", "extra small for kids"],
    "Bangles":     ["size 2.4", "size 2.6", "size 2.8", "size 8", "kids size"],
    "Necklace":    ["16 inch", "18 inch", "22 inch chain length"],
    "Finger Rings":["size 6", "size 8", "size 12", "adjustable"],
    "Anklets":     ["9 inch", "10 inch", "kids size"],
}
COLOR_OPTS = ["rose gold", "white gold", "yellow gold", "antique finish", "silver tone"]
WEIGHT_OPTS = ["lightweight (under 8g)", "around 12g", "under 15g for daily wear"]
PRICE_BANDS = ["under 10k", "under 25k", "under 50k", "below 1 lakh"]

def _design(p):
    d = random.choice(DESIGN_BY_PRODUCT[p])
    return random.choice([
        f"I came specifically looking for a {d} {p.lower()} but the store didn't have it.",
        f"Was hoping to find a {d} style in {p.lower()}. Nothing matched what I had in mind.",
        f"My family wanted a {d} design in {p.lower()}, but the available designs were very limited.",
        f"Saw a {d} design online and wanted the same in {p.lower()} here. Not available.",
    ])

def _size(p):
    s = random.choice(SIZE_BY_PRODUCT[p])
    return random.choice([
        f"Looking for {s} in {p.lower()}. The sizes available didn't fit.",
        f"Needed {s} for {p.lower()} but staff said it's out of stock right now.",
        f"My daughter needed {s} {p.lower()}; could not find the right size.",
        f"The {p.lower()} I liked wasn't available in {s}.",
    ])

def _stock(p):
    return random.choice([
        f"The {p.lower()} I wanted to buy is completely out of stock at the moment.",
        f"Saw the exact {p.lower()} I wanted in the catalogue but it's not in the store currently.",
        f"Was told the {p.lower()} model is sold out and would take 3-4 weeks to restock.",
    ])

def _price(p):
    band = random.choice(PRICE_BANDS)
    return random.choice([
        f"The {p.lower()} I liked was way over my budget. I was hoping for something {band}.",
        f"Prices on {p.lower()} were higher than I expected, I had a budget of {band}.",
        f"Found a beautiful {p.lower()} but it crossed my {band} budget by a lot.",
    ])

def _quality(p):
    return random.choice([
        f"I wasn't fully convinced about the finish quality on the {p.lower()} I tried.",
        f"The hallmark and purity details on the {p.lower()} were not clearly communicated.",
        f"The polish/finishing on the {p.lower()} looked uneven to me.",
    ])

def _weight(p):
    w = random.choice(WEIGHT_OPTS)
    return random.choice([
        f"I wanted something {w} but the {p.lower()} options shown were heavier.",
        f"Looking for a {w} {p.lower()}, but in-store stock was much heavier.",
    ])

def _color(p):
    c = random.choice(COLOR_OPTS)
    return random.choice([
        f"Wanted the {p.lower()} in {c} but only yellow gold was available.",
        f"Was looking for a {c} finish on {p.lower()}; could not find a matching piece.",
    ])

def _customization(p):
    return random.choice([
        f"Asked if the {p.lower()} can be customized with engraving; was told no.",
        f"Wanted a slight design change on the {p.lower()}; staff said customization is not possible right now.",
        f"Wanted made-to-order in {p.lower()}; the lead time of 6-8 weeks didn't work for me.",
    ])

def _service(p):
    return random.choice([
        f"The sales staff was busy and I couldn't get enough help while choosing the {p.lower()}.",
        f"Waited a long time to be attended for the {p.lower()} I wanted to see.",
    ])

def _others(p):
    return random.choice([
        f"I just came in to compare prices today, will revisit later for the {p.lower()}.",
        f"Family member wanted to be present before buying the {p.lower()}.",
        f"Will buy after the upcoming festival sale; postponing the {p.lower()} purchase.",
        f"Was actually looking for a different category but happened to browse {p.lower()} too.",
        f"Need to consult my husband/parents before deciding on the {p.lower()}.",
    ])

TOPIC_LIBRARY = [
    ("Design Unavailable",        _design),
    ("Size Unavailable",          _size),
    ("Stock Unavailable",         _stock),
    ("Price Too High",            _price),
    ("Quality Concerns",          _quality),
    ("Weight Concerns",           _weight),
    ("Color/Finish Mismatch",     _color),
    ("Customization Not Offered", _customization),
    ("Sales Service",             _service),
    ("Others",                    _others),
]

# Store-level biases so the analysis surfaces store-specific patterns.
STORE_BIAS = {
    "X1": [("Design Unavailable", "Bangles"),         ("Size Unavailable", "Finger Rings")],
    "X2": [("Stock Unavailable",  "Ear Rings"),       ("Price Too High",   "Necklace")],
    "X3": [("Size Unavailable",   "Anklets"),         ("Design Unavailable","Necklace")],
    "X4": [("Size Unavailable",   "Finger Rings"),    ("Weight Concerns",  "Bangles")],
    "X5": [("Customization Not Offered","Necklace"),  ("Color/Finish Mismatch","Ear Rings")],
    "X6": [("Price Too High",     "Bangles"),         ("Quality Concerns", "Finger Rings")],
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_name():
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"

def make_email(name):
    handle = name.lower().replace(" ", ".") + str(random.randint(1, 99))
    return f"{handle}@{random.choice(EMAIL_DOMAINS)}"

def pick_topic_and_product(store):
    if random.random() < 0.40:
        topic, product = random.choice(STORE_BIAS[store])
    else:
        topic = random.choices(
            [t[0] for t in TOPIC_LIBRARY],
            weights=[12, 12, 11, 10, 8, 7, 7, 7, 6, 7],
            k=1,
        )[0]
        product = random.choice(PRODUCTS)
    return topic, product

def build_reason(topic, product):
    base = dict(TOPIC_LIBRARY)[topic](product)
    if random.random() < 0.45:
        cat = random.choice(USER_CATEGORIES)
        base += random.choice([
            f" It was for {cat.lower()}.",
            f" Specifically for {cat.lower()} use.",
            f" The piece was meant for {cat.lower()}.",
        ])
    return base

def random_visit_date():
    today = datetime.today()
    return (today - timedelta(days=random.randint(0, 89))).strftime("%Y-%m-%d")

# ---------------------------------------------------------------------------
# Build rows
# ---------------------------------------------------------------------------
def generate_rows():
    rows = []
    feedback_id = 1
    for store in STORES:
        n_rows = 200 + random.randint(-8, 8)
        for _ in range(n_rows):
            topic, product = pick_topic_and_product(store)
            user_cat = random.choice(USER_CATEGORIES)
            customer = make_name()
            rows.append({
                "feedback_id":             feedback_id,
                "visit_date":              random_visit_date(),
                "store_code":              store,
                "customer_name":           customer,
                "customer_email":          make_email(customer),
                "user_category":           user_cat,
                "product_looking_for":     product,
                "reason_for_non_purchase": build_reason(topic, product),
                "ground_truth_topic":      topic,
            })
            feedback_id += 1
    random.shuffle(rows)
    for i, r in enumerate(rows, start=1):
        r["feedback_id"] = i
    return rows

# ---------------------------------------------------------------------------
# MySQL plumbing
# ---------------------------------------------------------------------------
CREATE_TABLE_SQL = """
CREATE TABLE non_purchasers_feedback (
    feedback_id             INT             NOT NULL PRIMARY KEY,
    visit_date              DATE            NOT NULL,
    store_code              VARCHAR(8)      NOT NULL,
    customer_name           VARCHAR(120)    NOT NULL,
    customer_email          VARCHAR(160)    NOT NULL,
    user_category           VARCHAR(40)     NOT NULL,
    product_looking_for     VARCHAR(40)     NOT NULL,
    reason_for_non_purchase TEXT            NOT NULL,
    ground_truth_topic      VARCHAR(40)     NOT NULL,
    INDEX idx_store   (store_code),
    INDEX idx_product (product_looking_for)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

INSERT_SQL = """
INSERT INTO non_purchasers_feedback
(feedback_id, visit_date, store_code, customer_name, customer_email,
 user_category, product_looking_for, reason_for_non_purchase, ground_truth_topic)
VALUES
(%(feedback_id)s, %(visit_date)s, %(store_code)s, %(customer_name)s, %(customer_email)s,
 %(user_category)s, %(product_looking_for)s, %(reason_for_non_purchase)s, %(ground_truth_topic)s)
"""

def get_password():
    pw = os.environ.get("MYSQL_PASSWORD")
    if pw is not None:
        return pw
    return getpass(f"Password for MySQL user '{MYSQL_CONFIG['user']}': ")

def load_to_mysql(rows):
    cfg = dict(MYSQL_CONFIG)
    cfg["password"] = get_password()

    try:
        conn = mysql.connector.connect(**cfg)
    except mysql.connector.Error as err:
        if err.errno == errorcode.ER_ACCESS_DENIED_ERROR:
            print("ERROR: Access denied. Check the MYSQL_PASSWORD env var or user privileges.")
        elif err.errno == errorcode.ER_BAD_DB_ERROR:
            print(f"ERROR: Database '{cfg['database']}' does not exist. "
                  "Create it first:  CREATE DATABASE merchandising;")
        else:
            print(f"ERROR connecting to MySQL: {err}")
        sys.exit(1)

    cur = conn.cursor()
    try:
        print("Dropping existing table (if any) ...")
        cur.execute("DROP TABLE IF EXISTS non_purchasers_feedback")

        print("Creating table non_purchasers_feedback ...")
        cur.execute(CREATE_TABLE_SQL)

        print(f"Inserting {len(rows)} rows ...")
        cur.executemany(INSERT_SQL, rows)
        conn.commit()

        # Quick verification
        cur.execute("SELECT COUNT(*) FROM non_purchasers_feedback")
        total = cur.fetchone()[0]

        cur.execute("""
            SELECT store_code, COUNT(*)
            FROM non_purchasers_feedback
            GROUP BY store_code
            ORDER BY store_code
        """)
        per_store = cur.fetchall()

        print("\n----- Load complete -----")
        print(f"Total rows: {total}")
        print("Rows per store:")
        for s, n in per_store:
            print(f"  {s}: {n}")

        cur.execute("""
            SELECT feedback_id, store_code, product_looking_for,
                   LEFT(reason_for_non_purchase, 90)
            FROM non_purchasers_feedback
            ORDER BY feedback_id
            LIMIT 5
        """)
        print("\nFirst 5 rows:")
        for row in cur.fetchall():
            print(" ", row)

    finally:
        cur.close()
        conn.close()

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    rows = generate_rows()
    load_to_mysql(rows)
