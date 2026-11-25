import hashlib
import io
import json
import os
import re
import time
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zipfile import BadZipFile

import pandas as pd
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from openpyxl.utils.exceptions import InvalidFileException
from sqlalchemy import desc
from sqlalchemy.exc import SQLAlchemyError
from xlrd.biffh import XLRDError

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "local-development-key")
database_url = os.environ.get("DATABASE_URL", "sqlite:///studies.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql+psycopg://", 1)
elif database_url.startswith("postgresql://"):
    database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024

db = SQLAlchemy(app)

# Schema version for StudyDataset.charts_cache.
# Bump this number whenever chart-building, grouping, or caching structures change
# so stale cached entries are automatically invalidated and rebuilt on next load.
_SCHEMA_VERSION = 2  # bumped: pids stripped from chart variants (stored separately)

# Maximum rows included in dashboard data preview payload to prevent DOM bloating
PREVIEW_ROW_LIMIT = 200


def _strip_pids_from_charts(charts):
    """Return a copy of *charts* with all 'pids' arrays removed from variants.

    PIDs are fetched on demand via /studies/<id>/category-pids so they don't
    need to live in the chart-data JSON blob that the browser parses on every
    page load.  For the BCC study this reduces the JSON from ~243 MB to ~1 MB.
    """
    stripped = []
    for chart in charts:
        c = {k: v for k, v in chart.items() if k != "variants"}
        variants = []
        for v in chart.get("variants", []):
            variants.append({k: val for k, val in v.items() if k != "pids"})
        c["variants"] = variants
        stripped.append(c)
    return stripped


def _pids_index_from_charts(charts):
    """Build a nested dict  chart_name → variant_index → category_index → [pids].

    This is stored separately in charts_cache under the key 'pids_index' so the
    /category-pids endpoint can answer PID lookups without re-running any
    computation.
    """
    index = {}
    for chart in charts:
        name = chart.get("name") or chart.get("key", "")
        for vi, variant in enumerate(chart.get("variants", [])):
            pids = variant.get("pids")
            if pids:
                index.setdefault(name, {})[vi] = pids
    return index


class StudyDataset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    study_name = db.Column(db.String(160), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    uploaded_at = db.Column(
        db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc)
    )
    rows = db.Column(db.Integer, nullable=False)
    columns = db.Column(db.Integer, nullable=False)
    duplicates_removed = db.Column(db.Integer, nullable=False)
    missing_values_filled = db.Column(db.Integer, nullable=False)
    column_names = db.Column(db.JSON, nullable=False)
    records = db.Column(db.JSON, nullable=False)
    # Optional local path note — set by the user after upload so they can find
    # their copy of the dataset file at any time.
    saved_path = db.Column(db.String(500), nullable=True)
    # Expected number of study participants entered by user
    expected_participants = db.Column(db.Integer, nullable=True)
    # List of column names used to uniquely identify records and detect duplicates
    clean_columns = db.Column(db.JSON, nullable=True)
    # Cleaning mode: 'pid', 'first_three', or 'custom'
    clean_mode = db.Column(db.String(50), nullable=True)
    # Maps sanitized header -> original header (for display only)
    original_headers = db.Column(db.JSON, nullable=True)
    charts_cache = db.Column(db.JSON, nullable=True)
    # Columns locked/hidden from dashboard display by an authorized user
    hidden_columns = db.Column(db.JSON, nullable=True)


class StudyPidSelection(db.Model):
    dataset_id = db.Column(
        db.Integer, db.ForeignKey("study_dataset.id"), primary_key=True
    )
    pid_column = db.Column(db.String(255), nullable=True)


class StudyCodebook(db.Model):
    dataset_id = db.Column(
        db.Integer, db.ForeignKey("study_dataset.id"), primary_key=True
    )
    filename = db.Column(db.String(255), nullable=False)
    variables = db.Column(db.JSON, nullable=False)
    unmatched_variables = db.Column(db.JSON, nullable=False)


class ParticipantTracking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    dataset_id = db.Column(
        db.Integer, db.ForeignKey("study_dataset.id"), nullable=True, index=True
    )
    site = db.Column(db.String(50), nullable=False, default="Lilongwe", index=True)
    pid = db.Column(db.String(100), nullable=False, index=True)
    questionnaire_date = db.Column(db.String(100), nullable=True)
    dental_date = db.Column(db.String(100), nullable=True)
    endo_status = db.Column(db.String(50), nullable=True)
    endo_date = db.Column(db.String(100), nullable=True)
    comment = db.Column(db.String(500), nullable=True)


class AppSetting(db.Model):
    """Key-value settings stored in the database (e.g. admin_password)."""

    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.String(500), nullable=False)


def get_admin_password():
    """Return the current admin password from AppSetting (defaults to DATA123)."""
    setting = db.session.get(AppSetting, "admin_password")
    return setting.value if setting and setting.value else "DATA123"


def verify_admin_password(candidate):
    """Return True if candidate matches the stored admin password."""
    if not isinstance(candidate, str):
        return False
    return candidate == get_admin_password()


def make_unique_headers(headers):
    used = set()
    result = []
    for position, header in enumerate(headers, start=1):
        name = re.sub(r"\s+", " ", str(header)).strip() or f"Column {position}"
        candidate = name
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{name}_{suffix}"
            suffix += 1
        used.add(candidate.casefold())
        result.append(candidate)
    return result


def _normalize_header_token(name):
    """Lowercase, replace non-alphanumerics with _, collapse runs, strip edges."""
    s = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    return re.sub(r"_+", "_", s)


def _looks_like_display_header(col):
    """True if this header looks like a human display label that needs slugification.

    A header needs slugification only if it contains characters that break
    downstream parsing: spaces, question marks, parentheses, slashes, commas,
    or other punctuation beyond underscore/hyphen.

    Headers that are already identifier-safe (letters, digits, underscores,
    hyphens — with or without mixed case) are left exactly as-is.  This keeps
    PID, Visit_Number, houseHold_items, Gender, etc. untouched.
    """
    if not isinstance(col, str):
        return False
    s = col.strip()
    if not s:
        return False
    # Identifier-safe: only letters, digits, underscores, hyphens, dots
    # Mixed case is fine — we do not force lowercase on well-formed names.
    if re.fullmatch(r"[A-Za-z0-9_./-]+", s):
        return False
    # Contains at least one character that is NOT identifier-safe
    # (space, ?, (, ), comma, colon, etc.) → needs slugification
    return True


def _looks_like_machine_name_row(values):
    """True if a row of values looks like a RedCap machine-name header row.

    Requirements (stricter than before to avoid false positives):
      - At least 3 non-null values are checked.
      - 80%+ of non-null values match the snake_case pattern.
      - At least one value contains an underscore (bare digits / single letters
        are too ambiguous and must not be promoted to column names).
    """
    hits, checked = 0, 0
    has_underscore = False
    for v in values:
        if v is None or pd.isna(v):
            continue
        s = str(v).strip()
        if not s or s.lower() == "nan":
            continue
        checked += 1
        if re.fullmatch(r"[a-z][a-z0-9_]*", s):
            hits += 1
            if "_" in s:
                has_underscore = True
    return checked >= 3 and hits / checked >= 0.8 and has_underscore


def sanitize_headers_for_upload(df):
    """Normalize headers so detection and charting work, whether the source is
    a machine export or a labeled export.

    Steps:
      - Detect RedCap two-row header (row 2 = machine names). If present,
        re-read the dataframe with header=1 and drop the display-label row.
      - For each remaining column header, if it looks like a display label,
        slugify it to lowercase snake_case. Keep original header for later
        display if desired via a mapping stored on the DataFrame.

    Returns (df, header_map) where header_map: safe_header -> original_header.
    """
    # Detect two-row header
    try:
        first_row = df.iloc[0].tolist() if len(df) > 0 else []
    except Exception:
        first_row = []

    if _looks_like_machine_name_row(first_row):
        # Row 1 became data rows of machine names; rebuild with them as header.
        # All data rows should shift: header becomes first_row, data is df.iloc[1:]
        new_cols = [
            str(c).strip() if c and not pd.isna(c) else f"col_{i}"
            for i, c in enumerate(first_row)
        ]
        df = df.iloc[1:].copy()
        df.columns = new_cols
        df = df.reset_index(drop=True)

    header_map = {}
    new_cols = []
    seen = {}
    for i, col in enumerate(df.columns):
        orig = str(col) if col is not None else f"col_{i}"
        if _looks_like_display_header(orig):
            safe = _normalize_header_token(orig) or f"col_{i}"
            if safe and safe[0].isdigit():
                safe = f"col_{safe}"
        else:
            safe = str(orig).strip() or f"col_{i}"

        # Deduplicate
        if safe in seen:
            seen[safe] += 1
            safe = f"{safe}_{seen[safe]}"
        else:
            seen[safe] = 1

        header_map[safe] = orig
        new_cols.append(safe)

    df.columns = new_cols

    # Rename first plausible PID-ish column to 'pid' for downstream detection
    pid_like = [
        c
        for c in df.columns
        if c
        in {
            "pid",
            "bcc_pid",
            "participant_id",
            "patient_id",
            "subject_id",
            "respondent_id",
        }
    ]
    if not pid_like:
        # Look for '*_pid' or '*_id' where the stem contains patient|participant|subject|respondent|bcc
        for c in df.columns:
            if c.endswith("_pid") or c.endswith("_id"):
                stem = c.rsplit("_", 1)[0]
                if any(
                    kw in stem
                    for kw in (
                        "patient",
                        "participant",
                        "subject",
                        "respondent",
                        "bcc",
                    )
                ):
                    pid_like.append(c)
                    break
    if pid_like and pid_like[0] != "pid":
        df = df.rename(columns={pid_like[0]: "pid"})
        header_map["pid"] = header_map.pop(pid_like[0], pid_like[0])

    return df, header_map


def is_fixed_demographic_column(column):
    """Return True ONLY for fixed personal identity fields that should be
    forward/back-filled across all repeat visits for the same participant.

    This is intentionally narrow — only names and date-of-birth.
    Event dates (consent, operation, appointment, collection) and single-instance
    survey responses must NOT be ffilled, so they are excluded here.
    """
    col_low = re.sub(r"[^a-z0-9]+", "_", str(column).strip().lower()).strip("_")
    fixed_exact = {
        "first_name", "firstname", "given_name", "givenname",
        "last_name", "lastname", "surname", "family_name", "familyname",
        "middle_name", "middlename", "full_name", "fullname",
        "dob", "date_of_birth", "dateofbirth", "birth_date", "birthdate",
        "birth_year", "year_of_birth",
    }
    if col_low in fixed_exact:
        return True
    fixed_substrings = (
        "first_name", "last_name", "surname", "dob", "birth_date", "date_of_birth",
    )
    return any(kw in col_low for kw in fixed_substrings)


def is_personal_or_date_column(column, series=None, pid_columns=None):
    """Return True for columns that hold personal names, dates, DOB, or
    contact details so they are NEVER filled with the global mode value.
    PID columns are explicitly excluded — they are handled separately."""
    col_str = str(column).strip()
    col_low = col_str.lower().replace(" ", "_").replace("-", "_")

    if pid_columns and (column in pid_columns or col_str in pid_columns):
        return False

    personal_keywords = {
        "first_name", "firstname", "given_name", "givenname",
        "last_name", "lastname", "surname", "family_name", "familyname",
        "middle_name", "middlename", "full_name", "fullname", "name",
        "dob", "date_of_birth", "dateofbirth", "birth_date", "birthdate",
        "birth_year", "year_of_birth",
        "phone", "phone_number", "telephone", "mobile", "email",
        "address", "street", "postcode", "zip", "national_id", "nid",
        "passport", "id_number",
    }
    if col_low in personal_keywords:
        return True
    for kw in ("first_name", "last_name", "surname", "dob", "birth_date",
               "date_of_birth", "phone", "email", "address", "telephone"):
        if kw in col_low:
            return True

    # Date keyword check: match date, time, consent, enrolled, appointment, etc.
    date_hint_keywords = (
        "date", "consent", "enrolled", "enrollment", "signed",
        "appointment", "start_date", "stop_date", "end_date",
        "biopsy_date", "death_date", "censor_date",
    )
    tokens = set(re.findall(r"[a-z0-9]+", col_low))
    if any(kw in col_low for kw in date_hint_keywords) or "date" in tokens or "dates" in tokens:
        return True

    # Value-based date detection: if 50%+ of non-null values look like dates
    if series is not None:
        try:
            nonnull = series.dropna()
            if len(nonnull) >= 2:
                sample = nonnull.astype(str).str.strip()
                date_like = (
                    sample.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}").mean()
                    + sample.str.match(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}").mean()
                )
                if date_like >= 0.5:
                    return True
                # Excel serial dates: bare integers > 30000 (post-1982)
                try:
                    as_num = pd.to_numeric(sample, errors="coerce")
                    if as_num.notna().mean() >= 0.8 and (as_num > 30000).mean() >= 0.8:
                        return True
                except Exception:
                    pass
        except Exception:
            pass

    return False


def clean_dataframe(
    dataframe, categorical_columns=None, preserve_missing_columns=None,
    pid_column=None,
):
    categorical_columns = categorical_columns or set()
    preserve_missing_columns = preserve_missing_columns or set()
    dataframe.columns = make_unique_headers(dataframe.columns)
    dataframe = dataframe.replace([float("inf"), float("-inf")], float("nan"))
    pid_columns = set(detect_pid_columns(dataframe.columns))

    empty_columns = [
        column
        for column in dataframe.columns
        if column not in pid_columns and dataframe[column].isna().all()
    ]
    dataframe = dataframe.drop(columns=empty_columns).dropna(axis=0, how="all")
    duplicates_removed = 0
    dataframe = dataframe.reset_index(drop=True)

    # --- Per-participant forward/back-fill (DISABLED) ---
    # Previously filled fixed demographic columns (names, DOB) across repeat
    # visits. Now disabled: blanks stay blank, and charts count only non-null
    # cells so participant totals are not inflated by repeat rows.
    #
    # if pid_column and pid_column in dataframe.columns:
    #     for column in dataframe.columns:
    #         if column == pid_column:
    #             continue
    #         if dataframe[column].isna().any() and is_fixed_demographic_column(column):
    #             dataframe[column] = dataframe.groupby(pid_column, sort=False)[
    #                 column
    #             ].transform(lambda s: s.ffill().bfill())

    # Missing values are intentionally NOT filled. `missing_values_filled` is
    # kept at 0 for backward compatibility with the cleaning report;
    # `missing_values_retained` records how many nulls actually remain.
    missing_values_filled = 0
    missing_values_retained = int(dataframe.isna().sum().sum())

    # --- Global mode/median fill (DISABLED) ---
    # Previously filled blanks with a per-column mode (categorical) or median
    # (numeric), with a guard to skip per-participant columns in repeat-visit
    # datasets. Now disabled entirely.
    #
    # for column in dataframe.columns:
    #     missing_count = int(dataframe[column].isna().sum())
    #     if not missing_count:
    #         continue
    #     if column in preserve_missing_columns:
    #         continue
    #     # Never fill personal/date columns with the global mode.
    #     if is_personal_or_date_column(column, dataframe[column], pid_columns=pid_columns):
    #         continue
    #     # Never mode/median-fill a sparse column where each PID has at most 1
    #     # non-null value (e.g. a one-time consent date in a repeat-visit dataset).
    #     # Filling would artificially clone that single instance into every row.
    #     if (
    #         pid_column
    #         and pid_column in dataframe.columns
    #         and pid_column in pid_columns  # must be a true PID, not a fallback
    #     ):
    #         pid_counts = dataframe.groupby(pid_column, sort=False)[column].count()
    #         max_per_pid = int(pid_counts.max())
    #         pid_row_counts = dataframe[pid_column].value_counts()
    #         has_repeats = bool((pid_row_counts > 1).any())
    #         if max_per_pid <= 1 and has_repeats:
    #             continue
    #     missing_values_filled += missing_count
    #     if (
    #         pd.api.types.is_numeric_dtype(dataframe[column])
    #         and column not in categorical_columns
    #     ):
    #         median = dataframe[column].median()
    #         dataframe[column] = dataframe[column].fillna(
    #             0 if pd.isna(median) else median
    #         )
    #     else:
    #         mode = dataframe[column].mode(dropna=True)
    #         dataframe[column] = dataframe[column].fillna(
    #             mode.iloc[0] if not mode.empty else "Unknown"
    #         )

    def json_value(value):
        if pd.isna(value):
            return None
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if hasattr(value, "item"):
            value = value.item()
        if isinstance(value, Decimal):
            return float(value)
        return value

    records = [
        {column: json_value(value) for column, value in row.items()}
        for row in dataframe.to_dict(orient="records")
    ]

    return (
        dataframe,
        duplicates_removed,
        missing_values_filled,
        missing_values_retained,
        records,
    )


