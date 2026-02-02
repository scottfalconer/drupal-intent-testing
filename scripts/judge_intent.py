#!/usr/bin/env python3
"""Judge intent test runs using a manifest and run artifacts."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency
    yaml = None

from scripts import collectors
from scripts.intent import manifest as manifest_lib

JSON = Dict[str, Any]


def load_json(path: Path) -> JSON:
    return json.loads(path.read_text(encoding="utf-8"))


def get_checkpoint(run: JSON, name: Optional[str]) -> Optional[JSON]:
    checkpoints = run.get("checkpoints") or []
    if not checkpoints:
        return None
    if name:
        for cp in checkpoints:
            if cp.get("name") == name:
                return cp
        return None
    return checkpoints[-1]


def load_artifact(path: Optional[str]) -> Optional[JSON]:
    if not path:
        return None
    try:
        return load_json(Path(path))
    except Exception:
        return None


def extract_ai_fields(cp: JSON) -> Tuple[str, str]:
    ai_path = (cp.get("artifacts") or {}).get("ai_explorer")
    payload = load_artifact(ai_path) if ai_path else None
    if not payload:
        return "", ""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return "", ""
    return data.get("final_answer") or "", data.get("tool_payload") or ""


def extract_drupal_messages(cp: JSON) -> Tuple[Optional[str], Optional[str]]:
    msg_path = (cp.get("artifacts") or {}).get("drupal_messages")
    payload = load_artifact(msg_path) if msg_path else None
    if not payload:
        return None, None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return None, None
    return data.get("status"), data.get("alert")


def get_by_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if part == "":
            continue
        match = re.match(r"^([\w-]+)(\[(\d+)\])?$", part)
        if not match:
            return None
        key = match.group(1)
        idx = match.group(3)
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
        if idx is not None:
            if not isinstance(current, list):
                return None
            i = int(idx)
            if i >= len(current):
                return None
            current = current[i]
    return current


def evaluate_text_assertion(text: str, patterns: List[str], expect_present: bool) -> Tuple[bool, List[str]]:
    hits: List[str] = []
    for raw in patterns:
        try:
            pattern = re.compile(raw)
        except re.error:
            continue
        if pattern.search(text or ""):
            hits.append(raw)
    if expect_present:
        return bool(hits), hits
    return not bool(hits), hits


def evaluate_assertion(assertion: JSON, run: JSON) -> JSON:
    result: JSON = {
        "id": assertion.get("id"),
        "type": assertion.get("type"),
        "severity": assertion.get("severity", "fail"),
        "passed": False,
        "status": "FAIL",
        "message": "",
    }

    checkpoint_name = assertion.get("checkpoint")
    cp = get_checkpoint(run, checkpoint_name)
    if not cp:
        result["status"] = "ERROR"
        result["message"] = "checkpoint not found"
        return result

    a_type = assertion.get("type")
    scope = assertion.get("scope")

    if a_type in ("text_absent", "text_present"):
        patterns = assertion.get("patterns") or []
        text = ""
        if scope == "final_answer":
            text, _tool = extract_ai_fields(cp)
        elif scope == "tool_call":
            _final, text = extract_ai_fields(cp)
        elif scope == "drupal_status":
            text, _alert = extract_drupal_messages(cp)
            text = text or ""
        elif scope == "drupal_alert":
            _status, text = extract_drupal_messages(cp)
            text = text or ""
        else:
            text = ""
        if text is None:
            result["status"] = "ERROR"
            result["message"] = "text scope unavailable"
            return result
        expect_present = a_type == "text_present"
        passed, hits = evaluate_text_assertion(text, patterns, expect_present)
        result["passed"] = passed
        result["status"] = "PASS" if passed else "FAIL"
        result["message"] = "" if passed else f"patterns matched: {hits}"
        return result

    if a_type == "yaml_path_equals":
        _final, tool = extract_ai_fields(cp)
        if not tool:
            result["status"] = "ERROR"
            result["message"] = "tool payload not found"
            return result
        if yaml is None:
            result["status"] = "ERROR"
            result["message"] = "PyYAML required to parse tool payload"
            return result
        try:
            parsed = yaml.safe_load(tool)
        except Exception as exc:
            result["status"] = "ERROR"
            result["message"] = f"tool payload parse error: {exc}"
            return result
        path = assertion.get("path")
        expected = assertion.get("expected")
        actual = get_by_path(parsed, path or "")
        passed = actual == expected
        result["passed"] = passed
        result["status"] = "PASS" if passed else "FAIL"
        result["message"] = f"expected {expected}, got {actual}"
        return result

    if a_type == "no_console_errors":
        errors_path = (cp.get("artifacts") or {}).get("errors")
        payload = load_artifact(errors_path) if errors_path else None
        count = len(collectors.extract_log_entries(payload or {}))
        passed = count == 0
        result["passed"] = passed
        result["status"] = "PASS" if passed else "FAIL"
        result["message"] = f"console errors count: {count}"
        return result

    if a_type == "no_drupal_messages":
        level = assertion.get("level", "alert")
        status, alert = extract_drupal_messages(cp)
        target = alert if level == "alert" else status
        passed = not target
        result["passed"] = passed
        result["status"] = "PASS" if passed else "FAIL"
        result["message"] = f"{level} messages present: {target}" if target else ""
        return result

    if a_type == "url_contains":
        url = cp.get("url") or ""
        expected = assertion.get("contains") or ""
        passed = expected in url
        result["passed"] = passed
        result["status"] = "PASS" if passed else "FAIL"
        result["message"] = f"url {url} contains {expected}"
        return result

    result["status"] = "ERROR"
    result["message"] = f"unknown assertion type: {a_type}"
    return result


def judge(manifest: JSON, run: JSON) -> JSON:
    assertions = []
    for item in manifest.get("assertions", []):
        assertions.append(item)
    for guard in manifest.get("guards", []):
        assertions.append(guard)

    evaluated: List[JSON] = []
    for assertion in assertions:
        evaluated.append(evaluate_assertion(assertion, run))

    # include DSL assertions if present
    for assertion in run.get("assertions", []):
        evaluated.append({
            "id": assertion.get("id"),
            "type": assertion.get("type"),
            "severity": "fail",
            "passed": assertion.get("passed"),
            "status": "PASS" if assertion.get("passed") else "FAIL",
            "message": assertion.get("message"),
        })

    failures = [a for a in evaluated if not a.get("passed") and a.get("status") == "FAIL" and a.get("severity") == "fail"]
    errors = [a for a in evaluated if a.get("status") == "ERROR"]

    if errors:
        verdict = "ERROR"
        ready = False
    elif failures:
        verdict = "FAIL"
        ready = False
    else:
        verdict = "PASS"
        ready = True

    return {
        "verdict": verdict,
        "ready_to_submit": ready,
        "intent_statement": manifest.get("intent_statement"),
        "adr": manifest.get("adr", []),
        "assertions": evaluated,
        "failures": failures,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Judge intent test results")
    parser.add_argument("--manifest", required=True, help="Path to manifest (YAML or JSON)")
    parser.add_argument("--run", required=True, help="Path to intent_run.json")
    parser.add_argument("--output", default="intent_verdict.json", help="Output verdict path")
    parser.add_argument("--judge-run", default="modified", help="Run name to judge (single|baseline|modified)")
    parser.add_argument("--run-key", dest="judge_run", help=argparse.SUPPRESS)
    args = parser.parse_args()

    manifest, errors = manifest_lib.load_and_validate(args.manifest)
    if errors:
        print("Manifest invalid:")
        for err in errors:
            print(f"- {err}")
        return 2

    run_payload = load_json(Path(args.run))
    runs = run_payload.get("runs") or {}
    run = runs.get(args.judge_run) or runs.get("single") or run_payload
    if not run:
        print("Run data not found")
        return 2

    verdict = judge(manifest, run)
    Path(args.output).write_text(json.dumps(verdict, indent=2), encoding="utf-8")

    if verdict["verdict"] == "PASS":
        return 0
    if verdict["verdict"] == "FAIL":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
