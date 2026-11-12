# StudyDash

A Flask web app that accepts CSV or Excel study data and an optional matching codebook, removes empty rows, handles missing values, stores all uploaded data rows, and generates a dashboard with summary metrics, recommended charts, and a data preview. Duplicate PID records are identified for review and are never removed automatically. Each study also has a downloadable Excel workbook containing its cleaning summary, codebook, and complete cleaned dataset.

## Run locally on Windows

In PowerShell, from the project folder, run:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

Then open `http://127.0.0.1:5000`. Use the `.venv\Scripts\python.exe` command for both installing packages and running the app; running `python app.py` can select a different Python installation that does not have pandas installed.

To use PostgreSQL, set `DATABASE_URL` in PowerShell before starting the app, for example:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://postgres:your-password@localhost:5432/studydash"
$env:SECRET_KEY = "replace-with-a-private-random-value"
.\.venv\Scripts\python.exe app.py
```

For a quick local preview, the app uses SQLite if `DATABASE_URL` is not set. PostgreSQL is recommended for shared or deployed use. The database tables are created automatically when the app starts.

## Verify the installation

Run the focused upload and dashboard tests with:

```text
.venv\Scripts\python.exe -m unittest -v test_app
.venv\Scripts\python.exe -m pip check
```

## Automatic cleaning, charts, and reports

- Optionally upload a CSV or Excel codebook alongside the study dataset. The reader recognizes common alternatives for response-list/question, code (`name`, `code`, or `value`), English label (`label::English`, `label`, `English`, or `value label`), and Chichewa label columns. English and Chichewa labels are optional; code and a response-list/question column are needed for automatic interpretation. Clear unsupported/ambiguous structure is retained with a dashboard warning and detected header names instead of rejecting the dataset.
- English labels are shown by default, with a dashboard control to switch to Chichewa. Charts, legends, category filters, frequency tables, and mapped preview cells use the selected labels. Use each chart’s corner mapping icon to choose or clear its response list; the existing unresolved-mapping section remains available. A response list is automatically matched only when the dataset column has the same name as the list or an explicit variable mapping is included. Numeric-domain matches are offered for user selection only when the observed values exactly match a list's codes; overlapping values alone are not treated as a mapping.
- The dashboard field search includes a dropdown populated from the fields actually charted for the current dataset. Selecting a field filters the dashboard to that chart; text search can be combined with the selection.
- Upload preserves duplicate records. The dashboard detects PID-like fields (`PID`, participant ID, respondent ID, and similar names); if more than one is found, choose which PID to use. Rows sharing a nonblank PID are split into identical and differing groups, with values shown side by side. You can edit individual cells or explicitly drop a row; edits and drops save immediately.
- Charts for multiple columns mapped to the same response list are grouped together in a collapsed section. Expand the response-list group to view those related field charts; standalone fields remain directly visible.
- Multi-response and categorized survey columns are automatically detected and grouped by their base question stem across varied export formats: delimited (`stem/1`, `stem_1`, `stem.1`, `stem 1`) and direct numeric suffixes (`stem0`, `stem1`, `stem99`, such as `ethnicity0..9`, `water_source0..99`, `what_participant_consented0..8`, `religion1..3`).
- **Grouped by default with full column toggles**:
  - **Charts View Toggle**: Defaults to "Grouped view", presenting clean aggregate multi-response bar charts showing participant selection counts and codebook option labels. Users can click "Show all columns" in the chart toolbar to reveal every single sub-column and base column chart, or click "View individual columns (N)" inline on any grouped card to inspect its child columns.
  - **Table View Toggle**: Data Preview, Missing PID records, and All PID records include a `[ Grouped columns (Default) | Show all columns ]` toggle. In grouped mode, repetitive sub-columns are collapsed to keep wide datasets concise and readable; toggling "Show all columns" immediately reveals every raw sub-column.
  - **Duplicate Review Comparison**: Supports toggling grouped vs all columns when comparing differing duplicate records side-by-side.
- Multi-response option codes map automatically to codebook response choices (including bilingual English and Chichewa labels), even when option codes are formatted as scientific notation (`1E+1`) or decimal integers (`10.0`). Missing cells are preserved and not counted as selections; numeric zero indicates unselected.
- The plain base column is consolidated with its option sub-columns in grouped mode to avoid duplicate redundant charts, but remains fully viewable in all-columns mode.
- **Separate and combined download options**:
  - **Download All (3 Sheets)**: Combined Excel workbook containing the Cleaning Report, Cleaned Data, bilingual Codebook, and Response List mappings.
  - **Cleaned Data Only**: Standalone Excel workbook containing only the cleaned dataset spreadsheet.
  - **Cleaning Report Only**: Standalone Excel workbook containing the cleaning report summary, steps, codebook, and mappings without data rows.
- **Participant Procedure & Activity Tracking (Lilongwe & Salima)**:
  - Interactive tracking section for Lilongwe (`ESD-01`) and Salima (`ESD-02`) study sites.
  - Monitors completion across **Questionnaire**, **Dental examination**, and **Endoscopy (ENDO)** procedures.
  - Visual summary of clinical reasons and non-completion findings (e.g. *Refused further participation*, *High Blood Pressure*, *Esophageal Varices*, *Esophageal Tumor*, *Could not tolerate scope*, etc.) with interactive pill cloud filtering.
  - Real-time text search and status filter buttons (`All`, `ENDO: YES`, `ENDO: NO`, `Pending`, `Has Comment`).
  - Cross-references tracking PIDs against uploaded questionnaire dataset records with `In Dataset` and `Missing in Data` status badges.
  - Allows downloading current tracking sheets as `.xlsx` and uploading updated tracking files anytime.
- PID/submission/instance/device identifiers, administrative metadata, notes/comments, and `other_...` free-text fields are omitted from default charts. The Flask log prints detected question groups and their columns for verification.
- The report workbook includes the cleaning report, cleaned data, bilingual response-list codebook, and saved response-list mappings. Codebooks using the older `Variable`, `Value`, and `Value label` format are still accepted for compatibility.
- Removes completely empty rows and columns. Exact duplicate rows are retained for PID-based review rather than dropped automatically.
- Trims whitespace in column names and supplies names for unnamed columns.
- Makes duplicate column names unique.
- Treats infinite numeric values as missing, then fills continuous numeric missing values with the column median. Text and codebook-coded categorical values use the most common value (or `Unknown` if no value exists). Multiple-response missing values remain missing for accurate option counts.
- Shows one recommended chart per field: histograms for numeric columns, bar charts for categorical columns (including coded responses), and trend lines for date columns grouped by month.
- Groups date values by month for the trend charts and limits the view to the latest 18 months.
- Downloads an Excel workbook with cleaning counts, cleaning steps, every cleaned record, and the uploaded codebook when provided.

The combined upload request (study dataset plus optional codebook) is limited to 25 MB. Uploaded files are processed in memory; cleaned records, codebook labels, and dataset metadata are stored in the configured database.