def detect_csv_delimiter(filename, line):
    """Detect whether a CSV/TSV file uses comma or tab delimiters.

    Avoids false-positive tab detection on CSV files whose question headers
    contain internal tab characters (e.g. bilingual translations).
    """
    ext = os.path.splitext(filename or "")[1].lower()
    if ext == ".tsv":
        return "\t"
    if ext == ".csv":
        if "," in line:
            return ","
        if "\t" in line:
            return "\t"
        return ","
    comma_count = line.count(",")
    tab_count = line.count("\t")
    return "\t" if tab_count > comma_count else ","


_EXCEL_SERIAL_DATE_HINTS = (
    "date", "dob", "birth", "consent", "enrolled", "enrollment",
    "visit", "appointment", "biopsy", "reported", "received",
    "withdraw", "death", "died", "censor", "progress",
    "start_date", "stop_date", "end_date",
)
# Plausible Excel serial range: 1954-01-01 (19723) to 2089-12-31 (~69337).
# Using slightly wider [20000, 80000] to give headroom.
_EXCEL_SERIAL_MIN = 20000
_EXCEL_SERIAL_MAX = 80000


def _coerce_excel_serials_to_dates(df):
    """Convert numeric columns that look like Excel serial dates into ISO date
    strings (YYYY-MM-DD).  Applies only to columns whose name suggests a date
    and whose non-null values are ≥80% in [20000, 80000] (roughly 1954-2089).
    """
    for col in df.columns:
        if not pd.api.types.is_numeric_dtype(df[col]):
            continue
        col_lower = str(col).lower()
        if not any(kw in col_lower for kw in _EXCEL_SERIAL_DATE_HINTS):
            continue
        nums = df[col].dropna()
        if nums.empty:
            continue
        in_range = nums.between(_EXCEL_SERIAL_MIN, _EXCEL_SERIAL_MAX)
        if in_range.mean() < 0.8:
            continue
        try:
            converted = pd.to_datetime(
                nums[in_range], unit="D", origin="1899-12-30", errors="coerce"
            ).dt.strftime("%Y-%m-%d")
            df[col] = df[col].astype(object)
            df.loc[nums[in_range].index, col] = converted.values
        except Exception:
            pass  # leave column unchanged if conversion fails
    return df


def read_upload(upload):
    filename = getattr(upload, "filename", "") or ""
    extension = os.path.splitext(filename)[1].lower()
    if extension in (".csv", ".tsv"):
        pos = upload.tell() if hasattr(upload, "tell") else None
        line = upload.readline()
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="ignore")
        sep = detect_csv_delimiter(filename, line)
        if pos is not None and hasattr(upload, "seek"):
            upload.seek(pos)
        df = pd.read_csv(upload, sep=sep, low_memory=False)
        return _coerce_excel_serials_to_dates(df)
    if extension in (".xlsx", ".xls"):
        df = pd.read_excel(upload)
        return _coerce_excel_serials_to_dates(df)
    raise ValueError("Upload a CSV or Excel file (.csv, .xlsx, .xls).")


def read_upload_headers(upload):
    filename = getattr(upload, "filename", "") or ""
    extension = os.path.splitext(filename)[1].lower()
    if extension in (".csv", ".tsv"):
        pos = upload.tell() if hasattr(upload, "tell") else None
        line = upload.readline()
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="ignore")
        sep = detect_csv_delimiter(filename, line)
        if pos is not None and hasattr(upload, "seek"):
            upload.seek(pos)
        df = pd.read_csv(upload, sep=sep, nrows=2)
        if pos is not None and hasattr(upload, "seek"):
            upload.seek(pos)
        df, header_map = sanitize_headers_for_upload(df)
        cols = list(df.columns)
        # Build display_names: original label for each sanitized column
        display_names = [header_map.get(c, c) for c in cols]
        return cols, display_names
    if extension in (".xlsx", ".xls"):
        pos = upload.tell() if hasattr(upload, "tell") else None
        df = pd.read_excel(upload, nrows=2)
        if pos is not None and hasattr(upload, "seek"):
            upload.seek(pos)
        df, header_map = sanitize_headers_for_upload(df)
        cols = list(df.columns)
        display_names = [header_map.get(c, c) for c in cols]
        return cols, display_names
    return [], []


def is_missing(value):
    return value is None or bool(pd.isna(value))


def normalize_code(value):
    if is_missing(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    if isinstance(value, (int, float, Decimal)) or not re.match(
        r"^[+-]?0\d", text
    ):
        try:
            number = Decimal(text)
            if number.is_finite():
                if number == number.to_integral():
                    return str(int(number))
                return str(number.normalize())
        except (InvalidOperation, ValueError):
            pass
    return text


def _excel_serial_to_datetime(value):
    """Convert a single Excel serial number to a pandas Timestamp, or None."""
    try:
        ts = pd.to_datetime(value, unit="D", origin="1899-12-30")
        return ts
    except Exception:
        return None


def _looks_like_excel_serials(series):
    """Return True if ≥50% of the non-null values in *series* (numeric) are
    in the plausible Excel-serial range [20000, 80000].
    """
    nums = pd.to_numeric(series, errors="coerce").dropna()
    if nums.empty:
        return False
    return nums.between(_EXCEL_SERIAL_MIN, _EXCEL_SERIAL_MAX).mean() >= 0.5


def extract_option_match(column):
    match = re.match(r"^(.*?)([/_\.\s]|(?<=[a-zA-Z]))([0-9]+)$", str(column).strip())
    if match:
        stem = match.group(1).rstrip("/_.- ")
        delimiter = match.group(2)
        code = match.group(3)
        if stem:
            return stem, code, delimiter
    return None, None, None


def is_excluded_visualization_column(column):
    col_str = str(column).strip()
    col_lower = col_str.casefold()
    col_clean = re.sub(r"[^a-z0-9]+", "", col_lower)

    # Exclude system-generated technical metadata
    if col_clean in {
        "id",
        "rowid",
        "uuid",
        "guid",
        "deviceid",
        "device",
        "submissiondate",
        "submission",
        "instanceid",
        "metadata",
        "meta",
        "simserial",
    }:
        return True

    # Exclude primary participant ID columns
    if col_clean in {
        "pid",
        "participantid",
        "subjectid",
        "respondentid",
    } or col_lower.startswith("pid_") or col_lower.startswith("pid-"):
        return True

    spaced_name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", col_str)
    tokens = set(re.findall(r"[a-z0-9]+", spaced_name.casefold()))
    excluded_tokens = {
        "device",
        "deviceid",
        "guid",
        "instanceid",
        "metadata",
        "submission",
        "submissiondate",
        "uuid",
    }
    starts_with_admin_prefix = re.match(
        r"^(?:start|end|today)(?:[_\s]|$)", col_lower
    )
    is_field_notes = "field_notes" in col_lower or "notes" in tokens
    is_other_specify = col_lower.startswith("other_") or col_lower.startswith("other ")

    return (
        bool(tokens.intersection(excluded_tokens))
        or starts_with_admin_prefix is not None
        or is_field_notes
        or is_other_specify
    )


def _is_date_like_column_name(name):
    """Return True for column names that look like date fields.

    Date columns (e.g. 'Date of biopsy 1', 'consent_date_2') must not be
    grouped as multi-response subcolumns even when their sanitized name ends in
    a number, because they are not check-all-that-apply options.
    """
    lower = str(name).lower()
    date_hints = ("date", "dob", "birth", "timestamp")
    return any(kw in lower for kw in date_hints)


def group_question_columns(df):
    columns = list(df.columns) if hasattr(df, "columns") else list(df)
    eligible_columns = [
        str(column)
        for column in columns
        if not is_excluded_visualization_column(column)
    ]
    col_lookup = {col.casefold(): col for col in eligible_columns}
    options_by_stem = {}

    for column in eligible_columns:
        stem, code, delimiter = extract_option_match(column)
        if stem is not None:
            # Skip date-like columns — 'Date of biopsy 1' ends in 1 but is not
            # a multi-response checkbox option.
            if _is_date_like_column_name(column):
                continue
            options_by_stem.setdefault(stem.casefold(), []).append(
                (column, stem, code, delimiter)
            )

    multiple_response_questions = {}
    for stem_lower, items in options_by_stem.items():
        has_delimited = any(item[3] in {"/", "_", ".", " "} for item in items)
        if len(items) > 1 or (stem_lower in col_lookup and has_delimited):
            canonical_name = col_lookup.get(stem_lower, items[0][1])
            items_sorted = sorted(items, key=lambda x: int(x[2]))
            multiple_response_questions[canonical_name] = [x[0] for x in items_sorted]

    subcolumn_to_group = {}
    for group_name, cols in multiple_response_questions.items():
        for col in cols:
            subcolumn_to_group[col] = group_name

    # RedCap labeled exports: "Question text? (select all that apply) (choice=Option)"
    choice_pattern = re.compile(
        r"^(.*?)\s*\(select all that apply\).*?\(choice=(.+?)\)\s*$",
        re.IGNORECASE,
    )
    slug_choice_pattern = re.compile(
        r"^(.*?)(?:_+select_all_that_apply)?_+choice_(.+?)$",
        re.IGNORECASE,
    )
    for column in eligible_columns:
        if column in subcolumn_to_group:
            continue
        # Date columns are never multi-response options
        if _is_date_like_column_name(column):
            continue
        col_str = str(column)
        m = choice_pattern.match(col_str) or slug_choice_pattern.match(col_str)
        if not m:
            continue
        stem = m.group(1).strip("_ ")
        if not stem:
            continue
        options_by_stem.setdefault(stem, []).append((column, stem, None, None))

    # Merge new stems into multiple_response_questions.
    # options_by_stem may have casefold keys from the first pass; compare
    # case-insensitively to avoid creating duplicate entries under a different
    # casing than what multiple_response_questions already holds.
    mrq_lower = {k.casefold(): k for k in multiple_response_questions}
    for stem, items in options_by_stem.items():
        existing_key = mrq_lower.get(stem.casefold())
        if len(items) > 1 and existing_key is None:
            multiple_response_questions[stem] = [x[0] for x in items]
            for col in multiple_response_questions[stem]:
                subcolumn_to_group[col] = stem


    mr_names_lower = {k.casefold(): k for k in multiple_response_questions}

    groups = {}
    for column in eligible_columns:
        if column in subcolumn_to_group:
            group_name = subcolumn_to_group[column]
            if group_name not in groups:
                groups[group_name] = multiple_response_questions[group_name]
        elif column.casefold() in mr_names_lower:
            group_name = mr_names_lower[column.casefold()]
            if group_name not in groups:
                groups[group_name] = multiple_response_questions[group_name]
        else:
            groups[column] = [column]

    return groups


def detect_pid_columns(columns):
    candidates = []
    for column in columns:
        col_str = str(column).strip()
        col_lower = col_str.casefold()
        normalized = re.sub(r"[^a-z0-9]+", "", col_lower)
        tokens = set(re.findall(r"[a-z0-9]+", col_lower))
        if normalized in {
            "pid",
            "participantid",
            "participantidentifier",
            "respondentid",
            "subjectid",
            "patientid",
        }:
            candidates.append(column)
        elif re.search(r"(^|[\s_\.-])pid($|[\s_\.-])", col_lower):
            candidates.append(column)
        elif (
            ("patient" in tokens and "id" in tokens)
            or ("subject" in tokens and "id" in tokens)
            or ("participant" in tokens and "id" in tokens)
        ):
            candidates.append(column)
        elif normalized.startswith("pid"):
            candidates.append(column)
    return candidates


def pid_natural_sort_key(value):
    text = str(value).strip()
    numbers = tuple(int(part) for part in re.findall(r"\d+", text))
    natural_text = tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", text)
        if part
    )
    return (0, numbers, natural_text) if numbers else (1, (), natural_text)


def sort_pid_rows(records, pid_columns):
    def row_key(entry):
        pid_values = [
            entry["record"].get(column)
            for column in pid_columns
            if not is_missing(entry["record"].get(column))
            and str(entry["record"].get(column)).strip()
        ]
        if not pid_values:
            return (1, (), entry["index"])
        return (
            0,
            min(pid_natural_sort_key(value) for value in pid_values),
            entry["index"],
        )

    return sorted(records, key=row_key)


def find_pid_sequence_gaps(records, pid_columns):
    gaps_by_column = []
    for column in pid_columns:
        values_by_prefix = {}
        for record in records:
            value = record.get(column)
            if is_missing(value) or not str(value).strip():
                continue
            text = str(value).strip()
            match = re.search(r"(\d+)(?:\.0+)?$", text)
            if not match:
                continue
            suffix = match.group(1)
            prefix = text[:match.start()]
            key = prefix.casefold()
            group = values_by_prefix.setdefault(
                key,
                {"prefix": prefix, "width": 0, "numbers": set()},
            )
            group["width"] = max(group["width"], len(suffix))
            group["numbers"].add(int(suffix))

        for group in values_by_prefix.values():
            numbers = group["numbers"]
            if len(numbers) < 2:
                continue
            missing_numbers = sorted(
                set(range(min(numbers) + 1, max(numbers))).difference(numbers)
            )
            if missing_numbers:
                gaps_by_column.append(
                    {
                        "column": column,
                        "prefix": group["prefix"],
                        "missing_ids": [
                            f"{group['prefix']}{number:0{group['width']}d}"
                            for number in missing_numbers
                        ],
                    }
                )
    return gaps_by_column


def get_dataset_id_columns(dataset):
    if getattr(dataset, "clean_columns", None):
        valid = [c for c in dataset.clean_columns if c in dataset.column_names]
        if valid:
            return valid
    from flask import has_app_context
    pid_column = None
    if has_app_context() and getattr(dataset, "id", None) is not None:
        try:
            selection = db.session.get(StudyPidSelection, dataset.id)
            if selection:
                pid_column = selection.pid_column
        except Exception:
            pass
    candidates = detect_pid_columns(dataset.column_names)
    if not pid_column and len(candidates) == 1:
        pid_column = candidates[0]
    if pid_column and pid_column in dataset.column_names:
        return [pid_column]
    if not candidates and "pid" in dataset.column_names:
        return ["pid"]
    return candidates[:1] if candidates else []


def find_pid_duplicate_groups(dataset, pid_columns):
    if not pid_columns:
        return [], []
    if isinstance(pid_columns, str):
        id_cols = [pid_columns]
    else:
        id_cols = [c for c in pid_columns if c in dataset.column_names]
    if not id_cols:
        return [], []

    excluded_diff_cols = set(detect_pid_columns(dataset.column_names)).union(id_cols)
    groups_by_key = {}

    for index, record in enumerate(dataset.records):
        key_parts = []
        has_any_val = False
        display_parts = []
        for col in id_cols:
            val = record.get(col)
            if not is_missing(val) and str(val).strip():
                has_any_val = True
                norm = normalize_code(val)
                norm_str = norm.casefold() if norm is not None else str(val).strip().casefold()
                key_parts.append(norm_str)
                display_parts.append(f"{col}: {val}")
            else:
                key_parts.append("")
                display_parts.append(f"{col}: —")

        if not has_any_val:
            continue

        composite_key = tuple(key_parts)
        if len(id_cols) > 1:
            combined_value = " / ".join(
                str(record.get(col)).strip()
                for col in id_cols
                if not is_missing(record.get(col)) and str(record.get(col)).strip()
            ) or " · ".join(display_parts)
        else:
            combined_value = str(record.get(id_cols[0]))
        primary_pid = combined_value
        display_key = combined_value

        groups_by_key.setdefault(composite_key, []).append(
            {
                "index": index,
                "record": record,
                "pid": primary_pid,
                "composite_key": display_key,
            }
        )

    identical = []
    differing = []
    for rows in groups_by_key.values():
        if len(rows) < 2:
            continue
        signatures = {
            tuple(
                (column, repr(row["record"].get(column)))
                for column in dataset.column_names
                if column not in excluded_diff_cols
            )
            for row in rows
        }
        different_columns = [
            column
            for column in dataset.column_names
            if column not in excluded_diff_cols
            and len({repr(row["record"].get(column)) for row in rows}) > 1
        ]
        group = {
            "pid": rows[0]["pid"],
            "composite_key": rows[0]["composite_key"],
            "id_columns": id_cols,
            "rows": rows,
            "different_columns": different_columns,
        }
        (identical if len(signatures) == 1 else differing).append(group)
    return identical, differing


