"""
Script to apply RedCap labeled export support to app.py and templates/dashboard.html
"""

import re

# -----------------------------------------------------------------------------
# Read app.py
# -----------------------------------------------------------------------------
with open("app.py", "r", encoding="utf-8") as f:
    app_src = f.read()

# 1. Add original_headers to StudyDataset
if "original_headers = db.Column" not in app_src:
    app_src = app_src.replace(
        "    clean_mode = db.Column(db.String(50), nullable=True)",
        "    clean_mode = db.Column(db.String(50), nullable=True)\n    # Maps sanitized header -> original header (for display only)\n    original_headers = db.Column(db.JSON, nullable=True)"
    )
    print("1. Added original_headers to StudyDataset model")

# 2. Add header sanitization functions after make_unique_headers
sanitizer_funcs = '''

def _normalize_header_token(name):
    """Lowercase, replace non-alphanumerics with _, collapse runs, strip edges."""
    s = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    return re.sub(r"_+", "_", s)


def _looks_like_display_header(col):
    """True if this header looks like a human display label rather than a machine name.

    Machine names: lowercase, snake_case, no punctuation other than _.
    Display labels: contain spaces, ?, (, ), /, commas, mixed case, etc.
    """
    if not isinstance(col, str):
        return False
    s = col.strip()
    if not s:
        return False
    # Already machine-ish: only lowercase letters, digits, underscores
    if re.fullmatch(r"[a-z0-9_]+", s):
        return False
    return True


def _looks_like_machine_name_row(values):
    """True if a row of values looks like machine column names (all lowercase snake_case)."""
    hits, checked = 0, 0
    for v in values:
        if v is None or pd.isna(v):
            continue
        s = str(v).strip()
        if not s or s.lower() == "nan":
            continue
        checked += 1
        if re.fullmatch(r"[a-z0-9_]+", s):
            hits += 1
    return checked > 0 and hits / checked >= 0.8


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
'''

if "def sanitize_headers_for_upload" not in app_src:
    target = "    return result\n"
    idx = app_src.find(target)
    if idx != -1:
        insert_pos = idx + len(target)
        app_src = app_src[:insert_pos] + sanitizer_funcs + app_src[insert_pos:]
        print("2. Added sanitize_headers_for_upload and helpers")

# 3. Update read_upload_headers to read 2 rows and run sanitize_headers_for_upload
old_read_headers = '''def read_upload_headers(upload):
    filename = getattr(upload, "filename", "") or ""
    extension = os.path.splitext(filename)[1].lower()
    if extension in (".csv", ".tsv"):
        pos = upload.tell() if hasattr(upload, "tell") else None
        line = upload.readline()
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="ignore")
        sep = "\\t" if "\\t" in line else ","
        if pos is not None and hasattr(upload, "seek"):
            upload.seek(pos)
        df = pd.read_csv(upload, sep=sep, nrows=1)
        if pos is not None and hasattr(upload, "seek"):
            upload.seek(pos)
        return make_unique_headers(df.columns)
    if extension in (".xlsx", ".xls"):
        pos = upload.tell() if hasattr(upload, "tell") else None
        df = pd.read_excel(upload, nrows=1)
        if pos is not None and hasattr(upload, "seek"):
            upload.seek(pos)
        return make_unique_headers(df.columns)
    return []'''

new_read_headers = '''def read_upload_headers(upload):
    filename = getattr(upload, "filename", "") or ""
    extension = os.path.splitext(filename)[1].lower()
    if extension in (".csv", ".tsv"):
        pos = upload.tell() if hasattr(upload, "tell") else None
        line = upload.readline()
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="ignore")
        sep = "\\t" if "\\t" in line else ","
        if pos is not None and hasattr(upload, "seek"):
            upload.seek(pos)
        df = pd.read_csv(upload, sep=sep, nrows=2)
        if pos is not None and hasattr(upload, "seek"):
            upload.seek(pos)
        df, _ = sanitize_headers_for_upload(df)
        return list(df.columns)
    if extension in (".xlsx", ".xls"):
        pos = upload.tell() if hasattr(upload, "tell") else None
        df = pd.read_excel(upload, nrows=2)
        if pos is not None and hasattr(upload, "seek"):
            upload.seek(pos)
        df, _ = sanitize_headers_for_upload(df)
        return list(df.columns)
    return []'''

