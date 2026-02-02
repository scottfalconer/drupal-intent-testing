import tempfile
import unittest
from pathlib import Path

from scripts import explore


class ExploreTests(unittest.TestCase):
    def test_prefix_url(self) -> None:
        base = "https://example.com"
        self.assertEqual(explore._prefix_url(base, "/user/login"), "https://example.com/user/login")
        self.assertEqual(explore._prefix_url(base, "user/login"), "https://example.com/user/login")
        self.assertEqual(explore._prefix_url(base, "https://other/site"), "https://other/site")

    def test_allowed_by_safety(self) -> None:
        self.assertFalse(explore.allowed_by_safety("Save", "read-only"))
        self.assertFalse(explore.allowed_by_safety("Delete", "read-only"))
        self.assertTrue(explore.allowed_by_safety("Delete", "dangerous"))
        self.assertFalse(explore.allowed_by_safety("Log out", "dangerous"))

    def test_write_report_uses_output_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sess = explore.ExplorationSession(
                base_url="https://example.com",
                output_dir=Path(tmp),
                goal="test",
            )
            path = sess.write_report(duration_minutes=0.0, mode="guided", output_name="custom.md")
            self.assertTrue(path.exists())
            self.assertEqual(path.name, "custom.md")


if __name__ == "__main__":
    unittest.main()
