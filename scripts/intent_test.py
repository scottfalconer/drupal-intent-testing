#!/usr/bin/env python3
"""Run an intent manifest end-to-end and emit a verdict."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import collectors
from scripts.intent import manifest as manifest_lib
from scripts import compare_runs
from scripts import judge_intent

JSON = Dict[str, Any]


def run_shell(cmd: str, *, cwd: Optional[str] = None) -> JSON:
    result = collectors.run_probe(cmd, cwd=cwd)
    return result


def build_model_select_js(model: str, selector: str) -> str:
    return (
        "(() => {"
        f"const sel = document.querySelector({json.dumps(selector)});"
        "if (!sel) return {selected: null};"
        f"const target = {json.dumps(model)};"
        "let matched = null;"
        "for (const opt of Array.from(sel.options || [])) {"
        "if (opt.value === target || (opt.textContent || '').trim() === target) {"
        "sel.value = opt.value; matched = {value: opt.value, label: (opt.textContent || '').trim()}; break;"
        "}"
        "}"
        "sel.dispatchEvent(new Event('change', {bubbles:true}));"
        "return {selected: matched};"
        "})()"
    )


def build_prompt_set_js(prompt: str, selector: Optional[str]) -> str:
    selectors = [selector] if selector else ["#edit-prompt", "textarea[name='prompt']", "textarea"]
    selectors_js = json.dumps(selectors)
    return (
        "(() => {"
        f"const selectors = {selectors_js};"
        "let el = null;"
        "for (const sel of selectors) {"
        "el = document.querySelector(sel);"
        "if (el) break;"
        "}"
        f"const value = {json.dumps(prompt)};"
        "if (!el) return {found: false};"
        "el.value = value;"
        "el.dispatchEvent(new Event('input', {bubbles:true}));"
        "el.dispatchEvent(new Event('change', {bubbles:true}));"
        "return {found: true};"
        "})()"
    )


def action_run_ai_agent_explorer(
    *,
    session: str,
    prompt_text: str,
    model: Optional[str],
    prompt_selector: Optional[str],
    model_selector: str,
    run_buttons: List[str],
    completion_texts: List[str],
    completion_timeout_ms: int,
    post_completion_timeout_ms: int,
    post_completion_stable_ms: int,
    pre_min_count: int,
) -> JSON:
    records: List[JSON] = []

    if model:
        js = build_model_select_js(model, model_selector)
        records.append(collectors.run_agent_browser_cmd(f"eval --json {shlex.quote(js)}", session=session, capture_json=True, timeout=60))

    if prompt_text:
        js = build_prompt_set_js(prompt_text, prompt_selector)
        records.append(collectors.run_agent_browser_cmd(f"eval --json {shlex.quote(js)}", session=session, capture_json=True, timeout=60))

    clicked = False
    for button in run_buttons:
        rec = collectors.run_agent_browser_cmd(
            f"find role button click --name {shlex.quote(button)}",
            session=session,
            capture_json=True,
            timeout=60,
        )
        records.append(rec)
        if rec.get("returncode", 1) == 0:
            clicked = True
            break

    completion_by = None
    timeout_s = max(1, int(completion_timeout_ms / 1000))
    for text in completion_texts:
        rec = collectors.run_agent_browser_cmd(f"wait --text {shlex.quote(text)}", session=session, capture_json=True, timeout=timeout_s)
        records.append(rec)
        if rec.get("returncode", 1) == 0:
            completion_by = f"text:{text}"
            break

    pre_blocks = wait_for_pre_blocks(
        session=session,
        timeout_ms=post_completion_timeout_ms,
        stable_ms=post_completion_stable_ms,
        min_count=pre_min_count,
    )

    return {
        "records": records,
        "clicked": clicked,
        "completion_detected_by": completion_by or "timeout",
        "pre_blocks": pre_blocks,
    }


def wait_for_pre_blocks(
    *,
    session: str,
    timeout_ms: int,
    stable_ms: int,
    min_count: int,
) -> JSON:
    start = time.time()
    last_count = None
    last_change = time.time()
    last_record: Optional[JSON] = None
    timeout_s = max(1, int(timeout_ms / 1000))

    while time.time() - start < timeout_s:
        rec = collectors.run_agent_browser_cmd(
            "eval --json \"(() => document.querySelectorAll('.explorer-messages pre').length)()\"",
            session=session,
            capture_json=True,
            timeout=10,
        )
        last_record = rec
        data = rec.get("parsed", {}).get("data") if isinstance(rec.get("parsed"), dict) else None
        count = data if isinstance(data, int) else None
        now_ts = time.time()
        if count is not None and count != last_count:
            last_count = count
            last_change = now_ts
        if count is not None and count >= min_count and (now_ts - last_change) >= (stable_ms / 1000.0):
            return {
                "stabilized": True,
                "count": count,
                "record": last_record,
            }
        time.sleep(0.5)

    return {
        "stabilized": False,
        "count": last_count,
        "record": last_record,
    }


def execute_steps(
    *,
    steps: List[JSON],
    base_url: str,
    session: str,
    output_dir: Path,
    probe_cmds: List[str],
    probe_cwd: Optional[str],
    raw_value_patterns: List[str],
    label_terms: List[str],
    tool_payload_patterns: List[str],
    timeouts: JSON,
) -> JSON:
    output_dir.mkdir(parents=True, exist_ok=True)
    results: JSON = {
        "session": session,
        "started": collectors.now(),
        "base_url": base_url,
        "steps": [],
        "checkpoints": [],
        "assertions": [],
        "extracts": {},
        "probes": {},
    }

    for step in steps:
        record: JSON = {"step": step, "result": None}
        if not isinstance(step, dict):
            record["error"] = "step must be an object"
            results["steps"].append(record)
            continue

        if "open" in step:
            url = compare_runs._prefix_url(base_url, str(step["open"]))
            record["result"] = collectors.run_agent_browser_cmd(f"open {shlex.quote(url)}", session=session, capture_json=True, timeout=int(timeouts.get("page_load_ms", 120000) / 1000))

        elif "wait" in step:
            wait_arg = step["wait"]
            if isinstance(wait_arg, (int, float)):
                time.sleep(float(wait_arg))
                record["result"] = {"waited_seconds": float(wait_arg)}
            else:
                record["result"] = collectors.run_agent_browser_cmd(f"wait {wait_arg}", session=session, capture_json=True, timeout=int(timeouts.get("page_load_ms", 120000) / 1000))

        elif "checkpoint" in step:
            name = str(step["checkpoint"])
            cp = collectors.collect_checkpoint(
                name=name,
                session=session,
                out_dir=output_dir,
                mode="full",
                probe_cmds=probe_cmds,
                probe_cwd=probe_cwd,
                raw_value_patterns=raw_value_patterns,
                label_terms=label_terms,
                tool_payload_patterns=tool_payload_patterns,
                include_screenshot=True,
            )
            results["checkpoints"].append(cp)
            record["result"] = {"checkpoint": name}

        elif "action" in step:
            action = step.get("action")
            if isinstance(action, dict):
                action_type = action.get("type") or action.get("name")
                action_payload = action
            else:
                action_type = action
                action_payload = step

            if action_type == "run_ai_agent_explorer":
                prompt_text = ""
                if action_payload.get("prompt_file"):
                    prompt_text = Path(action_payload["prompt_file"]).read_text(encoding="utf-8")
                elif action_payload.get("prompt"):
                    prompt_text = str(action_payload["prompt"])

                completion_texts = action_payload.get("completion_texts") or ["Final Answer", "Ran"]
                completion_timeout_ms = int(action_payload.get("completion_timeout_ms") or timeouts.get("ai_response_ms", 600000))
                post_completion_timeout_ms = int(action_payload.get("post_completion_timeout_ms") or 60000)
                post_completion_stable_ms = int(action_payload.get("post_completion_stable_ms") or 1500)
                pre_min_count = int(action_payload.get("pre_min_count") or 1)
                run_buttons = action_payload.get("run_buttons")
                if not run_buttons:
                    run_button = action_payload.get("run_button") or "Run Agent"
                    run_buttons = [run_button, "Run"]

                result = action_run_ai_agent_explorer(
                    session=session,
                    prompt_text=prompt_text,
                    model=action_payload.get("model"),
                    prompt_selector=action_payload.get("prompt_selector"),
                    model_selector=action_payload.get("model_selector", "#edit-model"),
                    run_buttons=run_buttons,
                    completion_texts=completion_texts,
                    completion_timeout_ms=completion_timeout_ms,
                    post_completion_timeout_ms=post_completion_timeout_ms,
                    post_completion_stable_ms=post_completion_stable_ms,
                    pre_min_count=pre_min_count,
                )
                record["result"] = result
            else:
                record["error"] = f"Unknown action type: {action_type}"

        elif "command" in step:
            cmd = step["command"]
            record["result"] = collectors.run_agent_browser_cmd(cmd, session=session, capture_json=True, timeout=120)

        else:
            record["error"] = "Unknown step type"

        results["steps"].append(record)

    results["completed"] = collectors.now()
    return results


def build_login_steps(env: JSON) -> List[JSON]:
    if not env.get("admin_user"):
        return []
    return [
        {"open": env.get("login_url", "/user/login")},
        {"wait": "--load networkidle"},
        {"command": f"find label {shlex.quote('Username')} fill {shlex.quote(env.get('admin_user'))}"},
        {"command": f"find label {shlex.quote('Password')} fill {shlex.quote(env.get('admin_pass', ''))}"},
        {"command": f"find role button click --name {shlex.quote('Log in')}"},
        {"wait": "--load networkidle"},
    ]


def run_manifest(manifest: JSON, output_dir: Path) -> JSON:
    env = manifest.get("environment", {})
    strategy = manifest.get("strategy", {})
    steps = manifest.get("steps", [])
    base_url = env.get("base_url")
    mode = strategy.get("mode", "single")
    timeouts = strategy.get("timeouts", {})
    probe_cwd = env.get("probe_cwd") or env.get("project_root")

    probe_cmds = [probe.get("command") for probe in manifest.get("probes", []) if isinstance(probe, dict) and probe.get("command")]
    raw_value_patterns = strategy.get("raw_value_regex", []) if isinstance(strategy, dict) else []
    label_terms = strategy.get("label_terms", []) if isinstance(strategy, dict) else []
    tool_payload_patterns = strategy.get("tool_payload_regex", []) if isinstance(strategy, dict) else []

    all_steps = build_login_steps(env) + steps

    results: JSON = {
        "generated": collectors.now(),
        "mode": mode,
        "runs": {},
    }

    if mode == "single":
        run = execute_steps(
            steps=all_steps,
            base_url=base_url,
            session="intent",
            output_dir=output_dir,
            probe_cmds=probe_cmds,
            probe_cwd=probe_cwd,
            raw_value_patterns=raw_value_patterns,
            label_terms=label_terms,
            tool_payload_patterns=tool_payload_patterns,
            timeouts=timeouts,
        )
        results["runs"]["single"] = run
        return results

    between_cmd = strategy.get("between_cmd")

    baseline = execute_steps(
        steps=all_steps,
        base_url=base_url,
        session="intent_baseline",
        output_dir=output_dir / "baseline",
        probe_cmds=probe_cmds,
        probe_cwd=probe_cwd,
        raw_value_patterns=raw_value_patterns,
        label_terms=label_terms,
        tool_payload_patterns=tool_payload_patterns,
        timeouts=timeouts,
    )
    results["runs"]["baseline"] = baseline

    if between_cmd:
        results.setdefault("shell", {})["between"] = run_shell(between_cmd, cwd=probe_cwd)

    modified = execute_steps(
        steps=all_steps,
        base_url=base_url,
        session="intent_modified",
        output_dir=output_dir / "modified",
        probe_cmds=probe_cmds,
        probe_cwd=probe_cwd,
        raw_value_patterns=raw_value_patterns,
        label_terms=label_terms,
        tool_payload_patterns=tool_payload_patterns,
        timeouts=timeouts,
    )
    results["runs"]["modified"] = modified

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run intent tests from a manifest")
    parser.add_argument("manifest", help="Path to intent manifest (YAML or JSON)")
    parser.add_argument("--output-dir", default="./test_outputs", help="Output directory")
    parser.add_argument("--run-file", default="intent_run.json", help="Run results filename")
    parser.add_argument("--verdict-file", default="intent_verdict.json", help="Verdict filename")
    parser.add_argument("--judge-run", default="modified", help="Run name to judge (single|baseline|modified)")
    parser.add_argument("--run-key", dest="judge_run", help=argparse.SUPPRESS)
    args = parser.parse_args()

    manifest, errors = manifest_lib.load_and_validate(args.manifest)
    if errors:
        print("Manifest validation failed:")
        for err in errors:
            print(f"- {err}")
        return 2

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    run_payload = run_manifest(manifest, output_dir)
    run_payload["manifest"] = manifest
    run_path = output_dir / args.run_file
    run_path.write_text(json.dumps(run_payload, indent=2), encoding="utf-8")

    verdict_path = output_dir / args.verdict_file
    verdict = judge_intent.judge(manifest, run_payload.get("runs", {}).get(args.judge_run) or run_payload.get("runs", {}).get("single") or {})
    verdict_path.write_text(json.dumps(verdict, indent=2), encoding="utf-8")

    if verdict["verdict"] == "PASS":
        return 0
    if verdict["verdict"] == "FAIL":
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
