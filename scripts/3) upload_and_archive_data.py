r"""
3) Upload water bill data to the database, then archive the bills —
   but ONLY if the upload succeeded.

Only runs where psql can reach the database (the server) — not from a
laptop, same as script 0). Calls psql to \copy the extracted CSV into
the `water.water` table; if that succeeds, moves the corresponding bills
from input_bills/ into bill_archive/FY<YY>/ (see water_common.upload_and_archive).
If the upload fails, the bills are left in input_bills/ untouched so a
fix + re-run can't skip any bill's database row.

Paths/database/table/columns all come from config.py, same as script 1).
"""

import sys
from pathlib import Path

# Ensure project root is on sys.path so we can import `water_common`.
SCRIPT_DIR = Path(__file__).resolve().parent
WATER_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(WATER_BILLS_ROOT))

import config
from water_common import upload_and_archive


if __name__ == "__main__":
    upload_and_archive(
        source_dir=config.SOURCE_DIR,
        archive_dir=config.ARCHIVE_DIR,
        filename_pattern=config.FILENAME_PATTERN,
    )
    
    

r"""

Script verification methodology:

--- create new table in database with exact column names and column data types, but ignore the existing values ---

CREATE TABLE water.water_test AS 
TABLE water.water 
WITH NO DATA;

--- update the script above to upload into the test table ---
\copy water.water_test(account_no, date, thousands_gal, days, water_charges, sewer_charges, payments, current_charges, water_charges_adjusted, sewer_charges_adjusted, note) FROM 'water_bills/output/water_bills_data.csv' CSV HEADER;

--- run the script ---

--- check if the values in water_bills_data.csv is in the water.water_test table ---

"""