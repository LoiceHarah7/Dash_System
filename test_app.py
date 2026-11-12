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

        # Check tracking section presence
        self.assertIn(b"Participant Procedure Tracking", response.data)
        self.assertIn(b"Lilongwe Site (ESD-01)", response.data)
        self.assertIn(b"Salima Site (ESD-02)", response.data)
        self.assertIn(b"ESD-01-0001", response.data)
        self.assertIn(b"Could not tolerate scope", response.data)
        self.assertIn(b"Refused further participation", response.data)
        self.assertIn(b"Esophageal Varices", response.data)

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


if __name__ == "__main__":
    unittest.main()