if old_read_headers in app_src:
    app_src = app_src.replace(old_read_headers, new_read_headers)
    print("3. Updated read_upload_headers")

# 4. Multi-response grouping for labeled exports in group_question_columns
target_group = "    subcolumn_to_group = {}\n    for group_name, cols in multiple_response_questions.items():\n        for col in cols:\n            subcolumn_to_group[col] = group_name\n"

labeled_multi_choice = '''    # RedCap labeled exports: "Question text? (select all that apply) (choice=Option)"
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
        col_str = str(column)
        m = choice_pattern.match(col_str) or slug_choice_pattern.match(col_str)
        if not m:
            continue
        stem = m.group(1).strip("_ ")
        if not stem:
            continue
        options_by_stem.setdefault(stem, []).append((column, stem, None, None))

    # Merge new stems into multiple_response_questions
    for stem, items in options_by_stem.items():
        if len(items) > 1 and stem not in multiple_response_questions:
            multiple_response_questions[stem] = [x[0] for x in items]
            for col in multiple_response_questions[stem]:
                subcolumn_to_group[col] = stem
'''

if "choice_pattern = re.compile" not in app_src:
    idx = app_src.find(target_group)
    if idx != -1:
        insert_pos = idx + len(target_group)
        app_src = app_src[:insert_pos] + "\n" + labeled_multi_choice + "\n" + app_src[insert_pos:]
        print("4. Added labeled multi-choice pattern in group_question_columns")

# 5. Fix PID detection fallback in get_dataset_id_columns()
old_get_dataset_id = '''    candidates = detect_pid_columns(dataset.column_names)
    if not pid_column and len(candidates) == 1:
        pid_column = candidates[0]
    if pid_column and pid_column in dataset.column_names:
        return [pid_column]
    return candidates[:1] if candidates else []'''

new_get_dataset_id = '''    candidates = detect_pid_columns(dataset.column_names)
    if not pid_column and len(candidates) == 1:
        pid_column = candidates[0]
    if pid_column and pid_column in dataset.column_names:
        return [pid_column]
    if not candidates and "pid" in dataset.column_names:
        return ["pid"]
    return candidates[:1] if candidates else []'''

if old_get_dataset_id in app_src:
    app_src = app_src.replace(old_get_dataset_id, new_get_dataset_id)
    print("5. Fixed PID fallback in get_dataset_id_columns")

# 6. Extract _build_text_chart and fix is_discrete numeric branch
old_text_branch = '''    else:
        raw_values = values.astype(str)
        # Detect dates in both YYYY-MM-DD and DD/MM/YYYY (and similar) formats
        looks_like_date = (
            raw_values.str.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}").mean() >= 0.5
            or raw_values.str.match(r"^\d{1,2}[/-]\d{1,2}[/-]\d{4}").mean() >= 0.5
        )
        if looks_like_date:
            try:
                parsed_dates = pd.to_datetime(
                    raw_values, errors="coerce", dayfirst=True
                )
            except Exception:
                parsed_dates = pd.to_datetime(raw_values, errors="coerce")
        else:
            parsed_dates = pd.Series(dtype="datetime64[ns]")
        if looks_like_date and parsed_dates.notna().sum() >= len(raw_values) * 0.5:
            date_values = parsed_dates.dropna()
            col_low = column.lower().replace(" ", "_").replace("-", "_")
            is_dob_col = any(
                kw in col_low
                for kw in ("dob", "birth_date", "date_of_birth", "dateofbirth",
                            "birth_year", "year_of_birth")
            )
            if is_dob_col:
                # Group by birth YEAR — these are historic dates spread over decades
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
                frequencies = periods.value_counts().sort_index().tail(36)
                labels = frequencies.index.tolist()
                pids_per_period = []
                for period in labels:
                    period_indices = periods[periods == period].index
                    pids_per_period.append(row_pids.loc[period_indices].tolist())
                summary_str = (
                    f"{len(date_values):,} dated records · "
                    f"latest {len(frequencies):,} months"
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
                        "values": [int(value) for value in frequencies.tolist()],
                        "pids": pids_per_period,
                    }
                ],
                "summary": summary_str,
            }

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
        }'''

