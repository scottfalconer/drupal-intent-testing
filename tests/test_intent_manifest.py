import json
import tempfile
import unittest
from pathlib import Path

from scripts.intent import manifest as manifest_lib


class IntentManifestTests(unittest.TestCase):
    def test_validate_manifest_missing_fields(self) -> None:
        manifest = {"issue": {}, "environment": {}, "strategy": {}, "steps": []}
        errors = manifest_lib.validate_manifest(manifest)
        self.assertTrue(errors)

    def test_load_and_validate_json(self) -> None:
        payload = {
            "issue": {"url": "https://example.com", "title": "Test"},
            "environment": {"base_url": "https://site"},
            "strategy": {"mode": "single"},
            "steps": [{"open": "/"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            manifest, errors = manifest_lib.load_and_validate(str(path))
        self.assertFalse(errors)
        self.assertEqual(manifest["strategy"]["mode"], "single")

    def test_intent_statement_without_issue(self) -> None:
        payload = {
            "intent_statement": "User wants to see labels",
            "environment": {"base_url": "https://site"},
            "strategy": {"mode": "single"},
            "steps": [{"open": "/"}],
        }
        errors = manifest_lib.validate_manifest(payload)
        self.assertFalse(errors)

    def test_adr_list_accepts_strings(self) -> None:
        payload = {
            "intent_statement": "Intent ok",
            "adr": ["One", "Two"],
            "environment": {"base_url": "https://site"},
            "strategy": {"mode": "single"},
            "steps": [{"open": "/"}],
        }
        errors = manifest_lib.validate_manifest(payload)
        self.assertFalse(errors)


if __name__ == "__main__":
    unittest.main()
