import io
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zipfile import BadZipFile

import pandas as pd
from flask import (
    Flask,
    abort,
    flash,
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


def clean_dataframe(
    dataframe, categorical_columns=None, preserve_missing_columns=None
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

    missing_values_filled = 0
    for column in dataframe.columns:
        missing_count = int(dataframe[column].isna().sum())
        if not missing_count:
            continue
        if column in preserve_missing_columns:
            continue
        missing_values_filled += missing_count
        if (
            pd.api.types.is_numeric_dtype(dataframe[column])
            and column not in categorical_columns
        ):
            median = dataframe[column].median()
            dataframe[column] = dataframe[column].fillna(
                0 if pd.isna(median) else median
            )
        else:
            mode = dataframe[column].mode(dropna=True)
            dataframe[column] = dataframe[column].fillna(
                mode.iloc[0] if not mode.empty else "Unknown"
            )

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
    return dataframe, duplicates_removed, missing_values_filled, records


def read_upload(upload):
    extension = os.path.splitext(upload.filename)[1].lower()
    if extension in (".csv", ".tsv"):
        pos = upload.tell() if hasattr(upload, "tell") else None
        line = upload.readline()
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="ignore")
        sep = "\t" if "\t" in line else ","
        if pos is not None and hasattr(upload, "seek"):
            upload.seek(pos)
        return pd.read_csv(upload, sep=sep)
    if extension in (".xlsx", ".xls"):
        return pd.read_excel(upload)
    raise ValueError("Upload a CSV or Excel file (.csv, .xlsx, .xls).")


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
    spaced_name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", str(column))
    tokens = set(re.findall(r"[a-z0-9]+", spaced_name.casefold()))
    excluded_tokens = {
        "comment",
        "comments",
        "description",
        "device",
        "email",
        "guid",
        "id",
        "instance",
        "meta",
        "metadata",
        "note",
        "notes",
        "pid",
        "remark",
        "remarks",
        "submission",
        "uuid",
        "username",
    }
    starts_with_admin_prefix = re.match(
        r"^(?:start|end|today)(?:[_\s]|$)", str(column).casefold()
    )
    return (
        bool(tokens.intersection(excluded_tokens))
        or starts_with_admin_prefix is not None
        or str(column).casefold().startswith("other_")
    )


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
        normalized = re.sub(r"[^a-z0-9]+", "", str(column).casefold())
        if (
            normalized == "pid"
            or normalized.startswith("pid")
            or normalized in {
                "participantid",
                "participantidentifier",
                "respondentid",
                "subjectid",
            }
        ):
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


def find_pid_duplicate_groups(dataset, pid_column):
    pid_columns = set(detect_pid_columns(dataset.column_names))
    groups_by_pid = {}
    for index, record in enumerate(dataset.records):
        pid_value = record.get(pid_column)
        if is_missing(pid_value) or not str(pid_value).strip():
            continue
        normalized_pid = normalize_code(pid_value)
        if normalized_pid is None:
            continue
        normalized_pid = normalized_pid.casefold()
        groups_by_pid.setdefault(normalized_pid, []).append(
            {"index": index, "record": record, "pid": pid_value}
        )

    identical = []
    differing = []
    for rows in groups_by_pid.values():
        if len(rows) < 2:
            continue
        signatures = {
            tuple(
                (column, repr(row["record"].get(column)))
                for column in dataset.column_names
                if column not in pid_columns
            )
            for row in rows
        }
        different_columns = [
            column
            for column in dataset.column_names
            if column not in pid_columns
            and len({repr(row["record"].get(column)) for row in rows}) > 1
        ]
        group = {
            "pid": rows[0]["pid"],
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
        ("variable", "variablename", "question", "questionname", "field", "fieldname")
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
    if code_header is None:
        warnings.append(
            "Could not identify a code column. Recognized alternatives include "
            "name, code, and value; this codebook was saved for review but not applied."
        )
    else:
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

    return {
        "lists": lists,
        "variable_lists": variable_lists,
        "candidates": candidates,
        "coded_columns": sorted(coded_columns),
        "warnings": list(dict.fromkeys(warnings)),
        "source_columns": source_columns,
    }


def codebook_labels(codebook, column):
    if not codebook:
        return {}
    list_name = codebook.get("variable_lists", {}).get(column)
    return codebook.get("lists", {}).get(list_name, {}) if list_name else {}


def extract_row_pids(frame):
    pid_cols = detect_pid_columns(frame.columns)
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
        frequencies = codes.value_counts().head(10)
        category_codes = frequencies.index.tolist()
        ordered_english = [
            value_labels.get(code, {}).get("en")
            or value_labels.get(code, {}).get("ny")
            or f"Unlabelled ({code})"
            for code in category_codes
        ]
        ordered_chichewa = [
            value_labels.get(code, {}).get("ny")
            or value_labels.get(code, {}).get("en")
            or f"Unlabelled ({code})"
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
                f"{len(values):,} responses · {len(frequencies):,} response "
                f"categories · response list: {list_name}"
            ),
        }
    elif pd.api.types.is_numeric_dtype(values):
        numeric = pd.to_numeric(values, errors="coerce").dropna()
        if numeric.empty:
            return None
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
        raw_values = values.astype(str)
        looks_like_date = (
            raw_values.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}").mean() >= 0.8
        )
        parsed_dates = (
            pd.to_datetime(raw_values, errors="coerce", format="mixed", utc=True)
            if looks_like_date
            else pd.Series(dtype="datetime64[ns, UTC]")
        )
        if looks_like_date and parsed_dates.notna().sum() >= len(raw_values) * 0.8:
            date_values = parsed_dates.dropna()
            periods = date_values.dt.tz_localize(None).dt.to_period("M").astype(str)
            frequencies = periods.value_counts().sort_index().tail(18)
            pids_per_period = []
            for period in frequencies.index:
                period_indices = periods[periods == period].index
                pids_per_period.append(row_pids.loc[period_indices].tolist())

            return {
                "name": column,
                "description": description,
                "kind": "date",
                "variants": [
                    {
                        "type": "line",
                        "label": "Trend line",
                        "labels": frequencies.index.tolist(),
                        "labels_en": frequencies.index.tolist(),
                        "labels_ny": frequencies.index.tolist(),
                        "values": [int(value) for value in frequencies.tolist()],
                        "pids": pids_per_period,
                    }
                ],
                "summary": (
                    f"{len(date_values):,} dated records · "
                    f"latest {len(frequencies):,} months"
                ),
            }

        frequencies = raw_values.value_counts().head(10)
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
                f"{values.nunique():,} unique values · "
                f"showing top {len(frequencies)}"
            ),
        }


