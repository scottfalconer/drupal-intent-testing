import unittest

from scripts import collectors


class CollectorsTests(unittest.TestCase):
    def test_should_add_json(self) -> None:
        self.assertTrue(collectors.should_add_json(["snapshot"], want_json=True))
        self.assertFalse(collectors.should_add_json(["screenshot"], want_json=True))
        self.assertFalse(collectors.should_add_json(["snapshot", "--json"], want_json=True))
        self.assertFalse(collectors.should_add_json(["snapshot"], want_json=False))

    def test_analyze_ai_output(self) -> None:
        summary = collectors.analyze_ai_output(
            final_answer="Use legal tone",
            tool_payload="{\"tone\": \"hg:legal\"}",
            raw_value_patterns=[r"hg:[a-z]+"],
            label_terms=["Legal tone"],
        )
        self.assertFalse(summary["raw_in_final_answer"])
        self.assertTrue(summary["raw_in_tool_payload"])
        self.assertIn("Legal tone", summary["label_terms_present_in_final_answer"])

    def test_extract_log_entries(self) -> None:
        record = {"parsed": {"data": {"errors": []}}}
        self.assertEqual(collectors.extract_log_entries(record), [])
        record = {"parsed": {"data": {"errors": ["boom"]}}}
        self.assertEqual(collectors.extract_log_entries(record), ["boom"])


if __name__ == "__main__":
    unittest.main()
