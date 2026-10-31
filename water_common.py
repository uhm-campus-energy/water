"""
water_common.py

Shared toolbox for the water bills.

Each project keeps its OWN:
    - config.py                    -> paths, filename suffix, DB table/columns
    - N) *_bill_extract.py         -> project-specific PDF data extraction (unchanged)
    - an extract_bill_info() function -> project-specific account/date regex

This module holds the logic that used to be copy-pasted three times with
small, drifting differences:
    - MD5 duplicate detection
    - renaming into canonical ACCOUNT_YYYY_MM_DD_<suffix>.pdf format
    - checking the archive for duplicates / moving duplicates aside
    - moving unique bills into bill_archive/FY<YY>/
    - uploading a CSV to the database via `psql \\copy`

Fix a bug here once, and all three projects get the fix.
"""

import csv
import hashlib
import os
import shutil
import subprocess
from datetime import datetime, date
from pathlib import Path


# ── Duplicate detection ──────────────────────────────────────────────────

def get_pdf_hash(pdf_path):
    """Calculate MD5 hash of a file, used for exact duplicate detection."""
    md5_hash = hashlib.md5()
    with open(pdf_path, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            md5_hash.update(chunk)
    return md5_hash.hexdigest()


def bill_exists_in_archive(filename, archive_dir):
    """Check if a bill with this canonical name already exists in any FY folder."""
    archive_dir = Path(archive_dir)
    if not archive_dir.exists():
        return False
    # The archive layout is archive_dir/FY<YY>/yymm/<filename>, so search
    # recursively for the filename anywhere under the archive directory.
    for p in archive_dir.rglob(filename):
        if p.is_file():
            return True
    return False


def move_to_duplicate(pdf_path, duplicate_dir):
    """Move a file into the duplicate_bills folder, avoiding overwrites."""
    duplicate_dir = Path(duplicate_dir)
    duplicate_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = Path(pdf_path)
    dst_path = duplicate_dir / pdf_path.name
    counter = 1
    while dst_path.exists():
        dst_path = duplicate_dir / f"{pdf_path.stem}_{counter}{pdf_path.suffix}"
        counter += 1

    shutil.move(str(pdf_path), str(dst_path))
    print(f"DUPLICATE moved: {dst_path.name}")
    return dst_path


# ── Step 0.5: Skip bills already in the database ─────────────────────────

def load_max_dates(csv_path):
    """
    Read the extract CSV (account_no, max) produced by script 0) into a
    dict mapping account number (str) -> the latest date already in the
    database for that account. Example extract path: extracts/water_bill_max_date.csv.

    Returns an empty dict (nothing gets filtered) if the file doesn't exist
    yet — e.g. script 0) hasn't been run on the server.
    """
    csv_path = Path(csv_path)
    max_dates = {}

    if not csv_path.exists():
        print(f"[WARNING] {csv_path} not found. ")
        print(f"Skipping already-billed check.")
        return max_dates

    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            account_no = (row.get("account_no") or "").strip()
            max_date = (row.get("max") or "").strip()
            if account_no and max_date:
                max_dates[account_no] = datetime.strptime(max_date, "%Y-%m-%d").date()

    return max_dates


def filter_already_billed(folder_path, duplicate_dir, max_dates, get_account_no_and_date):
    """
    Move PDFs whose bill date is not AFTER the max date already in the
    database for that account into duplicate_dir, before organize_bills()
    ever renames or archives them.

    get_account_no_and_date: a function(pdf_path) -> (account_no, bill_date)
                              bill_date is a datetime.date, or None if it
                              couldn't be read. Project-specific, since every
                              water bill's account number and bill date may
                              be in different locations in the PDF.
    """
    folder = Path(folder_path)
    duplicate_dir = Path(duplicate_dir)

    if not folder.exists():
        return 0

    if max_dates is None:
        return 0

    pdf_files = [f for f in folder.glob("*.pdf") if f.parent == folder]
    moved = 0

    for pdf_file in pdf_files:
        account_no, bill_date = get_account_no_and_date(pdf_file)

        if not account_no or not bill_date:
            print(f"  [WARNING] Could not determine account/date for {pdf_file.name}. Skipping already-billed check.")
            continue

        max_date = max_dates.get(account_no)
        if max_date is not None and bill_date <= max_date:
            print(
                f"Already in database (bill date {bill_date} <= max {max_date} "
                f"for account {account_no}):\n  {pdf_file.name}"
            )
            move_to_duplicate(pdf_file, duplicate_dir)
            moved += 1

    print(f"Already-billed check: {moved} bill(s) moved to duplicates.\n")
    return moved


def load_max_date(csv_path):
    """
    Read a single-value extract (one "max" column, one row), produced by
    a script 0) that queries e.g. SELECT max(date_to) FROM substations.bill,
    into a single datetime.date.

    Use this (instead of load_max_dates) when the database only tracks one
    overall max date rather than one per account.

    Returns None (nothing gets filtered) if the file doesn't exist yet, or
    the value is blank — e.g. script 0) hasn't been run on the server, or
    the table is empty.
    """
    csv_path = Path(csv_path)

    if not csv_path.exists():
        print(f"[WARNING] {csv_path} not found. \nSkipping already-billed check.")
        return None

    with open(csv_path, newline="") as f:
        row = next(csv.DictReader(f), None)

    max_date = (row.get("max") or "").strip() if row else ""
    if not max_date:
        return None

    return datetime.strptime(max_date, "%Y-%m-%d").date()


def filter_already_billed_by_date(folder_path, duplicate_dir, max_date, get_bill_date):
    """
    Move PDFs whose bill date is not AFTER max_date into duplicate_dir,
    before organize_bills() ever renames or archives them.

    Use this (instead of filter_already_billed) when the database only
    tracks one overall max date rather than one per account
    (see load_max_date).

    get_bill_date: a function(pdf_path) -> datetime.date, or None if it
                   couldn't be read. Project-specific.
    """
    folder = Path(folder_path)
    duplicate_dir = Path(duplicate_dir)

    if not folder.exists():
        return

    if max_date is None:
        # debug message to explain that there's not max date so skipping
        # print("Already-billed check: no max date available, skipping.\n")
        return

    pdf_files = [f for f in folder.glob("*.pdf") if f.parent == folder]
    moved = 0

    for pdf_file in pdf_files:
        bill_date = get_bill_date(pdf_file)

        if not bill_date:
            print(f"  [WARNING] Could not determine bill date for {pdf_file.name}. Skipping already-billed check.")
            continue

        if bill_date <= max_date:
            print(f"Already in database (bill date {bill_date} <= max {max_date}):\n  {pdf_file.name}")
            move_to_duplicate(pdf_file, duplicate_dir)
            moved += 1

    print(f"Already-billed check: {moved} bill(s) moved to duplicates.\n")


# ── Step 1: Organize (rename + dedupe) ───────────────────────────────────

def organize_bills(folder_path, archive_dir, duplicate_dir, filename_suffix, extract_bill_info, already_billed_count=0):
    """
    Rename PDFs into canonical ACCOUNT_YYYY_MM_DD_<suffix>.pdf format.
    Move exact duplicates (by file hash) and bills already present in the
    archive into duplicate_dir.

    extract_bill_info: a function(pdf_path) -> (account_number, date_str)
                        This is the one piece that stays project-specific,
                        since every bill layout is different. Pass in the
                        project's own extraction function.
    """
    folder = Path(folder_path)
    archive_dir = Path(archive_dir)
    duplicate_dir = Path(duplicate_dir)

    if not folder.exists():
        print(f"[ERROR] Folder does not exist:\n  {folder}")
        return

    duplicate_dir.mkdir(parents=True, exist_ok=True)
    # debug print (in case it's looking at wrong folder or saying files not found)
    # print(f"Processing PDFs in: {folder}")
    # print(f"Duplicate folder: {duplicate_dir}\n")

    pdf_files = [f for f in folder.glob("*.pdf") if f.parent == folder]
    if not pdf_files:
        print("No PDF files found in the folder.")
        return

    # debug output to help figure out where the error is coming from
    # print("=" * 70)
    # print("STEP 1: Analyze PDFs")
    # print("=" * 70)

    file_info = {}
    hash_map = {}

    for pdf_file in pdf_files:
        account_number, date_str = extract_bill_info(pdf_file)

        # Normalize the extracted date into the canonical YYYY_MM_DD form
        if isinstance(date_str, (datetime, date)):
            date_part = date_str.strftime("%Y_%m_%d")
        else:
            date_part = str(date_str).replace("-", "_")

        if not account_number or not date_str:
            print(f"  [WARNING] Could not extract info from {pdf_file.name}. Skipping.")
            file_info[pdf_file] = {"valid": False}
            continue

        canonical_name = f"{account_number}_{date_part}_{filename_suffix}.pdf"
        file_hash = get_pdf_hash(pdf_file)

        file_info[pdf_file] = {
            "valid": True,
            "canonical_name": canonical_name,
            "account": account_number,
        }
        hash_map.setdefault(file_hash, []).append(pdf_file)

    # debug output to help figure out where the error is coming from
    # print("\n" + "=" * 70)
    # print("STEP 2: Move exact duplicate PDFs")
    # print("=" * 70)

    duplicates_moved = already_billed_count
    for file_hash, files in hash_map.items():
        if len(files) <= 1:
            continue

        print(f"\nFound {len(files)} identical files")
        keep_file = files[0]
        print(f"  Keeping: {keep_file.name}")

        for dup_file in files[1:]:
            info = file_info.get(dup_file)
            try:
                if info and info.get("valid"):
                    canonical_name = info["canonical_name"]
                    temp_path = dup_file.parent / canonical_name
                    if dup_file.name != canonical_name:
                        if temp_path.exists():
                            temp_path = dup_file.parent / f"TEMP_{dup_file.name}"
                        dup_file.rename(temp_path)
                        dup_file = temp_path
                move_to_duplicate(dup_file, duplicate_dir)
                duplicates_moved += 1
            except Exception as e:
                print(f"  [ERROR] Moving duplicate: {e}")

    # debug output to help figure out where the error is coming from
    # print("\n" + "=" * 70)
    # print("STEP 3: Rename files")
    # print("=" * 70)

    renamed_count = 0
    skipped_count = 0

    for pdf_file, info in file_info.items():
        if not pdf_file.exists():
            continue
        if not info.get("valid"):
            print(f"\nSkipping invalid file: {pdf_file.name}")
            skipped_count += 1
            continue

        canonical_name = info["canonical_name"]
        canonical_path = pdf_file.parent / canonical_name

        if bill_exists_in_archive(canonical_name, archive_dir):
            print(f"\nDuplicate found in archive:\n  {canonical_name}")
            if pdf_file.name != canonical_name:
                temp_path = canonical_path
                counter = 1
                while temp_path.exists():
                    temp_path = pdf_file.parent / f"{canonical_path.stem}_TEMP{counter}.pdf"
                    counter += 1
                pdf_file.rename(temp_path)
                pdf_file = temp_path
            move_to_duplicate(pdf_file, duplicate_dir)
            duplicates_moved += 1
            continue

        # debug to figure out which files are already named correctly
        # if pdf_file.name == canonical_name:
        #     print(f"\nAlready correctly named:\n  {pdf_file.name}")
        #     continue

        try:
            pdf_file.rename(canonical_path)
            # After renaming, move the canonical file into an account subfolder
            account = info.get("account")
            if account:
                account_dir = folder / str(account)
                account_dir.mkdir(parents=True, exist_ok=True)
                dest_in_account = account_dir / canonical_name
                if dest_in_account.exists():
                    base = dest_in_account.stem
                    ext = dest_in_account.suffix
                    counter = 1
                    while dest_in_account.exists():
                        dest_in_account = account_dir / f"{base}_{counter}{ext}"
                        counter += 1
                shutil.move(str(canonical_path), str(dest_in_account))
            # debug to check what files are being renamed/moved to
            # print(f"\nRenamed and moved:\n  To:   {canonical_name} (account folder: {account})")
            renamed_count += 1
        except Exception as e:
            print(f"\n[ERROR] Renaming {pdf_file.name}: {e}")
            skipped_count += 1

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"PDF's Found:          {len(pdf_files)}")
    print(f"Total PDFs processed: {len(pdf_files)}")
    print(f"Duplicates moved:     {duplicates_moved}")
    print(f"Files renamed:        {renamed_count}")
    print(f"Files skipped:        {skipped_count}")
    # print(f"\nDuplicate folder:\n  {duplicate_dir}")
    print("=" * 70)