def coerce_edited_value(original, value):
    value = value.strip()
    if not value:
        return None
    if isinstance(original, bool):
        normalized = value.casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        raise ValueError("Enter true or false for this value.")
    if isinstance(original, int) and not isinstance(original, bool):
        try:
            return int(value)
        except ValueError as error:
            raise ValueError("Enter a whole number for this value.") from error
    if isinstance(original, float):
        try:
            return float(value)
        except ValueError as error:
            raise ValueError("Enter a number for this value.") from error
    return value


def multiselect_selection(value):
    if is_missing(value):
        return False
    # Date strings (e.g. "02-Dec-16") must never be counted as "selected"
    if isinstance(value, str):
        v = value.strip()
        _mon = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
        if re.match(rf"^\d{{1,2}}[-/\s]{_mon}[-/\s]\d{{2,4}}$", v, re.IGNORECASE):
            return False
    code = normalize_code(value)
    return str(code).casefold() not in {"0", "false", "no", "off", "unchecked", ""}


def multiselect_labels(question, columns, codebook):
    codebook = codebook or {}
    variable_lists = codebook.get("variable_lists", {})
    response_lists = codebook.get("lists", {})
    list_name = variable_lists.get(question)
    if list_name is None:
        matching_name = next(
            (name for name in response_lists if name.casefold() == question.casefold()),
            None,
        )
        if matching_name is not None:
            list_name = matching_name
    if list_name is None:
        mapped_lists = {
            variable_lists[column]
            for column in columns
            if column in variable_lists
        }
        if len(mapped_lists) == 1:
            list_name = next(iter(mapped_lists))

    option_codes = []
    for column in columns:
        _, code, _ = extract_option_match(column)
        option_codes.append(code if code is not None else str(column))

    if list_name is None and response_lists:
        matching_by_codes = [
            name
            for name, ch in response_lists.items()
            if set(option_codes).issubset(
                set(ch.keys()) | {normalize_code(k) for k in ch.keys()}
            )
        ]
        if len(matching_by_codes) == 1:
            list_name = matching_by_codes[0]
        elif len(matching_by_codes) > 1:
            overlap = [
                name
                for name in matching_by_codes
                if name.casefold() in question.casefold()
                or question.casefold() in name.casefold()
            ]
            if len(overlap) == 1:
                list_name = overlap[0]

    choices = response_lists.get(list_name, {}) if list_name else {}
    if not choices:
        return [f"Option {code}" for code in option_codes], [
            f"Option {code}" for code in option_codes
        ], list_name

    def lookup_choice(code):
        norm = normalize_code(code)
        entry = choices.get(code) or (choices.get(norm) if norm else None) or {}
        return entry

    english = [
        lookup_choice(code).get("en")
        or lookup_choice(code).get("ny")
        or f"Option {code}"
        for code in option_codes
    ]
    chichewa = [
        lookup_choice(code).get("ny")
        or lookup_choice(code).get("en")
        or f"Option {code}"
        for code in option_codes
    ]
    return english, chichewa, list_name


def categorical_chart_variant(
    labels,
    labels_en,
    labels_ny,
    values,
    category_count=None,
    codes=None,
    pids=None,
):
    category_count = category_count if category_count is not None else len(labels)
    if 2 <= category_count <= 3:
        chart_type, chart_label = "pie", "Pie chart"
    elif 4 <= category_count <= 5:
        chart_type, chart_label = "donut", "Donut chart"
    else:
        chart_type, chart_label = "bar", "Bar chart"

    variant = {
        "type": chart_type,
        "label": chart_label,
        "labels": labels,
        "labels_en": labels_en,
        "labels_ny": labels_ny,
        "values": [int(value) for value in values],
    }
    if codes is not None:
        variant["codes"] = codes
    if pids is not None:
        variant["pids"] = pids
    return variant


def parse_codebook(upload, data_frame):
    codebook_frame = read_upload(upload)
    normalized_headers = {
        re.sub(r"[^a-z0-9]+", "", str(column).casefold()): column
        for column in codebook_frame.columns
    }
    warnings = []

    def find_header(aliases):
        matches = [
            normalized_headers[alias]
            for alias in aliases
            if alias in normalized_headers
        ]
        if len(matches) > 1:
            warnings.append(
                f"Multiple possible columns were found ({', '.join(map(str, matches))}); "
                f"using {matches[0]}."
            )
        return matches[0] if matches else None

    list_header = find_header(
        ("listname", "list", "responselist", "responselistname", "choicegroup")
    )
    variable_header = find_header(
        (
            "variable", "variablename", "variablefieldname", "question",
            "questionname", "field", "fieldname",
        )
    )
    code_header = find_header(
        ("name", "code", "value", "optioncode", "choicecode", "responsecode")
    )
    english_header = find_header(
        (
            "labelenglish",
            "englishlabel",
            "labelen",
            "english",
            "label",
            "fieldlabel",
            "valuelabel",
            "optionlabel",
            "choicelabel",
        )
    )
    chichewa_header = find_header(
        (
            "labelchichewa",
            "chichewalabel",
            "labelny",
            "chichewa",
            "ny",
        )
    )
    form_header = find_header(
        ("formname", "form", "instrumentname", "instrument", "crfname", "crf")
    )
    field_label_header = find_header(
        ("fieldlabel", "label", "labelenglish", "englishlabel", "description")
    )
    choices_header = find_header(
        ("choicescalculationsorsliderlabels", "choices", "options", "calculationorsliderlabels")
    )

    data_columns = list(data_frame.columns)
    column_lookup = {column.casefold(): column for column in data_columns}
    multiple_response_columns = {
        column
        for columns in group_question_columns(data_frame).values()
        if len(columns) > 1
        for column in columns
    }
    lists = {}
    explicit_variable_lists = {}
    source_columns = [str(column) for column in codebook_frame.columns]
    group_header = list_header or variable_header

    # Parse inline choices from RedCap data dictionaries (Choices, Calculations, OR Slider Labels)
    if choices_header is not None and variable_header is not None:
        for _, row in codebook_frame.iterrows():
            var_val = row[variable_header]
            choices_val = row[choices_header]
            if is_missing(var_val) or is_missing(choices_val):
                continue
            raw_choices = str(choices_val).strip()
            if not raw_choices:
                continue
            var_name = str(var_val).strip()
            choice_map = {}
            for item in raw_choices.split("|"):
                item = item.strip()
                if not item:
                    continue
                if "," in item:
                    code_part, label_part = item.split(",", 1)
                    code_clean = normalize_code(code_part.strip())
                    if code_clean is not None:
                        choice_map[code_clean] = {"en": label_part.strip()}
            if choice_map:
                lists[var_name] = choice_map
                data_col = column_lookup.get(var_name.casefold())
                if data_col:
                    explicit_variable_lists[data_col] = var_name

    if code_header is None and choices_header is None:
        warnings.append(
            "Could not identify a code column. Recognized alternatives include "
            "name, code, and value; this codebook was saved for review but not applied."
        )
    elif code_header is not None:
        if group_header is None:
            list_name = "Codebook choices"
            warnings.append(
                "Could not identify a response-list or question column. Codes were "
                "saved under one general list; choose the correct mapping on each chart."
            )
        else:
            list_name = None

        if english_header is None and chichewa_header is None:
            warnings.append(
                "No label column was identified. Available option codes will be shown "
                "instead of invented labels."
            )

        ambiguous_codes = {}
        for _, row in codebook_frame.iterrows():
            code = normalize_code(row[code_header])
            if code is None:
                continue

            if group_header is None:
                row_list_name = list_name
            else:
                group_value = row[group_header]
                if is_missing(group_value) or not str(group_value).strip():
                    continue
                row_list_name = str(group_value).strip()

            labels = {}
            for language, header in (
                ("en", english_header),
                ("ny", chichewa_header),
            ):
                if header is None:
                    continue
                value = row[header]
                if not is_missing(value) and str(value).strip():
                    labels[language] = str(value).strip()

            choice_list = lists.setdefault(row_list_name, {})
            ambiguous_list = ambiguous_codes.setdefault(row_list_name, set())
            if code in ambiguous_list:
                continue
            previous = choice_list.get(code)
            if previous is not None and previous != labels:
                choice_list.pop(code)
                ambiguous_list.add(code)
                warnings.append(
                    f"Conflicting labels were found for code {code} in "
                    f"response list {row_list_name}; that code will display as its value."
                )
                continue
            choice_list[code] = labels

        if list_header is not None and variable_header is not None:
            for _, row in codebook_frame.iterrows():
                variable_value = row[variable_header]
                list_value = row[list_header]
                if is_missing(variable_value) or is_missing(list_value):
                    continue
                data_column = column_lookup.get(
                    str(variable_value).strip().casefold()
                )
                row_list_name = str(list_value).strip()
                if data_column and row_list_name in lists:
                    explicit_variable_lists[data_column] = row_list_name

        if not lists:
            warnings.append(
                "No usable code and response-list rows were found. Review the codebook "
                "columns and confirm which column contains option codes."
            )

    candidates = {}
    variable_lists = {}
    coded_columns = set()
    for column in data_columns:
        observed = {
            code
            for code in (normalize_code(value) for value in data_frame[column].dropna())
            if code is not None
        }
        if not observed:
            continue

        explicit_list = explicit_variable_lists.get(column)
        if explicit_list:
            variable_lists[column] = explicit_list
            coded_columns.add(column)
            continue
        if column in multiple_response_columns:
            continue
        same_name = next(
            (name for name in lists if name.casefold() == column.casefold()), None
        )
        if same_name:
            variable_lists[column] = same_name
            coded_columns.add(column)
            continue

        numeric_codes = all(
            re.fullmatch(r"[+-]?\d+(?:\.0+)?", code) is not None
            for code in observed
        )
        categorical_domain = len(observed) <= min(
            30, max(8, int(len(data_frame) * 0.2))
        )
        if not numeric_codes or not categorical_domain:
            continue

        matching_lists = sorted(
            (
                list_name
                for list_name, choices in lists.items()
                if observed == set(choices)
            ),
            key=str.casefold,
        )
        if not matching_lists:
            continue
        coded_columns.add(column)
        candidates[column] = matching_lists

    # Map variables to Form Names (RedCap data dictionaries)
    variable_forms = {}
    if form_header is not None and variable_header is not None:
        for _, row in codebook_frame.iterrows():
            form_val = row[form_header]
            if is_missing(form_val) or not str(form_val).strip():
                continue
            form_name = str(form_val).strip()

            var_val = row[variable_header]
            if not is_missing(var_val) and str(var_val).strip():
                raw_var = str(var_val).strip()
                variable_forms[raw_var] = form_name
                slug_var = re.sub(r"[^a-z0-9]+", "_", raw_var.lower()).strip("_")
                slug_var = re.sub(r"_+", "_", slug_var)
                if slug_var:
                    variable_forms[slug_var] = form_name

            if field_label_header is not None:
                label_val = row[field_label_header]
                if not is_missing(label_val) and str(label_val).strip():
                    raw_label = str(label_val).strip()
                    variable_forms[raw_label] = form_name
                    slug_label = re.sub(r"[^a-z0-9]+", "_", raw_label.lower()).strip("_")
                    slug_label = re.sub(r"_+", "_", slug_label)
                    if slug_label:
                        variable_forms[slug_label] = form_name

    return {
        "lists": lists,
        "variable_lists": variable_lists,
        "candidates": candidates,
        "coded_columns": sorted(coded_columns),
        "warnings": list(dict.fromkeys(warnings)),
        "source_columns": source_columns,
        "variable_forms": variable_forms,
    }


def codebook_labels(codebook, column):
    if not codebook:
        return {}
    list_name = codebook.get("variable_lists", {}).get(column)
    return codebook.get("lists", {}).get(list_name, {}) if list_name else {}


def extract_row_pids(frame, id_columns=None):
    if id_columns and len(id_columns) > 1:
        valid_cols = [c for c in id_columns if c in frame.columns]
        if valid_cols:
            pids = []
            for idx, row in frame.iterrows():
                parts = [
                    str(row.get(col)).strip()
                    for col in valid_cols
                    if not is_missing(row.get(col))
                    and str(row.get(col)).strip()
                    and str(row.get(col)).strip().lower() != "nan"
                ]
                pids.append(" / ".join(parts) if parts else f"Row {idx + 1}")
            return pd.Series(pids, index=frame.index)

    pid_cols = (
        [c for c in id_columns if c in frame.columns]
        if (id_columns and len(id_columns) == 1)
        else detect_pid_columns(frame.columns)
    )
    pids = []
    for idx, row in frame.iterrows():
        pid_val = None
        for col in pid_cols:
            val = row.get(col)
            if val is not None and str(val).strip() and str(val).strip().lower() != "nan":
                pid_val = str(val).strip()
                break
        pids.append(pid_val if pid_val else f"Row {idx + 1}")
    return pd.Series(pids, index=frame.index)




def _build_date_chart_from_datetimes(column, date_values, row_pids, description=""):
    """Build a date chart dict from an already-parsed datetime64 Series.

    *date_values* must be a Series of Timestamps (no NaTs).
    *row_pids* must be index-aligned with *date_values*.
    Returns the same chart dict shape as the string-date path in _build_text_chart.
    """
    col_low = column.lower().replace(" ", "_").replace("-", "_")
    is_dob_col = any(
        kw in col_low
        for kw in (
            "dob",
            "birth_date",
            "date_of_birth",
            "dateofbirth",
            "birth_year",
            "year_of_birth",
        )
    )
    if is_dob_col:
        year_series = date_values.dt.year.astype(str)
        frequencies = year_series.value_counts().sort_index()
        labels = frequencies.index.tolist()
        pids_per_period = []
        for yr in labels:
            yr_indices = year_series[year_series == yr].index
            pids_per_period.append(row_pids.loc[yr_indices].tolist())
        summary_str = f"{len(date_values):,} birth records · {len(frequencies):,} birth years"
    else:
        periods = date_values.dt.to_period("M").astype(str)
        frequencies = periods.value_counts().sort_index()
        if len(frequencies) > 120:
            frequencies = frequencies.tail(120)
        labels = frequencies.index.tolist()
        pids_per_period = []
        for period in labels:
            period_indices = periods[periods == period].index
            pids_per_period.append(row_pids.loc[period_indices].tolist())
        summary_str = (
            f"{len(date_values):,} dated records · "
            f"{len(frequencies):,} months"
        )
    return {
        "name": column,
        "description": description,
        "kind": "date",
        "variants": [
            {
                "type": "line",
                "label": "Trend line",
                "labels": labels,
                "labels_en": labels,
                "labels_ny": labels,
                "values": [int(v) for v in frequencies.tolist()],
                "pids": pids_per_period,
            }
        ],
        "summary": summary_str,
    }


