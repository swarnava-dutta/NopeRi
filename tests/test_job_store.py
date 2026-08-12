import csv
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agents.job_store import load_job_ids, write_csv_row


class JobStoreTests(unittest.TestCase):
    def test_load_ids_accepts_bom_and_normalises_values(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "jobs.csv")
            with open(path, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(["job_id", "title"])
                writer.writerow([" 00123 ", "one"])
                writer.writerow(["00123", "duplicate"])
                writer.writerow(["", "blank"])

            self.assertEqual(load_job_ids(path), {"00123"})

    def test_load_ids_rejects_wrong_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "jobs.csv")
            with open(path, "w", newline="", encoding="utf-8") as handle:
                handle.write("id,title\n1,one\n")

            with self.assertRaisesRegex(ValueError, "job_id"):
                load_job_ids(path)

            with self.assertRaisesRegex(ValueError, "job_id"):
                write_csv_row(path, ["job_id", "title"], {"job_id": "2", "title": "two"})

    def test_append_reuses_legacy_header(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "applied.csv")
            with open(path, "w", newline="", encoding="utf-8") as handle:
                handle.write("job_id,title,company,applied_at\n")

            write_csv_row(
                path,
                ["job_id", "title", "company", "applied_at", "questionnaire_answers"],
                {
                    "job_id": "42",
                    "title": "AI Engineer",
                    "company": "Example",
                    "applied_at": "2026-08-12T00:00:00+00:00",
                    "questionnaire_answers": "[]",
                },
            )

            with open(path, newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(len(rows[0]), 4)
            self.assertEqual(len(rows[1]), 4)
            self.assertEqual(rows[1][0], "42")


if __name__ == "__main__":
    unittest.main()
