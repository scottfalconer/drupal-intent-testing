#!/usr/bin/env python3
"""
Shared collectors for intent-testing evidence packs.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

JSON = Dict[str, Any]

JSON_COMMANDS = {
    "open",
    "snapshot",
    "find",
    "wait",
    "get",
    "click",
    "fill",
    "press",
    "errors",
    "console",
    "tab",
    "frame",
    "eval",
}

TOOL_PAYLOAD_PATTERNS = [
    r"\bset_component_structure\b",
    r"\boperations:",
    r"\bcomponents:",
]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def should_add_json(cmd_parts: Sequence[str], *, want_json: bool) -> bool:
    if not want_json or not cmd_parts:
        return False
    if "--json" in cmd_parts:
        return False
    return cmd_parts[0] in JSON_COMMANDS


def run_agent_browser(
    cmd_parts: Sequence[str],
    *,
    session: str,
    want_json: bool = True,
    timeout: int = 120,
) -> JSON:
    argv = ["agent-browser", "--session", session] + list(cmd_parts)
    if should_add_json(cmd_parts, want_json=want_json):
        argv.append("--json")
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    rec: JSON = {
        "time": now(),
        "session": session,
        "argv": argv,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if want_json and result.stdout.strip():
        try:
            rec["parsed"] = json.loads(result.stdout)
        except json.JSONDecodeError:
            rec["parsed_error"] = "stdout was not valid JSON"
    return rec


def run_agent_browser_cmd(
    cmd: str,
    *,
    session: str,
    capture_json: bool = True,
    timeout: int = 120,
) -> JSON:
    cmd_parts = shlex.split(cmd)
    return run_agent_browser(cmd_parts, session=session, want_json=capture_json, timeout=timeout)


def write_record(path: Path, record: JSON) -> None:
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def extract_data_field(record: JSON) -> Any:
    parsed = record.get("parsed")
    if isinstance(parsed, dict) and "data" in parsed:
        return parsed.get("data")
    return None


def extract_text_field(record: JSON) -> Optional[str]:
    data = extract_data_field(record)
    if isinstance(data, str):
        return data
    return None


def extract_log_entries(record: JSON) -> List[Any]:
    data = extract_data_field(record)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("errors", "messages", "logs", "entries"):
            items = data.get(key)
            if isinstance(items, list):
                return items
        return []
    if data:
        return [data]
    return []


def collect_snapshot(*, session: str, out_file: Path) -> JSON:
    rec = run_agent_browser(["snapshot", "-i", "-c"], session=session, want_json=True, timeout=120)
    write_record(out_file, rec)
    return rec


def collect_screenshot(*, session: str, out_file: Path) -> JSON:
    return run_agent_browser(["screenshot", str(out_file)], session=session, want_json=False, timeout=120)


def collect_console(*, session: str, out_file: Path) -> JSON:
    rec = run_agent_browser(["console"], session=session, want_json=True, timeout=60)
    write_record(out_file, rec)
    return rec


def collect_errors(*, session: str, out_file: Path) -> JSON:
    rec = run_agent_browser(["errors"], session=session, want_json=True, timeout=60)
    write_record(out_file, rec)
    return rec


def collect_url(*, session: str) -> Tuple[Optional[str], JSON]:
    rec = run_agent_browser(["get", "url"], session=session, want_json=True, timeout=30)
    url = extract_text_field(rec)
    return url, rec


def collect_drupal_messages(*, session: str, out_file: Path) -> JSON:
    script = (
        "(() => {"
        "const statusEls = Array.from(document.querySelectorAll('[role=\"status\"]'));"
        "const alertEls = Array.from(document.querySelectorAll('[role=\"alert\"]'));"
        "const getText = (els) => els.map(e => (e.textContent || '').trim()).filter(Boolean).join('\\n');"
        "return {status: getText(statusEls) || null, alert: getText(alertEls) || null};"
        "})()"
    )
    rec = run_agent_browser(["eval", "--json", script], session=session, want_json=True, timeout=30)
    data = {}
    parsed = rec.get("parsed")
    if isinstance(parsed, dict) and "data" in parsed and isinstance(parsed["data"], dict):
        data = parsed["data"]
        if isinstance(data.get("result"), dict):
            data = data["result"]
        if isinstance(data.get("result"), dict):
            data = data["result"]
        if isinstance(data.get("result"), dict):
            data = data["result"]
    payload = {"time": now(), "data": data, "record": rec}
    write_record(out_file, payload)
    return payload


def _compile_patterns(patterns: Iterable[str]) -> List[re.Pattern]:
    compiled: List[re.Pattern] = []
    for raw in patterns:
        try:
            compiled.append(re.compile(raw))
        except re.error as exc:
            warnings.warn(f"Invalid regex pattern ignored: {raw!r} ({exc})")
    return compiled


def analyze_ai_output(
    *,
    final_answer: str,
    tool_payload: str,
    raw_value_patterns: Iterable[str],
    label_terms: Iterable[str],
) -> JSON:
    raw_patterns = _compile_patterns(raw_value_patterns)
    raw_matches_final: List[str] = []
    raw_matches_tool: List[str] = []
    for pattern in raw_patterns:
        raw_matches_final.extend(pattern.findall(final_answer or ""))
        raw_matches_tool.extend(pattern.findall(tool_payload or ""))
    raw_matches_final = sorted(set(raw_matches_final))
    raw_matches_tool = sorted(set(raw_matches_tool))

    label_terms_present = [
        term for term in label_terms if term and term.lower() in (final_answer or "").lower()
    ]
    return {
        "final_answer_len": len(final_answer or ""),
        "tool_payload_len": len(tool_payload or ""),
        "raw_in_final_answer": bool(raw_matches_final),
        "raw_in_tool_payload": bool(raw_matches_tool),
        "raw_matches_final_answer": raw_matches_final,
        "raw_matches_tool_payload": raw_matches_tool,
        "label_terms_present_in_final_answer": label_terms_present,
    }


def collect_ai_explorer_messages(
    *,
    session: str,
    out_file: Path,
    raw_value_patterns: Iterable[str],
    label_terms: Iterable[str],
    tool_payload_patterns: Iterable[str],
) -> Optional[JSON]:
    script = (
        "(() => {"
        "const pres = Array.from(document.querySelectorAll('.explorer-messages pre'))"
        ".map(p => p.textContent || '');"
        "const modelSelect = document.querySelector('#edit-model');"
        "let model = null;"
        "if (modelSelect && modelSelect.options && modelSelect.selectedIndex >= 0) {"
        "const opt = modelSelect.options[modelSelect.selectedIndex];"
        "model = {value: opt ? opt.value : null, label: opt ? (opt.textContent || '') : null};"
        "}"
        "return {pre_texts: pres, model: model};"
        "})()"
    )
    rec = run_agent_browser(["eval", "--json", script], session=session, want_json=True, timeout=60)
    data = {}
    parsed = rec.get("parsed")
    if isinstance(parsed, dict) and "data" in parsed and isinstance(parsed["data"], dict):
        data = parsed["data"]
        if isinstance(data.get("result"), dict):
            data = data["result"]
    pre_texts = data.get("pre_texts") if isinstance(data, dict) else []
    model = data.get("model") if isinstance(data, dict) else None

    if not isinstance(pre_texts, list):
        pre_texts = []

    final_answer = pre_texts[-1] if pre_texts else ""
    tool_payload = ""
    payload_patterns = _compile_patterns(tool_payload_patterns or TOOL_PAYLOAD_PATTERNS)
    for text in pre_texts:
        if any(p.search(text or "") for p in payload_patterns):
            tool_payload = text
            break

    summary = analyze_ai_output(
        final_answer=final_answer,
        tool_payload=tool_payload,
        raw_value_patterns=raw_value_patterns,
        label_terms=label_terms,
    )
    summary["ai_explorer_empty"] = not bool(pre_texts)
    if not pre_texts:
        reason = "no pre blocks found"
        if rec.get("parsed_error"):
            reason = "eval returned invalid JSON"
        elif rec.get("returncode", 0) != 0:
            reason = f"eval failed (rc={rec.get('returncode')})"
        summary["ai_explorer_reason"] = reason
    payload = {
        "time": now(),
        "data": {
            "pre_texts": pre_texts,
            "final_answer": final_answer,
            "tool_payload": tool_payload,
            "final_answer_snippet": (final_answer or "")[:300],
            "tool_payload_snippet": (tool_payload or "")[:300],
            "model": model,
        },
        "summary": summary,
        "record": rec,
    }
    write_record(out_file, payload)
    return payload


def summarize_log_record(record: JSON) -> JSON:
    count = len(extract_log_entries(record))
    return {"count": count}


def run_probe(cmd: Union[str, Sequence[str]], *, cwd: Optional[str] = None) -> JSON:
    if isinstance(cmd, (list, tuple)):
        argv = list(cmd)
        cmd_str = " ".join(shlex.quote(part) for part in argv)
    else:
        cmd_str = cmd
        try:
            argv = shlex.split(cmd)
        except ValueError as exc:
            return {
                "time": now(),
                "command": cmd,
                "cwd": cwd,
                "returncode": 2,
                "stdout": "",
                "stderr": "",
                "error": f"command parse error: {exc}",
            }
    if not argv:
        return {
            "time": now(),
            "command": cmd_str,
            "cwd": cwd,
            "returncode": 2,
            "stdout": "",
            "stderr": "",
            "error": "command was empty after parsing",
        }
    try:
        result = subprocess.run(argv, shell=False, capture_output=True, text=True, cwd=cwd)
    except OSError as exc:
        return {
            "time": now(),
            "command": cmd_str,
            "argv": argv,
            "cwd": cwd,
            "returncode": 2,
            "stdout": "",
            "stderr": "",
            "error": f"command execution failed: {exc}",
        }
    return {
        "time": now(),
        "command": cmd_str,
        "argv": argv,
        "cwd": cwd,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def collect_checkpoint(
    *,
    name: str,
    session: str,
    out_dir: Path,
    mode: str,
    probe_cmds: Iterable[str],
    probe_cwd: Optional[str],
    raw_value_patterns: Iterable[str],
    label_terms: Iterable[str],
    tool_payload_patterns: Iterable[str],
    include_screenshot: bool = True,
) -> JSON:
    checkpoint: JSON = {
        "name": name,
        "time": now(),
        "mode": mode,
        "artifacts": {},
        "summary": {},
        "errors": [],
    }

    url, url_rec = collect_url(session=session)
    checkpoint["url"] = url
    if url_rec.get("parsed_error"):
        checkpoint["errors"].append("url lookup returned invalid JSON")

    if mode in ("full", "snapshot"):
        snap_path = out_dir / f"{name}.snapshot.json"
        try:
            collect_snapshot(session=session, out_file=snap_path)
            checkpoint["artifacts"]["snapshot"] = str(snap_path)
        except Exception as exc:
            checkpoint["errors"].append(f"snapshot failed: {exc}")

    if include_screenshot and mode == "full":
        shot_path = out_dir / f"{name}.screenshot.png"
        try:
            collect_screenshot(session=session, out_file=shot_path)
            checkpoint["artifacts"]["screenshot"] = str(shot_path)
        except Exception as exc:
            checkpoint["errors"].append(f"screenshot failed: {exc}")

    console_path = out_dir / f"{name}.console.json"
    errors_path = out_dir / f"{name}.errors.json"
    try:
        console_rec = collect_console(session=session, out_file=console_path)
        checkpoint["artifacts"]["console"] = str(console_path)
        checkpoint["summary"]["console"] = summarize_log_record(console_rec)
    except Exception as exc:
        checkpoint["errors"].append(f"console failed: {exc}")

    try:
        errors_rec = collect_errors(session=session, out_file=errors_path)
        checkpoint["artifacts"]["errors"] = str(errors_path)
        checkpoint["summary"]["errors"] = summarize_log_record(errors_rec)
    except Exception as exc:
        checkpoint["errors"].append(f"errors failed: {exc}")

    if mode == "full":
        drupal_path = out_dir / f"{name}.drupal_messages.json"
        try:
            drupal_payload = collect_drupal_messages(session=session, out_file=drupal_path)
            checkpoint["artifacts"]["drupal_messages"] = str(drupal_path)
            checkpoint["summary"]["drupal_messages"] = drupal_payload.get("data")
        except Exception as exc:
            checkpoint["errors"].append(f"drupal messages failed: {exc}")

        ai_path = out_dir / f"{name}.ai_explorer.json"
        try:
            ai_payload = collect_ai_explorer_messages(
                session=session,
                out_file=ai_path,
                raw_value_patterns=raw_value_patterns,
                label_terms=label_terms,
                tool_payload_patterns=tool_payload_patterns,
            )
            checkpoint["artifacts"]["ai_explorer"] = str(ai_path)
            checkpoint["summary"]["ai_explorer"] = (ai_payload or {}).get("summary")
        except Exception as exc:
            checkpoint["errors"].append(f"ai explorer failed: {exc}")

        probe_paths: List[str] = []
        for idx, cmd in enumerate(probe_cmds, 1):
            try:
                rec = run_probe(cmd, cwd=probe_cwd)
                probe_path = out_dir / f"{name}.probe.{idx}.json"
                write_record(probe_path, rec)
                probe_paths.append(str(probe_path))
            except Exception as exc:
                checkpoint["errors"].append(f"probe {idx} failed: {exc}")
        if probe_paths:
            checkpoint["artifacts"]["probes"] = probe_paths

    return checkpoint