# ── Step 3: Move renamed bills into the FY archive ───────────────────────

def get_fiscal_year(year: int, month: int) -> str:
    """
    Hawaii fiscal year logic (matches all three original scripts):
      - Jul-Dec => next fiscal year
      - Jan-Jun => same fiscal year
    """
    fy = year + 1 if month >= 7 else year
    return f"FY{str(fy)[-2:]}"


def move_bills_to_archive(source_dir, archive_dir, filename_pattern):
    """
    Move canonically-named bills out of input_bills into
    bill_archive/FY<YY>/, based on the date encoded in the filename.

    filename_pattern: compiled regex with named groups acct, year, month, day
                       e.g. re.compile(r"^(?P<acct>\\d+)_(?P<year>\\d{4})_(?P<month>\\d{2})_(?P<day>\\d{2}).*\\.pdf$", re.IGNORECASE)
    """
    source_dir = Path(source_dir)
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    skipped = 0

    # determine duplicate folder path (common name used elsewhere)
    duplicate_dir = source_dir / "duplicate_bills"
    duplicate_dir_resolved = str(Path(duplicate_dir).resolve())

    # the input folder has subfolders so traversing it differs from projects
    # that place all PDFs in a single folder
    for root, _, files in os.walk(source_dir):
        for file in files:
            if file.lower().endswith(".pdf"):
                file_path = Path(root) / file
                # Skip any files that live in the duplicate_bills folder
                if str(Path(root).resolve()).startswith(duplicate_dir_resolved):
                    # leave duplicates alone — they are handled elsewhere
                    skipped += 1
                    continue
                match = filename_pattern.match(file)

                if not match:
                    print(f"Skipping unmatched filename: {file}")
                    skipped += 1
                    continue

                year = int(match.group("year"))
                month = int(match.group("month"))

                fy_folder = get_fiscal_year(year, month)

                # yymm for subfolder within each FY folder (e.g., 2503 for March 2025)
                yymm = str(year)[2:] + match.group("month")

                # create FY folder if it doesn't exist
                target_dir = archive_dir / fy_folder / yymm
                target_dir.mkdir(parents=True, exist_ok=True)

                destination = target_dir / file

                # avoid overwrite collisions
                if destination.exists():
                    base = file_path.stem
                    ext = file_path.suffix
                    counter = 1

                    while destination.exists():
                        destination = target_dir / f"{base}_{counter}{ext}"
                        counter += 1
                
                # if you want to see the file names that are being moved
                # print(f"Moving {file} -> bill_archive/{fy_folder}/{yymm}")
                
                shutil.move(str(file_path), str(destination))

                moved += 1

    print(f"Finished moving bills. Total moved: {moved}, Total skipped: {skipped}")

    # delete empty subfolders from input folder
    subfolder = 0
    for root, dirs, _ in os.walk(source_dir):
        for dir in dirs:
            dir_path = Path(root) / dir
            # never remove the duplicate folder (or its subfolders)
            try:
                if str(dir_path.resolve()).startswith(duplicate_dir_resolved):
                    continue
            except Exception:
                # on resolution issues, skip removal to be safe
                continue
            if not any(dir_path.iterdir()):
                # print(f"Removing empty folder: {dir_path}")
                dir_path.rmdir()
                subfolder += 1

    print(f"Empty subfolders removed: {subfolder}")