def _build_text_chart(column, values, row_pids, description=""):
    # --- Strip blank / sentinel values before any chart processing ---
    # This ensures chart counts reflect only records with real data,
    # not NaN→"nan" artefacts from astype(str).
    _blank_sentinels = {"nan", "none", "null", "", "na", "n/a"}
    non_blank_mask = ~values.astype(str).str.strip().str.lower().isin(_blank_sentinels)
    values = values[non_blank_mask]
    row_pids = row_pids.loc[values.index] if hasattr(row_pids, "loc") else row_pids
    if values.empty:
        return None

    # Fast path: column already holds datetime64 objects (e.g. from Excel)
    if pd.api.types.is_datetime64_any_dtype(values):
        date_values = values.dropna()
        if not date_values.empty:
            return _build_date_chart_from_datetimes(
                column, date_values, row_pids.loc[date_values.index], description
            )

    raw_values = values.astype(str)
    # Detect dates in ISO (YYYY-MM-DD), European (DD/MM/YYYY), and
    # month-abbreviation (DD-Mon-YY / DD-Mon-YYYY) formats.
    is_iso_date = raw_values.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}").mean() >= 0.5
    is_dmy_date = raw_values.str.match(r"^\d{1,2}[/-]\d{1,2}[/-]\d{4}").mean() >= 0.5
    _month_abbr = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    is_mon_date = raw_values.str.match(
        rf"^\d{{1,2}}[-/\s]{_month_abbr}[-/\s]\d{{2,4}}$",
        case=False,
    ).mean() >= 0.5
    looks_like_date = is_iso_date or is_dmy_date or is_mon_date
    if looks_like_date:
        if is_mon_date and not is_iso_date and not is_dmy_date:
            # Try DD-Mon-YY first (2-digit year), then DD-Mon-YYYY
            try:
                parsed_dates = pd.to_datetime(raw_values, format="%d-%b-%y", errors="coerce")
                if parsed_dates.notna().mean() < 0.5:
                    parsed_dates = pd.to_datetime(raw_values, format="%d-%b-%Y", errors="coerce")
                if parsed_dates.notna().mean() < 0.5:
                    # Try with space/slash separators as fallback
                    parsed_dates = pd.to_datetime(raw_values, errors="coerce")
            except Exception:
                parsed_dates = pd.to_datetime(raw_values, errors="coerce")
        else:
            use_dayfirst = bool(is_dmy_date and not is_iso_date)
            try:
                parsed_dates = pd.to_datetime(
                    raw_values, errors="coerce", format="mixed", dayfirst=use_dayfirst
                )
            except Exception:
                try:
                    parsed_dates = pd.to_datetime(
                        raw_values, errors="coerce", dayfirst=use_dayfirst
                    )
                except Exception:
                    parsed_dates = pd.to_datetime(raw_values, errors="coerce")
    else:
        parsed_dates = pd.Series(dtype="datetime64[ns]")
    if looks_like_date and parsed_dates.notna().sum() >= len(raw_values) * 0.5:
        date_values = parsed_dates.dropna()
        return _build_date_chart_from_datetimes(
            column, date_values, row_pids.loc[date_values.index], description
        )

    frequencies = raw_values.value_counts()
    pids_per_cat = []
    for cat in frequencies.index:
        cat_indices = raw_values[raw_values == cat].index
        pids_per_cat.append(row_pids.loc[cat_indices].tolist())

    variants = [
        categorical_chart_variant(
            frequencies.index.tolist(),
            frequencies.index.tolist(),
            frequencies.index.tolist(),
            frequencies.tolist(),
            category_count=raw_values.nunique(),
            pids=pids_per_cat,
        )
    ]
    return {
        "name": column,
        "description": description,
        "kind": "categories",
        "coded": False,
        "variants": variants,
        "summary": (
            f"{values.nunique():,} unique values · showing first 10 of {len(frequencies):,}"
            if len(frequencies) > 10
            else f"{values.nunique():,} unique values · {len(frequencies):,} categories"
        ),
    }


def build_single_column_chart(
    column,
    frame,
    variable_lists,
    response_lists,
    coded_columns,
    description="",
    row_pids=None,
):
    if row_pids is None:
        row_pids = extract_row_pids(frame)
    values = frame[column].dropna()
    # Strip string sentinel blanks that may survive after astype/csv round-trip
    _blank_sentinels = {"nan", "none", "null", "", "na", "n/a"}
    _str_mask = values.astype(str).str.strip().str.lower().isin(_blank_sentinels)
    values = values[~_str_mask]
    if values.empty:
        return None
    list_name = variable_lists.get(column)
    value_labels = response_lists.get(list_name, {})
    if column in coded_columns and not list_name:
        return {
            "name": column,
            "description": description,
            "kind": "unmapped",
            "summary": "Choose the matching response list to display labels.",
            "variants": [],
        }
    if value_labels:
        codes = values.map(normalize_code)
        frequencies = codes.value_counts()
        category_codes = frequencies.index.tolist()
        ordered_english = [
            value_labels.get(code, {}).get("en")
            or value_labels.get(code, {}).get("ny")
            or str(code)
            for code in category_codes
        ]
        ordered_chichewa = [
            value_labels.get(code, {}).get("ny")
            or value_labels.get(code, {}).get("en")
            or str(code)
            for code in category_codes
        ]
        pids_per_category = []
        for code in category_codes:
            cat_indices = codes[codes == code].index
            pids_per_category.append(row_pids.loc[cat_indices].tolist())

        variants = [
            categorical_chart_variant(
                ordered_english,
                ordered_english,
                ordered_chichewa,
                frequencies.tolist(),
                category_count=values.nunique(),
                codes=category_codes,
                pids=pids_per_category,
            )
        ]
        return {
            "name": column,
            "description": description,
            "kind": "categories",
            "coded": True,
            "response_list": list_name,
            "variants": variants,
            "summary": (
                f"{len(values):,} responses · showing first 10 of {len(frequencies):,} categories · response list: {list_name}"
                if len(frequencies) > 10
                else f"{len(values):,} responses · {len(frequencies):,} response categories · response list: {list_name}"
            ),
        }
    elif pd.api.types.is_numeric_dtype(values):
        # --- Part B: Excel serial date interception ---
        # If the column name hints at a date AND the values look like Excel
        # serials (pre-existing data not caught by Part A upload-time coercion),
        # convert and render as a date chart instead of numeric/histogram.
        col_lower_b = column.lower().replace(" ", "_").replace("-", "_")
        if any(kw in col_lower_b for kw in _EXCEL_SERIAL_DATE_HINTS):
            if _looks_like_excel_serials(values):
                nums = pd.to_numeric(values, errors="coerce").dropna()
                in_range = nums.between(_EXCEL_SERIAL_MIN, _EXCEL_SERIAL_MAX)
                if in_range.mean() >= 0.5:
                    try:
                        date_values = pd.to_datetime(
                            nums[in_range], unit="D", origin="1899-12-30", errors="coerce"
                        ).dropna()
                        if not date_values.empty:
                            return _build_date_chart_from_datetimes(
                                column,
                                date_values,
                                row_pids.loc[date_values.index],
                                description,
                            )
                    except Exception:
                        pass  # fall through to normal numeric handling
        # --- End serial interception ---
        numeric = pd.to_numeric(values, errors="coerce")
        non_numeric_count = int(numeric.isna().sum()) - int(values.isna().sum())
        numeric = numeric.dropna()
        if non_numeric_count > max(1, int(0.05 * max(1, len(values)))):
            return _build_text_chart(column, values, row_pids, description)
        if numeric.empty:
            return None
        col_lower = column.lower().replace(" ", "_").replace("-", "_")
        is_measurement_name = any(
            m in col_lower for m in ("age", "weight", "height", "bp", "score", "temp")
        )
        # Discrete whole numbers (e.g. sex/gender 1/2, visit 1/2/3, outcome 0/1)
        is_discrete = (
            column not in coded_columns
            and numeric.nunique() <= 15
            and (numeric % 1 == 0).all()
            and not (is_measurement_name and numeric.min() >= 15)
        )
        if is_discrete:
            # Map whole numbers cleanly: 1.0 -> '1', 2.0 -> '2'
            clean_str = numeric.map(lambda v: str(int(v)) if v == int(v) else str(v))
            frequencies = clean_str.value_counts().sort_index()
            pids_per_cat = []
            for cat in frequencies.index:
                cat_indices = clean_str[clean_str == cat].index
                pids_per_cat.append(row_pids.loc[cat_indices].tolist())
            variants = [
                categorical_chart_variant(
                    frequencies.index.tolist(),
                    frequencies.index.tolist(),
                    frequencies.index.tolist(),
                    frequencies.tolist(),
                    category_count=clean_str.nunique(),
                    pids=pids_per_cat,
                )
            ]
            return {
                "name": column,
                "description": description,
                "kind": "categories",
                "coded": False,
                "variants": variants,
                "summary": (
                    f"{clean_str.nunique():,} unique values · {len(frequencies):,} categories"
                ),
            }
        bin_count = min(8, max(1, numeric.nunique()))
        counts, edges = pd.cut(numeric, bins=bin_count, retbins=True)
        histogram = counts.value_counts(sort=False)
        labels = [
            f"{edges[index]:g}–{edges[index + 1]:g}"
            for index in range(len(edges) - 1)
        ]
        pids_per_bin = []
        for cat in counts.cat.categories:
            bin_indices = counts[counts == cat].index
            pids_per_bin.append(row_pids.loc[bin_indices].tolist())

        return {
            "name": column,
            "description": description,
            "kind": "numeric",
            "variants": [
                {
                    "type": "histogram",
                    "label": "Histogram",
                    "labels": labels,
                    "labels_en": labels,
                    "labels_ny": labels,
                    "values": [int(value) for value in histogram.tolist()],
                    "pids": pids_per_bin,
                }
            ],
            "summary": (
                f"{len(numeric):,} values · Average {numeric.mean():,.2f} · "
                f"Median {numeric.median():,.2f}"
            ),
        }
    else:
        return _build_text_chart(column, values, row_pids, description)


