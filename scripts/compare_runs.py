#!/usr/bin/env python3
"""
Paired comparison testing for Drupal using agent-browser.

Runs the same navigation script twice (baseline + modified), captures:
- accessibility snapshots (JSON)
- screenshots

Then produces a structured diff that is *less noisy* than raw snapshot string diffs by
normalizing the snapshot refs map into a sorted list of semantic element descriptors.

Design goals:
- Works from CLI agents (Claude Code / Codex) without vision models.
- Scenario scripts can use native agent-browser commands (find/wait/etc).
- Semi-deterministic A/B: you can restore DB/state between runs (e.g., ddev snapshot restore).

Script DSL:
- open /path            (prefix base URL)
- snapshot <name>       (snapshot -i -c --json)
- checkpoint <name>     (full evidence bundle)
- screenshot <file.png> (screenshot)
- wait <seconds>        (sleep) OR wait <agent-browser wait args...> (passed through)
- expect <args...>      (assert/await; passed to agent-browser wait)
- any other line is passed through as raw agent-browser command
"""

import argparse
import json
import shlex
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from difflib import unified_diff
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from scripts import collectors


JSON = Dict[str, Any]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_split(args: str) -> Tuple[Optional[List[str]], Optional[str]]:
    try:
        return shlex.split(args), None
    except ValueError as exc:
        return None, str(exc)


