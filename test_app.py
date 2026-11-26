import io
import os
import unittest
from datetime import date
from types import SimpleNamespace

import pandas as pd

os.environ["DATABASE_URL"] = "sqlite://"

from app import (
    StudyPidSelection,
    StudyCodebook,
    StudyDataset,
    app,
    dashboard_data,
    detect_pid_columns,
    duplicate_row_indices,
    db,
    find_pid_duplicate_groups,
    find_pid_sequence_gaps,
    group_question_columns,
)
from openpyxl import Workbook, load_workbook


class StudyDashTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        with app.app_context():
            db.drop_all()
            db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_question_groups_match_actual_survey_columns_without_false_merges(self):
        columns = [
            "houseHold_items/0",
            "houseHold_items/1",
            "houseHold_items/2",
            "houseHold_items/10",
            "water_source",
            "water_source/1",
            "water_source/2",
            "water_source/10",
            "ethnicity/0",
            "ethnicity/1",
            "ethnicity/2",
            "ethnicity/5",
            "age",
            "gender",
            "marital_status",
            "distance_from_school",
            "other_waterSource",
            "other_houseHold_items",
            "Pid_lilongwe",
            "SubmissionDate",
            "instanceID",
            "DeviceID",
            "field_notes",
        ]
        groups = group_question_columns(pd.DataFrame(columns=columns))

        self.assertEqual(
            groups["houseHold_items"],
            [
                "houseHold_items/0",
                "houseHold_items/1",
                "houseHold_items/2",
                "houseHold_items/10",
            ],
        )
        self.assertEqual(
            groups["water_source"],
            ["water_source/1", "water_source/2", "water_source/10"],
        )
        self.assertEqual(
            groups["ethnicity"],
            ["ethnicity/0", "ethnicity/1", "ethnicity/2", "ethnicity/5"],
        )
        for ordinary_column in (
            "age",
            "gender",
            "marital_status",
            "distance_from_school",
        ):
            self.assertEqual(groups[ordinary_column], [ordinary_column])
        for excluded_column in (
            "other_waterSource",
            "other_houseHold_items",
            "Pid_lilongwe",
            "SubmissionDate",
            "instanceID",
            "DeviceID",
            "field_notes",
        ):
            self.assertNotIn(excluded_column, groups)
        self.assertNotIn("water_source/1", groups)

    def test_pid_detection_supports_common_names(self):
        self.assertEqual(
            detect_pid_columns(
                ["age", "PID", "participant_id", "RespondentID", "instanceID"]
            ),
            ["PID", "participant_id", "RespondentID"],
        )

    def test_dashboard_missing_pids_metric_counts_blank_pid_rows(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Missing PID count",
                "file": (
                    io.BytesIO(b"PID,age\nP1,20\n,30\nP3,40\n"),
                    "missing-pid.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Missing PIDs", response.data)
        self.assertIn(b"rows without a PID", response.data)
        self.assertIn(b'id="missing-pids-toggle"', response.data)
        self.assertIn(b'class="metric-label">Missing PIDs</span><strong>1</strong>', response.data)
        self.assertIn(b'id="missing-pid-records" hidden', response.data)
        self.assertIn(b">Rows missing a PID<", response.data)
        self.assertIn(b"<td>2</td>", response.data)
        with app.app_context():
            dataset = db.session.get(StudyDataset, 1)
            self.assertIsNone(dataset.records[1]["PID"])

    def test_missing_pid_metric_counts_only_rows_missing_all_pid_columns(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Regional PID columns",
                "file": (
                    io.BytesIO(
                        b"PID_Lilongwe,PID_Salima,age\n"
                        b"LW-1,,20\n"
                        b",SL-1,30\n"
                        b",,40\n"
                    ),
                    "regional-pids.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'class="metric-label">Missing PIDs</span><strong>1</strong>', response.data)
        self.assertIn(b"Missing across all detected PID fields: PID_Lilongwe, PID_Salima", response.data)
        with app.app_context():
            dataset = db.session.get(StudyDataset, 1)
            self.assertEqual(dataset.records[0]["PID_Lilongwe"], "LW-1")
            self.assertEqual(dataset.records[1]["PID_Salima"], "SL-1")
            self.assertIsNone(dataset.records[2]["PID_Lilongwe"])
            self.assertIsNone(dataset.records[2]["PID_Salima"])

    def test_all_pid_rows_are_viewable_and_naturally_sorted_by_pid_number(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Sorted PID rows",
                "file": (
                    io.BytesIO(
                        b"PID_Lilongwe,PID_Salima,age\n"
                        b"LW-10,,30\n"
                        b",SL-2,20\n"
                        b"LW-2,,25\n"
                        b",,40\n"
                    ),
                    "sorted-regional-pids.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"View all PID rows", response.data)
        self.assertIn(b'id="all-pid-records" hidden', response.data)
        self.assertIn("4 records · sorted by PID number".encode(), response.data)
        table = response.data.split(b'id="all-pid-records"', 1)[1].split(
            b'<section class="duplicate-section"', 1
        )[0]
        self.assertLess(table.index(b"SL-2"), table.index(b"LW-10"))
        self.assertLess(table.index(b"LW-2"), table.index(b"LW-10"))
        self.assertGreater(table.index(b"<td>4</td>"), table.index(b"LW-10"))

    def test_pid_sequence_gap_detection_supports_four_digit_suffixes(self):
        records = [
            {"PID_Lilongwe": "LW-1001", "PID_Salima": None},
            {"PID_Lilongwe": "LW-1003", "PID_Salima": None},
            {"PID_Lilongwe": None, "PID_Salima": "SL-2001"},
            {"PID_Lilongwe": None, "PID_Salima": "SL-2004"},
        ]

        gaps = find_pid_sequence_gaps(
            records, ["PID_Lilongwe", "PID_Salima"]
        )

        self.assertEqual(
            gaps,
            [
                {"column": "PID_Lilongwe", "prefix": "LW-", "missing_ids": ["LW-1002"]},
                {
                    "column": "PID_Salima",
                    "prefix": "SL-",
                    "missing_ids": ["SL-2002", "SL-2003"],
                },
            ],
        )

    def test_dashboard_separates_blank_pid_rows_from_sequence_gaps(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "PID sequence gaps",
                "file": (
                    io.BytesIO(
                        b"PID_Lilongwe,PID_Salima,age\n"
                        b"LW-1001,,20\n"
                        b"LW-1003,,30\n"
                        b",SL-2001,40\n"
                        b",SL-2004,50\n"
                    ),
                    "pid-sequence-gaps.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b'class="metric-label">Missing PIDs</span><strong>0</strong>',
            response.data,
        )
        self.assertIn(
            b"View potential missing PID numbers (3)",
            response.data,
        )
        self.assertIn(b"LW-1002", response.data)
        self.assertIn(b"SL-2002", response.data)
        self.assertIn(b"SL-2003", response.data)

    def test_dashboard_counts_pids_when_every_pid_value_is_missing(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "All PIDs missing",
                "file": (
                    io.BytesIO(b"PID,age\n,20\n,30\n"),
                    "all-pids-missing.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'class="metric-label">Missing PIDs</span><strong>2</strong>', response.data)
        with app.app_context():
            dataset = db.session.get(StudyDataset, 1)
            self.assertIn("PID", dataset.column_names)
            self.assertTrue(all(record["PID"] is None for record in dataset.records))

    def test_multiple_response_chart_counts_selections_and_uses_reliable_labels(self):
        columns = ["water_source/1", "water_source/2", "water_source/10", "age"]
        dataset = SimpleNamespace(
            id=999,
            column_names=columns,
            records=[
                {
                    "water_source/1": 1,
                    "water_source/2": 0,
                    "water_source/10": 0,
                    "age": 20,
                },
                {
                    "water_source/1": 0,
                    "water_source/2": 1,
                    "water_source/10": 0,
                    "age": 30,
                },
                {
                    "water_source/1": None,
                    "water_source/2": None,
                    "water_source/10": None,
                    "age": 40,
                },
            ],
        )
        codebook = {
            "lists": {
                "water_source": {
                    "1": {"en": "Piped water", "ny": "Madzi apampopi"},
                    "2": {"en": "Well", "ny": "Chitsime"},
                    "10": {"en": "River", "ny": "Mtsinje"},
                }
            }
        }

        charts, _, _all_pids = dashboard_data(dataset, codebook)
        question_chart = next(chart for chart in charts if chart["name"] == "water_source")

        self.assertEqual(len(question_chart["variants"]), 1)
        variant = question_chart["variants"][0]
        self.assertEqual(variant["type"], "bar")
        self.assertEqual(
            variant["labels_en"], ["Piped water", "Well", "River"]
        )
        self.assertEqual(variant["values"], [1, 1, 0])
        self.assertIn("2 participants selected 2 options", question_chart["summary"])
        self.assertIn("age", [chart["name"] for chart in charts])

    def test_categorical_chart_type_matches_category_count(self):
        columns = ["two_categories", "four_categories", "six_categories"]
        records = [
            {
                "two_categories": "A" if index % 2 else "B",
                "four_categories": f"Choice {index % 4}",
                "six_categories": f"Choice {index % 6}",
            }
            for index in range(12)
        ]
        dataset = SimpleNamespace(
            id=1000,
            column_names=columns,
            records=records,
        )

        charts, _, _all_pids = dashboard_data(dataset)
        chart_types = {
            chart["name"]: chart["variants"][0]["type"]
            for chart in charts
        }

        self.assertEqual(chart_types["two_categories"], "pie")
        self.assertEqual(chart_types["four_categories"], "donut")
        self.assertEqual(chart_types["six_categories"], "bar")

    def test_upload_preserves_missing_multiple_response_cells_for_counts(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Multiple response cleaning",
                "file": (
                    io.BytesIO(
                        b"water_source/1,water_source/10,age\n"
                        b"1,0,20\n"
                        b",1,30\n"
                        b"0,,40\n"
                    ),
                    "multiple-response.csv",
                ),
                "codebook": (
                    io.BytesIO(
                        b"list_name,name,label::English,label::Chichewa\n"
                        b"unrelated_list,0,No,Ayi\n"
                        b"unrelated_list,1,Yes,Inde\n"
                    ),
                    "codebook.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2 participants selected 2 options", response.data)
        self.assertNotIn(b"Review response-list mappings", response.data)
        self.assertIn(b"Option 1", response.data)
        self.assertIn(b"Option 10", response.data)
        with app.app_context():
            dataset = db.session.query(StudyDataset).one()
            self.assertEqual(dataset.missing_values_filled, 0)
            self.assertIsNone(dataset.records[1]["water_source/1"])
            self.assertIsNone(dataset.records[2]["water_source/10"])

    def test_home_page_renders_upload_form(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Upload study data", response.data)
        self.assertIn(b'id="file-name"', response.data)
        # Saved dashboards section appears ABOVE the upload form
        self.assertIn(b"SAVED DASHBOARDS", response.data)
        self.assertIn(b'id="saved-dashboards"', response.data)
        self.assertIn(b'id="upload-new"', response.data)
        dashboard_pos = response.data.index(b"saved-dashboards")
        upload_pos = response.data.index(b"upload-new")
        self.assertLess(dashboard_pos, upload_pos, "Saved dashboards section must appear before the upload form")

        script_response = self.client.get("/static/upload.js")
        self.assertEqual(script_response.status_code, 200)
        self.assertIn(b"fileInput.addEventListener", script_response.data)
        self.assertIn(b"codebookInput.addEventListener", script_response.data)
        script_response.close()

    def test_save_copy_banner_shown_on_new_upload_and_save_path_persisted(self):
        # Upload a dataset — redirect includes ?new_upload=1
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Path test study",
                "file": (io.BytesIO(b"PID,score\nP1,10\nP2,20\n"), "scores.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)
        location = response.headers.get("Location", "")
        self.assertIn("new_upload=1", location)

        # Follow the redirect — save-copy banner must be present
        response = self.client.get(location, follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"save-copy-banner", response.data)
        self.assertIn(b"Save a copy so you can find it later", response.data)

        # Save a path
        response = self.client.post(
            "/studies/1/save-path",
            data={"saved_path": r"C:\Users\researcher\data\scores.csv"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"File path saved", response.data)

        # Verify path is stored in DB
        with app.app_context():
            dataset = db.session.get(StudyDataset, 1)
            self.assertEqual(dataset.saved_path, r"C:\Users\researcher\data\scores.csv")

        # Home page shows the path in the saved-dashboards list
        home = self.client.get("/")
        self.assertIn(b"scores.csv", home.data)
        self.assertIn(b"study-icon-saved", home.data)

        # Clear the path
        response = self.client.post(
            "/studies/1/save-path",
            data={"saved_path": ""},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            dataset = db.session.get(StudyDataset, 1)
            self.assertIsNone(dataset.saved_path)

    def test_csv_upload_cleans_data_and_renders_dashboard(self):
        csv_data = (
            b"age,group,empty\n"
            b"20,Alpha,\n"
            b",Alpha,\n"
            b"30,Beta,\n"
            b"30,Beta,\n"
        )

        response = self.client.post(
            "/upload",
            data={
                "study_name": "Community survey",
                "file": (io.BytesIO(csv_data), "survey.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"cleaned and its dashboard is ready", response.data)
        self.assertIn(b"Records", response.data)
        self.assertIn(b"Missing PIDs", response.data)
        self.assertIn(b"<strong>4</strong>", response.data)
        self.assertIn(b"30.0", response.data)
        self.assertIn(b"Alpha", response.data)
        self.assertIn(b"Histogram", response.data)
        self.assertIn(b"Pie chart", response.data)
        self.assertIn(b'id="chart-search-input"', response.data)
        self.assertIn(b'id="chart-field-select"', response.data)
        self.assertIn(b'<option value="">All fields</option>', response.data)
        self.assertIn(b"Search graphs by name", response.data)
        self.assertNotIn(b"Box plot", response.data)
        self.assertNotIn(b"Donut chart", response.data)
        self.assertEqual(response.data.count(b'class="chart-visual"'), 2)
        self.assertIn(b"Download cleaning report", response.data)

        with app.app_context():
            dataset = db.session.query(StudyDataset).one()
            self.assertEqual(dataset.rows, 4)
            self.assertEqual(dataset.columns, 2)
            self.assertEqual(dataset.duplicates_removed, 0)
            self.assertEqual(dataset.missing_values_filled, 1)
            self.assertEqual(dataset.records[1]["age"], 30.0)
            self.assertEqual(dataset.records[2], dataset.records[3])

    def test_cleaning_report_download_contains_report_and_cleaned_data(self):
        self.client.post(
            "/upload",
            data={
                "study_name": "Report survey",
                "file": (
                    io.BytesIO(b"score,group\n10,Control\n,Control\n20,Treatment\n"),
                    "report.csv",
                ),
            },
            content_type="multipart/form-data",
        )

        response = self.client.get("/studies/1/report")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.mimetype,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        workbook = load_workbook(io.BytesIO(response.data), read_only=True)
        self.assertEqual(workbook.sheetnames, ["Cleaning report", "Cleaned data"])
        report_rows = list(workbook["Cleaning report"].values)
        self.assertIn(("Duplicate rows removed automatically", 0), report_rows)
        self.assertIn(("Missing values filled", 1), report_rows)
        cleaned_rows = list(workbook["Cleaned data"].values)
        self.assertEqual(cleaned_rows[0], ("score", "group"))
        self.assertEqual(cleaned_rows[2], (15, "Control"))
        workbook.close()
        response.close()

    def test_pid_duplicate_review_classifies_identical_and_differing_rows(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "PID duplicates",
                "file": (
                    io.BytesIO(
                        b"PID,age,group\n"
                        b"P1,20,A\n"
                        b"P1,20,A\n"
                        b"P2,30,B\n"
                        b"P2,31,B\n"
                    ),
                    "pid-survey.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"PID duplicate review", response.data)
        self.assertIn(b"Identical rows with matching PIDs", response.data)
        self.assertIn(b"Matching PID values with different answers", response.data)
        self.assertIn(b"PID P1", response.data)
        self.assertIn(b"PID P2", response.data)
        self.assertIn(b"duplicate-comparison", response.data)
        self.assertIn(b'class="duplicate-different-cell"', response.data)
        with app.app_context():
            dataset = db.session.get(StudyDataset, 1)
            self.assertEqual(dataset.rows, 4)
            self.assertEqual(dataset.duplicates_removed, 0)
            self.assertEqual(
                db.session.get(StudyPidSelection, dataset.id).pid_column, "PID"
            )
            identical, differing = find_pid_duplicate_groups(dataset, "PID")
            self.assertEqual(len(identical), 1)
            self.assertEqual(len(differing), 1)

    def test_regional_pid_columns_do_not_hide_salima_duplicate_answer_differences(self):
        dataset = SimpleNamespace(
            column_names=["Pid_lilongwe", "pid_Salima", "answer", "age"],
            records=[
                {
                    "Pid_lilongwe": "LW-1001",
                    "pid_Salima": "SL-2001",
                    "answer": "yes",
                    "age": 20,
                },
                {
                    "Pid_lilongwe": "LW-1002",
                    "pid_Salima": "SL-2001",
                    "answer": "no",
                    "age": 20,
                },
            ],
        )

        identical, differing = find_pid_duplicate_groups(dataset, "pid_Salima")

        self.assertEqual(identical, [])
        self.assertEqual(len(differing), 1)
        self.assertEqual(differing[0]["pid"], "SL-2001")
        self.assertEqual(differing[0]["different_columns"], ["answer"])

    def test_pid_duplicate_review_supports_ambiguous_pid_selection_edit_and_drop(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Select PID",
                "file": (
                    io.BytesIO(
                        b"PID,participant_id,value\n"
                        b"A,A,one\n"
                        b"A,A,two\n"
                    ),
                    "ambiguous-pid.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a PID field", response.data)

        response = self.client.post(
            "/studies/1/pid-selection",
            data={"pid_column": "PID"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Matching PID values with different answers", response.data)

        response = self.client.post(
            "/studies/1/duplicate-row/edit",
            data={"row_index": "1", "column": "value", "value": "updated"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            dataset = db.session.get(StudyDataset, 1)
            self.assertEqual(dataset.records[1]["value"], "updated")

        response = self.client.post(
            "/studies/1/duplicate-row/drop",
            data={"row_index": "1"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            dataset = db.session.get(StudyDataset, 1)
            self.assertEqual(dataset.rows, 1)
            self.assertEqual(len(dataset.records), 1)

    def test_date_column_gets_line_and_monthly_bar_charts(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Monthly visits",
                "file": (
                    io.BytesIO(
                        b"visit_date\n2025-01-03\n2025-01-15\n2025-02-02\n"
                    ),
                    "visits.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Trend line", response.data)
        self.assertNotIn(b"Monthly bars", response.data)
        self.assertEqual(response.data.count(b'class="chart-visual"'), 1)

    def test_kobo_codebook_labels_single_chart_per_field_and_report(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Labeled survey",
                "file": (
                    io.BytesIO(b"sex,score\n1,10\n2,14\n,20\n"),
                    "survey.csv",
                ),
                "codebook": (
                    io.BytesIO(
                        "list_name,name,label::English,label::Chichewa\n"
                        "sex,1,Male,Mwamuna\n"
                        "sex,2,Female,Mkazi\n".encode("utf-8")
                    ),
                    "survey-codebook.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Codebook: survey-codebook.csv", response.data)
        self.assertIn(b"Male", response.data)
        self.assertIn(b"Female", response.data)
        self.assertIn(b"Mwamuna", response.data)
        self.assertIn(b'id="label-language"', response.data)
        self.assertIn(b'Map sex to a codebook response list', response.data)
        self.assertEqual(response.data.count(b'class="chart-mapping"'), 2)
        self.assertIn(b'data-chart-host="0-0"', response.data)
        self.assertNotIn(b'data-chart-host="0-1"', response.data)
        self.assertNotIn(b'data-chart-host="0-2"', response.data)
        self.assertEqual(response.data.count(b'class="chart-visual"'), 2)
        self.assertIn(b'data-label-column="sex"', response.data)
        self.assertIn(b"Response frequencies", response.data)

        with app.app_context():
            codebook = db.session.get(StudyCodebook, 1)
            self.assertEqual(codebook.filename, "survey-codebook.csv")
            self.assertEqual(
                codebook.variables["lists"]["sex"]["1"],
                {"en": "Male", "ny": "Mwamuna"},
            )
            self.assertEqual(
                codebook.variables["variable_lists"]["sex"], "sex"
            )
            dataset = db.session.get(StudyDataset, 1)
            self.assertEqual(dataset.records[2]["sex"], 1)
            self.assertEqual(dataset.missing_values_filled, 1)

        report_response = self.client.get("/studies/1/report")
        workbook = load_workbook(io.BytesIO(report_response.data), read_only=True)
        self.assertEqual(
            workbook.sheetnames,
            [
                "Cleaning report",
                "Cleaned data",
                "Codebook",
                "Response list mappings",
            ],
        )
        codebook_rows = list(workbook["Codebook"].values)
        self.assertEqual(
            codebook_rows[0],
            (
                "list_name",
                "name",
                "label::English",
                "label::Chichewa",
                "matched dataset variables",
            ),
        )
        self.assertIn(("sex", "1", "Male", "Mwamuna", "sex"), codebook_rows)
        workbook.close()
        report_response.close()

    def test_three_column_dental_codebook_uses_alternate_headers(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Dental survey",
                "file": (
                    io.BytesIO(b"tooth_condition\n1\n2\n1\n"),
                    "dental.csv",
                ),
                "codebook": (
                    io.BytesIO(
                        b"Variable,Code,English\n"
                        b"tooth_condition,1,Healthy\n"
                        b"tooth_condition,2,Needs treatment\n"
                    ),
                    "dental-codebook.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Healthy", response.data)
        self.assertIn(b"Needs treatment", response.data)
        self.assertNotIn(b"Review codebook structure", response.data)
        with app.app_context():
            codebook = db.session.get(StudyCodebook, 1)
            self.assertEqual(
                codebook.variables["lists"]["tooth_condition"]["1"],
                {"en": "Healthy"},
            )
            self.assertEqual(codebook.variables["variable_lists"]["tooth_condition"], "tooth_condition")

    def test_codebook_without_labels_keeps_codes_for_chart_fallback(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Code-only dental survey",
                "file": (
                    io.BytesIO(b"tooth_condition\n1\n2\n1\n"),
                    "dental.csv",
                ),
                "codebook": (
                    io.BytesIO(b"Variable,Code\ntooth_condition,1\ntooth_condition,2\n"),
                    "dental-codebook.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No label column was identified", response.data)
        self.assertIn(b"1", response.data)
        self.assertIn(b"2", response.data)
        with app.app_context():
            codebook = db.session.get(StudyCodebook, 1)
            self.assertEqual(
                codebook.variables["lists"]["tooth_condition"]["1"], {}
            )

    def test_three_column_codebook_labels_multiple_response_chart(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Dental practices",
                "file": (
                    io.BytesIO(
                        b"dental_practices/1,dental_practices/3\n"
                        b"1,0\n"
                        b"0,1\n"
                        b"1,1\n"
                    ),
                    "dental-practices.csv",
                ),
                "codebook": (
                    io.BytesIO(
                        b"list_name,code,label\n"
                        b"dental_practices,1,Brushes teeth\n"
                        b"dental_practices,3,Uses fluoride\n"
                    ),
                    "dental-codebook.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Brushes teeth", response.data)
        self.assertIn(b"Uses fluoride", response.data)
        self.assertIn(b"3 participants selected 4 options", response.data)
        with app.app_context():
            codebook = db.session.get(StudyCodebook, 1)
            self.assertEqual(
                codebook.variables["lists"]["dental_practices"]["3"],
                {"en": "Uses fluoride"},
            )

    def test_ambiguous_codebook_structure_is_saved_with_review_warning(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Unrecognized codebook",
                "file": (io.BytesIO(b"sex\n1\n2\n"), "survey.csv"),
                "codebook": (
                    io.BytesIO(b"question,display text\nsex,Participant sex\n"),
                    "unrecognized.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Review codebook structure", response.data)
        self.assertIn(b"Could not identify a code column", response.data)
        self.assertIn(b"question, display text", response.data)
        with app.app_context():
            self.assertEqual(db.session.query(StudyDataset).count(), 1)
            codebook = db.session.get(StudyCodebook, 1)
            self.assertEqual(codebook.variables["lists"], {})

    def test_ambiguous_kobo_lists_require_user_mapping(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Ambiguous response lists",
                "file": (
                    io.BytesIO(b"answer\n0\n1\n0\n"),
                    "answers.csv",
                ),
                "codebook": (
                    io.BytesIO(
                        "list_name,name,label::English,label::Chichewa\n"
                        "gender,0,Female,Mkazi\n"
                        "gender,1,Male,Mwamuna\n"
                        "consent,0,No,Ayi\n"
                        "consent,1,Yes,Inde\n".encode("utf-8")
                    ),
                    "choices.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose the correct list", response.data)
        self.assertIn(b'<details class="codebook-notice">', response.data)
        self.assertIn(b"Review response-list mappings", response.data)
        self.assertIn(b'value="gender"', response.data)
        self.assertIn(b'value="consent"', response.data)
        self.assertIn(b'"kind": "unmapped"', response.data)

        response = self.client.post(
            "/studies/1/codebook-mapping",
            data={"column": "answer", "list_name": "consent"},
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Yes", response.data)
        self.assertIn(b"Ayi", response.data)
        self.assertIn(b"Response frequencies", response.data)
        with app.app_context():
            codebook = db.session.get(StudyCodebook, 1)
            self.assertEqual(codebook.variables["variable_lists"]["answer"], "consent")
            self.assertEqual(codebook.unmatched_variables, [])

        response = self.client.post(
            "/studies/1/codebook-mapping",
            data={"column": "answer", "list_name": ""},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Review response-list mappings", response.data)
        with app.app_context():
            codebook = db.session.get(StudyCodebook, 1)
            self.assertNotIn("answer", codebook.variables["variable_lists"])
            self.assertEqual(codebook.unmatched_variables, ["answer"])

    def test_numeric_measurement_is_not_matched_by_partial_code_overlap(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Age measurement",
                "file": (
                    io.BytesIO(b"age\n20\n30\n40\n"),
                    "ages.csv",
                ),
                "codebook": (
                    io.BytesIO(
                        b"list_name,name,label::English,label::Chichewa\n"
                        b"rating,1,Low,Low\n"
                        b"rating,2,Medium,Medium\n"
                        b"rating,3,High,High\n"
                    ),
                    "rating-codebook.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Histogram", response.data)
        self.assertNotIn(b"Apply response list", response.data)
        with app.app_context():
            codebook = db.session.get(StudyCodebook, 1)
            self.assertEqual(codebook.unmatched_variables, [])

    def test_fields_sharing_response_list_are_grouped_and_collapsed(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Grouped responses",
                "file": (
                    io.BytesIO(
                        b"first_choice,second_choice\n1,2\n2,1\n1,1\n"
                    ),
                    "responses.csv",
                ),
                "codebook": (
                    io.BytesIO(
                        "list_name,name,label::English,label::Chichewa,variable\n"
                        "yes_no,1,Yes,Inde,first_choice\n"
                        "yes_no,2,No,Ayi,first_choice\n"
                        "yes_no,1,Yes,Inde,second_choice\n"
                        "yes_no,2,No,Ayi,second_choice\n".encode("utf-8")
                    ),
                    "responses-codebook.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<details class="related-chart-group">', response.data)
        self.assertIn(b"Response list: yes_no", response.data)
        self.assertIn(b"2 related fields", response.data)
        self.assertNotIn(b'<details class="related-chart-group" open', response.data)
        self.assertIn(b"<h3>first_choice</h3>", response.data)
        self.assertIn(b"<h3>second_choice</h3>", response.data)

    def test_excel_upload_serializes_dates(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["visit_date", "score"])
        sheet.append([date(2025, 1, 2), 8])
        content = io.BytesIO()
        workbook.save(content)
        content.seek(0)

        response = self.client.post(
            "/upload",
            data={
                "study_name": "Follow-up visits",
                "file": (content, "visits.xlsx"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2025-01-02", response.data)

    def test_unsupported_file_type_shows_validation_message(self):
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Unsupported study",
                "file": (io.BytesIO(b"data"), "notes.txt"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Upload a CSV or Excel file", response.data)

    def test_group_question_columns_with_numeric_suffixes_and_delimiters(self):
        cols = [
            "what_participant_consented",
            "what_participant_consented0",
            "what_participant_consented1",
            "what_participant_consented2",
            "ethnicity",
            "ethnicity0",
            "ethnicity1",
            "water_source",
            "water_source0",
            "water_source10",
            "water_source99",
            "religion_1",
            "religion_2",
            "religion_3",
            "gender",
            "age",
            "liters_Ofchibuku",
            "liters_Ofchibuku2",
        ]
        groups = group_question_columns(pd.DataFrame(columns=cols))

        self.assertEqual(
            groups["what_participant_consented"],
            [
                "what_participant_consented0",
                "what_participant_consented1",
                "what_participant_consented2",
            ],
        )
        self.assertEqual(
            groups["ethnicity"],
            ["ethnicity0", "ethnicity1"],
        )
        self.assertEqual(
            groups["water_source"],
            ["water_source0", "water_source10", "water_source99"],
        )
        self.assertEqual(
            groups["religion"],
            ["religion_1", "religion_2", "religion_3"],
        )
        self.assertEqual(groups["gender"], ["gender"])
        self.assertEqual(groups["age"], ["age"])
        self.assertEqual(groups["liters_Ofchibuku"], ["liters_Ofchibuku"])
        self.assertEqual(groups["liters_Ofchibuku2"], ["liters_Ofchibuku2"])

    def test_dashboard_renders_grouping_toggles_and_subcolumns(self):
        csv_data = (
            "PID,gender,what_participant_consented,what_participant_consented0,what_participant_consented1\n"
            "P01,0,0 1,1,1\n"
            "P02,1,0,1,0\n"
            "P03,0,1,0,1\n"
        )
        codebook_data = (
            "list_name,name,label::English,label::Chichewa,matched dataset variables\n"
            "what_participant_consented,0,Interview,Kufunsidwa mafunso,what_participant_consented\n"
            "what_participant_consented,1,Urine sample,Kupereka Mkodzo,what_participant_consented\n"
            "gender,0,Male,Mwamuna,gender\n"
            "gender,1,Female,Mkazi,gender\n"
        )
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Multi-category grouping study",
                "file": (io.BytesIO(csv_data.encode("utf-8")), "study.csv"),
                "codebook": (
                    io.BytesIO(codebook_data.encode("utf-8")),
                    "codebook.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Grouped view (Default)", response.data)
        self.assertIn(b"Show all columns", response.data)
        self.assertIn(b"View individual columns (2)", response.data)
        self.assertIn(b"col-subcolumn", response.data)
        self.assertIn(b"view-grouped", response.data)
        self.assertIn(b"Interview", response.data)
        self.assertIn(b"Urine sample", response.data)

    def test_user_study_dataset_with_codebook_and_grouping(self):
        csv_data = (
            "SubmissionDate\tcentre\tPid_lilongwe\tpid_Salima\twhat_participant_consented\twhat_participant_consented0\twhat_participant_consented1\twhat_participant_consented2\twhat_participant_consented8\tethnicity\tethnicity0\tethnicity1\twater_source\twater_source0\twater_source10\twater_source99\n"
            "2024-03-14\t0\tESD-01-0001\t\t0 1 2 8\t1\t1\t1\t1\t0\t1\t0\t6\t0\t0\t0\n"
            "2024-03-19\t0\tESD-01-0002\t\t0 1\t1\t1\t0\t0\t1\t0\t1\t10\t0\t1\t0\n"
        )
        codebook_data = (
            "list_name\tname\tlabel::English\tlabel::Chichewa\tmatched dataset variables\n"
            "what_participant_consented\t0\tInterview\tKufunsidwa mafunso\twhat_participant_consented\n"
            "what_participant_consented\t1\tUrine sample\tKupereka Mkodzo\twhat_participant_consented\n"
            "what_participant_consented\t2\tBlood sample\tZoyesa za magazi\twhat_participant_consented\n"
            "what_participant_consented\t8\tCytology examination\tKutenga zoyesa\twhat_participant_consented\n"
            "ethnicity\t0\tChewa\tChewa\tethnicity\n"
            "ethnicity\t1\tNgoni\tNgoni\tethnicity\n"
            "water_source\t0\tPiped water into house\tMadzi apa mpopi\twater_source\n"
            "water_source\t1E+1\tLake/pond\tAmnyanja\twater_source\n"
            "water_source\t99\tOther\tZina\twater_source\n"
        )
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Full Survey Grouping Test",
                "file": (io.BytesIO(csv_data.encode("utf-8")), "study.csv"),
                "codebook": (
                    io.BytesIO(codebook_data.encode("utf-8")),
                    "codebook.csv",
                ),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Interview", response.data)
        self.assertIn(b"Urine sample", response.data)
        self.assertIn(b"Cytology examination", response.data)
        self.assertIn(b"Chewa", response.data)
        self.assertIn(b"Lake/pond", response.data)
        self.assertIn(b"ESD-01-0001", response.data)
        self.assertIn(b"Grouped view (Default)", response.data)
        self.assertIn(b"Show all columns", response.data)
        self.assertIn(b"View individual columns (4)", response.data)
        self.assertIn(b"View individual columns (2)", response.data)
        self.assertIn(b"View individual columns (3)", response.data)

    def test_separate_downloads_for_cleaned_data_report_and_combined_all(self):
        csv_data = b"PID,score,category\nP1,10,A\nP2,20,B\n"
        self.client.post(
            "/upload",
            data={
                "study_name": "Separate Download Test",
                "file": (io.BytesIO(csv_data), "data.csv"),
            },
            content_type="multipart/form-data",
        )

        # 1. Download all together
        resp_all = self.client.get("/studies/1/download/all")
        self.assertEqual(resp_all.status_code, 200)
        wb_all = load_workbook(io.BytesIO(resp_all.data), read_only=True)
        self.assertIn("Cleaning report", wb_all.sheetnames)
        self.assertIn("Cleaned data", wb_all.sheetnames)
        wb_all.close()

        # 2. Download cleaned data only
        resp_data = self.client.get("/studies/1/download/cleaned-data")
        self.assertEqual(resp_data.status_code, 200)
        wb_data = load_workbook(io.BytesIO(resp_data.data), read_only=True)
        self.assertEqual(wb_data.sheetnames, ["Cleaned data"])
        data_rows = list(wb_data["Cleaned data"].values)
        self.assertEqual(data_rows[0], ("PID", "score", "category"))
        self.assertEqual(data_rows[1], ("P1", 10, "A"))
        wb_data.close()

        # 3. Download cleaning report only
        resp_report = self.client.get("/studies/1/download/cleaning-report")
        self.assertEqual(resp_report.status_code, 200)
        wb_rep = load_workbook(io.BytesIO(resp_report.data), read_only=True)
        self.assertEqual(wb_rep.sheetnames, ["Cleaning report"])
        report_rows = list(wb_rep["Cleaning report"].values)
        self.assertIn(("Study", "Separate Download Test"), report_rows)
        wb_rep.close()

    def test_procedure_tracking_for_lilongwe_and_salima_with_upload_and_download(self):
        csv_data = b"Pid_lilongwe,age\nESD-01-0001,50\nESD-01-0013,60\n"
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Procedure Tracking Test",
                "file": (io.BytesIO(csv_data), "tracking_test.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        # Check download buttons on dashboard
        self.assertIn(b"Download All (3 Sheets)", response.data)
        self.assertIn(b"Cleaned Data", response.data)
        self.assertIn(b"Report Only", response.data)

        # Check tracking section presence (starts empty until user uploads a tracker)
        self.assertIn(b"Participant Procedure Tracking", response.data)
        self.assertIn(b"Lilongwe Site (ESD-01)", response.data)
        self.assertIn(b"Salima Site (ESD-02)", response.data)
        self.assertIn(b"0 tracked", response.data)

        # Upload tracking sheet for Lilongwe
        new_lilongwe_csv = (
            b"PID,QUESTIONEER,DENTAL,ENDO YES/NO,ENDO DATE,COMMENT\n"
            b"ESD-01-0001,01/03/2024,01/03/2024,NO,,Could not tolerate scope\n"
            b"ESD-01-0002,02/03/2024,02/03/2024,NO,,Refused further participation\n"
        )
        resp_lilongwe = self.client.post(
            "/studies/1/tracking/upload",
            data={
                "site": "Lilongwe",
                "tracking_file": (io.BytesIO(new_lilongwe_csv), "lilongwe_track.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp_lilongwe.status_code, 200)
        self.assertIn(b"ESD-01-0001", resp_lilongwe.data)
        self.assertIn(b"Could not tolerate scope", resp_lilongwe.data)
        self.assertIn(b"Refused further participation", resp_lilongwe.data)

        # Download tracking sheet for Lilongwe
        resp_track = self.client.get("/studies/1/tracking/download/lilongwe")
        self.assertEqual(resp_track.status_code, 200)
        wb_track = load_workbook(io.BytesIO(resp_track.data), read_only=True)
        self.assertIn("Lilongwe tracking", wb_track.sheetnames)
        sheet_rows = list(wb_track["Lilongwe tracking"].values)
        self.assertEqual(
            sheet_rows[0],
            ("PID", "QUESTIONEER", "DENTAL", "ENDO YES/NO", "ENDO DATE", "COMMENT"),
        )
        wb_track.close()

        # Upload updated tracking sheet for Salima
        new_salima_csv = (
            b"PID,QUESTIONEER,DENTAL,ENDO YES/NO,ENDO DATE,COMMENT\n"
            b"ESD-02-0001,01/03/2024,01/03/2024,YES,02-Mar-24,\n"
            b"ESD-02-0002,01/03/2024,01/03/2024,NO,,High Blood Pressure\n"
        )
        resp_upload = self.client.post(
            "/studies/1/tracking/upload",
            data={
                "site": "Salima",
                "tracking_file": (io.BytesIO(new_salima_csv), "salima_track.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp_upload.status_code, 200)
        self.assertIn(b"Successfully updated 2 tracking records for Salima", resp_upload.data)
        self.assertIn(b"High Blood Pressure", resp_upload.data)

    def test_study_folder_created_on_upload_and_delete_study(self):
        # 1. Upload study
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Delete Folder Test Study",
                "file": (io.BytesIO(b"PID,score\nP1,10\nP2,20\n"), "scores.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            dataset = StudyDataset.query.filter_by(study_name="Delete Folder Test Study").first()
            self.assertIsNotNone(dataset)
            self.assertIsNotNone(dataset.saved_path)
            self.assertTrue(os.path.isdir(dataset.saved_path))
            dataset_id = dataset.id

        # 2. Delete study
        resp_del = self.client.post(
            f"/studies/{dataset_id}/delete",
            follow_redirects=True,
        )
        self.assertEqual(resp_del.status_code, 200)
        self.assertIn(b"deleted", resp_del.data)

        with app.app_context():
            deleted = db.session.get(StudyDataset, dataset_id)
            self.assertIsNone(deleted)

    def test_clear_all_history_removes_all_studies(self):
        # Upload two studies
        self.client.post(
            "/upload",
            data={
                "study_name": "Study Alpha",
                "file": (io.BytesIO(b"PID,val\nA1,1\n"), "a.csv"),
            },
            content_type="multipart/form-data",
        )
        self.client.post(
            "/upload",
            data={
                "study_name": "Study Beta",
                "file": (io.BytesIO(b"PID,val\nB1,2\n"), "b.csv"),
            },
            content_type="multipart/form-data",
        )

        with app.app_context():
            count_before = StudyDataset.query.count()
            self.assertGreaterEqual(count_before, 2)

        # Clear history
        resp_clear = self.client.post("/studies/clear-history", follow_redirects=True)
        self.assertEqual(resp_clear.status_code, 200)
        self.assertIn(b"All study history and dashboards have been cleared", resp_clear.data)

        with app.app_context():
            self.assertEqual(StudyDataset.query.count(), 0)

    def test_tracking_section_is_minimised_initially_on_dashboard(self):
        self.client.post(
            "/upload",
            data={
                "study_name": "Tracking Minimise Test",
                "file": (io.BytesIO(b"PID,val\nT1,100\n"), "track_test.csv"),
            },
            content_type="multipart/form-data",
        )
        with app.app_context():
            dataset = StudyDataset.query.filter_by(study_name="Tracking Minimise Test").first()
            dash_url = f"/studies/{dataset.id}"

        resp = self.client.get(dash_url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="tracking-body" hidden', resp.data)
        self.assertIn(b'aria-expanded="false"', resp.data)
        self.assertIn(b"Expand Tracking", resp.data)

    def test_chart_snapshot_button_and_all_categories_retained(self):
        # Create a dataset with 15 distinct categories in a column
        rows = ["PID,category"]
        for i in range(15):
            rows.append(f"P{i},Category_{i:02d}")
        csv_data = "\n".join(rows).encode("utf-8")

        self.client.post(
            "/upload",
            data={
                "study_name": "Snapshot & Categories Test",
                "file": (io.BytesIO(csv_data), "cats.csv"),
            },
            content_type="multipart/form-data",
        )
        with app.app_context():
            dataset = StudyDataset.query.filter_by(study_name="Snapshot & Categories Test").first()
            charts, preview, pids = dashboard_data(dataset)
            cat_chart = next(c for c in charts if c["name"] == "category")
            # All 15 categories must be retained in the variant payload
            self.assertEqual(len(cat_chart["variants"][0]["labels"]), 15)
            self.assertIn("showing first 10 of 15", cat_chart["summary"])

            # Also dashboard HTML must render snapshot button and toggle limit button
            resp = self.client.get(f"/studies/{dataset.id}")
            self.assertEqual(resp.status_code, 200)
            self.assertIn(b'data-snapshot-chart="', resp.data)
            self.assertIn(b"Snapshot", resp.data)
            self.assertIn(b'data-toggle-chart-limit="', resp.data)

    def test_inspect_headers_endpoint(self):
        csv_bytes = b"PID,Visit_Number,Instance_ID,Survey_Score,Gender\n1,1,1,10,M\n"
        response = self.client.post(
            "/api/inspect-headers",
            data={"file": (io.BytesIO(csv_bytes), "sample.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        json_data = response.get_json()
        self.assertIn("columns", json_data)
        self.assertEqual(
            json_data["columns"],
            ["PID", "Visit_Number", "Instance_ID", "Survey_Score", "Gender"],
        )
        self.assertIn("display_names", json_data)
        self.assertEqual(
            json_data["display_names"],
            ["PID", "Visit_Number", "Instance_ID", "Survey_Score", "Gender"],
        )

    def test_inspect_headers_labeled_export_returns_display_names(self):
        csv_bytes = (
            b'"Breast Cancer Cohort (BCC) Patient ID","Repeat Instrument",'
            b'"Gender","First Name"\n'
            b'BCC-001,,Female,Mary\n'
        )
        response = self.client.post(
            "/api/inspect-headers",
            data={"file": (io.BytesIO(csv_bytes), "bcc.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        json_data = response.get_json()
        # Sanitized machine names for form submission
        self.assertEqual(json_data["columns"][0], "pid")
        self.assertIn("repeat_instrument", json_data["columns"])
        # Original display labels for UI — the first column was renamed to
        # pid internally, but display_names preserves the original label
        self.assertEqual(
            json_data["display_names"][0],
            "Breast Cancer Cohort (BCC) Patient ID",
        )
        self.assertIn("Repeat Instrument", json_data["display_names"])

    def test_csv_with_internal_tab_in_header_preserves_comma_delimiter(self):
        csv_bytes = (
            b'"Breast Cancer Cohort (BCC) Patient ID","Repeat Instrument","Repeat Instance",'
            b'"Q1. Lack of energy.\tNdilibe mphamvu.","Q2. Nausea.\tNdili ndi mseru."\n'
            b'BCC-001,Visit1,1,1,2\n'
        )
        response = self.client.post(
            "/api/inspect-headers",
            data={"file": (io.BytesIO(csv_bytes), "bcc_tabs.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        json_data = response.get_json()
        self.assertEqual(len(json_data["columns"]), 5)
        self.assertEqual(json_data["columns"][0], "pid")
        self.assertEqual(json_data["columns"][1], "repeat_instrument")
        self.assertEqual(json_data["columns"][2], "repeat_instance")
        self.assertEqual(json_data["display_names"][0], "Breast Cancer Cohort (BCC) Patient ID")
        self.assertEqual(json_data["display_names"][1], "Repeat Instrument")
        self.assertEqual(json_data["display_names"][2], "Repeat Instance")

    def test_first_three_columns_clean_mode_and_expected_participants(self):
        # Study where one PID has multiple visits/instances.
        # Repeating PID across different visits is NOT duplicate;
        # only matching (PID, visit, instance) is duplicate.
        csv_data = (
            b"PID,visit,instance,score,group\n"
            b"P101,1,1,80,A\n"
            b"P101,2,1,85,A\n"  # Same PID, different visit -> Valid visit, NOT duplicate!
            b"P101,1,1,80,A\n"  # Exactly identical repeat of (P101, 1, 1) -> Identical duplicate
            b"P102,1,1,90,B\n"
            b"P102,1,1,95,B\n"  # Same (P102, 1, 1) but different score -> Differing duplicate
            b"P103,1,1,70,A\n"
        )
        response = self.client.post(
            "/upload",
            data={
                "study_name": "Longitudinal Repeat Visits Study",
                "expected_participants": "300",
                "clean_mode": "first_three",
                "file": (io.BytesIO(csv_data), "longitudinal.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            dataset = StudyDataset.query.filter_by(
                study_name="Longitudinal Repeat Visits Study"
            ).first()
            self.assertIsNotNone(dataset)
            self.assertEqual(dataset.expected_participants, 300)
            self.assertEqual(dataset.clean_mode, "first_three")
            self.assertEqual(dataset.clean_columns, ["PID", "visit", "instance"])

            # Verify duplicate detection grouping using the first 3 columns:
            identical, differing = find_pid_duplicate_groups(
                dataset, dataset.clean_columns
            )
            # Only (P101, 1, 1) is identical duplicate
            self.assertEqual(len(identical), 1)
            self.assertEqual(identical[0]["composite_key"], "P101 / 1 / 1")

            # Only (P102, 1, 1) is differing duplicate
            self.assertEqual(len(differing), 1)
            self.assertEqual(differing[0]["composite_key"], "P102 / 1 / 1")

            # Verify that row 2 (P101, visit 2, instance 1) was NOT treated as a duplicate
            all_dup_indices = duplicate_row_indices(dataset, dataset.clean_columns)
            # Row index 1 is P101 visit 2 instance 1 -> must NOT be in duplicates!
            self.assertNotIn(1, all_dup_indices)

        # Check that dashboard HTML displays the expected participants and unique key
        self.assertIn(b"Total Participants Expected", response.data)
        self.assertIn(b"300", response.data)
        self.assertIn(b"PID + visit + instance", response.data)

    def test_custom_columns_clean_mode(self):
        csv_data = (
            b"id_num,site,sample_id,result\n"
            b"A1,Lilongwe,S1,Positive\n"
            b"A1,Salima,S1,Positive\n"
            b"A1,Lilongwe,S1,Positive\n"
        )
        self.client.post(
            "/upload",
            data={
                "study_name": "Custom Columns Study",
                "clean_mode": "custom",
                "custom_columns": ["id_num", "site"],
                "file": (io.BytesIO(csv_data), "custom.csv"),
            },
            content_type="multipart/form-data",
        )
        with app.app_context():
            dataset = StudyDataset.query.filter_by(
                study_name="Custom Columns Study"
            ).first()
            self.assertEqual(dataset.clean_mode, "custom")
            self.assertEqual(dataset.clean_columns, ["id_num", "site"])
            identical, differing = find_pid_duplicate_groups(
                dataset, dataset.clean_columns
            )
            self.assertEqual(len(identical), 1)

    def test_charts_generated_without_codebook(self):
        # A dataset uploaded without a codebook should still generate charts for all its eligible columns
        csv_data = (
            b"PID,visit,instance,site_id,clinic,outcome\n"
            b"P1,1,1,S1,Central,Recovered\n"
            b"P1,2,1,S1,Central,Recovered\n"
            b"P2,1,1,S2,North,Ongoing\n"
            b"P3,1,1,S2,North,Recovered\n"
        )
        response = self.client.post(
            "/upload",
            data={
                "study_name": "No Codebook Study",
                "clean_mode": "first_three",
                "file": (io.BytesIO(csv_data), "no_codebook.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            dataset = StudyDataset.query.filter_by(
                study_name="No Codebook Study"
            ).first()
            charts, preview, all_pids = dashboard_data(dataset)
            chart_names = [c["name"] for c in charts]
            # Verify that columns like site_id, clinic, outcome, visit, instance are charted
            self.assertIn("site_id", chart_names)
            self.assertIn("clinic", chart_names)
            self.assertIn("outcome", chart_names)
            self.assertIn("visit", chart_names)
            self.assertIn("instance", chart_names)
            for chart in charts:
                self.assertTrue(
                    len(chart["variants"]) > 0,
                    f"Chart for {chart['name']} must have variants",
                )
        # Dashboard must not say "No chartable columns found"
        self.assertNotIn(b"No chartable columns found", response.data)


class ExcelSerialDateTests(unittest.TestCase):
    """Tests for Excel-serial-to-ISO-date conversion (Part A upload-time and
    Part B chart-time fallback)."""

    def setUp(self):
        app.config["TESTING"] = True
        with app.app_context():
            db.drop_all()
            db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def _chart(self, col_name, values):
        """Build a single-column chart with no codebook."""
        from app import build_single_column_chart
        df = pd.DataFrame(
            {col_name: values, "pid": [f"P{i}" for i in range(len(values))]}
        )
        pids = df["pid"]
        return build_single_column_chart(col_name, df, {}, {}, set(), row_pids=pids)

    def test_part_a_csv_serial_dates_converted_on_upload(self):
        """Part A: _coerce_excel_serials_to_dates converts numeric date cols to ISO strings."""
        from app import _coerce_excel_serials_to_dates
        # 42936 = 2017-07-09, 43090 = 2017-12-11, 43200 = 2018-04-01
        df = pd.DataFrame({
            "consent_date": [42936, 43090, 43200],
            "score": [1, 2, 3],
        })
        result = _coerce_excel_serials_to_dates(df)
        for val in result["consent_date"]:
            self.assertRegex(str(val), r"^\d{4}-\d{2}-\d{2}$",
                             f"Expected ISO date string, got {val!r}")
        # score column (no date keyword) must be unchanged
        self.assertTrue(pd.api.types.is_numeric_dtype(result["score"]))

    def test_part_b_stored_serials_render_as_date_chart(self):
        """Part B: pre-existing integer serials with a date column name → date chart."""
        # 45000 = 2023-03-15, 45010 = 2023-03-25
        values = [45000, 45010, 45000, 45010, 45000]
        chart = self._chart("visit_date", values)
        self.assertIsNotNone(chart, "Expected a chart to be built")
        self.assertEqual(chart.get("kind"), "date",
                         f"Expected kind='date', got {chart.get('kind')!r}")

    def test_part_b_dob_serials_render_as_year_chart(self):
        """Part B: DOB serial numbers → year-bucket date chart."""
        # 20000 ≈ 1954, 25000 ≈ 1968
        values = [20000, 20000, 25000, 25000, 25000]
        chart = self._chart("dob", values)
        self.assertIsNotNone(chart)
        self.assertEqual(chart.get("kind"), "date")
        # DOB path groups by year → labels should be 4-digit year strings
        labels = chart["variants"][0]["labels"]
        self.assertTrue(all(len(lbl) == 4 and lbl.isdigit() for lbl in labels),
                        f"Expected year labels, got {labels}")

    def test_non_date_numeric_column_unchanged(self):
        """visit_number / age / weight_kg must not be misidentified as serial dates."""
        for col_name in ("visit_number", "age", "weight_kg"):
            values = [1, 2, 3, 1, 2]
            chart = self._chart(col_name, values)
            self.assertIsNotNone(chart)
            self.assertNotEqual(chart.get("kind"), "date",
                                f"Column {col_name!r} should not produce a date chart")

    def test_string_dates_unchanged(self):
        """String dates like '30/05/2017' still go through the existing regex path."""
        values = ["30/05/2017", "20/06/2017", "07/07/2017"]
        chart = self._chart("visit_date", values)
        self.assertIsNotNone(chart)
        self.assertEqual(chart.get("kind"), "date",
                         "String DD/MM/YYYY dates should still produce a date chart")


class MonthAbbrevDateTests(unittest.TestCase):
    """Tests for DD-Mon-YY and DD-Mon-YYYY date handling and multi-response protection."""

    def _chart(self, col_name, values):
        from app import build_single_column_chart
        df = pd.DataFrame(
            {col_name: values, "pid": [f"P{i}" for i in range(len(values))]}
        )
        return build_single_column_chart(col_name, df, {}, {}, set(), row_pids=df["pid"])

    def test_dd_mon_yy_dates_render_as_monthly_trend_line(self):
        """Values like '02-Dec-16', '07-Nov-16' render as monthly trend line, not donut/categories."""
        values = ["02-Dec-16", "07-Nov-16", "14-Nov-16", "18-Nov-2016", "10-Jan-17"]
        chart = self._chart("biopsy_date", values)
        self.assertIsNotNone(chart)
        self.assertEqual(chart.get("kind"), "date")
        variant = chart["variants"][0]
        self.assertEqual(variant.get("type"), "line")
        labels = variant.get("labels", [])
        self.assertTrue(any(l.startswith("2016-") or l.startswith("2017-") for l in labels),
                        f"Expected YYYY-MM period labels, got {labels}")

    def test_mixed_date_formats_parse_correctly(self):
        """Columns with both '02-Dec-16' and '2016-12-02' parse as date chart."""
        values = ["02-Dec-16", "2016-12-02", "14-Nov-16", "2016-11-14"]
        chart = self._chart("consent_date", values)
        self.assertIsNotNone(chart)
        self.assertEqual(chart.get("kind"), "date")

    def test_dob_column_with_dd_mon_yy_renders_as_year_buckets(self):
        """DOB with '02-Dec-66' renders as year buckets, not binary donut."""
        values = ["02-Dec-66", "15-May-75", "20-Jan-80", "12-Aug-90"]
        chart = self._chart("dob", values)
        self.assertIsNotNone(chart)
        self.assertEqual(chart.get("kind"), "date")
        labels = chart["variants"][0]["labels"]
        self.assertTrue(all(len(lbl) == 4 and lbl.isdigit() for lbl in labels),
                        f"Expected year labels, got {labels}")

    def test_date_of_biopsy_1_not_grouped_as_multiselect_subcolumn(self):
        """Columns like 'Date of biopsy 1' must not be grouped as multi-response subcolumns."""
        from app import group_question_columns
        cols = ["Date of biopsy 1", "Date of biopsy 2", "other_q1", "other_q2"]
        groups = group_question_columns(cols)
        # 'Date of biopsy 1' should remain an individual column, not merged into a multi-response group
        self.assertEqual(groups.get("Date of biopsy 1"), ["Date of biopsy 1"])
        self.assertEqual(groups.get("Date of biopsy 2"), ["Date of biopsy 2"])

    def test_multiselect_selection_rejects_mon_dates(self):
        """multiselect_selection('02-Dec-16') must return False, not True."""
        from app import multiselect_selection
        self.assertFalse(multiselect_selection("02-Dec-16"))
        self.assertFalse(multiselect_selection("7-Nov-16"))
        self.assertFalse(multiselect_selection("18-Nov-2016"))
        # Real checkbox values
        self.assertTrue(multiselect_selection("1"))
        self.assertTrue(multiselect_selection("Checked"))
        self.assertFalse(multiselect_selection("0"))
        self.assertFalse(multiselect_selection("Unchecked"))


class PIDOnDemandTests(unittest.TestCase):
    """Tests for on-demand PID fetching endpoint and PID stripping from chart-data."""

    def setUp(self):
        app.config["TESTING"] = True
        with app.app_context():
            db.drop_all()
            db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_category_pids_endpoint_returns_pids(self):
        """POST /studies/<id>/category-pids returns the list of PIDs for that category."""
        from app import StudyDataset, db
        with app.app_context():
            records = [
                {"pid": "P01", "gender": "Female"},
                {"pid": "P02", "gender": "Female"},
                {"pid": "P03", "gender": "Male"},
            ]
            ds = StudyDataset(
                study_name="PID Test",
                filename="test.csv",
                rows=3,
                columns=2,
                duplicates_removed=0,
                missing_values_filled=0,
                column_names=["pid", "gender"],
                records=records,
            )
            db.session.add(ds)
            db.session.commit()
            ds_id = ds.id

        resp = self.client.post(
            f"/studies/{ds_id}/category-pids",
            data={
                "chart_name": "gender",
                "variant_index": "0",
                "category_index": "0",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("pids", data)
        self.assertGreater(len(data["pids"]), 0)

    def test_charts_cache_strips_pids_from_variants(self):
        """_strip_pids_from_charts removes 'pids' from all chart variants."""
        from app import _strip_pids_from_charts
        sample_charts = [
            {
                "name": "col1",
                "variants": [
                    {"type": "bar", "labels": ["A", "B"], "values": [1, 2], "pids": [["P1"], ["P2"]]}
                ]
            }
        ]
        stripped = _strip_pids_from_charts(sample_charts)
        self.assertNotIn("pids", stripped[0]["variants"][0])
        self.assertIn("labels", stripped[0]["variants"][0])


class PersonalColumnImputation(unittest.TestCase):
    """Tests that personal names and DOB are never overwritten with global mode."""

    def _import_helpers(self):
        from app import clean_dataframe, is_personal_or_date_column, build_single_column_chart
        return clean_dataframe, is_personal_or_date_column, build_single_column_chart

    def _chart(self, col_name, values):
        """Helper: build a chart for a single column with no codebook."""
        from app import build_single_column_chart
        df = pd.DataFrame({col_name: values, "pid": [f"P{i}" for i in range(len(values))]})
        pids = df["pid"]
        return build_single_column_chart(col_name, df, {}, {}, set(), row_pids=pids)

    def test_is_personal_or_date_column_detects_known_names(self):
        _, is_personal_or_date_column, _ = self._import_helpers()
        self.assertTrue(is_personal_or_date_column("first_name"))
        self.assertTrue(is_personal_or_date_column("surname"))
        self.assertTrue(is_personal_or_date_column("dob"))
        self.assertTrue(is_personal_or_date_column("date_of_birth"))
        self.assertTrue(is_personal_or_date_column("phone_number"))
        self.assertTrue(is_personal_or_date_column("email"))

    def test_is_personal_or_date_column_safe_for_categorical(self):
        _, is_personal_or_date_column, _ = self._import_helpers()
        # Low-cardinality coded column – should NOT be treated as personal
        s = pd.Series(["Male", "Female", "Male", "Female"] * 10)
        self.assertFalse(is_personal_or_date_column("sex", s))

    def test_personal_names_and_dob_not_overwritten_with_global_mode(self):
        """In a longitudinal dataset each participant's name/DOB must stay
        their own across all visits, never the global mode of another person."""
        clean_dataframe, _, _ = self._import_helpers()

        # 2 participants × 2 visits; names/DOBs only filled on visit 1
        data = pd.DataFrame({
            "pid":        ["P1", "P1", "P2", "P2"],
            "visit":      [1,    2,    1,    2],
            "first_name": ["Grace",    None,       "Blessings", None],
            "dob":        ["1995-04-12", None,     "2001-08-20", None],
            "score":      [10,    20,    30,    40],
        })

        df, _, _, _ = clean_dataframe(data, pid_column="pid")

        # Participant 1 follow-up should have their OWN name, not "Blessings"
        self.assertEqual(df.loc[1, "first_name"], "Grace")
        self.assertEqual(df.loc[1, "dob"], "1995-04-12")

        # Participant 2 follow-up should have their OWN name, not "Grace"
        self.assertEqual(df.loc[3, "first_name"], "Blessings")
        self.assertEqual(df.loc[3, "dob"], "2001-08-20")

    def test_names_not_filled_when_no_pid_column(self):
        """Without a pid_column, missing names stay None — not filled with mode."""
        clean_dataframe, _, _ = self._import_helpers()

        data = pd.DataFrame({
            "pid":        ["P1", "P2"],
            "first_name": ["Aaron", None],
            "score":      [10, 20],
        })
        df, _, _, _ = clean_dataframe(data)
        # name column should remain None/NaN — not "Aaron"
        self.assertTrue(pd.isna(df.loc[1, "first_name"]) or df.loc[1, "first_name"] is None)

    def test_dob_chart_groups_by_birth_year(self):
        """DOB chart should produce year-level labels, not monthly slices."""
        dobs = (
            ["1990-01-15", "1991-03-22", "1992-07-04"] * 5
            + ["12/05/1988", "23/09/1995"] * 3
        )
        result = self._chart("dob", dobs)
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "date")
        labels = result["variants"][0]["labels"]
        # All labels should be 4-digit year strings
        for lbl in labels:
            self.assertRegex(str(lbl), r"^\d{4}$", f"Expected year label, got: {lbl}")

    def test_dd_mm_yyyy_dates_parsed_correctly(self):
        """European-style DD/MM/YYYY dates must be parsed (not silently dropped)."""
        dates = ["15/01/2020", "22/03/2021", "04/07/2022"] * 10
        result = self._chart("collection_date", dates)
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "date")

    def test_charts_display_exact_dataset_values_without_codebook(self):
        """When no codebook is entered, charts must display exact dataset values
        (e.g. 'Female' and 'Male' for Gender, not numbers) and all dataset columns."""
        from app import dashboard_data

        data = pd.DataFrame({
            "bcc_pid": ["P1", "P1", "P2", "P3"],
            "Gender": ["Female", "Female", "Male", "Female"],
            "clinic": ["Central", "Central", "North", "South"],
            "visit": [1, 2, 1, 1],
        })
        dataset = SimpleNamespace(
            id=500,
            study_name="No Codebook Gender Test",
            filename="bcc.csv",
            column_names=list(data.columns),
            records=data.to_dict(orient="records"),
            clean_columns=None,
        )

        charts, _, _ = dashboard_data(dataset, codebook=None)
        charts_by_name = {c["name"]: c for c in charts}

        # All columns except bcc_pid should be charted
        self.assertNotIn("bcc_pid", charts_by_name)
        self.assertIn("Gender", charts_by_name)
        self.assertIn("clinic", charts_by_name)
        self.assertIn("visit", charts_by_name)

        # Gender must have exact values 'Female' and 'Male' (NOT numbers!)
        gender_labels = charts_by_name["Gender"]["variants"][0]["labels"]
        self.assertIn("Female", gender_labels)
        self.assertIn("Male", gender_labels)
        self.assertNotIn("1", gender_labels)
        self.assertNotIn("2", gender_labels)

        # Clinic must have exact values
        clinic_labels = charts_by_name["clinic"]["variants"][0]["labels"]
        self.assertIn("Central", clinic_labels)
        self.assertIn("North", clinic_labels)

        # Visit must have exact discrete values '1' and '2' (not a histogram!)
        visit_labels = charts_by_name["visit"]["variants"][0]["labels"]
        self.assertIn("1", visit_labels)
        self.assertIn("2", visit_labels)

    def test_bcc_pid_detected_and_demographics_ffill(self):
        """bcc_pid must be detected as a PID column, and demographics must ffill
        per-participant without filling with global mode."""
        from app import detect_pid_columns, clean_dataframe

        cols = ["bcc_pid", "visit", "first_name", "last_name", "Gender"]
        detected = detect_pid_columns(cols)
        self.assertIn("bcc_pid", detected)

        df = pd.DataFrame({
            "bcc_pid": ["BCC-01", "BCC-01", "BCC-02", "BCC-02"],
            "visit": [1, 2, 1, 2],
            "first_name": ["Grace", None, "Mary", None],
            "last_name": ["Chikuse", None, "Banda", None],
            "Gender": ["Female", "Female", "Female", "Female"],
        })
        cleaned, _, _, _ = clean_dataframe(df, pid_column=detected[0])
        # BCC-01 follow up has Grace Chikuse
        self.assertEqual(cleaned.loc[1, "first_name"], "Grace")
        self.assertEqual(cleaned.loc[1, "last_name"], "Chikuse")
        # BCC-02 follow up has Mary Banda
        self.assertEqual(cleaned.loc[3, "first_name"], "Mary")
        self.assertEqual(cleaned.loc[3, "last_name"], "Banda")


class CRFGroupingTests(unittest.TestCase):
    """Tests for the CRFs category grouping feature using RedCap data dictionaries."""

    def setUp(self):
        from app import app, db
        app.config["TESTING"] = True
        with app.app_context():
            db.drop_all()
            db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        from app import db
        with app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_parse_codebook_extracts_variable_forms_and_choices(self):
        """RedCap data dictionary extracts variable_forms mapping and inline choices."""
        from app import parse_codebook
        import io

        dict_csv = (
            'Variable / Field Name,Form Name,Field Label,"Choices, Calculations, OR Slider Labels"\n'
            'gender,demographics_contact_information,Gender,"1, Female | 2, Male"\n'
            'ethnic,demographics_contact_information,Ethnicity,"1, African | 2, European"\n'
            'ses_education,socioeconomical_status,Education Level,"1, None | 2, Primary | 3, Secondary"\n'
            'hiv_status,medical_history,Patient\'s HIV Status,"1, Positive | 2, Negative"\n'
        )
        dict_file = io.BytesIO(dict_csv.encode("utf-8"))
        dict_file.filename = "data_dictionary.csv"
        df_dummy = pd.DataFrame({"gender": [1, 2], "ethnic": [1, 2], "ses_education": [1, 2], "hiv_status": [1, 2]})
        result = parse_codebook(dict_file, df_dummy)

        self.assertIn("variable_forms", result)
        vforms = result["variable_forms"]
        # Machine names
        self.assertEqual(vforms["gender"], "demographics_contact_information")
        self.assertEqual(vforms["ethnic"], "demographics_contact_information")
        self.assertEqual(vforms["ses_education"], "socioeconomical_status")
        self.assertEqual(vforms["hiv_status"], "medical_history")
        # Display labels
        self.assertEqual(vforms["Gender"], "demographics_contact_information")
        self.assertEqual(vforms["Ethnicity"], "demographics_contact_information")
        self.assertEqual(vforms["Education Level"], "socioeconomical_status")
        # Slugs
        self.assertEqual(vforms["education_level"], "socioeconomical_status")

        # Insertion order preserved
        forms_in_order = list(dict.fromkeys(vforms.values()))
        self.assertEqual(
            forms_in_order,
            ["demographics_contact_information", "socioeconomical_status", "medical_history"]
        )

        # Choices extracted
        self.assertIn("gender", result["lists"])
        self.assertEqual(result["lists"]["gender"]["1"]["en"], "Female")
        self.assertEqual(result["lists"]["gender"]["2"]["en"], "Male")

    def test_dashboard_data_attaches_form_to_charts(self):
        """dashboard_data assigns chart['form'] based on variable_forms mapping."""
        from app import dashboard_data
        from types import SimpleNamespace

        data = pd.DataFrame({
            "pid": ["P1", "P2"],
            "gender": ["Female", "Male"],
            "education": ["Primary", "Secondary"],
            "unmapped_col": ["A", "B"],
        })
        dataset = SimpleNamespace(
            id=10,
            study_name="Form Test",
            filename="data.csv",
            column_names=list(data.columns),
            records=data.to_dict(orient="records"),
            clean_columns=None,
        )
        codebook_data = {
            "variable_forms": {
                "gender": "demographics_contact_information",
                "education": "socioeconomical_status",
            }
        }
        charts, _, _ = dashboard_data(dataset, codebook_data)
        charts_by_name = {c["name"]: c for c in charts}

        self.assertEqual(charts_by_name["gender"]["form"], "demographics_contact_information")
        self.assertEqual(charts_by_name["education"]["form"], "socioeconomical_status")
        self.assertIsNone(charts_by_name["unmapped_col"]["form"])

    def test_dashboard_with_dictionary_defaults_to_crfs_view(self):
        """Dashboard with data dictionary shows active CRFs button and renders form sections in order."""
        import io

        dataset_csv = "pid,gender,ses_education,hiv_status\nP1,1,2,1\nP2,2,3,2\n"
        dict_csv = (
            'Variable / Field Name,Form Name,Field Label,"Choices, Calculations, OR Slider Labels"\n'
            'gender,demographics_contact_information,Gender,"1, Female | 2, Male"\n'
            'ses_education,socioeconomical_status,Education,"1, None | 2, Primary | 3, Secondary"\n'
            'hiv_status,medical_history,HIV Status,"1, Positive | 2, Negative"\n'
        )
        response = self.client.post(
            "/upload",
            data={
                "study_name": "CRF Study",
                "file": (io.BytesIO(dataset_csv.encode("utf-8")), "dataset.csv"),
                "codebook": (io.BytesIO(dict_csv.encode("utf-8")), "data_dictionary.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

        html = response.data.decode("utf-8")
        # Segmented control exists
        self.assertIn('data-category-dimension="crfs"', html)
        self.assertIn('data-category-dimension="columns"', html)
        self.assertIn('data-category-dimension="visits"', html)

        # CRFs is the active dimension when dictionary is uploaded
        self.assertIn('data-category-dimension="crfs"', html)
        self.assertIn('id="crfs-view"', html)
        self.assertIn('id="columns-view" hidden', html)
        self.assertIn('id="visits-view" hidden', html)

        # Dropdowns in search/filter bar
        self.assertIn('id="chart-field-select"', html)
        self.assertIn('id="chart-form-select"', html)
        self.assertIn('id="chart-visit-select"', html)
        self.assertIn('Choose form', html)
        self.assertIn('Choose visit', html)
        self.assertIn('<option value="">All forms</option>', html)
        self.assertIn('<option value="demographics_contact_information">Demographics Contact Information</option>', html)
        self.assertIn('<option value="socioeconomical_status">Socioeconomical Status</option>', html)
        self.assertIn('<option value="medical_history">Medical History</option>', html)
        self.assertIn('<option value="">All visits</option>', html)

        # Form sections rendered in dictionary order
        pos_demo = html.find('Demographics Contact Information')
        pos_ses = html.find('Socioeconomical Status')
        pos_med = html.find('Medical History')
        self.assertTrue(pos_demo > 0, "Demographics section should exist")
        self.assertTrue(pos_ses > 0, "Socioeconomical Status section should exist")
        self.assertTrue(pos_med > 0, "Medical History section should exist")
        self.assertTrue(pos_demo < pos_ses < pos_med, "Sections should appear in dictionary order")

    def test_dashboard_without_dictionary_keeps_columns_active_and_disables_crfs(self):
        """Dashboard without data dictionary keeps Columns active and disables CRFs button."""
        import io

        dataset_csv = "pid,gender,age\nP1,Female,30\nP2,Male,40\n"
        response = self.client.post(
            "/upload",
            data={
                "study_name": "No Dict Study",
                "file": (io.BytesIO(dataset_csv.encode("utf-8")), "dataset.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

        html = response.data.decode("utf-8")
        # Columns is active
        self.assertIn('data-category-dimension="columns"', html)
        # CRFs is disabled
        self.assertIn('data-category-dimension="crfs"', html)
        self.assertIn('disabled', html)
        # crfs-view is NOT rendered
        self.assertNotIn('id="crfs-view"', html)
        # columns-view is not hidden
        self.assertIn('id="columns-view"', html)
        self.assertNotIn('id="columns-view" hidden', html)
        # Field dropdown is shown, form dropdown and visit dropdown are hidden
        self.assertIn('id="chart-field-select"', html)
        self.assertNotIn('id="chart-field-select" aria-label="Choose a field to view" hidden', html)
        self.assertIn('id="chart-form-select" aria-label="Choose a form to view" hidden', html)
        self.assertIn('id="chart-visit-select" aria-label="Choose a visit to view" hidden', html)


class DashboardCacheTests(unittest.TestCase):
    """Tests dashboard caching, invalidation, manual clear, and preview trimming."""

    def setUp(self):
        from app import app, db
        self.app = app
        self.db = db
        app.config["TESTING"] = True
        app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
        self.client = app.test_client()
        with app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            self.db.session.remove()
            self.db.drop_all()

    def test_dashboard_populates_cache_and_serves_cache_hit(self):
        import io
        from unittest.mock import patch
        from app import StudyDataset

        csv_data = "pid,gender,age\nP1,Female,30\nP2,Male,40\n"
        res = self.client.post(
            "/upload",
            data={
                "study_name": "Cache Test",
                "file": (io.BytesIO(csv_data.encode("utf-8")), "data.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)
        html = res.data.decode("utf-8")
        self.assertIn("Charts served from cache (built ", html)
        self.assertIn("Clear chart cache", html)
        self.assertIn("This can take 1–2 minutes for large studies.", html)

        with self.app.app_context():
            from app import _SCHEMA_VERSION
            ds = self.db.session.query(StudyDataset).one()
            self.assertIsNotNone(ds.charts_cache)
            self.assertEqual(ds.charts_cache.get("_schema_version"), _SCHEMA_VERSION)
            self.assertEqual(ds.charts_cache.get("_records_len"), 2)
            ds_id = ds.id

        # Second load should be a cache hit and NOT invoke dashboard_data
        with patch("app.dashboard_data", side_effect=AssertionError("dashboard_data should not run on cache hit")):
            res2 = self.client.get(f"/studies/{ds_id}")
            self.assertEqual(res2.status_code, 200)
            self.assertIn("Charts served from cache (built ", res2.data.decode("utf-8"))

    def test_clear_cache_endpoint_clears_cache(self):
        import io
        from app import StudyDataset

        csv_data = "pid,gender,age\nP1,Female,30\nP2,Male,40\n"
        self.client.post(
            "/upload",
            data={
                "study_name": "Clear Cache Study",
                "file": (io.BytesIO(csv_data.encode("utf-8")), "data.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        with self.app.app_context():
            ds = self.db.session.query(StudyDataset).one()
            self.assertIsNotNone(ds.charts_cache)
            ds_id = ds.id

        # Clear cache without following redirect to verify DB state immediately
        res = self.client.post(f"/studies/{ds_id}/clear-cache", follow_redirects=False)
        self.assertEqual(res.status_code, 302)
        with self.app.app_context():
            ds = self.db.session.get(StudyDataset, ds_id)
            self.assertIsNone(ds.charts_cache)

        # Following redirect rebuilds the cache and shows flash message
        res2 = self.client.get(f"/studies/{ds_id}")
        self.assertEqual(res2.status_code, 200)
        self.assertIn("Chart cache cleared. Next page load will rebuild charts.", res2.data.decode("utf-8"))
        with self.app.app_context():
            ds = self.db.session.get(StudyDataset, ds_id)
            self.assertIsNotNone(ds.charts_cache)

    def test_edit_and_drop_duplicate_invalidate_cache(self):
        import io
        from app import StudyDataset

        csv_data = "pid,gender,age\nP1,Female,30\nP1,Male,31\nP2,Female,25\n"
        self.client.post(
            "/upload",
            data={
                "study_name": "Dup Invalidate Study",
                "file": (io.BytesIO(csv_data.encode("utf-8")), "data.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        with self.app.app_context():
            ds = self.db.session.query(StudyDataset).one()
            self.assertIsNotNone(ds.charts_cache)
            ds_id = ds.id

        # Edit duplicate row -> must invalidate cache
        res_edit = self.client.post(
            f"/studies/{ds_id}/duplicate-row/edit",
            data={"row_index": "1", "column": "gender", "value": "Female"},
            follow_redirects=False,
        )
        self.assertEqual(res_edit.status_code, 302)
        with self.app.app_context():
            ds = self.db.session.get(StudyDataset, ds_id)
            self.assertIsNone(ds.charts_cache)

        # Reload dashboard -> rebuilds cache
        self.client.get(f"/studies/{ds_id}")
        with self.app.app_context():
            ds = self.db.session.get(StudyDataset, ds_id)
            self.assertIsNotNone(ds.charts_cache)

        # Drop duplicate row -> must invalidate cache
        res_drop = self.client.post(
            f"/studies/{ds_id}/duplicate-row/drop",
            data={"row_index": "1"},
            follow_redirects=False,
        )
        self.assertEqual(res_drop.status_code, 302)
        with self.app.app_context():
            ds = self.db.session.get(StudyDataset, ds_id)
            self.assertIsNone(ds.charts_cache)

    def test_save_codebook_mapping_invalidates_cache(self):
        import io
        from app import StudyDataset

        dataset_csv = "pid,q1\nP1,1\nP2,2\n"
        codebook_csv = (
            "list_name,name,label::English\n"
            "list_a,1,Yes\n"
            "list_a,2,No\n"
            "list_b,1,Male\n"
            "list_b,2,Female\n"
        )
        self.client.post(
            "/upload",
            data={
                "study_name": "Codebook Invalidate Study",
                "file": (io.BytesIO(dataset_csv.encode("utf-8")), "data.csv"),
                "codebook": (io.BytesIO(codebook_csv.encode("utf-8")), "cb.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        with self.app.app_context():
            ds = self.db.session.query(StudyDataset).one()
            self.assertIsNotNone(ds.charts_cache)
            ds_id = ds.id

        res_map = self.client.post(
            f"/studies/{ds_id}/codebook-mapping",
            data={"column": "q1", "list_name": "list_a"},
            follow_redirects=False,
        )
        self.assertEqual(res_map.status_code, 302)
        with self.app.app_context():
            ds = self.db.session.get(StudyDataset, ds_id)
            self.assertIsNone(ds.charts_cache)

    def test_preview_is_trimmed_to_limit(self):
        from types import SimpleNamespace
        from app import dashboard_data, PREVIEW_ROW_LIMIT

        records = [{"pid": f"P{i}", "age": 20 + (i % 50)} for i in range(PREVIEW_ROW_LIMIT + 50)]
        dataset = SimpleNamespace(
            id=99,
            study_name="Trim Test",
            filename="trim.csv",
            column_names=["pid", "age"],
            records=records,
            original_headers=None,
            clean_columns=None,
        )
        _, preview, _ = dashboard_data(dataset, None)
        self.assertEqual(len(preview), PREVIEW_ROW_LIMIT)


if __name__ == "__main__":
    unittest.main()
