"""
config.py — everything specific to the WATER bill pipeline.

PATHS ARE PORTABLE:
This finds the "water_bills" root automatically, based on where THIS file
sits, so the same code works on your laptop and later on the server
without editing any paths.

Actual folder layout this expects:

    water_bills/
    ├── scripts/
    │   ├── config.py                       <- this file
    │   ├── 1) organize_substation_bills.py
    │   ├── 2) substation_bill_extract.py
    │   ├── 3) upload_csv_to_database.py
    │   └── 4) move_to_archive.py
    ├── input_bills/
       └── duplicate_bills
    ├── bill_archive/
    └── output/
       └── water_bills_data.csv
"""

import re
from pathlib import Path
from datetime import date, datetime
import pymupdf

# ── Find folders automatically, relative to this file ────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent      # .../water_bills/scripts
PROJECT_DIR = SCRIPT_DIR.parent                   # .../water_bills/
WATER_BILLS_ROOT = PROJECT_DIR                    # same as project directory since there's no sub folder for bill type

# ── Paths ──────────────────────────────────────────────────────────────
SOURCE_DIR = PROJECT_DIR / "input_bills"
ARCHIVE_DIR = PROJECT_DIR / "bill_archive"
DUPLICATE_DIR = SOURCE_DIR / "duplicate_bills"
OUTPUT_CSV = PROJECT_DIR / "output" / "water_bills_data.csv"

# ── Filename ──────────────────────────────────────────────────────────
# Canonical format: ACCOUNTNUMBER_YYYY_MM_DD_substation_bill.pdf
FILENAME_SUFFIX = "water_bill"

FILENAME_PATTERN = re.compile(
    r"^(?P<acct>\d+)_(?P<year>\d{4})_(?P<month>\d{2})_(?P<day>\d{2}).*\.pdf$",
    re.IGNORECASE,
)

# ── Database (step 3) ────────────────────────────────────────────────
DATABASE = "uhm2023"
TABLE = "water.water"
COLUMNS = [
    "account_no",
    "date",
    "thousands_gal",
    "days",
    "water_charges",
    "sewer_charges",
    "payments",
    "current_charges",
    "water_charges_adjusted",
    "sewer_charges_adjusted",
    "note",
]

# ── Database (step 0) ─────────────────────────────────────────────────
# extracts/ is shared across all water_bills projects, so it lives at the
# repo root.
EXTRACTS_DIR = WATER_BILLS_ROOT / "extracts"

# gets the max date across all accounts so that you don't miss a month
# if you don't want to input housing water data then change this query to
# SELECT max(date) FROM water.water
MAX_DATE_QUERY = """
WITH account_start AS (
    SELECT account_no,
           date_trunc('month', MIN(date))::date AS first_month
    FROM water.water
    GROUP BY account_no
),
months AS (
    SELECT mo::date AS month
    FROM calendar.fy_mo
    WHERE mo >= date_trunc('month', CURRENT_DATE - INTERVAL '1 year')
      AND mo <= date_trunc('month', CURRENT_DATE)
),
expected AS (
    SELECT a.account_no, m.month
    FROM account_start a
    JOIN months m ON m.month >= a.first_month
),
actual AS (
    SELECT account_no,
           date_trunc('month', date)::date AS month
    FROM water.water
    GROUP BY account_no, date_trunc('month', date)
),
gaps AS (
    SELECT e.month AS missing_month
    FROM expected e
    LEFT JOIN actual a
           ON a.account_no = e.account_no
          AND a.month = e.month
    WHERE a.account_no IS NULL
),
fallback AS (
    SELECT MIN(last_date) AS max
    FROM (
        SELECT account_no, MAX(date) AS last_date
        FROM water.water
        GROUP BY account_no
    ) AS per_account_max
)
SELECT COALESCE(
    (SELECT (MIN(missing_month) - INTERVAL '1 month')::date FROM gaps),
    (SELECT max FROM fallback)
) AS max
"""

MAX_DATE_CSV = EXTRACTS_DIR / "water_bill_max_date.csv"

# ── PDF parsing (project-specific — every bill layout is different) ───
def extract_bill_info(pdf_path):
    """
    Parsing logic for water bills: pulls raw text with pymupdf and
    returns (account_number, billing_date) where billing_date is a
    datetime.date, or (None, None) if either can't be found/parsed.
    """
    try:
        doc = pymupdf.open(pdf_path)
        text = ""
        for page in doc:
            text += page.get_text()
        doc.close()
    except Exception as e:
        print(f"  [ERROR] Reading {pdf_path.name}: {e}")
        return None, None

    if not text:
        return None, None

    acct_match = re.search(r"Account Number\s*[:\-]?\s*(\d+)", text, re.IGNORECASE)
    account_number = acct_match.group(1) if acct_match else None

    date_match = re.search(r"Billing Date\s*[:\-]?\s*([A-Za-z0-9 ,/-]+)", text, re.IGNORECASE)
    billing_date = None
    if date_match:
        raw_date = date_match.group(1).strip()
        for fmt in ("%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                billing_date = datetime.strptime(raw_date, fmt).date()
                break
            except ValueError:
                continue

    return account_number, billing_date

def get_bill_date(pdf_path):
    """
    Extract the bill's Billing Date as a datetime.date, for checking
    against a max-date extract (script 0), if you have one for water.
    """
    _, billing_date = extract_bill_info(pdf_path)
    return billing_date
