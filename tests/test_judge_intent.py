import json
import tempfile
import unittest
from pathlib import Path

from scripts import judge_intent


class JudgeIntentTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _ai_checkpoint(self, tmp: str, *, final_answer: str, tool_payload: str) -> dict:
        ai_path = Path(tmp) / "ai.json"
        ai_payload = {
            "data": {
                "final_answer": final_answer,
                "tool_payload": tool_payload,
            }
        }
        self._write_json(ai_path, ai_payload)
        return {"name": "after", "artifacts": {"ai_explorer": str(ai_path)}}

    def test_text_absent_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = self._ai_checkpoint(tmp, final_answer="Use Legal Tone", tool_payload='{"tone": "hg:legal"}')
            run = {"checkpoints": [cp]}
            manifest = {
                "assertions": [
                    {
                        "id": "no_raw",
                        "type": "text_absent",
                        "scope": "final_answer",
                        "patterns": ["hg:"],
                        "severity": "fail",
                        "checkpoint": "after",
                    }
                ]
            }
            verdict = judge_intent.judge(manifest, run)
            self.assertEqual(verdict["verdict"], "PASS")

    def test_text_present_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cp = self._ai_checkpoint(tmp, final_answer="Legal Tone enabled", tool_payload="")
            run = {"checkpoints": [cp]}
            manifest = {
                "assertions": [
                    {
                        "id": "has_label",
                        "type": "text_present",
                        "scope": "final_answer",
                        "patterns": ["Legal Tone"],
                        "severity": "fail",
                        "checkpoint": "after",
                    }
                ]
            }
            verdict = judge_intent.judge(manifest, run)
            self.assertEqual(verdict["verdict"], "PASS")

    def test_yaml_path_equals_allows_hyphen_keys(self) -> None:
        if judge_intent.yaml is None:
            self.skipTest("PyYAML not installed")
        with tempfile.TemporaryDirectory() as tmp:
            tool_payload = "component-list:\n  - type: hero\n"
            cp = self._ai_checkpoint(tmp, final_answer="", tool_payload=tool_payload)
            run = {"checkpoints": [cp]}
            manifest = {
                "assertions": [
                    {
                        "id": "component_type",
                        "type": "yaml_path_equals",
                        "path": "component-list[0].type",
                        "expected": "hero",
                        "severity": "fail",
                        "checkpoint": "after",
                    }
                ]
            }
            verdict = judge_intent.judge(manifest, run)
            self.assertEqual(verdict["verdict"], "PASS")

    def test_no_console_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            errors_path = Path(tmp) / "errors.json"
            self._write_json(errors_path, {"parsed": {"data": {"errors": []}}})
            cp = {"name": "after", "artifacts": {"errors": str(errors_path)}}
            run = {"checkpoints": [cp]}
            manifest = {
                "assertions": [
                    {
                        "id": "no_console",
                        "type": "no_console_errors",
                        "severity": "fail",
                        "checkpoint": "after",
                    }
                ]
            }
            verdict = judge_intent.judge(manifest, run)
            self.assertEqual(verdict["verdict"], "PASS")

    def test_no_drupal_messages_alert_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            msg_path = Path(tmp) / "messages.json"
            self._write_json(msg_path, {"data": {"status": None, "alert": "Boom"}})
            cp = {"name": "after", "artifacts": {"drupal_messages": str(msg_path)}}
            run = {"checkpoints": [cp]}
            manifest = {
                "assertions": [
                    {
                        "id": "no_alerts",
                        "type": "no_drupal_messages",
                        "level": "alert",
                        "severity": "fail",
                        "checkpoint": "after",
                    }
                ]
            }
            verdict = judge_intent.judge(manifest, run)
            self.assertEqual(verdict["verdict"], "FAIL")

    def test_url_contains(self) -> None:
        cp = {"name": "after", "url": "https://example.com/node/1"}
        run = {"checkpoints": [cp]}
        manifest = {
            "assertions": [
                {
                    "id": "url_check",
                    "type": "url_contains",
                    "contains": "/node/1",
                    "severity": "fail",
                    "checkpoint": "after",
                }
            ]
        }
        verdict = judge_intent.judge(manifest, run)
        self.assertEqual(verdict["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main()
