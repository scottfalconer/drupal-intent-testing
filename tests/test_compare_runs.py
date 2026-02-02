import json
import tempfile
import unittest
from pathlib import Path

from scripts import compare_runs


class CompareRunsTests(unittest.TestCase):
    def _write_json(self, directory: str, name: str, payload: dict) -> Path:
        path = Path(directory) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_compare_snapshots_counts_duplicates(self) -> None:
        baseline = {
            "success": True,
            "data": {
                "refs": {
                    "e1": {"role": "link", "name": "A"},
                    "e2": {"role": "link", "name": "A"},
                }
            },
        }
        modified = {
            "success": True,
            "data": {
                "refs": {
                    "e1": {"role": "link", "name": "A"},
                }
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            base_path = self._write_json(tmp, "baseline.json", baseline)
            mod_path = self._write_json(tmp, "modified.json", modified)
            comp = compare_runs.compare_snapshots(base_path, mod_path)

        self.assertFalse(comp["same"])
        self.assertEqual(comp["changes"]["removed_count"], 1)
        self.assertEqual(comp["changes"]["added_count"], 0)

    def test_compare_snapshots_flags_parse_errors(self) -> None:
        baseline = {"parsed_error": "stdout was not valid JSON"}
        modified = {"success": True, "data": {"refs": {}}}
        with tempfile.TemporaryDirectory() as tmp:
            base_path = self._write_json(tmp, "baseline.json", baseline)
            mod_path = self._write_json(tmp, "modified.json", modified)
            comp = compare_runs.compare_snapshots(base_path, mod_path)

        self.assertIn("error", comp)
        self.assertFalse(comp["same"])

    def test_compare_drupal_messages(self) -> None:
        baseline = {"data": {"status": "Saved", "alert": None}}
        modified = {"data": {"status": "Saved", "alert": "Error"}}
        with tempfile.TemporaryDirectory() as tmp:
            base_path = self._write_json(tmp, "baseline.json", baseline)
            mod_path = self._write_json(tmp, "modified.json", modified)
            comp = compare_runs.compare_drupal_messages(base_path, mod_path)

        self.assertFalse(comp["same"])
        self.assertIn("alert", comp["diffs"])

    def test_compare_ai_explorer(self) -> None:
        baseline = {"data": {"pre_texts": ["tool call", "Final answer A"]}}
        modified = {"data": {"pre_texts": ["tool call", "Final answer B"]}}
        with tempfile.TemporaryDirectory() as tmp:
            base_path = self._write_json(tmp, "baseline.json", baseline)
            mod_path = self._write_json(tmp, "modified.json", modified)
            comp = compare_runs.compare_ai_explorer(base_path, mod_path)

        self.assertFalse(comp["same"])
        self.assertTrue(comp["diffs"]["final_answer"])


if __name__ == "__main__":
    unittest.main()
