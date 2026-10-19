"""
0) Pre-process: pull the max billed-through date from the database.

Only runs where psql can reach the database (the server) — not from a
laptop. Refreshes extracts/water_bill_max_date.csv so later steps
can see what's already in the database before processing new bills.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
WATER_BILLS_ROOT = SCRIPT_DIR.parent   # water_bills/  <- where water_common.py lives
sys.path.insert(0, str(WATER_BILLS_ROOT))

import config
from water_common import extract_query_to_csv


if __name__ == "__main__":
    extract_query_to_csv(
        database=config.DATABASE,
        query=config.MAX_DATE_QUERY,
        csv_path=config.MAX_DATE_CSV,
    )