helper_text_chart = '''def _build_text_chart(column, values, row_pids, description=""):
    raw_values = values.astype(str)
    # Detect dates in both YYYY-MM-DD and DD/MM/YYYY (and similar) formats
    looks_like_date = (
        raw_values.str.match(r"^\\d{4}[-/]\\d{1,2}[-/]\\d{1,2}").mean() >= 0.5
        or raw_values.str.match(r"^\\d{1,2}[/-]\\d{1,2}[/-]\\d{4}").mean() >= 0.5
    )
    if looks_like_date:
        try:
            parsed_dates = pd.to_datetime(
                raw_values, errors="coerce", dayfirst=True
            )
        except Exception:
            parsed_dates = pd.to_datetime(raw_values, errors="coerce")
    else:
        parsed_dates = pd.Series(dtype="datetime64[ns]")
    if looks_like_date and parsed_dates.notna().sum() >= len(raw_values) * 0.5:
        date_values = parsed_dates.dropna()
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
            # Group by birth YEAR — these are historic dates spread over decades
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
            frequencies = periods.value_counts().sort_index().tail(36)
            labels = frequencies.index.tolist()
            pids_per_period = []
            for period in labels:
                period_indices = periods[periods == period].index
                pids_per_period.append(row_pids.loc[period_indices].tolist())
            summary_str = (
                f"{len(date_values):,} dated records · "
                f"latest {len(frequencies):,} months"
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
                    "values": [int(value) for value in frequencies.tolist()],
                    "pids": pids_per_period,
                }
            ],
            "summary": summary_str,
        }

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
'''

# Add _build_text_chart before build_single_column_chart
if "def _build_text_chart(" not in app_src:
    target_bscc = "def build_single_column_chart("
    idx = app_src.find(target_bscc)
    if idx != -1:
        app_src = app_src[:idx] + helper_text_chart + "\n\n" + app_src[idx:]
        print("6a. Added _build_text_chart function")

# Replace else branch and numeric branch in build_single_column_chart
old_numeric_head = '''    elif pd.api.types.is_numeric_dtype(values):
        numeric = pd.to_numeric(values, errors="coerce").dropna()
        if numeric.empty:
            return None'''

new_numeric_head = '''    elif pd.api.types.is_numeric_dtype(values):
        numeric = pd.to_numeric(values, errors="coerce")
        non_numeric_count = int(numeric.isna().sum()) - int(values.isna().sum())
        numeric = numeric.dropna()
        if non_numeric_count > max(1, int(0.05 * max(1, len(values)))):
            return _build_text_chart(column, values, row_pids, description)
        if numeric.empty:
            return None'''

if old_numeric_head in app_src:
    app_src = app_src.replace(old_numeric_head, new_numeric_head)
    print("6b. Updated numeric check with non_numeric_count fallback")

new_else_branch = '''    else:
        return _build_text_chart(column, values, row_pids, description)'''

if old_text_branch in app_src:
    app_src = app_src.replace(old_text_branch, new_else_branch)
    print("6c. Replaced else branch with call to _build_text_chart")

# 7. Update dashboard_data to attach display_name and key
old_dash_chart_append = '''            chart = build_single_column_chart(
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
                charts.append(chart)'''

new_dash_chart_append = '''            chart = build_single_column_chart(
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
                display_name = (getattr(dataset, "original_headers", None) or {}).get(column, column)
                chart["key"] = column
                chart["name"] = display_name
                charts.append(chart)'''

