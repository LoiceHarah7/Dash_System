# StudyDash Performance & Bottleneck Audit

This document summarizes the performance optimizations implemented for the study dashboard and documents the findings of the full bottleneck audit of `dashboard()` and `dashboard_data()`.

---

## 1. Implemented Optimizations

### A. Dashboard Data Caching (`StudyDataset.charts_cache`)
- Added a nullable `charts_cache` JSON column to `StudyDataset` (with automatic `ALTER TABLE` migration for existing SQLite databases).
- Stores computed dashboard outputs keyed by:
  - `_schema_version` (currently `1`)
  - `_records_len` (`len(dataset.records)`)
  - `_codebook_hash` (MD5 of `codebook.variables`)
  - `_original_headers_hash` (MD5 of `dataset.original_headers`)
  - `_cached_at` (UTC ISO timestamp)
- Cached payload includes: `charts`, `preview`, `all_dataset_pids`, `form_sections`, `chart_sections`, `identical_pid_groups`, `differing_pid_groups`, `missing_pid_rows`, `all_pid_rows`, `pid_sequence_gaps`, `pid_sequence_gap_count`, `unique_participant_count`, and `unique_participant_pids`.
- On cache hit, `dashboard_data()`, `group_related_charts()`, `find_pid_duplicate_groups()`, and `find_pid_sequence_gaps()` are skipped completely.

### B. Preview Trimming (`PREVIEW_ROW_LIMIT = 200`)
- `dashboard_data()` now slices `dataset.records[:PREVIEW_ROW_LIMIT]` instead of serializing all 5,000+ rows into `preview`.
- Prevents multi-megabyte HTML payloads on large studies. Full data remains available in Excel/CSV downloads.

### C. Cache Invalidation & Manual Clear
- `_invalidate_charts_cache(dataset)` sets `dataset.charts_cache = None` in every endpoint that modifies records, codebook mappings, or PID selections:
  - `save_pid_selection`
  - `edit_duplicate_row`
  - `drop_duplicate_row`
  - `save_codebook_mapping`
- Added `POST /studies/<id>/clear-cache` and a **Clear chart cache** button with a confirmation warning on the dashboard.
- Dashboard displays `Charts served from cache (built YYYY-MM-DD HH:MM:SS).` when serving from cache.

### D. Observability
- `dashboard()` logs per-phase timings on cache miss (`dashboard_data`, `group_related_charts`, `form_sections`, `duplicates and pid scans`) and logs `dashboard <id>: cache hit|cache miss (X.XXs)` at `INFO` level on every load.

---

## 2. Bottleneck Audit Findings

### 1. Eager Dual-Rendering in `templates/dashboard.html` (Major Remaining Bottleneck)
- **Observation**: When `has_form_map` is `True`, `dashboard.html` eagerly renders **both** `#columns-view` (iterating `chart_sections`) and `#crfs-view` (iterating `form_sections.items()`). Client-side JS toggles the `hidden` attribute between them.
- **Impact**: Doubles the Jinja template rendering work and HTML payload size for chart cards on studies with a data dictionary.
- **Recommendation**: Render only the active view (`#crfs-view` when `has_form_map` is `True`) server-side, and have JS fetch the alternate view on demand via a lightweight endpoint when the user switches dimensions.

### 2. Per-Load Tracking Queries & `observed_pids_set`
- **Observation**: `dashboard()` runs:
  - `db.session.get(StudyPidSelection, dataset.id)`
  - `db.session.get(StudyCodebook, dataset.id)`
  - `get_or_seed_tracking(dataset.id, "Lilongwe")` and `get_or_seed_tracking(dataset.id, "Salima")`, each issuing a `ParticipantTracking.query` SQL query.
  - Iterates `dataset.records` on every load to construct `observed_pids_set` for tracking summaries.
- **Impact**: On a 5,000-row dataset, building `observed_pids_set` is fast (~3–5ms), and tracking queries are ~100 rows each. However, tracking data is collapsed by default (`#tracking-body` is `hidden`).
- **Recommendation**: Load tracking tables lazily when expanded, or cache `observed_pids_set` inside `charts_cache`.

### 3. Duplicate Review & PID Scans (Option A vs Option B)
- **Observation**: Currently implemented **Option A** — `identical_pid_groups`, `differing_pid_groups`, `missing_pid_rows`, `all_pid_rows`, and `pid_sequence_gaps` are cached inside `charts_cache` alongside chart data.
- **Recommendation (Option B)**: If `charts_cache` JSON size grows too large for datasets with thousands of duplicate rows, move duplicate review computation behind a dedicated `/studies/<id>/duplicates` endpoint and render it on demand.

### 4. `build_single_column_chart()` and `_build_text_chart()`
- **Confirmed**: `dashboard_data()` passes `row_pids` explicitly into `build_single_column_chart()`, so `extract_row_pids()` is **not** rebuilt per column.
- **Confirmed**: `.astype(str)` is called once per column for blank sentinel stripping (`_blank_sentinels`). If a numeric column falls back to `_build_text_chart()`, it returns immediately without re-running categorical logic.
- **Confirmed**: No `.apply()` calls exist in `build_single_column_chart()` or `_build_text_chart()`. `normalize_code` is mapped per-cell only in the codebook-mapped branch.

### 5. `.iterrows()` Audit
- **Confirmed**: Zero `.iterrows()` calls exist in `dashboard()`, `dashboard_data()`, or any chart-building helper. (`.iterrows()` is only used in `parse_codebook()` at upload time.)

### 6. `group_related_charts()` Double Computation
- **Observation**: On cache miss, `group_related_charts()` is called once for `chart_sections` and once per form for `form_sections`.
- **Mitigation**: Both results are cached in `charts_cache`, so the double computation only occurs on cache miss (first load or after invalidation).
