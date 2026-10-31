import sys
from pypdf import PdfReader
import re
import os
import pandas as pd
from pathlib import Path
from datetime import timedelta, datetime

# Ensure project root is on sys.path so we can import `water_common`.
SCRIPT_DIR = Path(__file__).resolve().parent
WATER_BILLS_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(WATER_BILLS_ROOT))

import config
from water_common import clean_dollar_amount

# variables from config.py file
input_folder = config.SOURCE_DIR
output_file = config.OUTPUT_CSV
columns = config.COLUMNS

def extract_account_no(text):
    match = re.search(r"Account Number\s*([\d]+)", text)
    return match.group(1) if match else None

def extract_water_charges(text):
    match = re.search(r"TOTAL WATER CHARGES\s*\$([\d\.,-]+)", text)

    # clean function is from water_common file
    return clean_dollar_amount(match.group(1)) if match else None


def extract_sewer_charges(text):
    match = re.search(r"TOTAL SEWER CHARGES\s*\$([\d\.,-]+)", text)
    return clean_dollar_amount(match.group(1)) if match else None


def extract_current_charges(text):
    match = re.search(r"Current Charges\s*\$([\d\.,-]+)", text)
    return clean_dollar_amount(match.group(1)) if match else None

def extract_payments(text):
    """0.0 for a '$0.00' line, the parsed amount for a '-$X.XX' line, else None."""
    if "Payments $0.00" in text:
        return 0.0
    match = re.search(r"Payments\s*-\$?([\d\.,]+)", text)
    return clean_dollar_amount(match.group(1)) if match else None

def extract_usage_profile(text):
    """
    Returns (date_str, thousands_gal, days). These three come from one
    regex match since they're printed together in the same table row —
    splitting them into separate regexes would just mean matching the
    same table three times.
    """
    match = re.search(
        r"Water Usage Profile.*?(\d{2}/\d{2}/\d{4})\s+(\d+)\s+(\d+)", text, re.DOTALL
    )
    if not match:
        return None, None, None
    date_str, thousands_gal, days = match.groups()
    return date_str, int(thousands_gal), int(days)

def extract_sewer_usage(text):
    matches = re.findall(r"Sewer Volume Charge\s*-\s*([\d\.,]+)\s*Kgal", text)
    if not matches:
        return None
    return int(round(sum(float(m.replace(",", "")) for m in matches)))

def extract_fields_from_text(text, filename, back_text=""):
    """Pull every column for one bill out of its first-page text."""
    try:
        account_no = extract_account_no(text)
        water_charges = extract_water_charges(text)
        sewer_charges = extract_sewer_charges(text)
        current_charges = extract_current_charges(text)
        payments = extract_payments(text)
        date_str, thousands_gal, days = extract_usage_profile(text)
        sewer_usage = extract_sewer_usage(back_text)
    except Exception as e:
        print(f"Error processing {filename}: {e}")
        return None

    fields = {
        "account_no": account_no,
        "date": date_str,
        "thousands_gal": thousands_gal,
        "days": days,
        "water_charges": water_charges,
        "sewer_charges": sewer_charges,
        "payments": payments,
        "current_charges": current_charges,
        "water_usage": thousands_gal,
        "sewer_usage": sewer_usage,
    }

    if any(v is None for v in fields.values()):
        missing = [k for k, v in fields.items() if v is None]
        # debgug stuff ignore
        # print(f"Warning: {filename} missing field(s): {', '.join(missing)}")
        return None

    return fields

def extract_all_bills(input_folder):
    # Do not include PDFs that live in the duplicate_bills subfolder
    duplicate_dir = str(Path(config.DUPLICATE_DIR).resolve())
    pdf_files = [
        os.path.join(root, f)
        for root, _, files in os.walk(input_folder)
        for f in files
        if f.lower().endswith(".pdf")
        and not f.startswith("combined_")
        and not str(Path(root).resolve()).startswith(duplicate_dir)
    ]
    print(f"Found {len(pdf_files)} PDFs across all subfolders")

    if len(pdf_files) == 0:
        # No PDFs to process — stop the script immediately.
        sys.exit(0)

    all_data = []
    for pdf_path in pdf_files:
        try:
            reader = PdfReader(pdf_path)
            if len(reader.pages) == 0:
                print(f"No pages in {pdf_path}")
                continue

            text = reader.pages[0].extract_text() or ""
            if not text.strip():
                print(f"No text extracted from {os.path.basename(pdf_path)}")
                continue
            back_text = reader.pages[1].extract_text() if len(reader.pages) > 1 else ""

            fields = extract_fields_from_text(text, os.path.basename(pdf_path), back_text or "")
            if fields:
                all_data.append(fields)

        except Exception as e:
            print(f"Error reading {pdf_path}: {e}")

    print(f"Extracted {len(all_data)} of {len(pdf_files)} PDFs successfully")
    return pd.DataFrame(all_data, columns=columns)

def parse_dates(df):
    """Try 4-digit year first, then fall back to 2-digit year."""
    df["date"] = pd.to_datetime(df["date"], format="%m/%d/%Y", errors="coerce")
    mask = df["date"].isna()
    if mask.any():
        df.loc[mask, "date"] = pd.to_datetime(
            df.loc[mask, "date"], format="%m/%d/%y", errors="coerce"
        )
    return df

# === Step 4: Detect missing / double-period bills ===

