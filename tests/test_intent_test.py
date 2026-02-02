import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import intent_test


class IntentTestTests(unittest.TestCase):
    def test_execute_steps_handles_basic_flow(self) -> None:
        steps = [
            {"open": "/foo"},
            {"wait": 0.1},
            {"command": "find role button click --name Save"},
            {"checkpoint": "after"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("scripts.intent_test.collectors.run_agent_browser_cmd") as run_cmd, \
                mock.patch("scripts.intent_test.collectors.collect_checkpoint") as collect_cp:
                run_cmd.return_value = {"returncode": 0}
                collect_cp.return_value = {"name": "after"}
                results = intent_test.execute_steps(
                    steps=steps,
                    base_url="https://example.com",
                    session="intent",
                    output_dir=Path(tmp),
                    probe_cmds=[],
                    probe_cwd=None,
                    raw_value_patterns=[],
                    label_terms=[],
                    tool_payload_patterns=[],
                    timeouts={},
                )
                self.assertEqual(len(results["steps"]), 4)
                self.assertEqual(len(results["checkpoints"]), 1)
                first_cmd = run_cmd.call_args_list[0].args[0]
                self.assertTrue(first_cmd.startswith("open "))
                self.assertIn("https://example.com/foo", first_cmd)

    def test_run_manifest_includes_login_steps(self) -> None:
        manifest = {
            "environment": {
                "base_url": "https://example.com",
                "admin_user": "user",
                "admin_pass": "pass",
            },
            "strategy": {"mode": "single"},
            "steps": [{"open": "/after"}],
        }
        captured = {}

        def fake_execute_steps(*, steps, **kwargs):
            captured["steps"] = steps
            return {"steps": steps, "checkpoints": []}

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("scripts.intent_test.execute_steps", side_effect=fake_execute_steps):
                result = intent_test.run_manifest(manifest, Path(tmp))

        steps = captured["steps"]
        self.assertEqual(steps[0]["open"], "/user/login")
        self.assertEqual(steps[-1]["open"], "/after")
        self.assertIn("single", result["runs"])

    def test_main_writes_outputs_and_returns_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = Path(tmp) / "manifest.json"
            manifest = {
                "issue": {"url": "https://example.com", "title": "Test"},
                "environment": {"base_url": "https://example.com"},
                "steps": [{"open": "/"}],
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            out_dir = Path(tmp) / "out"

            with mock.patch("scripts.intent_test.run_manifest") as run_manifest, \
                mock.patch("scripts.intent_test.judge_intent.judge") as judge:
                run_manifest.return_value = {"runs": {"modified": {}}}
                judge.return_value = {"verdict": "PASS"}
                with mock.patch.object(sys, "argv", ["intent_test.py", str(manifest_path), "--output-dir", str(out_dir)]):
                    rc = intent_test.main()

            self.assertEqual(rc, 0)
            self.assertTrue((out_dir / "intent_run.json").exists())
            self.assertTrue((out_dir / "intent_verdict.json").exists())


if __name__ == "__main__":
    unittest.main()