if old_dash_chart_append in app_src:
    app_src = app_src.replace(old_dash_chart_append, new_dash_chart_append)
    print("7a. Attached display_name in no-codebook dashboard_data")

# Also for the codebook branch in dashboard_data, ensure key and display_name are set
old_cb_single = '''        column = question_columns[0]
        chart = build_single_column_chart(
            column, frame, variable_lists, response_lists, coded_columns,
            row_pids=row_pids,
        )
        if chart:
            chart["is_grouped"] = False
            chart["is_subcolumn"] = False
            chart["is_base_column"] = False
            charts.append(chart)'''

new_cb_single = '''        column = question_columns[0]
        chart = build_single_column_chart(
            column, frame, variable_lists, response_lists, coded_columns,
            row_pids=row_pids,
        )
        if chart:
            chart["is_grouped"] = False
            chart["is_subcolumn"] = False
            chart["is_base_column"] = False
            display_name = (getattr(dataset, "original_headers", None) or {}).get(column, column)
            chart["key"] = column
            chart["name"] = display_name
            charts.append(chart)'''

if old_cb_single in app_src:
    app_src = app_src.replace(old_cb_single, new_cb_single)
    print("7b. Attached display_name in codebook single-column dashboard_data")

# 8. Call sanitizer in upload() and save original_headers
old_upload_start = '''    try:
        raw = read_upload(file)
        raw_expected = request.form.get("expected_participants", "").strip()'''

new_upload_start = '''    try:
        raw = read_upload(file)
        raw, header_map = sanitize_headers_for_upload(raw)
        if header_map:
            print(f"[upload] sanitized {len(header_map)} headers for labeled export")
        raw_expected = request.form.get("expected_participants", "").strip()'''

if old_upload_start in app_src:
    app_src = app_src.replace(old_upload_start, new_upload_start)
    print("8a. Added sanitize_headers_for_upload call in upload()")

# Save original_headers to dataset
old_create_dataset = '''    dataset = StudyDataset(
        study_name=study_name,
        filename=os.path.basename(file.filename),
        rows=len(cleaned),
        columns=len(cleaned.columns),
        duplicates_removed=duplicates,
        missing_values_filled=missing,
        column_names=list(cleaned.columns),
        records=records,
        expected_participants=expected_participants,
        clean_columns=clean_columns,
        clean_mode=clean_mode,
    )'''

new_create_dataset = '''    dataset = StudyDataset(
        study_name=study_name,
        filename=os.path.basename(file.filename),
        rows=len(cleaned),
        columns=len(cleaned.columns),
        duplicates_removed=duplicates,
        missing_values_filled=missing,
        column_names=list(cleaned.columns),
        records=records,
        expected_participants=expected_participants,
        clean_columns=clean_columns,
        clean_mode=clean_mode,
        original_headers=header_map,
    )'''

if old_create_dataset in app_src:
    app_src = app_src.replace(old_create_dataset, new_create_dataset)
    print("8b. Added original_headers=header_map to StudyDataset instantiation")

# Fallback for clean_columns when clean_mode == "pid"
old_clean_mode_pid = '''    else:  # "pid"
        detected_pid = detect_pid_columns(cleaned.columns)
        clean_columns = [detected_pid[0]] if detected_pid else None'''

new_clean_mode_pid = '''    else:  # "pid"
        detected_pid = detect_pid_columns(cleaned.columns)
        if detected_pid:
            clean_columns = [detected_pid[0]]
        elif "pid" in cleaned.columns:
            clean_columns = ["pid"]
        else:
            clean_columns = None'''

if old_clean_mode_pid in app_src:
    app_src = app_src.replace(old_clean_mode_pid, new_clean_mode_pid)
    print("8c. Added clean_columns fallback to ['pid'] in upload()")

# 9. In dashboard() route: add diagnostic log if not charts
old_dash_route = '''    charts, preview, all_dataset_pids = dashboard_data(dataset, codebook_data)
    chart_sections = group_related_charts(charts)'''