def _load_json_file(path: Path) -> Tuple[Optional[JSON], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, f"failed to parse JSON: {exc}"


def run_cmd(cmd: str, *, session: str, capture_json: bool = True, timeout: int = 120) -> JSON:
    return collectors.run_agent_browser_cmd(cmd, session=session, capture_json=capture_json, timeout=timeout)


def parse_script(script_path: str) -> List[JSON]:
    commands: List[JSON] = []
    with open(script_path, "r", encoding="utf-8") as f:
        for line_num, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(maxsplit=1)
            cmd_type = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            commands.append({"line": line_num, "type": cmd_type, "args": args, "raw": line})
    return commands


def _prefix_url(base_url: str, maybe_path: str) -> str:
    if maybe_path.startswith("http://") or maybe_path.startswith("https://"):
        return maybe_path
    if maybe_path.startswith("/"):
        return base_url.rstrip("/") + maybe_path
    # Treat as relative path segment
    return base_url.rstrip("/") + "/" + maybe_path


def _try_parse_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except Exception:
        return None


def normalize_snapshot(snapshot_json: JSON) -> List[JSON]:
    """
    Normalize agent-browser snapshot JSON into a stable list.

    Expected format:
      {"success":true,"data":{"snapshot":"...","refs":{"e1":{"role":"heading","name":"Title"},...}}}

    We discard:
      - the snapshot string (contains ref ids, can be noisy)
      - the ref keys themselves (e1/e2...), since they can renumber
    """
    if not isinstance(snapshot_json, dict):
        return []
    if "data" in snapshot_json and isinstance(snapshot_json.get("data"), dict):
        data = snapshot_json["data"]
    else:
        data = snapshot_json
    refs = {}
    if isinstance(data, dict):
        refs = data.get("refs", {}) or {}
    elements: List[JSON] = []
    if isinstance(refs, dict):
        for _ref, info in refs.items():
            if not isinstance(info, dict):
                continue
            el: JSON = {}
            for k in ("role", "name", "value", "level", "checked", "disabled", "selected", "expanded", "pressed", "href"):
                if k in info:
                    el[k] = info[k]
            # Keep only meaningful entries (but preserve booleans if present).
            if el.get("role") or el.get("name"):
                elements.append(el)

    elements.sort(key=lambda e: (
        str(e.get("role", "")),
        str(e.get("name", "")),
        str(e.get("value", "")),
        str(e.get("href", "")),
    ))
    return elements


def element_summary(elements: List[JSON]) -> JSON:
    by_role: Dict[str, int] = {}
    for el in elements:
        role = str(el.get("role", ""))
        if role:
            by_role[role] = by_role.get(role, 0) + 1
    return {
        "count": len(elements),
        "by_role": dict(sorted(by_role.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def extract_snapshot_payload(snapshot_raw: JSON) -> Tuple[Optional[JSON], Optional[str]]:
    if not isinstance(snapshot_raw, dict):
        return None, "snapshot file was not a JSON object"
    if snapshot_raw.get("parsed_error"):
        return None, str(snapshot_raw["parsed_error"])
    if "stdout" in snapshot_raw and "parsed" not in snapshot_raw and "success" not in snapshot_raw and "data" not in snapshot_raw:
        return None, "snapshot missing parsed payload"
    if "parsed" in snapshot_raw:
        parsed = snapshot_raw.get("parsed")
        if isinstance(parsed, dict):
            return parsed, None
        return None, "snapshot parsed payload was missing or invalid"
    if "success" in snapshot_raw and "data" in snapshot_raw:
        return snapshot_raw, None
    if "data" in snapshot_raw or "refs" in snapshot_raw:
        return snapshot_raw, None
    return None, "unrecognized snapshot format"


def diff_normalized(a: List[JSON], b: List[JSON], fromfile: str, tofile: str) -> List[str]:
    a_str = json.dumps(a, indent=2, sort_keys=True)
    b_str = json.dumps(b, indent=2, sort_keys=True)
    return list(unified_diff(
        a_str.splitlines(),
        b_str.splitlines(),
        fromfile=fromfile,
        tofile=tofile,
        lineterm=""
    ))


def compare_snapshots(baseline_snap_path: Path, modified_snap_path: Path) -> JSON:
    baseline_raw, baseline_parse_err = _load_json_file(baseline_snap_path)
    modified_raw, modified_parse_err = _load_json_file(modified_snap_path)
    if baseline_parse_err or modified_parse_err:
        return {
            "same": False,
            "baseline": {"file": str(baseline_snap_path), "error": baseline_parse_err},
            "modified": {"file": str(modified_snap_path), "error": modified_parse_err},
            "changes": {"added": [], "removed": [], "added_count": 0, "removed_count": 0},
            "diff_lines": [],
            "error": {"baseline": baseline_parse_err, "modified": modified_parse_err},
        }

    baseline_for_norm, baseline_err = extract_snapshot_payload(baseline_raw)
    modified_for_norm, modified_err = extract_snapshot_payload(modified_raw)
    if baseline_err or modified_err:
        return {
            "same": False,
            "baseline": {"file": str(baseline_snap_path), "error": baseline_err},
            "modified": {"file": str(modified_snap_path), "error": modified_err},
            "changes": {"added": [], "removed": [], "added_count": 0, "removed_count": 0},
            "diff_lines": [],
            "error": {"baseline": baseline_err, "modified": modified_err},
        }

    base_norm = normalize_snapshot(baseline_for_norm or {})
    mod_norm = normalize_snapshot(modified_for_norm or {})

    same = base_norm == mod_norm

    # Provide a compact set-like view: (role,name) pairs.
    def key(el: JSON) -> Tuple[str, str]:
        return (str(el.get("role", "")), str(el.get("name", "")))

    base_counts = Counter(key(el) for el in base_norm)
    mod_counts = Counter(key(el) for el in mod_norm)

    added = mod_counts - base_counts
    removed = base_counts - mod_counts

    def _expanded(counter: Counter) -> List[Tuple[str, str, int]]:
        items: List[Tuple[str, str, int]] = []
        for (role, name), count in counter.items():
            items.append((role, name, count))
        return sorted(items, key=lambda item: (item[0], item[1], item[2]))

    return {
        "same": same,
        "baseline": {
            "file": str(baseline_snap_path),
            "summary": element_summary(base_norm),
        },
        "modified": {
            "file": str(modified_snap_path),
            "summary": element_summary(mod_norm),
        },
        "changes": {
            "added": [{"role": r, "name": n, "count": c} for r, n, c in _expanded(added)[:50]],
            "removed": [{"role": r, "name": n, "count": c} for r, n, c in _expanded(removed)[:50]],
            "added_count": sum(added.values()),
            "removed_count": sum(removed.values()),
        },
        "diff_lines": [] if same else diff_normalized(base_norm, mod_norm, f"baseline/{baseline_snap_path.name}", f"modified/{modified_snap_path.name}"),
    }


def extract_log_entries(log_raw: JSON) -> Tuple[List[Any], Optional[str]]:
    if not isinstance(log_raw, dict):
        return [], "log file was not a JSON object"
    if log_raw.get("parsed_error"):
        return [], str(log_raw["parsed_error"])
    if "stdout" in log_raw and "parsed" not in log_raw and "success" not in log_raw and "data" not in log_raw:
        return [], "log missing parsed payload"
    payload = log_raw.get("parsed", log_raw)
    if isinstance(payload, dict) and "data" in payload:
        data = payload["data"]
    else:
        data = payload
    if data is None:
        return [], None
    if isinstance(data, list):
        return data, None
    return [data], None


def normalize_entries(entries: List[Any]) -> List[str]:
    normalized = []
    for entry in entries:
        try:
            normalized.append(json.dumps(entry, sort_keys=True))
        except TypeError:
            normalized.append(str(entry))
    return sorted(normalized)


def compare_logs(baseline_path: Path, modified_path: Path) -> JSON:
    base_raw = json.loads(baseline_path.read_text(encoding="utf-8"))
    mod_raw = json.loads(modified_path.read_text(encoding="utf-8"))

    base_entries, base_err = extract_log_entries(base_raw)
    mod_entries, mod_err = extract_log_entries(mod_raw)

    if base_err or mod_err:
        return {
            "same": False,
            "baseline": {"file": str(baseline_path), "error": base_err},
            "modified": {"file": str(modified_path), "error": mod_err},
            "error": {"baseline": base_err, "modified": mod_err},
        }

    base_norm = normalize_entries(base_entries)
    mod_norm = normalize_entries(mod_entries)
    same = base_norm == mod_norm

    return {
        "same": same,
        "baseline": {
            "file": str(baseline_path),
            "summary": {"count": len(base_entries), "sample": base_entries[:20]},
        },
        "modified": {
            "file": str(modified_path),
            "summary": {"count": len(mod_entries), "sample": mod_entries[:20]},
        },
    }


def load_json(path: Path) -> Tuple[Optional[JSON], Optional[str]]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, str(exc)


def diff_text(a: str, b: str, fromfile: str, tofile: str) -> List[str]:
    return list(unified_diff(
        (a or "").splitlines(),
        (b or "").splitlines(),
        fromfile=fromfile,
        tofile=tofile,
        lineterm="",
    ))


def extract_drupal_messages(payload: JSON) -> JSON:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        data = payload["data"]
    else:
        data = {}
    return {
        "status": data.get("status"),
        "alert": data.get("alert"),
    }


def compare_drupal_messages(baseline_path: Path, modified_path: Path) -> JSON:
    base_raw, base_err = load_json(baseline_path)
    mod_raw, mod_err = load_json(modified_path)
    if base_err or mod_err or base_raw is None or mod_raw is None:
        return {
            "same": False,
            "baseline": {"file": str(baseline_path), "error": base_err},
            "modified": {"file": str(modified_path), "error": mod_err},
            "error": {"baseline": base_err, "modified": mod_err},
        }
    base_msg = extract_drupal_messages(base_raw)
    mod_msg = extract_drupal_messages(mod_raw)
    same = base_msg == mod_msg
    diff_status = [] if base_msg.get("status") == mod_msg.get("status") else diff_text(
        base_msg.get("status") or "",
        mod_msg.get("status") or "",
        f"baseline/{baseline_path.name}",
        f"modified/{modified_path.name}",
    )
    diff_alert = [] if base_msg.get("alert") == mod_msg.get("alert") else diff_text(
        base_msg.get("alert") or "",
        mod_msg.get("alert") or "",
        f"baseline/{baseline_path.name}",
        f"modified/{modified_path.name}",
    )
    return {
        "same": same,
        "baseline": {"file": str(baseline_path), "data": base_msg},
        "modified": {"file": str(modified_path), "data": mod_msg},
        "diffs": {"status": diff_status, "alert": diff_alert},
    }


def extract_ai_explorer(payload: JSON) -> JSON:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = {}
    pre_texts = data.get("pre_texts") if isinstance(data.get("pre_texts"), list) else []
    final_answer = data.get("final_answer") or (pre_texts[-1] if pre_texts else "")
    tool_payload = data.get("tool_payload") or ""
    model = data.get("model")
    summary = payload.get("summary") if isinstance(payload, dict) else None
    return {
        "pre_texts": pre_texts,
        "final_answer": final_answer,
        "tool_payload": tool_payload,
        "model": model,
        "summary": summary,
    }


def compare_ai_explorer(baseline_path: Path, modified_path: Path) -> JSON:
    base_raw, base_err = load_json(baseline_path)
    mod_raw, mod_err = load_json(modified_path)
    if base_err or mod_err or base_raw is None or mod_raw is None:
        return {
            "same": False,
            "baseline": {"file": str(baseline_path), "error": base_err},
            "modified": {"file": str(modified_path), "error": mod_err},
            "error": {"baseline": base_err, "modified": mod_err},
        }
    base_data = extract_ai_explorer(base_raw)
    mod_data = extract_ai_explorer(mod_raw)
    same = base_data.get("final_answer") == mod_data.get("final_answer") and base_data.get("tool_payload") == mod_data.get("tool_payload") and base_data.get("pre_texts") == mod_data.get("pre_texts")
    final_diff = [] if base_data.get("final_answer") == mod_data.get("final_answer") else diff_text(
        base_data.get("final_answer") or "",
        mod_data.get("final_answer") or "",
        f"baseline/{baseline_path.name}",
        f"modified/{modified_path.name}",
    )
    tool_diff = [] if base_data.get("tool_payload") == mod_data.get("tool_payload") else diff_text(
        base_data.get("tool_payload") or "",
        mod_data.get("tool_payload") or "",
        f"baseline/{baseline_path.name}",
        f"modified/{modified_path.name}",
    )
    return {
        "same": same,
        "baseline": {"file": str(baseline_path), "data": base_data},
        "modified": {"file": str(modified_path), "data": mod_data},
        "diffs": {"final_answer": final_diff, "tool_payload": tool_diff},
    }


def compare_probes(baseline_paths: List[str], modified_paths: List[str]) -> JSON:
    comparisons: List[JSON] = []
    total = max(len(baseline_paths), len(modified_paths))
    changed = 0
    for idx in range(total):
        base_path = Path(baseline_paths[idx]) if idx < len(baseline_paths) else None
        mod_path = Path(modified_paths[idx]) if idx < len(modified_paths) else None
        if not base_path or not mod_path:
            comparisons.append({
                "index": idx + 1,
                "baseline": str(base_path) if base_path else None,
                "modified": str(mod_path) if mod_path else None,
                "same": False,
                "error": "missing probe result",
            })
            changed += 1
            continue
        base_raw, base_err = load_json(base_path)
        mod_raw, mod_err = load_json(mod_path)
        if base_err or mod_err or base_raw is None or mod_raw is None:
            comparisons.append({
                "index": idx + 1,
                "baseline": str(base_path),
                "modified": str(mod_path),
                "same": False,
                "error": {"baseline": base_err, "modified": mod_err},
            })
            changed += 1
            continue
        base_rc = base_raw.get("returncode")
        mod_rc = mod_raw.get("returncode")
        base_out = base_raw.get("stdout") or ""
        mod_out = mod_raw.get("stdout") or ""
        base_err_txt = base_raw.get("stderr") or ""
        mod_err_txt = mod_raw.get("stderr") or ""
        same = base_rc == mod_rc and base_out == mod_out and base_err_txt == mod_err_txt
        if not same:
            changed += 1
        comparisons.append({
            "index": idx + 1,
            "baseline": {"file": str(base_path), "returncode": base_rc},
            "modified": {"file": str(mod_path), "returncode": mod_rc},
            "diffs": {
                "stdout": [] if base_out == mod_out else diff_text(base_out, mod_out, f"baseline/{base_path.name}", f"modified/{mod_path.name}"),
                "stderr": [] if base_err_txt == mod_err_txt else diff_text(base_err_txt, mod_err_txt, f"baseline/{base_path.name}", f"modified/{mod_path.name}"),
            },
            "same": same,
        })
    return {"same": changed == 0, "changed": changed, "entries": comparisons}


def build_markdown_report(report: JSON) -> str:
    lines: List[str] = []
    lines.append("# Drupal Intent Testing Comparison")
    lines.append("")
    lines.append(f"**Generated:** {report.get('generated')}")
    cfg = report.get("config", {})
    lines.append(f"**Site:** {cfg.get('url')}")
    lines.append(f"**Script:** {cfg.get('script')}")
    lines.append(f"**Verdict:** {report.get('summary', {}).get('verdict')}")
    lines.append("")
    summary = report.get("summary", {})
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Checkpoints total: {summary.get('checkpoints_total')}")
    lines.append(f"- Snapshot matching: {summary.get('matching')}")
    lines.append(f"- Snapshot different: {summary.get('different')}")
    lines.append(f"- Missing checkpoints: {summary.get('missing')}")
    lines.append(f"- Errors: {summary.get('errors')}")
    lines.append("")

    comparison = report.get("comparison", {})
    missing = comparison.get("missing_checkpoints", [])
    if missing:
        lines.append("## Missing checkpoints")
        lines.append("")
        for item in missing:
            lines.append(f"- {item.get('checkpoint')} (baseline={item.get('baseline')}, modified={item.get('modified')})")
        lines.append("")

    errors = comparison.get("errors", [])
    if errors:
        lines.append("## Errors")
        lines.append("")
        for item in errors:
            lines.append(f"- {item.get('checkpoint')}: {item.get('snapshot')}")
        lines.append("")

    changed_checkpoints = comparison.get("changed_checkpoints", [])
    if changed_checkpoints:
        lines.append("## Changed checkpoints")
        lines.append("")
        for name in changed_checkpoints:
            cp = comparison.get("checkpoints", {}).get(name, {})
            parts = []
            if cp.get("snapshot", {}).get("same") is False:
                parts.append("snapshot")
            if cp.get("drupal_messages", {}).get("same") is False:
                parts.append("drupal_messages")
            if cp.get("ai_explorer", {}).get("same") is False:
                parts.append("ai_explorer")
            if cp.get("console", {}).get("same") is False:
                parts.append("console")
            if cp.get("errors", {}).get("same") is False:
                parts.append("errors")
            if cp.get("probes", {}).get("same") is False:
                parts.append("probes")
            parts_txt = ", ".join(parts) if parts else "details"
            lines.append(f"- {name}: {parts_txt}")
            ai = cp.get("ai_explorer")
            if ai and ai.get("baseline") and ai.get("modified"):
                base_summary = (ai.get("baseline", {}).get("data") or {}).get("summary") or {}
                mod_summary = (ai.get("modified", {}).get("data") or {}).get("summary") or {}
                if base_summary or mod_summary:
                    lines.append(
                        f"- {name} AI summary: raw_in_final_answer {base_summary.get('raw_in_final_answer')} -> {mod_summary.get('raw_in_final_answer')}, "
                        f"raw_in_tool_payload {base_summary.get('raw_in_tool_payload')} -> {mod_summary.get('raw_in_tool_payload')}"
                    )
            dm = cp.get("drupal_messages")
            if dm and dm.get("baseline") and dm.get("modified"):
                base_msg = dm.get("baseline", {}).get("data") or {}
                mod_msg = dm.get("modified", {}).get("data") or {}
                if base_msg != mod_msg:
                    lines.append(
                        f"- {name} Drupal messages: status={base_msg.get('status')} -> {mod_msg.get('status')}, "
                        f"alert={base_msg.get('alert')} -> {mod_msg.get('alert')}"
                    )
        lines.append("")

    baseline_trace = (report.get("baseline") or {}).get("trace")
    modified_trace = (report.get("modified") or {}).get("trace")
    if baseline_trace or modified_trace:
        lines.append("## Artifacts")
        lines.append("")
        if baseline_trace:
            lines.append(f"- baseline trace: `{baseline_trace}`")
        if modified_trace:
            lines.append(f"- modified trace: `{modified_trace}`")
        lines.append("")

    return "\n".join(lines)


def execute_script(
    commands: List[JSON],
    *,
    base_url: str,
    session: str,
    out_dir: Path,
    stop_on_fail: bool,
    probe_cmds: List[str],
    probe_cwd: Optional[str],
    raw_value_patterns: List[str],
    label_terms: List[str],
    tool_payload_patterns: List[str],
) -> JSON:
    run_dir = out_dir / session
    run_dir.mkdir(parents=True, exist_ok=True)
    assert_dir = run_dir / "assertions"
    extract_dir = run_dir / "extracts"
    probe_dir = run_dir / "probes"
    assert_dir.mkdir(parents=True, exist_ok=True)
    extract_dir.mkdir(parents=True, exist_ok=True)
    probe_dir.mkdir(parents=True, exist_ok=True)

    results: JSON = {
        "session": session,
        "started": _now(),
        "base_url": base_url,
        "commands": [],
        "checkpoints": [],
        "assertions": [],
        "extracts": {},
        "probes": {},
        "snapshots": {},   # name -> file
        "screenshots": [], # list of files
        "logs": {          # name -> file
            "errors": {},
            "console": {},
        },
        "artifacts": {
            "drupal_messages": {},
            "ai_explorer": {},
            "probes": {},
        },
    }

    for cmd in commands:
        entry: JSON = {"command": cmd, "result": None}
        try:
            ctype = cmd["type"]
            args = cmd["args"]

            if ctype == "open":
                url = _prefix_url(base_url, args)
                entry["result"] = run_cmd(f'open "{url}"', session=session, capture_json=True, timeout=120)

            elif ctype in ("checkpoint", "snapshot"):
                if ctype == "checkpoint":
                    name = args.strip() or f"checkpoint_{len(results['checkpoints'])+1}"
                    mode = "full"
                    include_screenshot = True
                else:
                    name = args.strip() or f"snapshot_{len(results['snapshots'])+1}"
                    mode = "snapshot"
                    include_screenshot = False
                cp = collectors.collect_checkpoint(
                    name=name,
                    session=session,
                    out_dir=run_dir,
                    mode=mode,
                    probe_cmds=probe_cmds,
                    probe_cwd=probe_cwd,
                    raw_value_patterns=raw_value_patterns,
                    label_terms=label_terms,
                    tool_payload_patterns=tool_payload_patterns,
                    include_screenshot=include_screenshot,
                )
                entry["result"] = {"checkpoint": name}
                results["checkpoints"].append(cp)
                artifacts = cp.get("artifacts", {})
                if artifacts.get("snapshot"):
                    results["snapshots"][name] = artifacts["snapshot"]
                if artifacts.get("screenshot"):
                    results["screenshots"].append(artifacts["screenshot"])
                if artifacts.get("console"):
                    results["logs"]["console"][name] = artifacts["console"]
                if artifacts.get("errors"):
                    results["logs"]["errors"][name] = artifacts["errors"]
                if artifacts.get("drupal_messages"):
                    results["artifacts"]["drupal_messages"][name] = artifacts["drupal_messages"]
                if artifacts.get("ai_explorer"):
                    results["artifacts"]["ai_explorer"][name] = artifacts["ai_explorer"]
                if artifacts.get("probes"):
                    results["artifacts"]["probes"][name] = artifacts["probes"]

            elif ctype == "screenshot":
                name = args.strip() or f"screenshot_{len(results['screenshots'])+1}.png"
                if not name.lower().endswith(".png"):
                    name += ".png"
                shot_path = run_dir / name
                # screenshot can take time; don't force --json
                entry["result"] = run_cmd(f'screenshot "{shot_path}"', session=session, capture_json=False, timeout=120)
                results["screenshots"].append(str(shot_path))

            elif ctype == "wait":
                # Two forms:
                #   wait 2
                #   wait --load networkidle
                raw = args.strip()
                if not raw:
                    time.sleep(1.0)
                    entry["result"] = {"waited_seconds": 1.0}
                else:
                    maybe = _try_parse_float(raw)
                    if maybe is not None:
                        time.sleep(maybe)
                        entry["result"] = {"waited_seconds": maybe}
                    else:
                        # Pass through to agent-browser wait
                        entry["result"] = run_cmd(f"wait {raw}", session=session, capture_json=True, timeout=120)

            elif ctype == "expect":
                raw = args.strip()
                if not raw:
                    entry["result"] = {"returncode": 1, "stderr": "expect requires an argument"}
                else:
                    if raw.startswith("--"):
                        entry["result"] = run_cmd(f"wait {raw}", session=session, capture_json=True, timeout=120)
                    else:
                        parts = raw.split(maxsplit=1)
                        if parts[0].lower() == "text" and len(parts) > 1:
                            entry["result"] = run_cmd(f"wait --text {parts[1]}", session=session, capture_json=True, timeout=120)
                        elif parts[0].lower() == "selector" and len(parts) > 1:
                            entry["result"] = run_cmd(f"wait {parts[1]}", session=session, capture_json=True, timeout=120)
                        else:
                            entry["result"] = run_cmd(f"wait {raw}", session=session, capture_json=True, timeout=120)

            elif ctype == "extract":
                tokens, split_err = _safe_split(args)
                if split_err:
                    entry["result"] = {"returncode": 2, "stderr": f"extract parse error: {split_err}"}
                elif not tokens or len(tokens) < 2:
                    entry["result"] = {"returncode": 1, "stderr": "extract requires a type and name"}
                else:
                    extract_type = tokens[0]
                    name = tokens[1]
                    payload = {}
                    out_path = extract_dir / f"{name}.json"
                    if extract_type == "eval":
                        js = " ".join(tokens[2:]) if len(tokens) > 2 else ""
                        rec = run_cmd(f"eval --json {js}", session=session, capture_json=True, timeout=120)
                        payload = {"record": rec}
                    elif extract_type == "text":
                        locator = " ".join(tokens[2:]) if len(tokens) > 2 else ""
                        rec = run_cmd(f"get text {locator}", session=session, capture_json=True, timeout=120)
                        payload = {"record": rec}
                    else:
                        payload = {"error": f"unknown extract type: {extract_type}"}
                    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    results["extracts"][name] = str(out_path)
                    entry["result"] = {"extract": name, "path": str(out_path)}

            elif ctype == "probe":
                tokens, split_err = _safe_split(args)
                if split_err:
                    entry["result"] = {"returncode": 2, "stderr": f"probe parse error: {split_err}"}
                elif not tokens or len(tokens) < 2:
                    entry["result"] = {"returncode": 1, "stderr": "probe requires type and name"}
                else:
                    probe_type = tokens[0]
                    name = tokens[1]
                    cmd_parts: List[str] = []
                    if probe_type == "shell":
                        if "--" in tokens:
                            idx = tokens.index("--")
                            cmd_parts = tokens[idx + 1 :]
                        else:
                            cmd_parts = tokens[2:]
                    elif probe_type == "drush":
                        if "--" in tokens:
                            idx = tokens.index("--")
                            cmd_parts = ["drush"] + tokens[idx + 1 :]
                        else:
                            cmd_parts = ["drush"] + tokens[2:]
                    else:
                        entry["result"] = {"returncode": 1, "stderr": f"unknown probe type: {probe_type}"}
                        results["commands"].append(entry)
                        continue
                    rec = collectors.run_probe(cmd_parts, cwd=probe_cwd)
                    out_path = probe_dir / f"{name}.json"
                    collectors.write_record(out_path, rec)
                    results["probes"][name] = str(out_path)
                    entry["result"] = {"probe": name, "path": str(out_path), "returncode": rec.get("returncode")}

            elif ctype.startswith("assert-"):
                assert_type = ctype
                tokens, split_err = _safe_split(args)
                if split_err:
                    entry["result"] = {"returncode": 2, "stderr": f"assert parse error: {split_err}"}
                    results["commands"].append(entry)
                    if stop_on_fail:
                        entry["fatal"] = True
                    continue
                if not tokens:
                    entry["result"] = {"returncode": 1, "stderr": "assert requires arguments"}
                    results["commands"].append(entry)
                    if stop_on_fail:
                        entry["fatal"] = True
                    continue
                assert_id = None
                if "--id" in tokens:
                    idx = tokens.index("--id")
                    if idx + 1 < len(tokens):
                        assert_id = tokens[idx + 1]
                        del tokens[idx:idx + 2]
                if not assert_id:
                    assert_id = f"{assert_type}-{len(results['assertions'])+1}"
                result = {
                    "id": assert_id,
                    "type": assert_type,
                    "passed": False,
                    "message": "",
                }
                evidence_path = assert_dir / f"{assert_id}.json"

                def _write_evidence(payload: JSON) -> None:
                    evidence_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    result["evidence"] = str(evidence_path)

                if assert_type == "assert-present":
                    if "--text" in tokens:
                        idx = tokens.index("--text")
                        text = tokens[idx + 1] if idx + 1 < len(tokens) else ""
                        rec = run_cmd(f"wait --text {shlex.quote(text)}", session=session, capture_json=True, timeout=120)
                        _write_evidence({"record": rec})
                        result["passed"] = rec.get("returncode", 1) == 0
                        result["message"] = f"text {'found' if result['passed'] else 'not found'}: {text}"
                    elif "--selector" in tokens:
                        idx = tokens.index("--selector")
                        selector = tokens[idx + 1] if idx + 1 < len(tokens) else ""
                        rec = run_cmd(f"wait {selector}", session=session, capture_json=True, timeout=120)
                        _write_evidence({"record": rec})
                        result["passed"] = rec.get("returncode", 1) == 0
                        result["message"] = f"selector {'found' if result['passed'] else 'not found'}: {selector}"
                    else:
                        result["message"] = "assert-present requires --text or --selector"

                elif assert_type == "assert-absent":
                    if "--text" in tokens:
                        idx = tokens.index("--text")
                        text = tokens[idx + 1] if idx + 1 < len(tokens) else ""
                        js = f"(() => !document.body.innerText.includes({json.dumps(text)}))()"
                        rec = run_cmd(f"eval --json {shlex.quote(js)}", session=session, capture_json=True, timeout=60)
                        _write_evidence({"record": rec})
                        data = rec.get("parsed", {}).get("data") if isinstance(rec.get("parsed"), dict) else None
                        result["passed"] = bool(data)
                        result["message"] = f"text absent: {text}"
                    elif "--selector" in tokens:
                        idx = tokens.index("--selector")
                        selector = tokens[idx + 1] if idx + 1 < len(tokens) else ""
                        js = f"(() => document.querySelectorAll({json.dumps(selector)}).length === 0)()"
                        rec = run_cmd(f"eval --json {shlex.quote(js)}", session=session, capture_json=True, timeout=60)
                        _write_evidence({"record": rec})
                        data = rec.get("parsed", {}).get("data") if isinstance(rec.get("parsed"), dict) else None
                        result["passed"] = bool(data)
                        result["message"] = f"selector absent: {selector}"
                    else:
                        result["message"] = "assert-absent requires --text or --selector"

                elif assert_type == "assert-no-js-errors":
                    rec = run_cmd("errors", session=session, capture_json=True, timeout=60)
                    entries = collectors.extract_log_entries(rec)
                    _write_evidence({"record": rec, "count": len(entries)})
                    result["passed"] = len(entries) == 0
                    result["message"] = f"JS errors count: {len(entries)}"

                elif assert_type == "assert-no-drupal-alerts":
                    drupal_path = assert_dir / f"{assert_id}.drupal_messages.json"
                    payload = collectors.collect_drupal_messages(session=session, out_file=drupal_path)
                    result["evidence"] = str(drupal_path)
                    alert = payload.get("data", {}).get("alert") if isinstance(payload, dict) else None
                    result["passed"] = not alert
                    result["message"] = "no Drupal alert messages"

                elif assert_type == "assert-url":
                    if "--contains" in tokens:
                        idx = tokens.index("--contains")
                        part = tokens[idx + 1] if idx + 1 < len(tokens) else ""
                        url, _rec = collectors.collect_url(session=session)
                        _write_evidence({"url": url})
                        result["passed"] = bool(url and part in url)
                        result["message"] = f"url contains {part}"
                    else:
                        result["message"] = "assert-url requires --contains"

                elif assert_type == "assert-count":
                    selector = None
                    expected = None
                    if "--selector" in tokens:
                        idx = tokens.index("--selector")
                        selector = tokens[idx + 1] if idx + 1 < len(tokens) else None
                    if "--eq" in tokens:
                        idx = tokens.index("--eq")
                        if idx + 1 < len(tokens):
                            try:
                                expected = int(tokens[idx + 1])
                            except ValueError:
                                expected = None
                    if selector is None or expected is None:
                        result["message"] = "assert-count requires --selector and integer --eq"
                    else:
                        js = f"(() => document.querySelectorAll({json.dumps(selector)}).length)()"
                        rec = run_cmd(f"eval --json {shlex.quote(js)}", session=session, capture_json=True, timeout=60)
                        _write_evidence({"record": rec})
                        data = rec.get("parsed", {}).get("data") if isinstance(rec.get("parsed"), dict) else None
                        result["passed"] = data == expected
                        result["message"] = f"selector count {data} == {expected}"

                results["assertions"].append(result)
                result["returncode"] = 0 if result.get("passed") else 1
                entry["result"] = result

            else:
                # Pass-through raw agent-browser command:
                # e.g. "find label \"Username\" fill \"admin\""
                entry["result"] = run_cmd(cmd["raw"], session=session, capture_json=True, timeout=120)

            # Stop early on failures if requested.
            if stop_on_fail:
                res = entry.get("result", {})
                if isinstance(res, dict) and res.get("returncode", 0) != 0:
                    entry["fatal"] = True
                    results["commands"].append(entry)
                    break

        except Exception as e:
            entry["error"] = str(e)
            if stop_on_fail:
                entry["fatal"] = True
                results["commands"].append(entry)
                break

        results["commands"].append(entry)

    results["completed"] = _now()
    return results


def run_shell(cmd: str) -> JSON:
    tokens, split_err = _safe_split(cmd)
    if split_err:
        return {
            "time": _now(),
            "command": cmd,
            "returncode": 2,
            "stdout": "",
            "stderr": "",
            "error": f"command parse error: {split_err}",
        }
    if not tokens:
        return {
            "time": _now(),
            "command": cmd,
            "returncode": 2,
            "stdout": "",
            "stderr": "",
            "error": "command was empty after parsing",
        }
    try:
        result = subprocess.run(tokens, shell=False, capture_output=True, text=True)
    except OSError as exc:
        return {
            "time": _now(),
            "command": cmd,
            "argv": tokens,
            "returncode": 2,
            "stdout": "",
            "stderr": "",
            "error": f"command execution failed: {exc}",
        }
    return {
        "time": _now(),
        "command": cmd,
        "argv": tokens,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Paired A/B comparison testing for Drupal using agent-browser")
    parser.add_argument("--url", required=True, help="Base URL of the Drupal site, e.g. https://my.ddev.site")
    parser.add_argument("--script", required=True, help="Path to scenario script (.txt)")
    parser.add_argument("--output-dir", default="./test_outputs", help="Directory for artifacts")
    parser.add_argument("--output", default="comparison_report.json", help="Report filename (written under output-dir)")
    parser.add_argument("--output-md", default="comparison_report.md", help="Markdown summary filename (written under output-dir)")
    parser.add_argument("--no-pause", action="store_true", help="Do not pause between baseline and modified runs (required for CI/non-interactive)")
    parser.add_argument("--between-cmd", default="", help="Command to run between baseline and modified (no shell operators). Example: \"ddev snapshot restore intent-baseline\"")
    parser.add_argument("--before-cmd", default="", help="Command to run before baseline (no shell operators)")
    parser.add_argument("--after-cmd", default="", help="Command to run after modified (no shell operators)")
    parser.add_argument("--continue-on-fail", action="store_true", help="Continue scenario after a failing command")
    parser.add_argument("--probe-cmd", action="append", default=[], help="Optional backend probe command (repeatable, no shell operators)")
    parser.add_argument("--probe-cwd", default=None, help="Working directory for probe commands")
    parser.add_argument("--trace", action="store_true", help="Capture agent-browser trace.zip for each run")
    parser.add_argument("--raw-value-regex", action="append", default=[], help="Regex to flag raw values in AI output (repeatable). Example: --raw-value-regex \"\\\\bhg:\"")
    parser.add_argument("--label-term", action="append", default=[], help="Label term expected in final answer (case-insensitive substring, repeatable)")
    parser.add_argument("--tool-payload-regex", action="append", default=[], help="Regex to detect tool payload blocks (repeatable). Example: --tool-payload-regex \"\\\\bcomponents:\"")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    commands = parse_script(args.script)

    stop_on_fail = not args.continue_on_fail
    probe_cmds = args.probe_cmd or []
    probe_cwd = args.probe_cwd or None
    raw_value_patterns = args.raw_value_regex or []
    label_terms = args.label_term or []
    tool_payload_patterns = args.tool_payload_regex or collectors.TOOL_PAYLOAD_PATTERNS

    report: JSON = {
        "generated": _now(),
        "config": {
            "url": args.url,
            "script": args.script,
            "output_dir": str(out_dir),
            "between_cmd": args.between_cmd or None,
            "stop_on_fail": stop_on_fail,
            "probe_cmds": args.probe_cmd or [],
            "probe_cwd": args.probe_cwd or None,
            "raw_value_regex": args.raw_value_regex or [],
            "label_terms": args.label_term or [],
            "tool_payload_regex": args.tool_payload_regex or [],
            "trace": args.trace,
        },
        "shell": {},
        "baseline": {},
        "modified": {},
        "comparison": {},
        "summary": {},
    }

    # Ensure a clean state
    run_cmd("close", session="baseline", capture_json=False)
    run_cmd("close", session="modified", capture_json=False)

    if args.before_cmd:
        report["shell"]["before"] = run_shell(args.before_cmd)

    print(f"🅰️  BASELINE run ({len(commands)} steps)")
    baseline_trace = None
    if args.trace:
        baseline_trace = out_dir / "baseline.trace.zip"
        report["shell"]["trace_baseline_start"] = run_cmd(
            f"trace start {shlex.quote(str(baseline_trace))}",
            session="baseline",
            capture_json=False,
            timeout=30,
        )
    baseline = execute_script(
        commands,
        base_url=args.url,
        session="baseline",
        out_dir=out_dir,
        stop_on_fail=stop_on_fail,
        probe_cmds=probe_cmds,
        probe_cwd=probe_cwd,
        raw_value_patterns=raw_value_patterns,
        label_terms=label_terms,
        tool_payload_patterns=tool_payload_patterns,
    )
    if baseline_trace:
        report["shell"]["trace_baseline_stop"] = run_cmd(
            f"trace stop {shlex.quote(str(baseline_trace))}",
            session="baseline",
            capture_json=False,
            timeout=30,
        )
        baseline["trace"] = str(baseline_trace)
    report["baseline"] = baseline

    if not args.no_pause and not args.between_cmd:
        print("\n" + "="*72)
        print("⏸️  Baseline complete. Make your code/config changes now.")
        print("   Press ENTER to run MODIFIED…")
        print("="*72)
        if sys.stdin.isatty():
            input()
        else:
            print("⚠️  stdin is not a TTY; skipping pause. Use --no-pause or --between-cmd in automation.")

    if args.between_cmd:
        report["shell"]["between"] = run_shell(args.between_cmd)

    print(f"\n🅱️  MODIFIED run ({len(commands)} steps)")
    modified_trace = None
    if args.trace:
        modified_trace = out_dir / "modified.trace.zip"
        report["shell"]["trace_modified_start"] = run_cmd(
            f"trace start {shlex.quote(str(modified_trace))}",
            session="modified",
            capture_json=False,
            timeout=30,
        )
    modified = execute_script(
        commands,
        base_url=args.url,
        session="modified",
        out_dir=out_dir,
        stop_on_fail=stop_on_fail,
        probe_cmds=probe_cmds,
        probe_cwd=probe_cwd,
        raw_value_patterns=raw_value_patterns,
        label_terms=label_terms,
        tool_payload_patterns=tool_payload_patterns,
    )
    if modified_trace:
        report["shell"]["trace_modified_stop"] = run_cmd(
            f"trace stop {shlex.quote(str(modified_trace))}",
            session="modified",
            capture_json=False,
            timeout=30,
        )
        modified["trace"] = str(modified_trace)
    report["modified"] = modified

    if args.after_cmd:
        report["shell"]["after"] = run_shell(args.after_cmd)

    # Compare checkpoints
    base_checkpoints = {cp.get("name"): cp for cp in baseline.get("checkpoints", []) if cp.get("name")}
    mod_checkpoints = {cp.get("name"): cp for cp in modified.get("checkpoints", []) if cp.get("name")}

    all_names = sorted(set(base_checkpoints.keys()) | set(mod_checkpoints.keys()))

    checkpoint_diffs: Dict[str, JSON] = {}
    matching: List[str] = []
    missing: List[JSON] = []
    errors: List[JSON] = []
    changed_checkpoints: List[str] = []
    snapshot_diff_count = 0

    for name in all_names:
        base_cp = base_checkpoints.get(name)
        mod_cp = mod_checkpoints.get(name)
        if not base_cp or not mod_cp:
            missing.append({"checkpoint": name, "baseline": bool(base_cp), "modified": bool(mod_cp)})
            continue

        cp_diff: JSON = {}
        changed = False

        base_artifacts = base_cp.get("artifacts", {})
        mod_artifacts = mod_cp.get("artifacts", {})

        # Snapshot diff
        base_snap = base_artifacts.get("snapshot")
        mod_snap = mod_artifacts.get("snapshot")
        if base_snap and mod_snap:
            comp = compare_snapshots(Path(base_snap), Path(mod_snap))
            cp_diff["snapshot"] = comp
            if comp.get("error"):
                errors.append({"checkpoint": name, "snapshot": comp.get("error")})
            elif comp["same"]:
                matching.append(name)
            else:
                snapshot_diff_count += 1
                changed = True

        # Console/errors diffs
        base_con = base_artifacts.get("console")
        mod_con = mod_artifacts.get("console")
        if base_con and mod_con:
            comp = compare_logs(Path(base_con), Path(mod_con))
            cp_diff["console"] = comp
            if comp.get("same") is False:
                changed = True

        base_err = base_artifacts.get("errors")
        mod_err = mod_artifacts.get("errors")
        if base_err and mod_err:
            comp = compare_logs(Path(base_err), Path(mod_err))
            cp_diff["errors"] = comp
            if comp.get("same") is False:
                changed = True

        # Drupal messages diff
        base_msg = base_artifacts.get("drupal_messages")
        mod_msg = mod_artifacts.get("drupal_messages")
        if base_msg and mod_msg:
            comp = compare_drupal_messages(Path(base_msg), Path(mod_msg))
            cp_diff["drupal_messages"] = comp
            if comp.get("same") is False:
                changed = True

        # AI Explorer diff
        base_ai = base_artifacts.get("ai_explorer")
        mod_ai = mod_artifacts.get("ai_explorer")
        if base_ai and mod_ai:
            comp = compare_ai_explorer(Path(base_ai), Path(mod_ai))
            cp_diff["ai_explorer"] = comp
            if comp.get("same") is False:
                changed = True

        # Probe diff
        base_probes = base_artifacts.get("probes", [])
        mod_probes = mod_artifacts.get("probes", [])
        if base_probes or mod_probes:
            comp = compare_probes(base_probes, mod_probes)
            cp_diff["probes"] = comp
            if comp.get("same") is False:
                changed = True

        if changed:
            changed_checkpoints.append(name)
        checkpoint_diffs[name] = cp_diff

    report["comparison"] = {
        "matching_checkpoints": matching,
        "missing_checkpoints": missing,
        "errors": errors,
        "checkpoints": checkpoint_diffs,
        "changed_checkpoints": changed_checkpoints,
    }

    report["summary"] = {
        "checkpoints_total": len(all_names),
        "matching": len(matching),
        "different": snapshot_diff_count,
        "changed_checkpoints": len(changed_checkpoints),
        "missing": len(missing),
        "errors": len(errors),
        "verdict": "ERROR" if len(errors) > 0 else ("CHANGED" if snapshot_diff_count > 0 or len(missing) > 0 or len(changed_checkpoints) > 0 else "IDENTICAL"),
    }

    report_path = out_dir / args.output
    md_path = out_dir / args.output_md
    report["markdown_report"] = str(md_path)
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(build_markdown_report(report), encoding="utf-8")

    print("\n" + "="*72)
    print("📊 COMPARISON SUMMARY")
    print("="*72)
    print(f"Checkpoints total: {report['summary']['checkpoints_total']}")
    print(f"Matching:          {report['summary']['matching']}")
    print(f"Different:         {report['summary']['different']}")
    print(f"Changed:           {report['summary']['changed_checkpoints']}")
    print(f"Missing:           {report['summary']['missing']}")
    print(f"Errors:            {report['summary']['errors']}")
    print(f"Verdict:           {report['summary']['verdict']}")
    print(f"\nReport saved: {report_path}")
    print(f"Markdown saved: {md_path}")

    # Close sessions
    run_cmd("close", session="baseline", capture_json=False)
    run_cmd("close", session="modified", capture_json=False)

    verdict = report["summary"]["verdict"]
    if verdict == "IDENTICAL":
        return 0
    if verdict == "ERROR":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