def _form_for_column(col, variable_forms):
    if not variable_forms:
        return None
    # Exact match first
    if col in variable_forms:
        return variable_forms[col]
    # Slug match (sanitize_headers_for_upload produces these)
    slug = re.sub(r"[^a-z0-9]+", "_", str(col).lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if slug in variable_forms:
        return variable_forms[slug]
    # Fallback for multi-response variants (symptoms_gen___1 → symptoms_gen)
    stripped = re.sub(r"(_{2,}\d+|_\d+)$", "", str(col))
    if stripped in variable_forms:
        return variable_forms[stripped]
    return None


def dashboard_data(dataset, codebook=None):
    codebook = codebook or {}
    has_codebook = bool(
        codebook
        and (
            codebook.get("variable_lists")
            or codebook.get("lists")
            or codebook.get("coded_columns")
            or codebook.get("variable_forms")
        )
    )
    frame = pd.DataFrame(dataset.records, columns=dataset.column_names)
    variable_lists = codebook.get("variable_lists", {})
    response_lists = codebook.get("lists", {})
    candidates = codebook.get("candidates", {})
    coded_columns = set(codebook.get("coded_columns", []))
    id_columns = get_dataset_id_columns(dataset)
    row_pids = extract_row_pids(frame, id_columns)
    charts = []

    if not has_codebook:
        # WITHOUT CODEBOOK: Use pattern-based grouping (group_question_columns)
        # so that multi-response checkbox columns (e.g. q5_1, q5_2 … q5_n) are
        # grouped into a single bar chart just like they are when a codebook is
        # present.  Labels are derived from the sub-column name suffix rather
        # than from a codebook response list.
        pid_cols = set(detect_pid_columns(frame.columns))
        original_headers = getattr(dataset, "original_headers", None) or {}
        question_groups = group_question_columns(frame)
        app.logger.info(
            "Detected dashboard question groups (no-codebook) for study %s: %s",
            dataset.id,
            list(question_groups.keys())[:20],
        )
        for question_name, question_columns in question_groups.items():
            # Skip PID columns and fully-empty groups
            question_columns = [
                c for c in question_columns
                if c not in pid_cols
                and c in frame.columns
                and not frame[c].dropna().empty
            ]
            if not question_columns:
                continue

            if len(question_columns) > 1:
                # --- Grouped multi-response chart (no codebook labels) ---
                # Derive a readable label for each sub-column.
                # Prefer: original header → suffix after stem → full column name.
                def _label_for_subcol(col, stem=question_name):
                    if col in original_headers:
                        return original_headers[col]
                    _, code, _ = extract_option_match(col)
                    if code is not None:
                        return f"Option {code}"
                    # strip the stem prefix to get just the option part
                    suffix = col[len(stem):].lstrip("_/.- ")
                    return suffix if suffix else col

                english_labels = [_label_for_subcol(c) for c in question_columns]
                chichewa_labels = english_labels  # no translation without codebook

                selected = pd.DataFrame(
                    {
                        column: frame[column].map(multiselect_selection)
                        for column in question_columns
                    }
                )
                counts = selected.sum(axis=0).astype(int)
                total_selections = int(counts.sum())
                participants_with_selections = int(selected.any(axis=1).sum())
                pids_per_choice = [
                    row_pids.loc[selected[col][selected[col]].index].tolist()
                    for col in question_columns
                ]
                display_name = original_headers.get(question_name, question_name)
                charts.append(
                    {
                        "name": display_name,
                        "key": question_name,
                        "description": "",
                        "kind": "categories",
                        "coded": False,
                        "response_list": None,
                        "is_grouped": True,
                        "is_subcolumn": False,
                        "is_base_column": False,
                        "group_columns": question_columns,
                        "variants": [
                            {
                                "type": "bar",
                                "label": "Bar chart",
                                "labels": english_labels,
                                "labels_en": english_labels,
                                "labels_ny": chichewa_labels,
                                "values": [int(v) for v in counts.tolist()],
                                "pids": pids_per_choice,
                            }
                        ],
                        "summary": (
                            f"{participants_with_selections:,} participants selected "
                            f"{total_selections:,} options across "
                            f"{len(question_columns):,} choices"
                        ),
                    }
                )

                # Individual sub-column donut charts
                for sub_col, en_label in zip(question_columns, english_labels):
                    sub_series = frame[sub_col].map(multiselect_selection)
                    selected_cnt = int(sub_series.sum())
                    unselected_cnt = int((~sub_series).sum())
                    pct = (
                        (selected_cnt / len(sub_series) * 100)
                        if len(sub_series) > 0
                        else 0
                    )
                    selected_pids = row_pids.loc[sub_series[sub_series].index].tolist()
                    unselected_pids = row_pids.loc[sub_series[~sub_series].index].tolist()
                    charts.append(
                        {
                            "name": sub_col,
                            "description": f"Choice: {en_label}",
                            "kind": "categories",
                            "coded": False,
                            "response_list": None,
                            "is_grouped": False,
                            "is_subcolumn": True,
                            "is_base_column": False,
                            "parent_group": display_name,
                            "variants": [
                                {
                                    "type": "donut",
                                    "label": "Donut chart",
                                    "labels": [
                                        f"Selected ({selected_cnt:,})",
                                        f"Not selected ({unselected_cnt:,})",
                                    ],
                                    "labels_en": [
                                        f"Selected ({selected_cnt:,})",
                                        f"Not selected ({unselected_cnt:,})",
                                    ],
                                    "labels_ny": [
                                        f"Zosankhidwa ({selected_cnt:,})",
                                        f"Zosasankhidwa ({unselected_cnt:,})",
                                    ],
                                    "values": [selected_cnt, unselected_cnt],
                                    "pids": [selected_pids, unselected_pids],
                                }
                            ],
                            "summary": (
                                f"{selected_cnt:,} of {len(sub_series):,} participants "
                                f"selected ({pct:.1f}%) · {en_label}"
                            ),
                        }
                    )

                # Base column chart if the stem itself is also a column
                if question_name in frame.columns and question_name not in pid_cols:
                    base_chart = build_single_column_chart(
                        question_name,
                        frame,
                        variable_lists={},
                        response_lists={},
                        coded_columns=set(),
                        description="Raw question column",
                        row_pids=row_pids,
                    )
                    if base_chart:
                        base_chart["is_grouped"] = False
                        base_chart["is_subcolumn"] = False
                        base_chart["is_base_column"] = True
                        base_chart["parent_group"] = display_name
                        charts.append(base_chart)
                continue

            # --- Single column (no group) ---
            column = question_columns[0]
            chart = build_single_column_chart(
                column,
                frame,
                variable_lists={},
                response_lists={},
                coded_columns=set(),
                row_pids=row_pids,
            )
            if chart:
                chart["is_grouped"] = False
                chart["is_subcolumn"] = False
                chart["is_base_column"] = False
                display_name = original_headers.get(column, column)
                chart["key"] = column
                chart["name"] = display_name
                charts.append(chart)
    else:
        question_groups = group_question_columns(frame)
        app.logger.info(
            "Detected dashboard question groups for study %s: %s",
            dataset.id,
            question_groups,
        )
        for question_name, question_columns in question_groups.items():
            if len(question_columns) > 1:
                selected = pd.DataFrame(
                    {
                        column: frame[column].map(multiselect_selection)
                        for column in question_columns
                    }
                )
                counts = selected.sum(axis=0).astype(int)
                english_labels, chichewa_labels, list_name = multiselect_labels(
                    question_name, question_columns, codebook
                )
                total_selections = int(counts.sum())
                participants_with_selections = int(selected.any(axis=1).sum())
                # Compute PIDs per choice for grouped bar chart
                pids_per_choice = [
                    row_pids.loc[selected[col][selected[col]].index].tolist()
                    for col in question_columns
                ]
                charts.append(
                    {
                        "name": question_name,
                        "description": "",
                        "kind": "categories",
                        "coded": bool(list_name),
                        "response_list": list_name,
                        "is_grouped": True,
                        "is_subcolumn": False,
                        "is_base_column": False,
                        "group_columns": question_columns,
                        "variants": [
                            {
                                "type": "bar",
                                "label": "Bar chart",
                                "labels": english_labels,
                                "labels_en": english_labels,
                                "labels_ny": chichewa_labels,
                                "values": [int(value) for value in counts.tolist()],
                                "pids": pids_per_choice,
                            }
                        ],
                        "summary": (
                            f"{participants_with_selections:,} participants selected "
                            f"{total_selections:,} options across "
                            f"{len(question_columns):,} choices"
                        ),
                    }
                )

                # Sub-column charts for individual drilldown
                for sub_col, en_label, ny_label in zip(
                    question_columns, english_labels, chichewa_labels
                ):
                    if sub_col not in frame.columns:
                        continue
                    sub_series = frame[sub_col].map(multiselect_selection)
                    selected_cnt = int(sub_series.sum())
                    unselected_cnt = int((~sub_series).sum())
                    pct = (
                        (selected_cnt / len(sub_series) * 100)
                        if len(sub_series) > 0
                        else 0
                    )
                    selected_pids = row_pids.loc[sub_series[sub_series].index].tolist()
                    unselected_pids = row_pids.loc[sub_series[~sub_series].index].tolist()
                    charts.append(
                        {
                            "name": sub_col,
                            "description": f"Choice: {en_label}",
                            "kind": "categories",
                            "coded": bool(list_name),
                            "response_list": list_name,
                            "is_grouped": False,
                            "is_subcolumn": True,
                            "is_base_column": False,
                            "parent_group": question_name,
                            "variants": [
                                {
                                    "type": "donut",
                                    "label": "Donut chart",
                                    "labels": [
                                        f"Selected ({selected_cnt:,})",
                                        f"Not selected ({unselected_cnt:,})",
                                    ],
                                    "labels_en": [
                                        f"Selected ({selected_cnt:,})",
                                        f"Not selected ({unselected_cnt:,})",
                                    ],
                                    "labels_ny": [
                                        f"Zosankhidwa ({selected_cnt:,})",
                                        f"Zosasankhidwa ({unselected_cnt:,})",
                                    ],
                                    "values": [selected_cnt, unselected_cnt],
                                    "pids": [selected_pids, unselected_pids],
                                }
                            ],
                            "summary": (
                                f"{selected_cnt:,} of {len(sub_series):,} participants "
                                f"selected ({pct:.1f}%) · {en_label}"
                            ),
                        }
                    )

                # Base column individual chart if present
                if question_name in frame.columns:
                    base_chart = build_single_column_chart(
                        question_name,
                        frame,
                        variable_lists,
                        response_lists,
                        coded_columns,
                        description="Raw question column",
                        row_pids=row_pids,
                    )
                    if base_chart:
                        base_chart["is_grouped"] = False
                        base_chart["is_subcolumn"] = False
                        base_chart["is_base_column"] = True
                        base_chart["parent_group"] = question_name
                        charts.append(base_chart)
                continue

            column = question_columns[0]
            chart = build_single_column_chart(
                column, frame, variable_lists, response_lists, coded_columns,
                row_pids=row_pids,
            )
            if chart:
                chart["is_grouped"] = False
                chart["is_subcolumn"] = False
                chart["is_base_column"] = False
                charts.append(chart)

    variable_forms = codebook.get("variable_forms", {}) if codebook else {}
    for chart in charts:
        if not chart.get("form"):
            form = _form_for_column(chart.get("name", ""), variable_forms)
            if not form and chart.get("key"):
                form = _form_for_column(chart.get("key", ""), variable_forms)
            if not form and chart.get("group_columns"):
                form = _form_for_column(chart["group_columns"][0], variable_forms)
            if not form and chart.get("parent_group"):
                form = _form_for_column(chart.get("parent_group", ""), variable_forms)
            chart["form"] = form

    preview = [
        {key: (None if pd.isna(value) else value) for key, value in row.items()}
        for row in dataset.records[:PREVIEW_ROW_LIMIT]
    ]
    all_dataset_pids = row_pids.tolist()
    return charts, preview, all_dataset_pids


def group_related_charts(charts, chart_indices=None):
    list_counts = {}
    for chart in charts:
        if chart.get("is_subcolumn") or chart.get("is_base_column"):
            continue
        list_name = chart.get("response_list")
        if list_name:
            list_counts[list_name] = list_counts.get(list_name, 0) + 1

    sections = []
    grouped_sections = {}
    subcharts_by_parent = {}
    for local_index, chart in enumerate(charts):
        idx = chart_indices[local_index] if chart_indices is not None else local_index
        parent = chart.get("parent_group")
        if parent:
            subcharts_by_parent.setdefault(parent, []).append(
                {"chart": chart, "index": idx}
            )

    for local_index, chart in enumerate(charts):
        idx = chart_indices[local_index] if chart_indices is not None else local_index
        if chart.get("is_subcolumn") or chart.get("is_base_column"):
            continue
        list_name = chart.get("response_list")
        child_entries = [{"chart": chart, "index": idx}] + subcharts_by_parent.get(
            chart["name"], []
        )

        if list_name and list_counts.get(list_name, 0) > 1:
            if list_name not in grouped_sections:
                section = {
                    "title": f"Response list: {list_name}",
                    "related": True,
                    "charts": [],
                }
                grouped_sections[list_name] = section
                sections.append(section)
            grouped_sections[list_name]["charts"].extend(child_entries)
        else:
            sections.append(
                {
                    "title": chart["name"],
                    "related": False,
                    "charts": child_entries,
                }
            )
    return sections


def build_report(dataset):
    output = io.BytesIO()
    cleaned_data = pd.DataFrame(dataset.records, columns=dataset.column_names)
    codebook = db.session.get(StudyCodebook, dataset.id)
    summary_rows = [
        ("Study", dataset.study_name),
        ("Source file", dataset.filename),
        ("Uploaded", dataset.uploaded_at.strftime("%Y-%m-%d %H:%M UTC")),
    ]
    if getattr(dataset, "expected_participants", None):
        summary_rows.append(("Total participants expected", dataset.expected_participants))
    if getattr(dataset, "clean_columns", None):
        summary_rows.append(("Duplicate identification columns", ", ".join(dataset.clean_columns)))
    summary_rows.extend([
        ("Rows in cleaned output", dataset.rows),
        ("Columns in cleaned output", dataset.columns),
        ("Duplicate rows removed automatically", dataset.duplicates_removed),
        ("Duplicate PID groups reviewed", "See dashboard duplicate review"),
        ("Missing values filled", dataset.missing_values_filled),
    ])
    if codebook:
        summary_rows.extend(
            [
                ("Codebook file", codebook.filename),
                (
                    "Unresolved coded variables",
                    ", ".join(codebook.unmatched_variables) or "None",
                ),
                (
                    "Codebook review warnings",
                    " | ".join(codebook.variables.get("warnings", [])) or "None",
                ),
            ]
        )
    summary = pd.DataFrame(summary_rows, columns=["Cleaning report", "Result"])
    steps = pd.DataFrame(
        [
            (
                "Empty rows and columns",
                "Removed rows and columns that contained no values.",
            ),
            (
                "Duplicate rows",
                "No rows were removed automatically. Review matching PIDs on the dashboard.",
            ),
            ("Missing values", (
                "Filled continuous numeric columns with their median. Filled text "
                "and codebook-coded categorical columns with their most frequent "
                "value."
            )),
            ("Output", "Cleaned data is included in the 'Cleaned data' worksheet."),
        ],
        columns=["Cleaning step", "Action"],
    )
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", dataset.study_name).strip("-")
    download_name = f"{safe_name or 'study'}-cleaning-report-{dataset.id}.xlsx"
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Cleaning report", index=False)
        steps.to_excel(
            writer, sheet_name="Cleaning report", index=False, startrow=len(summary) + 2
        )
        cleaned_data.to_excel(writer, sheet_name="Cleaned data", index=False)
        if codebook:
            codebook_rows = []
            variable_lists = codebook.variables.get("variable_lists", {})
            for list_name, choices in codebook.variables.get("lists", {}).items():
                mapped_variables = [
                    column
                    for column, selected_list in variable_lists.items()
                    if selected_list == list_name
                ]
                for value, labels in choices.items():
                    codebook_rows.append(
                        (
                            list_name,
                            value,
                            labels.get("en", ""),
                            labels.get("ny", ""),
                            ", ".join(mapped_variables),
                        )
                    )
            pd.DataFrame(
                codebook_rows,
                columns=[
                    "list_name",
                    "name",
                    "label::English",
                    "label::Chichewa",
                    "matched dataset variables",
                ],
            ).to_excel(writer, sheet_name="Codebook", index=False)
            pd.DataFrame(
                [
                    (column, list_name)
                    for column, list_name in variable_lists.items()
                ],
                columns=["Dataset variable", "Response list"],
            ).to_excel(writer, sheet_name="Response list mappings", index=False)
    output.seek(0)
    return output, download_name


def build_cleaned_data_workbook(dataset):
    output = io.BytesIO()
    cleaned_data = pd.DataFrame(dataset.records, columns=dataset.column_names)
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", dataset.study_name).strip("-")
    download_name = f"{safe_name or 'study'}-cleaned-data-{dataset.id}.xlsx"
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        cleaned_data.to_excel(writer, sheet_name="Cleaned data", index=False)
    output.seek(0)
    return output, download_name


def build_cleaning_report_only_workbook(dataset):
    output = io.BytesIO()
    codebook = db.session.get(StudyCodebook, dataset.id)
    summary_rows = [
        ("Study", dataset.study_name),
        ("Source file", dataset.filename),
        ("Uploaded", dataset.uploaded_at.strftime("%Y-%m-%d %H:%M UTC")),
    ]
    if getattr(dataset, "expected_participants", None):
        summary_rows.append(("Total participants expected", dataset.expected_participants))
    if getattr(dataset, "clean_columns", None):
        summary_rows.append(("Duplicate identification columns", ", ".join(dataset.clean_columns)))
    summary_rows.extend([
        ("Rows in cleaned output", dataset.rows),
        ("Columns in cleaned output", dataset.columns),
        ("Duplicate rows removed automatically", dataset.duplicates_removed),
        ("Duplicate PID groups reviewed", "See dashboard duplicate review"),
        ("Missing values filled", dataset.missing_values_filled),
    ])
    if codebook:
        summary_rows.extend(
            [
                ("Codebook file", codebook.filename),
                (
                    "Unresolved coded variables",
                    ", ".join(codebook.unmatched_variables) or "None",
                ),
                (
                    "Codebook review warnings",
                    " | ".join(codebook.variables.get("warnings", [])) or "None",
                ),
            ]
        )
    summary = pd.DataFrame(summary_rows, columns=["Cleaning report", "Result"])
    steps = pd.DataFrame(
        [
            (
                "Empty rows and columns",
                "Removed rows and columns that contained no values.",
            ),
            (
                "Duplicate rows",
                "No rows were removed automatically. Review matching PIDs on the dashboard.",
            ),
            ("Missing values", (
                "Filled continuous numeric columns with their median. Filled text "
                "and codebook-coded categorical columns with their most frequent "
                "value."
            )),
            ("Output", "Cleaned data is available in the separate Cleaned Data download."),
        ],
        columns=["Cleaning step", "Action"],
    )
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", dataset.study_name).strip("-")
    download_name = f"{safe_name or 'study'}-cleaning-report-summary-{dataset.id}.xlsx"
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Cleaning report", index=False)
        steps.to_excel(
            writer, sheet_name="Cleaning report", index=False, startrow=len(summary) + 2
        )
        if codebook:
            codebook_rows = []
            variable_lists = codebook.variables.get("variable_lists", {})
            for list_name, choices in codebook.variables.get("lists", {}).items():
                mapped_variables = [
                    column
                    for column, selected_list in variable_lists.items()
                    if selected_list == list_name
                ]
                for value, labels in choices.items():
                    codebook_rows.append(
                        (
                            list_name,
                            value,
                            labels.get("en", ""),
                            labels.get("ny", ""),
                            ", ".join(mapped_variables),
                        )
                    )
            pd.DataFrame(
                codebook_rows,
                columns=[
                    "list_name",
                    "name",
                    "label::English",
                    "label::Chichewa",
                    "matched dataset variables",
                ],
            ).to_excel(writer, sheet_name="Codebook", index=False)
            pd.DataFrame(
                [
                    (column, list_name)
                    for column, list_name in variable_lists.items()
                ],
                columns=["Dataset variable", "Response list"],
            ).to_excel(writer, sheet_name="Response list mappings", index=False)
    output.seek(0)
    return output, download_name


def get_or_seed_tracking(dataset_id, site):
    site_name = "Salima" if str(site).lower() == "salima" else "Lilongwe"
    records = (
        ParticipantTracking.query.filter_by(dataset_id=dataset_id, site=site_name)
        .order_by(ParticipantTracking.pid)
        .all()
    )
    if not records:
        try:
            import tracking_data
            if site_name == "Lilongwe":
                seed = tracking_data.get_default_lilongwe_tracking()
            else:
                seed = tracking_data.get_default_salima_tracking()
            new_objs = [
                ParticipantTracking(
                    dataset_id=dataset_id,
                    site=site_name,
                    pid=r["pid"],
                    questionnaire_date=r.get("questionnaire_date"),
                    dental_date=r.get("dental_date"),
                    endo_status=r.get("endo_status"),
                    endo_date=r.get("endo_date"),
                    comment=r.get("comment"),
                )
                for r in seed
            ]
            db.session.bulk_save_objects(new_objs)
            db.session.commit()
            records = (
                ParticipantTracking.query.filter_by(
                    dataset_id=dataset_id, site=site_name
                )
                .order_by(ParticipantTracking.pid)
                .all()
            )
        except Exception:
            db.session.rollback()
            app.logger.exception("Could not auto-seed tracking records")
    return records


def compile_tracking_summary(records, observed_pids_set):
    total = len(records)
    q_done = sum(
        1 for r in records if r.questionnaire_date and str(r.questionnaire_date).strip()
    )
    d_done = sum(1 for r in records if r.dental_date and str(r.dental_date).strip())
    endo_yes = sum(1 for r in records if (r.endo_status or "").upper() == "YES")
    endo_no = sum(1 for r in records if (r.endo_status or "").upper() == "NO")
    endo_pending = total - endo_yes - endo_no

    reasons_map = {}
    for r in records:
        comm = (r.comment or "").strip()
        if comm:
            reasons_map[comm] = reasons_map.get(comm, 0) + 1
        elif (r.endo_status or "").upper() == "NO":
            reasons_map["Not Reported / Unspecified"] = (
                reasons_map.get("Not Reported / Unspecified", 0) + 1
            )

    reasons_sorted = sorted(
        [{"reason": k, "count": v} for k, v in reasons_map.items()],
        key=lambda x: -x["count"],
    )

    rows = []
    for r in records:
        clean_pid = str(r.pid).strip()
        norm_pid = re.sub(r"[^A-Za-z0-9]", "", clean_pid).casefold()
        in_dataset = (clean_pid in observed_pids_set) or (
            norm_pid in observed_pids_set
        )
        rows.append(
            {
                "id": r.id,
                "pid": r.pid,
                "questionnaire_date": r.questionnaire_date or "—",
                "dental_date": r.dental_date or "—",
                "endo_status": (r.endo_status or "").upper()
                if r.endo_status
                else "PENDING",
                "endo_date": r.endo_date or "—",
                "comment": r.comment or "",
                "in_dataset": in_dataset,
            }
        )

    return {
        "total": total,
        "questionnaire_count": q_done,
        "questionnaire_pct": round(q_done / total * 100, 1) if total else 0,
        "dental_count": d_done,
        "dental_pct": round(d_done / total * 100, 1) if total else 0,
        "endo_yes_count": endo_yes,
        "endo_yes_pct": round(endo_yes / total * 100, 1) if total else 0,
        "endo_no_count": endo_no,
        "endo_no_pct": round(endo_no / total * 100, 1) if total else 0,
        "endo_pending_count": endo_pending,
        "endo_pending_pct": round(endo_pending / total * 100, 1) if total else 0,
        "reasons": reasons_sorted,
        "rows": rows,
    }


@app.route("/", methods=["GET"])
def index():
    datasets = StudyDataset.query.order_by(desc(StudyDataset.uploaded_at)).all()
    return render_template("index.html", datasets=datasets)


@app.route("/upload", methods=["POST"])
def upload():
    study_name = request.form.get("study_name", "").strip()
    file = request.files.get("file")
    if not study_name:
        flash("Enter a study name before uploading.", "error")
        return redirect(url_for("index"))
    if len(study_name) > 160:
        flash("Study names must be 160 characters or fewer.", "error")
        return redirect(url_for("index"))
    if file is None or not file.filename:
        flash("Choose a CSV or Excel file to upload.", "error")
        return redirect(url_for("index"))

    try:
        raw = read_upload(file)
        raw, header_map = sanitize_headers_for_upload(raw)
        if header_map:
            print(f"[upload] sanitized {len(header_map)} headers for labeled export")

        raw_expected = request.form.get("expected_participants", "").strip()
        expected_participants = None
        if raw_expected:
            try:
                exp_val = int(raw_expected)
                if exp_val > 0:
                    expected_participants = exp_val
            except ValueError:
                expected_participants = None

        clean_mode = request.form.get("clean_mode", "pid").strip().lower()
        if clean_mode not in {"pid", "first_three", "custom"}:
            clean_mode = "pid"

        codebook_file = request.files.get("codebook")
        dictionary_file = request.files.get("dictionary")
        codebook_data = None
        categorical_columns = set()
        variables = None

        question_groups = group_question_columns(raw)
        preserve_missing_columns = {
            column
            for columns in question_groups.values()
            if len(columns) > 1
            for column in columns
        }
        preserve_missing_columns.update(detect_pid_columns(raw.columns))
        if clean_mode == "first_three" and len(raw.columns) >= 3:
            preserve_missing_columns.update(raw.columns[:3])
        elif clean_mode == "custom":
            custom_selected = request.form.getlist("custom_columns")
            preserve_missing_columns.update(custom_selected)

        # ---------- Parse reference files (codebook and/or dictionary) ----------
        raw.columns = make_unique_headers(raw.columns)
        codebook_parsed = None
        dictionary_parsed = None

        if codebook_file is not None and codebook_file.filename:
            codebook_parsed = parse_codebook(codebook_file, raw)

        if dictionary_file is not None and dictionary_file.filename:
            dictionary_parsed = parse_codebook(dictionary_file, raw)

        if codebook_parsed or dictionary_parsed:
            # Merge precedence:
            #   - labels (lists, variable_lists, coded_columns): CODEBOOK wins
            #   - forms  (variable_forms):                        DICTIONARY wins
            # Apply dictionary first, then codebook, so codebook overwrites labels.
            merged = {
                "lists": {},
                "variable_lists": {},
                "candidates": {},
                "coded_columns": set(),
                "warnings": [],
                "source_columns": [],
                "variable_forms": {},
            }
            for parsed in (dictionary_parsed, codebook_parsed):
                if not parsed:
                    continue
                merged["lists"].update(parsed.get("lists", {}))
                merged["variable_lists"].update(parsed.get("variable_lists", {}))
                merged["candidates"].update(parsed.get("candidates", {}))
                merged["coded_columns"].update(parsed.get("coded_columns", []))
                merged["warnings"].extend(parsed.get("warnings", []))
                merged["source_columns"].extend(parsed.get("source_columns", []))
                merged["variable_forms"].update(parsed.get("variable_forms", {}))

            # Explicit form precedence: dictionary overrides codebook for forms.
            if dictionary_parsed:
                merged["variable_forms"].update(
                    dictionary_parsed.get("variable_forms", {})
                )

            merged["coded_columns"] = sorted(merged["coded_columns"])
            merged["warnings"] = list(dict.fromkeys(merged["warnings"]))
            variables = merged
            categorical_columns = set(variables["coded_columns"])

        # ---------- Detect primary PID column for ffill (disabled but kept) ----------
        _pid_cols = detect_pid_columns(raw.columns)
        _primary_pid = None
        if clean_mode == "first_three" and len(raw.columns) >= 3:
            _primary_pid = raw.columns[0]
        elif clean_mode == "custom":
            custom_selected = request.form.getlist("custom_columns")
            if custom_selected:
                _primary_pid = custom_selected[0]
        if not _primary_pid:
            _primary_pid = _pid_cols[0] if _pid_cols else (
                raw.columns[0] if len(raw.columns) > 0 else None
            )

        cleaned, duplicates, missing, missing_retained, records = clean_dataframe(
            raw,
            categorical_columns,
            preserve_missing_columns=preserve_missing_columns,
            pid_column=_primary_pid,
        )

        # ---------- Trim reference data to columns that survived cleaning ----------
        if variables is not None:
            variables["coded_columns"] = [
                name for name in variables["coded_columns"] if name in cleaned.columns
            ]
            variables["variable_lists"] = {
                name: list_name
                for name, list_name in variables["variable_lists"].items()
                if name in cleaned.columns
            }
            variables["candidates"] = {
                name: list_names
                for name, list_names in variables["candidates"].items()
                if name in cleaned.columns
            }
            variables["variable_forms"] = {
                name: form
                for name, form in variables["variable_forms"].items()
                if name in cleaned.columns
            }

            unresolved = sorted(
                set(variables["candidates"]).difference(
                    variables["variable_lists"]
                ),
                key=str.casefold,
            )

            # Store under whichever filename was uploaded (for the report).
            if codebook_file is not None and codebook_file.filename:
                ref_filename = os.path.basename(codebook_file.filename)
            else:
                ref_filename = os.path.basename(dictionary_file.filename)

            codebook_data = StudyCodebook(
                filename=ref_filename,
                variables=variables,
                unmatched_variables=unresolved,
            )
    except (
        ValueError,
        UnicodeDecodeError,
        BadZipFile,
        InvalidFileException,
        XLRDError,
        OSError,
        pd.errors.ParserError,
        pd.errors.EmptyDataError,
    ) as error:
        flash(str(error), "error")
        return redirect(url_for("index"))

    if cleaned.empty or len(cleaned.columns) == 0:
        flash("The uploaded file has no usable rows or columns.", "error")
        return redirect(url_for("index"))

    clean_columns = None
    if clean_mode == "first_three":
        clean_columns = list(cleaned.columns[:3]) if len(cleaned.columns) >= 3 else list(cleaned.columns)
    elif clean_mode == "custom":
        custom_selected = request.form.getlist("custom_columns")
        valid_custom = [c for c in custom_selected if c in cleaned.columns]
        if valid_custom:
            clean_columns = valid_custom
        else:
            clean_columns = list(cleaned.columns[:3]) if len(cleaned.columns) >= 3 else list(cleaned.columns)
    else:  # "pid"
        detected_pid = detect_pid_columns(cleaned.columns)
        if detected_pid:
            clean_columns = [detected_pid[0]]
        elif "pid" in cleaned.columns:
            clean_columns = ["pid"]
        else:
            clean_columns = None

    dataset = StudyDataset(
        study_name=study_name,
        filename=os.path.basename(file.filename),
        rows=len(cleaned),
        columns=len(cleaned.columns),
        duplicates_removed=duplicates,
        missing_values_filled=missing_retained,
        column_names=list(cleaned.columns),
        records=records,
        expected_participants=expected_participants,
        clean_columns=clean_columns,
        clean_mode=clean_mode,
        original_headers=header_map,
    )
    db.session.add(dataset)
    try:
        db.session.flush()

        safe_study_title = (
            re.sub(r"[^\w\-]+", "_", study_name).strip("_") or f"study_{dataset.id}"
        )
        storage_dir = os.path.abspath(
            os.path.join(
                app.root_path, "saved_studies", f"{safe_study_title}_{dataset.id}"
            )
        )
        try:
            os.makedirs(storage_dir, exist_ok=True)
            cleaned_file_path = os.path.join(
                storage_dir, f"{safe_study_title}_cleaned.csv"
            )
            cleaned.to_csv(cleaned_file_path, index=False)
            file.seek(0)
            raw_copy_path = os.path.join(
                storage_dir, f"original_{os.path.basename(file.filename)}"
            )
            with open(raw_copy_path, "wb") as f_out:
                f_out.write(file.read())
            if codebook_file is not None and codebook_file.filename:
                codebook_file.seek(0)
                cb_copy_path = os.path.join(
                    storage_dir,
                    f"codebook_{os.path.basename(codebook_file.filename)}",
                )
                with open(cb_copy_path, "wb") as cb_out:
                    cb_out.write(codebook_file.read())
            if dictionary_file is not None and dictionary_file.filename:
                dictionary_file.seek(0)
                dict_copy_path = os.path.join(
                    storage_dir,
                    f"dictionary_{os.path.basename(dictionary_file.filename)}",
                )
                with open(dict_copy_path, "wb") as d_out:
                    d_out.write(dictionary_file.read())
        except Exception:
            app.logger.warning(
                "Could not auto-populate storage folder %s", storage_dir
            )

        dataset.saved_path = storage_dir

        detected_pid_columns = detect_pid_columns(cleaned.columns)
        if codebook_data is not None:
            # Clean up any potential stale/orphan codebook row for this id
            StudyCodebook.query.filter_by(dataset_id=dataset.id).delete()
            codebook_data.dataset_id = dataset.id
            db.session.add(codebook_data)
        if clean_columns and len(clean_columns) == 1 and clean_columns[0] in detected_pid_columns:
            StudyPidSelection.query.filter_by(dataset_id=dataset.id).delete()
            db.session.add(
                StudyPidSelection(
                    dataset_id=dataset.id,
                    pid_column=clean_columns[0],
                )
            )
        elif len(detected_pid_columns) == 1:
            StudyPidSelection.query.filter_by(dataset_id=dataset.id).delete()
            db.session.add(
                StudyPidSelection(
                    dataset_id=dataset.id,
                    pid_column=detected_pid_columns[0],
                )
            )
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not save uploaded study dataset")
        flash(
            "The cleaned dataset could not be saved. Check the database connection.",
            "error",
        )
        return redirect(url_for("index"))
    flash("Your study file was cleaned and its dashboard is ready.", "success")
    return redirect(url_for("dashboard", dataset_id=dataset.id, new_upload="1"))

def _invalidate_charts_cache(dataset):
    if dataset is not None:
        dataset.charts_cache = None


@app.route("/studies/<int:dataset_id>/clear-cache", methods=["POST"])
def clear_charts_cache(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    dataset.charts_cache = None
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not clear charts cache")
        flash("Could not clear the chart cache. Please try again.", "error")
        return redirect(url_for("dashboard", dataset_id=dataset.id))
    flash("Chart cache cleared. Next page load will rebuild charts.", "success")
    return redirect(url_for("dashboard", dataset_id=dataset.id))


@app.route("/studies/<int:dataset_id>/category-pids", methods=["POST"])
def category_pids(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)

    chart_index = request.form.get("chart_index", type=int)
    variant_index = request.form.get("variant_index", default=0, type=int)
    category_index = request.form.get("category_index", type=int)
    chart_name = request.form.get("chart_name")
    category_label = request.form.get("category_label")

    # 1. Try reading from charts_cache['pids_index']
    cache = dataset.charts_cache
    if cache and isinstance(cache, dict):
        pids_index = cache.get("pids_index", {})
        if chart_name and chart_name in pids_index:
            var_map = pids_index[chart_name]
            pids_list_for_var = var_map.get(str(variant_index)) or var_map.get(variant_index)
            if pids_list_for_var and category_index is not None and 0 <= category_index < len(pids_list_for_var):
                return jsonify({"pids": pids_list_for_var[category_index]})

        # If indexed by chart_index
        charts = cache.get("charts", [])
        if chart_index is not None and 0 <= chart_index < len(charts):
            c_name = charts[chart_index].get("name") or charts[chart_index].get("key")
            if c_name and c_name in pids_index:
                var_map = pids_index[c_name]
                pids_list_for_var = var_map.get(str(variant_index)) or var_map.get(variant_index)
                if pids_list_for_var and category_index is not None and 0 <= category_index < len(pids_list_for_var):
                    return jsonify({"pids": pids_list_for_var[category_index]})

    # 2. Fallback: on-demand computation for this specific dataset
    codebook = db.session.get(StudyCodebook, dataset.id)
    codebook_data = codebook.variables if codebook else None
    computed_charts, _, _ = dashboard_data(dataset, codebook_data)

    target_chart = None
    if chart_index is not None and 0 <= chart_index < len(computed_charts):
        target_chart = computed_charts[chart_index]
    elif chart_name:
        for c in computed_charts:
            if c.get("name") == chart_name or c.get("key") == chart_name:
                target_chart = c
                break

    if target_chart and target_chart.get("variants"):
        if 0 <= variant_index < len(target_chart["variants"]):
            variant = target_chart["variants"][variant_index]
            pids_arr = variant.get("pids") or []
            if category_index is not None and 0 <= category_index < len(pids_arr):
                return jsonify({"pids": pids_arr[category_index]})
            if category_label:
                labels = variant.get("labels") or []
                for idx, lbl in enumerate(labels):
                    if str(lbl).strip() == str(category_label).strip() and idx < len(pids_arr):
                        return jsonify({"pids": pids_arr[idx]})

    return jsonify({"pids": []})


@app.route("/studies/<int:dataset_id>")
def dashboard(dataset_id):
    """Render the study dashboard.

    Performance note:
    Dashboard data (charts, preview, form sections, grouped sections, duplicate
    groups, and PID scans) are cached in dataset.charts_cache. On cache hit,
    all chart computation is bypassed, reducing load time from 30+ seconds to <1s.
    The cache is automatically invalidated when dataset.records or codebook.variables change.
    """
    t_start = time.time()
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    codebook = db.session.get(StudyCodebook, dataset.id)
    codebook_data = codebook.variables if codebook else None

    cache = dataset.charts_cache
    cache_valid = False
    current_cb_hash = None
    current_headers_hash = None
    if cache and cache.get("_schema_version") == _SCHEMA_VERSION:
        current_cb_hash = (
            hashlib.md5(json.dumps(codebook_data, sort_keys=True, default=str).encode()).hexdigest()
            if codebook_data else None
        )
        current_headers_hash = (
            hashlib.md5(json.dumps(dataset.original_headers, sort_keys=True, default=str).encode()).hexdigest()
            if dataset.original_headers else None
        )
        cache_valid = (
            cache.get("_records_len") == len(dataset.records)
            and cache.get("_codebook_hash") == current_cb_hash
            and cache.get("_original_headers_hash") == current_headers_hash
        )

    if cache_valid:
        charts = cache.get("charts", [])
        preview = cache.get("preview", [])
        all_dataset_pids = cache.get("all_dataset_pids", [])
        form_sections = cache.get("form_sections") or OrderedDict()
        chart_sections = cache.get("chart_sections", [])
        identical_pid_groups = cache.get("identical_pid_groups", [])
        differing_pid_groups = cache.get("differing_pid_groups", [])
        missing_pid_rows = cache.get("missing_pid_rows", [])
        all_pid_rows = cache.get("all_pid_rows", [])
        pid_sequence_gaps = cache.get("pid_sequence_gaps", [])
        pid_sequence_gap_count = cache.get("pid_sequence_gap_count", 0)
        unique_participant_count = cache.get("unique_participant_count")
        unique_participant_pids = cache.get("unique_participant_pids")
    else:
        t0 = time.time()
        charts, preview, all_dataset_pids = dashboard_data(dataset, codebook_data)
        app.logger.info("dashboard_data for dataset %s: %.2fs", dataset.id, time.time() - t0)

        if not charts:
            app.logger.warning(
                "Dataset %s produced no charts. columns=%s dtypes=%s",
                dataset.id,
                dataset.column_names[:10],
                {c: type(dataset.records[0].get(c)).__name__ for c in dataset.column_names[:10]}
                if dataset.records else {},
            )

        t0 = time.time()
        chart_sections = group_related_charts(charts)
        app.logger.info("group_related_charts for dataset %s: %.2fs", dataset.id, time.time() - t0)

        t0 = time.time()
        # Build form_sections if a data dictionary with Form Names was uploaded
        variable_forms = (codebook_data or {}).get("variable_forms", {})
        form_order = []
        seen_forms = set()
        for form in variable_forms.values():
            if form and form not in seen_forms:
                seen_forms.add(form)
                form_order.append(form)

        charts_with_form = [c for c in charts if c.get("form")]
        form_sections = OrderedDict()
        if charts_with_form:
            def pretty(form):
                return form.replace("_", " ").title() if form else "Other"

            grouped_forms = OrderedDict()
            for form in form_order:
                grouped_forms[form] = []
            for chart in charts_with_form:
                grouped_forms.setdefault(chart["form"], []).append(chart)

            chart_to_global_idx = {id(c): i for i, c in enumerate(charts)}

            for form, form_charts in grouped_forms.items():
                if not form_charts:
                    continue
                pretty_title = pretty(form)
                final_title = pretty_title
                n = 2
                while final_title in form_sections:
                    final_title = f"{pretty_title} ({n})"
                    n += 1
                indices = [chart_to_global_idx[id(c)] for c in form_charts]
                form_sections[final_title] = {
                    "raw_form": form,
                    "charts": group_related_charts(form_charts, chart_indices=indices),
                }
        app.logger.info("form_sections for dataset %s: %.2fs", dataset.id, time.time() - t0)

    has_form_map = bool(form_sections)

    id_columns = get_dataset_id_columns(dataset)
    pid_candidates = detect_pid_columns(dataset.column_names)
    pid_selection = db.session.get(StudyPidSelection, dataset.id)
    pid_column = pid_selection.pid_column if pid_selection else None
    if pid_column not in pid_candidates:
        pid_column = pid_candidates[0] if len(pid_candidates) == 1 else None
    if not id_columns and pid_column:
        id_columns = [pid_column]

    if not cache_valid:
        t0 = time.time()
        # Option A: Cache duplicate review and PID sequence scans alongside chart data
        # (Option B: Lazy loading behind /studies/<id>/duplicates endpoint can be added in future)
        identical_pid_groups, differing_pid_groups = (
            find_pid_duplicate_groups(dataset, id_columns)
            if id_columns
            else ([], [])
        )
        missing_pid_rows = [
            {"index": index, "record": record}
            for index, record in enumerate(dataset.records)
            if pid_candidates
            and all(
                is_missing(record.get(column))
                or not str(record.get(column, "")).strip()
                for column in pid_candidates
            )
        ]
        enriched_rows = []
        for index, record in enumerate(dataset.records):
            row_pids = []
            for col in pid_candidates:
                val = record.get(col)
                if val is not None and str(val).strip() and str(val).strip().lower() != "nan":
                    row_pids.append(str(val).strip())
            enriched_rows.append(
                {
                    "index": index,
                    "record": record,
                    "pids": row_pids,
                    "primary_pid": row_pids[0] if row_pids else f"Row {index + 1}",
                }
            )
        all_pid_rows = sort_pid_rows(enriched_rows, pid_candidates)
        pid_sequence_gaps = find_pid_sequence_gaps(dataset.records, pid_candidates)
        pid_sequence_gap_count = sum(
            len(group["missing_ids"]) for group in pid_sequence_gaps
        )

        # --- Unique participants: deduplicate PID values across repeat rows ---
        _seen_pids = set()
        _unique_participant_pids = []
        for rec in dataset.records:
            pid_val = None
            for c in pid_candidates:
                val = rec.get(c)
                if val is not None and str(val).strip() and str(val).strip().lower() not in ("nan", "none", ""):
                    pid_val = str(val).strip()
                    break
            if pid_val and pid_val not in _seen_pids:
                _seen_pids.add(pid_val)
                _unique_participant_pids.append(pid_val)

        def _natural_key(s):
            return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]

        _unique_participant_pids.sort(key=_natural_key)
        unique_participant_count = len(_unique_participant_pids)
        unique_participant_pids = _unique_participant_pids
        app.logger.info("duplicates and pid scans for dataset %s: %.2fs", dataset.id, time.time() - t0)

        # Write to cache
        if current_cb_hash is None:
            current_cb_hash = (
                hashlib.md5(json.dumps(codebook_data, sort_keys=True, default=str).encode()).hexdigest()
                if codebook_data else None
            )
        if current_headers_hash is None:
            current_headers_hash = (
                hashlib.md5(json.dumps(dataset.original_headers, sort_keys=True, default=str).encode()).hexdigest()
                if dataset.original_headers else None
            )
        dataset.charts_cache = {
            "charts": _strip_pids_from_charts(charts),
            "pids_index": _pids_index_from_charts(charts),
            "preview": preview,
            "all_dataset_pids": all_dataset_pids,
            "form_sections": form_sections,
            "chart_sections": chart_sections,
            "identical_pid_groups": identical_pid_groups,
            "differing_pid_groups": differing_pid_groups,
            "missing_pid_rows": missing_pid_rows,
            "all_pid_rows": all_pid_rows,
            "pid_sequence_gaps": pid_sequence_gaps,
            "pid_sequence_gap_count": pid_sequence_gap_count,
            "unique_participant_count": unique_participant_count,
            "unique_participant_pids": unique_participant_pids,
            "_schema_version": _SCHEMA_VERSION,
            "_records_len": len(dataset.records),
            "_codebook_hash": current_cb_hash,
            "_original_headers_hash": current_headers_hash,
            "_cached_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            app.logger.exception("Could not write charts cache")
    elif unique_participant_count is None:
        _seen_pids = set()
        _unique_participant_pids = []
        for rec in dataset.records:
            pid_val = None
            for c in pid_candidates:
                val = rec.get(c)
                if val is not None and str(val).strip() and str(val).strip().lower() not in ("nan", "none", ""):
                    pid_val = str(val).strip()
                    break
            if pid_val and pid_val not in _seen_pids:
                _seen_pids.add(pid_val)
                _unique_participant_pids.append(pid_val)

        def _natural_key(s):
            return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]

        _unique_participant_pids.sort(key=_natural_key)
        unique_participant_count = len(_unique_participant_pids)
        unique_participant_pids = _unique_participant_pids

    question_groups = group_question_columns(dataset.column_names)
    subcolumn_to_group = {}
    grouped_categories = {}
    for group_name, cols in question_groups.items():
        if len(cols) > 1:
            grouped_categories[group_name] = cols
            for col in cols:
                subcolumn_to_group[col] = group_name

    label_maps = {}
    if codebook_data:
        for column, list_name in codebook_data.get("variable_lists", {}).items():
            label_maps[column] = codebook_data.get("lists", {}).get(list_name, {})
        for list_name, choices in codebook_data.get("lists", {}).items():
            matching_col = next(
                (c for c in dataset.column_names if c.casefold() == list_name.casefold()),
                None,
            )
            if matching_col and matching_col not in label_maps:
                label_maps[matching_col] = choices

    hidden_columns = list(dataset.hidden_columns or [])
    hidden_set = set(hidden_columns)
    visible_columns = [c for c in (dataset.column_names or []) if c not in hidden_set]
    if hidden_set:
        def _is_chart_hidden(chart):
            key = chart.get("key") or chart.get("name")
            if key in hidden_set or chart.get("name") in hidden_set:
                return True
            if chart.get("parent_group") in hidden_set:
                return True
            if chart.get("is_grouped") and chart.get("group_columns"):
                if all(gc in hidden_set for gc in chart["group_columns"]):
                    return True
            return False

        old_to_new_idx = {}
        visible_charts = []
        for old_i, c in enumerate(charts):
            if not _is_chart_hidden(c):
                old_to_new_idx[old_i] = len(visible_charts)
                visible_charts.append(c)
        charts = visible_charts

        def _remap_sections(sec_list):
            remapped = []
            for sec in sec_list:
                kept_entries = []
                for entry in sec.get("charts", []):
                    new_idx = old_to_new_idx.get(entry.get("index"))
                    if new_idx is not None:
                        kept_entries.append({"chart": entry["chart"], "index": new_idx})
                if kept_entries:
                    new_sec = dict(sec)
                    new_sec["charts"] = kept_entries
                    remapped.append(new_sec)
            return remapped

        chart_sections = _remap_sections(chart_sections)
        if form_sections:
            new_form_sections = OrderedDict()
            for fname, fdata in form_sections.items():
                remapped_fcharts = _remap_sections(fdata.get("charts", []))
                if remapped_fcharts:
                    new_form_sections[fname] = {
                        "raw_form": fdata.get("raw_form"),
                        "charts": remapped_fcharts,
                    }
            form_sections = new_form_sections

    grouped_chart_count = sum(
        1
        for chart in charts
        if not chart.get("is_subcolumn") and not chart.get("is_base_column")
    )
    total_individual_charts = sum(
        1 for chart in charts if not chart.get("is_grouped")
    )

    observed_pids_set = set()
    for rec in dataset.records:
        for c in pid_candidates:
            val = rec.get(c)
            if val is not None and str(val).strip():
                p = str(val).strip()
                observed_pids_set.add(p)
                observed_pids_set.add(re.sub(r"[^A-Za-z0-9]", "", p).casefold())

    lilongwe_records = get_or_seed_tracking(dataset.id, "Lilongwe")
    salima_records = get_or_seed_tracking(dataset.id, "Salima")

    lilongwe_tracking = compile_tracking_summary(lilongwe_records, observed_pids_set)
    salima_tracking = compile_tracking_summary(salima_records, observed_pids_set)

    app.logger.info(
        "dashboard %s: %s (%.2fs)",
        dataset_id,
        "cache hit" if cache_valid else "cache miss",
        time.time() - t_start,
    )

    return render_template(
        "dashboard.html",
        dataset=dataset,
        codebook=codebook,
        charts=charts,
        chart_sections=chart_sections,
        id_columns=id_columns,
        pid_candidates=pid_candidates,
        pid_column=pid_column,
        identical_pid_groups=identical_pid_groups,
        differing_pid_groups=differing_pid_groups,
        missing_pid_rows=missing_pid_rows,
        all_pid_rows=all_pid_rows,
        pid_sequence_gaps=pid_sequence_gaps,
        pid_sequence_gap_count=pid_sequence_gap_count,
        preview=preview,
        label_maps=label_maps,
        subcolumn_to_group=subcolumn_to_group,
        grouped_categories=grouped_categories,
        grouped_chart_count=grouped_chart_count,
        total_individual_charts=total_individual_charts,
        new_upload=request.args.get("new_upload") == "1",
        saved_path=dataset.saved_path,
        lilongwe_tracking=lilongwe_tracking,
        salima_tracking=salima_tracking,
        all_dataset_pids=all_dataset_pids,
        unique_participant_count=unique_participant_count,
        unique_participant_pids=unique_participant_pids,
        form_sections=form_sections,
        has_form_map=has_form_map,
        hidden_columns=hidden_columns,
        visible_columns=visible_columns,
    )


def duplicate_row_indices(dataset, pid_column=None):
    if pid_column is None:
        pid_column = get_dataset_id_columns(dataset)
    identical, differing = find_pid_duplicate_groups(dataset, pid_column)
    return {
        row["index"]
        for group in identical + differing
        for row in group["rows"]
    }


@app.route("/studies/<int:dataset_id>/pid-selection", methods=["POST"])
def save_pid_selection(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    pid_column = request.form.get("pid_column", "")
    if pid_column not in detect_pid_columns(dataset.column_names):
        abort(400, "Choose a PID column detected in this dataset.")

    selection = db.session.get(StudyPidSelection, dataset.id)
    if selection is None:
        selection = StudyPidSelection(dataset_id=dataset.id)
        db.session.add(selection)
    selection.pid_column = pid_column
    _invalidate_charts_cache(dataset)
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not save dataset PID selection")
        abort(500, "The PID selection could not be saved.")
    flash(f"Duplicate review is now using {pid_column}.", "success")
    return redirect(url_for("dashboard", dataset_id=dataset.id) + "#duplicate-review")


@app.route("/studies/<int:dataset_id>/duplicate-row/edit", methods=["POST"])
def edit_duplicate_row(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    id_columns = get_dataset_id_columns(dataset)
    if not id_columns:
        abort(400, "Choose an ID or PID column before editing duplicate rows.")

    try:
        row_index = int(request.form.get("row_index", ""))
    except ValueError:
        abort(400, "A valid duplicate row index is required.")
    column = request.form.get("column", "")
    if (
        row_index < 0
        or row_index >= len(dataset.records)
        or column not in dataset.column_names
        or row_index not in duplicate_row_indices(dataset, id_columns)
    ):
        abort(400, "Choose a field and row from the duplicate review.")

    records = list(dataset.records)
    record = dict(records[row_index])
    try:
        record[column] = coerce_edited_value(
            record.get(column), request.form.get("value", "")
        )
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("dashboard", dataset_id=dataset.id) + "#duplicate-review")
    records[row_index] = record
    dataset.records = records
    _invalidate_charts_cache(dataset)
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not save duplicate row edit")
        abort(500, "The row edit could not be saved.")
    flash(f"Saved the {column} value for row {row_index + 1}.", "success")
    return redirect(url_for("dashboard", dataset_id=dataset.id) + "#duplicate-review")


@app.route("/studies/<int:dataset_id>/duplicate-row/drop", methods=["POST"])
def drop_duplicate_row(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    id_columns = get_dataset_id_columns(dataset)
    if not id_columns:
        abort(400, "Choose an ID or PID column before dropping duplicate rows.")
    try:
        row_index = int(request.form.get("row_index", ""))
    except ValueError:
        abort(400, "A valid duplicate row index is required.")
    if row_index not in duplicate_row_indices(dataset, id_columns):
        abort(400, "Only rows shown in the duplicate review can be dropped.")

    records = list(dataset.records)
    records.pop(row_index)
    dataset.records = records
    dataset.rows = len(records)
    _invalidate_charts_cache(dataset)
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not drop duplicate PID row")
        abort(500, "The duplicate row could not be dropped.")
    flash(f"Dropped row {row_index + 1}.", "success")
    return redirect(url_for("dashboard", dataset_id=dataset.id) + "#duplicate-review")


@app.route("/studies/<int:dataset_id>/codebook-mapping", methods=["POST"])
def save_codebook_mapping(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    codebook = db.session.get(StudyCodebook, dataset_id)
    if codebook is None:
        abort(404)

    column = request.form.get("column", "")
    list_name = request.form.get("list_name", "")
    candidates = codebook.variables.get("candidates", {}).get(column, [])
    question_groups = group_question_columns(
        pd.DataFrame(columns=dataset.column_names)
    )
    valid_question = (
        column in dataset.column_names
        or column in question_groups
    )
    response_lists = codebook.variables.get("lists", {})
    if not valid_question or (list_name and list_name not in response_lists):
        abort(400, "Choose a chart field and a response list from this codebook.")

    variables = dict(codebook.variables)
    variable_lists = dict(variables.get("variable_lists", {}))
    if list_name:
        variable_lists[column] = list_name
    else:
        variable_lists.pop(column, None)
    variables["variable_lists"] = variable_lists
    codebook.variables = variables
    codebook.unmatched_variables = sorted(
        set(variables.get("candidates", {})).difference(variable_lists),
        key=str.casefold,
    )
    _invalidate_charts_cache(dataset)
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not save response-list mapping")
        abort(500, "The response-list mapping could not be saved.")
    flash(f"Response labels for {column} are now mapped to {list_name}.", "success")
    return redirect(url_for("dashboard", dataset_id=dataset.id))


@app.route("/studies/<int:dataset_id>/report")
@app.route("/studies/<int:dataset_id>/download/all")
def download_report(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    report, download_name = build_report(dataset)
    return send_file(
        report,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/studies/<int:dataset_id>/download/cleaned-data")
def download_cleaned_data(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    report, download_name = build_cleaned_data_workbook(dataset)
    return send_file(
        report,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/studies/<int:dataset_id>/download/cleaning-report")
def download_cleaning_report_only(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    report, download_name = build_cleaning_report_only_workbook(dataset)
    return send_file(
        report,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/studies/<int:dataset_id>/tracking/download/<site>")
def download_tracking_sheet(dataset_id, site):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    site_name = "Salima" if str(site).lower() == "salima" else "Lilongwe"
    records = get_or_seed_tracking(dataset.id, site_name)
    df = pd.DataFrame(
        [
            {
                "PID": r.pid,
                "QUESTIONEER": r.questionnaire_date or "",
                "DENTAL": r.dental_date or "",
                "ENDO YES/NO": (r.endo_status or "").upper(),
                "ENDO DATE": r.endo_date or "",
                "COMMENT": r.comment or "",
            }
            for r in records
        ]
    )
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name=f"{site_name} tracking", index=False)
    output.seek(0)
    download_name = f"{site_name}-tracking-{dataset.id}.xlsx"
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/studies/<int:dataset_id>/tracking/upload", methods=["POST"])
def upload_tracking_sheet(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    site = request.form.get("site", "Lilongwe")
    site_name = "Salima" if str(site).lower() == "salima" else "Lilongwe"
    file = request.files.get("tracking_file")
    if not file or not file.filename:
        flash("Please select a tracking CSV or Excel file to upload.", "error")
        return redirect(url_for("dashboard", dataset_id=dataset.id) + "#tracking-section")

    try:
        raw_df = read_upload(file)
        col_map = {}
        for col in raw_df.columns:
            c = str(col).strip().casefold()
            if "pid" in c:
                col_map["pid"] = col
            elif "quest" in c:
                col_map["questionnaire"] = col
            elif "dent" in c:
                col_map["dental"] = col
            elif "endo" in c and "date" not in c:
                col_map["endo_status"] = col
            elif "endo" in c and "date" in c:
                col_map["endo_date"] = col
            elif "comment" in c or "reason" in c:
                col_map["comment"] = col

        if "pid" not in col_map:
            flash("The tracking file must contain a 'PID' column.", "error")
            return redirect(url_for("dashboard", dataset_id=dataset.id) + "#tracking-section")

        # Replace existing records for this dataset and site
        ParticipantTracking.query.filter_by(
            dataset_id=dataset.id, site=site_name
        ).delete()

        new_entries = []
        for _, row in raw_df.iterrows():
            pid_raw = row.get(col_map["pid"])
            if pd.isna(pid_raw) or not str(pid_raw).strip() or str(pid_raw).strip().lower() == "nan":
                continue
            pid_val = str(pid_raw).strip()
            q_val = (
                str(row[col_map["questionnaire"]]).strip()
                if "questionnaire" in col_map and pd.notna(row.get(col_map["questionnaire"]))
                else None
            )
            d_val = (
                str(row[col_map["dental"]]).strip()
                if "dental" in col_map and pd.notna(row.get(col_map["dental"]))
                else None
            )
            endo_raw = (
                str(row[col_map["endo_status"]]).strip().upper()
                if "endo_status" in col_map and pd.notna(row.get(col_map["endo_status"]))
                else None
            )
            endo_s = endo_raw if endo_raw in ("YES", "NO") else None
            endo_d = (
                str(row[col_map["endo_date"]]).strip()
                if "endo_date" in col_map and pd.notna(row.get(col_map["endo_date"]))
                else None
            )
            comm = (
                str(row[col_map["comment"]]).strip()
                if "comment" in col_map and pd.notna(row.get(col_map["comment"]))
                else None
            )

            new_entries.append(
                ParticipantTracking(
                    dataset_id=dataset.id,
                    site=site_name,
                    pid=pid_val,
                    questionnaire_date=q_val,
                    dental_date=d_val,
                    endo_status=endo_s,
                    endo_date=endo_d,
                    comment=comm,
                )
            )

        db.session.bulk_save_objects(new_entries)
        db.session.commit()
        flash(
            f"Successfully updated {len(new_entries)} tracking records for {site_name}.",
            "success",
        )
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Tracking upload failed")
        flash(f"Failed to process tracking file: {str(e)}", "error")

    return redirect(url_for("dashboard", dataset_id=dataset.id) + "#tracking-section")


@app.route("/studies/<int:dataset_id>/tracking/edit", methods=["POST"])
def edit_tracking_row(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    row_id = request.form.get("row_id")
    record = ParticipantTracking.query.filter_by(id=row_id).first()
    if not record:
        abort(404)
    # Associate the record with this dataset if it isn't already
    if record.dataset_id is None:
        record.dataset_id = dataset.id
    record.questionnaire_date = request.form.get("questionnaire_date", "").strip() or None
    record.dental_date = request.form.get("dental_date", "").strip() or None
    endo_s = request.form.get("endo_status", "").strip().upper() or None
    record.endo_status = endo_s if endo_s in ("YES", "NO") else None
    record.endo_date = request.form.get("endo_date", "").strip() or None
    record.comment = request.form.get("comment", "").strip() or None
    try:
        db.session.commit()
        flash(f"Updated tracking record for {record.pid}.", "success")
    except Exception:
        db.session.rollback()
        flash("Could not update tracking record.", "error")
    return redirect(url_for("dashboard", dataset_id=dataset.id) + "#tracking-section")


@app.route("/studies/<int:dataset_id>/save-path", methods=["POST"])
def save_dataset_path(dataset_id):
    """Store the user's chosen local save path for quick reference on the home page."""
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    path = request.form.get("saved_path", "").strip()
    return_to = request.form.get("return_to", "")
    dataset.saved_path = path if path else None
    if path:
        try:
            os.makedirs(path, exist_ok=True)
            if dataset.records and dataset.column_names:
                df = pd.DataFrame(dataset.records, columns=dataset.column_names)
                df.to_csv(os.path.join(path, "cleaned_dataset.csv"), index=False)
        except Exception:
            pass
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not save dataset path")
        abort(500, "The file path could not be saved.")
    if path:
        flash(f"File path saved: {path}", "success")
    else:
        flash("File path cleared.", "success")
    if return_to == "index":
        return redirect(url_for("index"))
    return redirect(url_for("dashboard", dataset_id=dataset.id))


@app.route("/studies/<int:dataset_id>/delete", methods=["POST"])
def delete_study(dataset_id):
    """Delete an individual study dataset and its associated codebooks and tracking records."""
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    study_name = dataset.study_name
    try:
        StudyPidSelection.query.filter_by(dataset_id=dataset_id).delete()
        StudyCodebook.query.filter_by(dataset_id=dataset_id).delete()
        ParticipantTracking.query.filter_by(dataset_id=dataset_id).delete()
        db.session.delete(dataset)
        db.session.commit()
        flash(f'Study "{study_name}" and its dashboard were deleted.', "success")
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not delete study %s", dataset_id)
        flash("Could not delete study. Database error occurred.", "error")
    return redirect(url_for("index"))


@app.route("/studies/clear-history", methods=["POST"])
def clear_all_history():
    """Clear all study history and saved dashboards."""
    try:
        StudyPidSelection.query.delete()
        StudyCodebook.query.delete()
        ParticipantTracking.query.delete()
        StudyDataset.query.delete()
        db.session.commit()
        flash("All study history and dashboards have been cleared.", "success")
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception("Could not clear study history")
        flash("Could not clear history. Database error occurred.", "error")
    return redirect(url_for("index"))


@app.route("/studies/<int:dataset_id>/merge/compare", methods=["POST"])
def merge_compare(dataset_id):
    """Compare column structure of current dataset vs an uploaded Excel file.

    Returns JSON:
    {
      "current_name": str,
      "new_name": str,
      "columns": [
        {
          "column": str,          # column name
          "in_current": bool,
          "in_new": bool,
          "has_values_current": bool | null,  # null if not in current
          "has_values_new": bool | null,       # null if not in new
          "match": bool           # True if present in both
        }, ...
      ]
    }
    """
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    file = request.files.get("file")
    if file is None or not file.filename:
        return {"error": "No file uploaded."}, 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".xlsx", ".xls"):
        return {"error": "Please upload an Excel file (.xlsx or .xls)."}, 400
    try:
        new_df = pd.read_excel(file)
    except Exception as exc:
        return {"error": f"Could not read the file: {exc}"}, 400

    current_cols = list(dataset.column_names)
    new_cols = list(new_df.columns)
    current_set = {c.strip() for c in current_cols}
    new_set = {c.strip() for c in new_cols}
    all_cols = list(dict.fromkeys(current_cols + [c for c in new_cols if c.strip() not in current_set]))

    current_df = pd.DataFrame(dataset.records, columns=dataset.column_names)

    comparison = []
    for col in all_cols:
        in_cur = col.strip() in current_set
        in_new = col.strip() in new_set

        if in_cur:
            series_cur = current_df[col] if col in current_df.columns else pd.Series(dtype=object)
            has_values_cur = bool(series_cur.dropna().astype(str).str.strip().replace("", pd.NA).dropna().shape[0] > 0)
        else:
            has_values_cur = None

        if in_new:
            matched_new = next((c for c in new_df.columns if c.strip() == col.strip()), None)
            if matched_new is not None:
                series_new = new_df[matched_new]
                has_values_new = bool(series_new.dropna().astype(str).str.strip().replace("", pd.NA).dropna().shape[0] > 0)
            else:
                has_values_new = None
        else:
            has_values_new = None

        comparison.append({
            "column": col,
            "in_current": in_cur,
            "in_new": in_new,
            "has_values_current": has_values_cur,
            "has_values_new": has_values_new,
            "match": in_cur and in_new,
        })

    return {
        "current_name": dataset.study_name,
        "new_name": os.path.basename(file.filename),
        "current_rows": len(current_df),
        "new_rows": len(new_df),
        "columns": comparison,
    }


@app.route("/studies/<int:dataset_id>/merge/execute", methods=["POST"])
def merge_execute(dataset_id):
    """Execute the merge after user has decided which columns to add/drop.

    POST body (JSON):
    {
      "columns": ["col1", "col2", ...],  # final ordered column list to include
      "drop_from_new": ["colX", ...]     # columns to exclude from new file
    }
    Plus multipart file: 'file' — the new Excel dataset.

    Saves merged Excel to a temp folder inside dataset.saved_path (or a system temp dir).
    Returns JSON: { "output_path": str, "total_rows": int, "current_rows": int, "new_rows": int }
    """
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    file = request.files.get("file")
    if file is None or not file.filename:
        return {"error": "No file uploaded."}, 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in (".xlsx", ".xls"):
        return {"error": "Please upload an Excel file (.xlsx or .xls)."}, 400

    try:
        import json as _json
        final_columns = _json.loads(request.form.get("columns", "[]"))
        drop_from_new = set(_json.loads(request.form.get("drop_from_new", "[]")))
        new_df = pd.read_excel(file)
    except Exception as exc:
        return {"error": f"Could not process request: {exc}"}, 400

    # --- Build copies, never touch originals ---
    current_df = pd.DataFrame(dataset.records, columns=dataset.column_names).copy()
    new_df = new_df.copy()

    # Drop unwanted columns from new dataset
    new_df = new_df.drop(columns=[c for c in drop_from_new if c in new_df.columns], errors="ignore")

    # Align both DataFrames to the final column list
    for col in final_columns:
        if col not in current_df.columns:
            current_df[col] = None
        if col not in new_df.columns:
            new_df[col] = None

    current_df = current_df[final_columns]
    new_df = new_df[final_columns]

    merged_df = pd.concat([current_df, new_df], ignore_index=True)

    # Save to output directory
    base_dir = dataset.saved_path if (dataset.saved_path and os.path.isdir(dataset.saved_path)) else None
    if base_dir is None:
        import tempfile
        base_dir = tempfile.gettempdir()

    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", dataset.study_name).strip("-") or "study"
    merge_dir = os.path.join(base_dir, "merged")
    os.makedirs(merge_dir, exist_ok=True)
    output_filename = f"{safe_name}-merged-{dataset_id}.xlsx"
    output_path = os.path.join(merge_dir, output_filename)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        current_df.to_excel(writer, sheet_name="Current dataset (copy)", index=False)
        new_df.to_excel(writer, sheet_name="New dataset (copy)", index=False)
        merged_df.to_excel(writer, sheet_name="Merged", index=False)

    return {
        "output_path": output_path,
        "total_rows": len(merged_df),
        "current_rows": len(current_df),
        "new_rows": len(new_df),
    }


@app.route("/api/inspect-headers", methods=["POST"])
def inspect_headers():
    file = request.files.get("file")
    if file is None or not file.filename:
        return {"columns": []}, 400
    try:
        columns, display_names = read_upload_headers(file)
        return {"columns": columns, "display_names": display_names}
    except Exception as error:
        app.logger.warning("inspect_headers error: %s", error)
        return {"columns": [], "error": str(error)}, 200


@app.route("/api/verify-password", methods=["POST"])
def api_verify_password():
    """Verify the admin password stored in AppSetting."""
    payload = request.get_json(silent=True) or {}
    password = payload.get("password", "")
    if verify_admin_password(password):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Incorrect password."}), 403


@app.route("/studies/<int:dataset_id>/reveal-path", methods=["POST"])
def reveal_study_path(dataset_id):
    """Return the saved_path for a study only after verifying the admin password."""
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    payload = request.get_json(silent=True) or {}
    password = payload.get("password", "")
    if not verify_admin_password(password):
        return jsonify({"ok": False, "error": "Incorrect password."}), 403
    return jsonify({"ok": True, "saved_path": dataset.saved_path or ""})


@app.route("/studies/<int:dataset_id>/hidden-columns", methods=["POST"])
def update_hidden_columns(dataset_id):
    """Lock or unlock one or more columns on a study dashboard (password-protected)."""
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    payload = request.get_json(silent=True) or {}
    password = payload.get("password", "")
    if not verify_admin_password(password):
        return jsonify({"ok": False, "error": "Incorrect password."}), 403

    valid_cols = set(dataset.column_names or [])
    current_hidden = list(dataset.hidden_columns or [])

    if "hidden_columns" in payload and isinstance(payload["hidden_columns"], list):
        new_hidden = [c for c in payload["hidden_columns"] if c in valid_cols]
    elif "column" in payload:
        col = payload.get("column")
        if col not in valid_cols:
            return jsonify({"ok": False, "error": "Unknown column."}), 400
        locked = bool(payload.get("locked"))
        hidden_set = set(current_hidden)
        if locked:
            hidden_set.add(col)
        else:
            hidden_set.discard(col)
        new_hidden = [c for c in (dataset.column_names or []) if c in hidden_set]
    else:
        return jsonify({"ok": False, "error": "Missing column or hidden_columns."}), 400

    dataset.hidden_columns = new_hidden
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        return jsonify({"ok": False, "error": "Could not save locked columns."}), 500
    return jsonify({"ok": True, "hidden_columns": new_hidden})


with app.app_context():
    # Ensure the instance directory exists so SQLite can create the DB file
    # on a fresh clone (the directory is git-tracked via .gitkeep but the
    # database file itself is git-ignored for data privacy).
    os.makedirs(app.instance_path, exist_ok=True)
    db.create_all()
    try:
        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)
        existing_cols = {c["name"] for c in inspector.get_columns("study_dataset")}
        with db.engine.connect() as conn:
            if "expected_participants" not in existing_cols:
                conn.execute(text("ALTER TABLE study_dataset ADD COLUMN expected_participants INTEGER"))
            if "clean_columns" not in existing_cols:
                conn.execute(text("ALTER TABLE study_dataset ADD COLUMN clean_columns JSON"))
            if "clean_mode" not in existing_cols:
                conn.execute(text("ALTER TABLE study_dataset ADD COLUMN clean_mode VARCHAR(50)"))
            if "original_headers" not in existing_cols:
                conn.execute(text("ALTER TABLE study_dataset ADD COLUMN original_headers JSON"))
            if "charts_cache" not in existing_cols:
                conn.execute(text("ALTER TABLE study_dataset ADD COLUMN charts_cache JSON"))
            if "hidden_columns" not in existing_cols:
                conn.execute(text("ALTER TABLE study_dataset ADD COLUMN hidden_columns JSON"))
            # Clean up any orphan rows in child tables where dataset_id no longer exists
            conn.execute(text("DELETE FROM study_pid_selection WHERE dataset_id NOT IN (SELECT id FROM study_dataset)"))
            conn.execute(text("DELETE FROM study_codebook WHERE dataset_id NOT IN (SELECT id FROM study_dataset)"))
            conn.execute(text("DELETE FROM participant_tracking WHERE dataset_id IS NOT NULL AND dataset_id NOT IN (SELECT id FROM study_dataset)"))
            conn.commit()
        if db.session.get(AppSetting, "admin_password") is None:
            db.session.add(AppSetting(key="admin_password", value="DATA123"))
            db.session.commit()
    except Exception as e:
        app.logger.warning("Auto-migration note: %s", e)


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