def dashboard_data(dataset, codebook=None):
    codebook = codebook or {}
    frame = pd.DataFrame(dataset.records, columns=dataset.column_names)
    variable_lists = codebook.get("variable_lists", {})
    response_lists = codebook.get("lists", {})
    candidates = codebook.get("candidates", {})
    coded_columns = set(codebook.get("coded_columns", []))
    question_groups = group_question_columns(frame)
    app.logger.info(
        "Detected dashboard question groups for study %s: %s",
        dataset.id,
        question_groups,
    )
    row_pids = extract_row_pids(frame)
    charts = []
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

    preview = [
        {key: (None if pd.isna(value) else value) for key, value in row.items()}
        for row in dataset.records[:10]
    ]
    return charts, preview


def group_related_charts(charts):
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
    for index, chart in enumerate(charts):
        parent = chart.get("parent_group")
        if parent:
            subcharts_by_parent.setdefault(parent, []).append(
                {"chart": chart, "index": index}
            )

    for index, chart in enumerate(charts):
        if chart.get("is_subcolumn") or chart.get("is_base_column"):
            continue
        list_name = chart.get("response_list")
        child_entries = [{"chart": chart, "index": index}] + subcharts_by_parent.get(
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
        ("Rows in cleaned output", dataset.rows),
        ("Columns in cleaned output", dataset.columns),
        ("Duplicate rows removed automatically", dataset.duplicates_removed),
        ("Duplicate PID groups reviewed", "See dashboard duplicate review"),
        ("Missing values filled", dataset.missing_values_filled),
    ]
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
        ("Rows in cleaned output", dataset.rows),
        ("Columns in cleaned output", dataset.columns),
        ("Duplicate rows removed automatically", dataset.duplicates_removed),
        ("Duplicate PID groups reviewed", "See dashboard duplicate review"),
        ("Missing values filled", dataset.missing_values_filled),
    ]
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
        codebook_file = request.files.get("codebook")
        codebook_data = None
        categorical_columns = set()
        question_groups = group_question_columns(raw)
        preserve_missing_columns = {
            column
            for columns in question_groups.values()
            if len(columns) > 1
            for column in columns
        }
        preserve_missing_columns.update(detect_pid_columns(raw.columns))
        if codebook_file is not None and codebook_file.filename:
            raw.columns = make_unique_headers(raw.columns)
            variables = parse_codebook(codebook_file, raw)
            categorical_columns = set(variables["coded_columns"])
        cleaned, duplicates, missing, records = clean_dataframe(
            raw,
            categorical_columns,
            preserve_missing_columns=preserve_missing_columns,
        )
        if codebook_file is not None and codebook_file.filename:
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
            unresolved = sorted(
                set(variables["candidates"]).difference(
                    variables["variable_lists"]
                ),
                key=str.casefold,
            )
            codebook_data = StudyCodebook(
                filename=os.path.basename(codebook_file.filename),
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

    dataset = StudyDataset(
        study_name=study_name,
        filename=os.path.basename(file.filename),
        rows=len(cleaned),
        columns=len(cleaned.columns),
        duplicates_removed=duplicates,
        missing_values_filled=missing,
        column_names=list(cleaned.columns),
        records=records,
    )
    db.session.add(dataset)
    try:
        detected_pid_columns = detect_pid_columns(cleaned.columns)
        if codebook_data is not None:
            db.session.flush()
            codebook_data.dataset_id = dataset.id
            db.session.add(codebook_data)
        db.session.flush()
        if len(detected_pid_columns) == 1:
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


@app.route("/studies/<int:dataset_id>")
def dashboard(dataset_id):
    dataset = db.session.get(StudyDataset, dataset_id)
    if dataset is None:
        abort(404)
    codebook = db.session.get(StudyCodebook, dataset.id)
    codebook_data = codebook.variables if codebook else None
    charts, preview = dashboard_data(dataset, codebook_data)
    chart_sections = group_related_charts(charts)
    pid_candidates = detect_pid_columns(dataset.column_names)
    pid_selection = db.session.get(StudyPidSelection, dataset.id)
    pid_column = pid_selection.pid_column if pid_selection else None
    if pid_column not in pid_candidates:
        pid_column = pid_candidates[0] if len(pid_candidates) == 1 else None
    identical_pid_groups, differing_pid_groups = (
        find_pid_duplicate_groups(dataset, pid_column)
        if pid_column
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

    return render_template(
        "dashboard.html",
        dataset=dataset,
        codebook=codebook,
        charts=charts,
        chart_sections=chart_sections,
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
    )


def duplicate_row_indices(dataset, pid_column):
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
    selection = db.session.get(StudyPidSelection, dataset.id)
    pid_column = selection.pid_column if selection else None
    candidates = detect_pid_columns(dataset.column_names)
    if not pid_column and len(candidates) == 1:
        pid_column = candidates[0]
    if pid_column not in candidates:
        abort(400, "Choose a PID column before editing duplicate rows.")

    try:
        row_index = int(request.form.get("row_index", ""))
    except ValueError:
        abort(400, "A valid duplicate row index is required.")
    column = request.form.get("column", "")
    if (
        row_index < 0
        or row_index >= len(dataset.records)
        or column not in dataset.column_names
        or row_index not in duplicate_row_indices(dataset, pid_column)
    ):
        abort(400, "Choose a field and row from the PID duplicate review.")

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
    selection = db.session.get(StudyPidSelection, dataset.id)
    pid_column = selection.pid_column if selection else None
    candidates = detect_pid_columns(dataset.column_names)
    if not pid_column and len(candidates) == 1:
        pid_column = candidates[0]
    if pid_column not in candidates:
        abort(400, "Choose a PID column before dropping duplicate rows.")
    try:
        row_index = int(request.form.get("row_index", ""))
    except ValueError:
        abort(400, "A valid duplicate row index is required.")
    if row_index not in duplicate_row_indices(dataset, pid_column):
        abort(400, "Only rows shown in the PID duplicate review can be dropped.")

    records = list(dataset.records)
    records.pop(row_index)
    dataset.records = records
    dataset.rows = len(records)
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
    # Accept an empty string to clear the path, any non-empty value as the note.
    dataset.saved_path = path if path else None
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
    return redirect(url_for("dashboard", dataset_id=dataset.id))


with app.app_context():
    db.create_all()


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
