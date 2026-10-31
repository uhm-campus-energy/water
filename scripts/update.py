import os
import subprocess

# ---- Settings (edit these if your names differ) ----
DB_NAME = "uhm2023"
SCHEMA = "water"
STAGING_TABLE = f"{SCHEMA}.water_staging"
MAIN_TABLE = f"{SCHEMA}.water"
CSV_PATH = os.path.abspath(os.path.join("..", "output", "water_bills_data_new.csv"))
NOT_UPDATED_CSV_PATH = os.path.abspath(
    os.path.join("..", "output", "not_updated.csv")
)

# Must match the CSV header written by read_all.py, in the same order
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
    "water_usage",
    "sewer_usage",
]
KEY = ["account_no", "date"]  # how a CSV row is matched to a row in campus_water
UPDATE_COLUMNS = ["sewer_usage"]  # the only columns this script changes


def run_psql(sql, tuples_only=False):
    """Runs one psql command and returns its stdout. Raises on failure."""
    cmd = ["psql", DB_NAME, "-v", "ON_ERROR_STOP=1"]
    if tuples_only:
        cmd += ["-t", "-A"]
    cmd += ["-c", sql]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def update_data():
    cols = ", ".join(COLUMNS)
    set_clause = ", ".join(f"{c} = s.{c}" for c in UPDATE_COLUMNS)
    key_match = " AND ".join(f"m.{c} = s.{c}" for c in KEY)
    changed = " OR ".join(f"m.{c} IS DISTINCT FROM s.{c}" for c in UPDATE_COLUMNS)

    if not os.path.exists(CSV_PATH):
        print(f"[ERROR] Could not find {CSV_PATH}. Run read_all.py first.")
        return False

    try:
        # Step 1: make sure the new columns exist (safe to run more than once)
        print(f"[1/6] Making sure {', '.join(UPDATE_COLUMNS)} exist on both tables...")
        for table in (MAIN_TABLE, STAGING_TABLE):
            for col in UPDATE_COLUMNS:
                run_psql(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} numeric;")

        # Step 2: empty the staging table
        print(f"[2/6] Emptying {STAGING_TABLE}...")
        run_psql(f"TRUNCATE TABLE {STAGING_TABLE};")

        # Step 3: copy the CSV into staging
        print(f"[3/6] Copying {CSV_PATH} into {STAGING_TABLE}...")
        run_psql(rf"\copy {STAGING_TABLE}({cols}) FROM '{CSV_PATH}' CSV HEADER")
        staged = run_psql(f"SELECT count(*) FROM {STAGING_TABLE};", tuples_only=True)
        print(f"      {staged} rows staged.")

        # Step 4: save staged rows that have no match in the main table
        # (this script only updates existing rows, it never inserts)
        print(f"[4/6] Checking for rows with no match in {MAIN_TABLE}...")
        s_cols = ", ".join(f"s.{c}" for c in COLUMNS)
        unmatched_query = (
            f"SELECT {s_cols} FROM {STAGING_TABLE} s "
            f"WHERE NOT EXISTS (SELECT 1 FROM {MAIN_TABLE} m WHERE {key_match}) "
            f"ORDER BY s.account_no, s.date"
        )
        unmatched = run_psql(
            f"SELECT count(*) FROM ({unmatched_query}) u;", tuples_only=True
        )
        if int(unmatched) > 0:
            run_psql(
                rf"\copy ({unmatched_query}) TO '{NOT_UPDATED_CSV_PATH}' CSV HEADER"
            )
            print(f"      {unmatched} rows not in the table -> saved to {NOT_UPDATED_CSV_PATH}")
        else:
            print("      Every CSV row matched a row in the table.")

        # Step 5: update the new columns on matching rows
        print(f"[5/6] Updating {', '.join(UPDATE_COLUMNS)} in {MAIN_TABLE}...")
        updated = run_psql(
            f"""
            WITH upd AS (
                UPDATE {MAIN_TABLE} m
                SET {set_clause}
                FROM {STAGING_TABLE} s
                WHERE {key_match}
                  AND s.sewer_usage IS NOT NULL
                  AND ({changed})
                RETURNING 1
            )
            SELECT count(*) FROM upd;
            """,
            tuples_only=True,
        )
        unchanged = int(staged) - int(unmatched) - int(updated)
        print(f"      {updated} rows updated, {unchanged} already up to date.")

        # Step 6: check that nothing was left empty
        print(f"[6/6] Checking {MAIN_TABLE} for rows still missing usage values...")
        still_null = run_psql(
            f"SELECT count(*) FROM {MAIN_TABLE} "
            f"WHERE water_usage IS NULL OR sewer_usage IS NULL;",
            tuples_only=True,
        )
        print(f"      {still_null} rows still have an empty water_usage or sewer_usage.")
        print("[OK] Done.")

    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Database update failed (psql exit code {e.returncode}).")
        print(f"        {e.stderr.strip()}")
        return False
    except FileNotFoundError:
        print("[ERROR] Could not find 'psql'. Is it installed and on PATH?")
        return False

    return True


if __name__ == "__main__":
    update_data()