def infer_missing_bills(df):
    """
    - payments == 0 and charges ~2x normal → double-period bill (note only).
    - a date gap of 1.5x-3x the average billing period → infer a missing bill.
    - a gap of 3x+ → log that 2+ consecutive bills are missing (no fabrication).
    """
    print("Starting inference of missing bills...")
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.sort_values("date").reset_index(drop=True)

    inferred_rows = []
    avg_period = df["days"].mean() if not df["days"].isna().all() else 30

    for i in range(1, len(df)):
        prev = df.loc[i - 1]
        curr = df.loc[i]
        gap_days = (curr["date"] - prev["date"]).days

        if curr["payments"] == 0 and (curr["water_charges"] > 1.8 * prev["water_charges"]):
            print(
                f"Detected possible double-period bill on {curr['date'].date()} "
                f"of ${curr['water_charges'] + curr['sewer_charges']:.2f} with no payment."
            )
            df.loc[i, "note"] = "Double-period bill (no payment on previous month)"

        elif gap_days > avg_period * 1.5 and gap_days < avg_period * 3:
            inferred_date = prev["date"] + timedelta(days=avg_period)
            inferred_rows.append({
                "account_no": prev["account_no"],
                "date": inferred_date,
                "water_charges": (curr["water_charges"] + prev["water_charges"]) / 2,
                "sewer_charges": (curr["sewer_charges"] + prev["sewer_charges"]) / 2,
                "payments": curr["payments"],
                "current_charges": prev["current_charges"],
                "thousands_gal": (curr["thousands_gal"] + prev["thousands_gal"]) // 2,
                "days": int(avg_period),
                "note": "Inferred missing bill (estimated values)",
            })
            print(f"Inferred missing bill around {inferred_date.date()}")

        elif gap_days >= avg_period * 3:
            print(
                f"Missing two or more consecutive bills between "
                f"{prev['date'].date()} and {curr['date'].date()}"
            )

    if inferred_rows:
        df = pd.concat([df, pd.DataFrame(inferred_rows)], ignore_index=True)
        df = df.sort_values("date").reset_index(drop=True)

    return df

# === Step 6: Adjusted water/sewer charges — its own column-computing function ===

def compute_adjusted_charges(df):
    """
    If water + sewer already equals current_charges, the adjusted values
    are just the originals. Otherwise, subtract the previous month's
    charges (raw if there was no payment, adjusted if there was) to back
    out a double-billed amount.
    """
    df = df.sort_values(["account_no", "date"]).reset_index(drop=True)
    df = df.drop_duplicates(subset=["account_no", "date"], keep="first").reset_index(drop=True)

    df["water_charges_adjusted"] = 0.0
    df["sewer_charges_adjusted"] = 0.0
    prev_data = {}

    for idx, row in df.iterrows():
        account = row["account_no"]
        water, sewer = row["water_charges"], row["sewer_charges"]
        current, payments = row["current_charges"], row["payments"]

        is_equal = abs((water + sewer) - current) < 0.01

        if is_equal:
            water_adj, sewer_adj = round(water, 2), round(sewer, 2)
        elif account in prev_data:
            if payments == 0:
                water_adj = round(water - prev_data[account]["water_charges"], 2)
                sewer_adj = round(sewer - prev_data[account]["sewer_charges"], 2)
            else:
                water_adj = round(water - prev_data[account]["water_charges_adjusted"], 2)
                sewer_adj = round(sewer - prev_data[account]["sewer_charges_adjusted"], 2)
        else:
            water_adj, sewer_adj = round(water, 2), round(sewer, 2)

        df.at[idx, "water_charges_adjusted"] = water_adj
        df.at[idx, "sewer_charges_adjusted"] = sewer_adj

        prev_data[account] = {
            "water_charges": water,
            "sewer_charges": sewer,
            "water_charges_adjusted": water_adj,
            "sewer_charges_adjusted": sewer_adj,
        }

    return df

def reorder_columns(df):
    desired_order = [
        "account_no", "date", "thousands_gal", "days",
        "water_charges", "sewer_charges", "payments", "current_charges",
        "water_charges_adjusted", "sewer_charges_adjusted", "note", "water_usage", "sewer_usage",
    ]
    return df[[c for c in desired_order if c in df.columns]]

def keep_latest_outputs(output_path, keep=2):
    """Delete all but the `keep` most recent timestamped CSVs for this output."""
    pattern = f"{output_path.stem}_????????_??????{output_path.suffix}"
    files = sorted(output_path.parent.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
            print(f"Deleted old output: {old.name}")
        except OSError as e:
            print(f"Could not delete {old.name}: {e}")


# === Run pipeline ===

if __name__ == "__main__":
    print("Starting data extraction from PDFs...")
    df = extract_all_bills(input_folder)
    df["note"] = ""
    df = parse_dates(df)
    df = df.sort_values("date").reset_index(drop=True)

    df = infer_missing_bills(df)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")

    df = compute_adjusted_charges(df)
    df = reorder_columns(df)

    df["date"] = df["date"].dt.strftime("%m/%d/%Y")

    # Write a timestamped CSV so multiple runs don't overwrite each other.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    timestamped = output_path.parent / f"{output_path.stem}_{timestamp}{output_path.suffix}"
    df.to_csv(timestamped, index=False)
    keep_latest_outputs(output_path, keep=2)

    print("Processing complete!")
    print(f"Saved final CSV file as: {timestamped}")