new_dash_route = '''    charts, preview, all_dataset_pids = dashboard_data(dataset, codebook_data)
    if not charts:
        app.logger.warning(
            "Dataset %s produced no charts. columns=%s dtypes=%s",
            dataset.id,
            dataset.column_names[:10],
            {c: type(dataset.records[0].get(c)).__name__ for c in dataset.column_names[:10]}
            if dataset.records else {},
        )
    chart_sections = group_related_charts(charts)'''

if old_dash_route in app_src:
    app_src = app_src.replace(old_dash_route, new_dash_route)
    print("9. Added no-charts diagnostic logging in dashboard() route")

# 10. Database migration in with app.app_context()
old_migration = '''            if "clean_mode" not in existing_cols:
                conn.execute(text("ALTER TABLE study_dataset ADD COLUMN clean_mode VARCHAR(50)"))'''

new_migration = '''            if "clean_mode" not in existing_cols:
                conn.execute(text("ALTER TABLE study_dataset ADD COLUMN clean_mode VARCHAR(50)"))
            if "original_headers" not in existing_cols:
                conn.execute(text("ALTER TABLE study_dataset ADD COLUMN original_headers JSON"))'''

if old_migration in app_src:
    app_src = app_src.replace(old_migration, new_migration)
    print("10. Added original_headers migration block")

# 11. Clean up old debug print lines in upload() if present
app_src = re.sub(r"\s+_dbg_gender\(.*?\)", "", app_src)

# Write back app.py
with open("app.py", "w", encoding="utf-8") as f:
    f.write(app_src)

print("Updated app.py successfully!")

# -----------------------------------------------------------------------------
# Update templates/dashboard.html
# -----------------------------------------------------------------------------
with open("templates/dashboard.html", "r", encoding="utf-8") as f:
    dash_html = f.read()

old_empty_state = '''    {% else %}
      <div class="empty-state"><span>◫</span><strong>No chartable columns found</strong><p>This dataset was cleaned, but it doesn’t contain values that can be visualized.</p></div>
    {% endif %}'''

new_empty_state = '''    {% else %}
      <div class="empty-state">
        <span>◫</span>
        <strong>No chartable columns found</strong>
        <p>This usually means one of:</p>
        <ul style="text-align: left; max-width: 600px; margin: 12px auto; line-height: 1.6; padding-left: 24px;">
          <li>The file has only one column or all columns are empty.</li>
          <li>Every column was recognized as an ID / metadata column and excluded.</li>
          <li>The upload file is in a layout we don't yet parse (e.g., a multi-sheet Excel or RedCap two-row header we couldn't detect).</li>
        </ul>
        <p>Try re-exporting from RedCap as a single-sheet CSV with the "label" and "raw" choices appropriate to your study, then re-upload.</p>
      </div>
    {% endif %}'''

if old_empty_state in dash_html:
    dash_html = dash_html.replace(old_empty_state, new_empty_state)
    print("11. Updated dashboard.html empty-state diagnostic message")

# Also ensure hidden input for codebook mapping uses chart.key or chart.name
old_mapping_input = '<input type="hidden" name="column" value="{{ chart.name }}">'
new_mapping_input = '<input type="hidden" name="column" value="{{ chart.key or chart.name }}">'
if old_mapping_input in dash_html:
    dash_html = dash_html.replace(old_mapping_input, new_mapping_input)
    print("12. Updated mapping form hidden input in dashboard.html")

old_mapping_curr = '{% set current_list = codebook.variables.variable_lists.get(chart.name, chart.response_list or "") %}'
new_mapping_curr = '{% set current_list = codebook.variables.variable_lists.get(chart.key or chart.name, chart.response_list or "") %}'
if old_mapping_curr in dash_html:
    dash_html = dash_html.replace(old_mapping_curr, new_mapping_curr)
    print("13. Updated current_list lookup in dashboard.html")

with open("templates/dashboard.html", "w", encoding="utf-8") as f:
    f.write(dash_html)

print("All updates applied successfully!")