# ── Step 4: Upload CSV to database ───────────────────────────────────────

def upload_csv_to_database():

    # 1. Get the absolute path of the folder where this script lives
    # (/home/campusenergy/shared/water_bills/scripts)
    script_dir = Path(__file__).resolve()
    
    # 2. Look in 'output/' for the most recent CSV matching our pattern.
    output_dir = script_dir.parent / "output"

    # Pick the newest CSV matching our pattern in the output directory.
    candidates = sorted(
        [p for p in output_dir.glob("water_bills_data*.csv") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        print(f"[ERROR] No CSV matching 'water_bills_data*.csv' found in: {output_dir}")
        return False

    csv_path = candidates[0]
    print(f"[INFO] Using most recent CSV for upload: {csv_path}")

    # Optionally keep only the 5 most recent CSVs to avoid accumulating old files.
    try:
        base_stem = "water_bills_data"
        backups = sorted(
            [p for p in output_dir.glob(f"{base_stem}_*{csv_path.suffix}") if p.is_file()],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in backups[5:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception as e:
        print(f"[WARN] Could not clean up old CSVs: {e}")

    COLS = (
    "account_no, date, thousands_gal, days, water_charges, sewer_charges, "
    "payments, current_charges, water_charges_adjusted, sewer_charges_adjusted, "
    "note, water_usage, sewer_usage"
    )

    # empties the test table before uploading new data to it
    empty_command = [
        "psql",
        "uhm2023",
        "-c",
        r"""TRUNCATE TABLE water.water_staging;"""
    ]

    # copies data to test table
    copy_command = [
        "psql",
        "uhm2023",
        "-c",
        rf"""\copy water.water_test({COLS}) FROM '{csv_path}' CSV HEADER;"""
    ]

    # checks for duplicates as it inserts into the main table
    insert_command = [
        "psql",
        "uhm2023",
        "-c",
        rf"""
        INSERT INTO water.water
            SELECT * FROM water.water_staging
            ON CONFLICT (account_no, date) DO NOTHING;
        """
    ]

    update_command = [
        "psql",
        "uhm2023",
        "-c",
        rf"""
        INSERT INTO water.water AS w
            SELECT * FROM water.water_staging
            ON CONFLICT (account_no, date) 
            DO UPDATE SET 
                water_charges_adjusted = EXCLUDED.water_charges_adjusted,
                sewer_charges_adjusted = EXCLUDED.sewer_charges_adjusted
            WHERE w.water_charges_adjusted IS DISTINCT FROM EXCLUDED.water_charges_adjusted
               OR w.sewer_charges_adjusted IS DISTINCT FROM EXCLUDED.sewer_charges_adjusted;
        """
    ]

    try: 
        subprocess.run(empty_command, check=True)
        subprocess.run(copy_command, check=True)
        subprocess.run(insert_command, check=True)
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Database upload failed (psql exit code {e.returncode}).")
        print(f"        {csv_path} was NOT fully uploaded to water.water.")
        return False
    except FileNotFoundError:
        print("[ERROR] Could not find 'psql'. Is it installed and on PATH?")
        return False

    print(f"[OK] Uploaded {csv_path} to water.water")
    return True


def extract_query_to_csv(database, query, csv_path):
    """
    Run, via psql:
        \\copy (<query>) TO '<csv_path>' CSV HEADER;

    Inverse of upload_csv_to_database() — pulls a query's results down
    into a CSV instead of loading a CSV into a table. Used for pre-processing
    steps that need to see what's already in the database (e.g. the last
    billed date per account) before processing new bills.

    Only works where psql can reach the database (the server, not a laptop).

    database: e.g. "uhm2023"
    query:    e.g. "SELECT account_no, MAX(date) AS max FROM water.water GROUP BY account_no"
    csv_path: where to write the CSV, e.g. config.LAST_DATES_CSV

    Returns True if the export succeeded, False if it failed.
    The query is what it used to be it's different in config.py so that it catches missing or skipped months
    """
    csv_path = Path(csv_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "psql",
        database,
        "-c",
        rf"""\copy ({query}) TO '{csv_path}' CSV HEADER;""",
    ]

    try:
        subprocess.run(command, check=True)
        print(f"[OK] Extracted query results to {csv_path}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Database extract failed (psql exit code {e.returncode}).")
        return False
    except FileNotFoundError:
        print("[ERROR] Could not find 'psql'. Is it installed and on PATH?")
        return False


def upload_and_archive(source_dir, archive_dir, filename_pattern):
    """
    Upload the CSV to the database, and ONLY IF that succeeds, move the
    corresponding bills from source_dir into the FY archive.

    This exists so a failed database upload can never silently leave you
    with a bill sitting in bill_archive/ whose data never made it into
    the database.
    """
    success = upload_csv_to_database()

    # if not success:
    #     print("\nSkipping archive step — bills remain in:")
    #     print(f"  {source_dir}")
    #     print("Fix the upload issue, then re-run this script.")
    #     return False

    print("\nUpload succeeded — proceeding to archive bills.")
    move_bills_to_archive(source_dir, archive_dir, filename_pattern)
    return True

# Specific functions for water bill extraction
def clean_dollar_amount(amount_str):
    """Clean dollar amount strings and convert to float."""
    amount_str = amount_str.replace(",", "").strip()
    if amount_str.startswith("-"):
        return -float(amount_str[1:])
    return float(amount_str)