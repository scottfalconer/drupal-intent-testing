#!/usr/bin/env python3
"""
Exploratory UI testing for Drupal using agent-browser.

Two modes:
- guided: logs in and writes a session JSON file with the current snapshot + refs,
          intended for an LLM (Claude Code / Codex) to continue driving via agent-browser commands.
- fuzz: a seeded, time-boxed "monkey tester" that clicks/inputs semi-randomly and records artifacts.

This script does NOT require any vision model.
"""

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from scripts import collectors

JSON = Dict[str, Any]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run_agent_browser(cmd_parts: List[str], *, session: str, want_json: bool = True, timeout: int = 120) -> JSON:
    return collectors.run_agent_browser(cmd_parts, session=session, want_json=want_json, timeout=timeout)


def open_url(url: str, *, session: str) -> JSON:
    return run_agent_browser(["open", url], session=session, want_json=True)


def wait_for(args: List[str], *, session: str) -> JSON:
    return run_agent_browser(["wait"] + args, session=session, want_json=True)


def snapshot_interactive(*, session: str) -> Tuple[Optional[JSON], List[JSON]]:
    rec = run_agent_browser(["snapshot", "-i", "-c"], session=session, want_json=True)
    parsed = rec.get("parsed")
    if not isinstance(parsed, dict):
        return None, []
    data = parsed.get("data", {})
    refs = data.get("refs", {}) if isinstance(data, dict) else {}
    elements: List[JSON] = []
    if isinstance(refs, dict):
        for ref, info in refs.items():
            if not isinstance(info, dict):
                continue
            role = info.get("role", "")
            name = info.get("name", "")
            elements.append({
                "ref": f"@{ref}",
                "role": role,
                "name": name,
            })
    # deterministic sort for stable output
    elements.sort(key=lambda e: (str(e.get("role","")), str(e.get("name","")), str(e.get("ref",""))))
    return parsed, elements


def get_url(*, session: str) -> Optional[str]:
    rec = run_agent_browser(["get", "url"], session=session, want_json=True)
    parsed = rec.get("parsed", {})
    # agent-browser JSON format tends to be {"success":true,"data":"..."} for scalar commands
    if isinstance(parsed, dict):
        data = parsed.get("data")
        if isinstance(data, str):
            return data
    return None


def _prefix_url(base_url: str, maybe_path: str) -> str:
    if maybe_path.startswith("http://") or maybe_path.startswith("https://"):
        return maybe_path
    if maybe_path.startswith("/"):
        return base_url.rstrip("/") + maybe_path
    return base_url.rstrip("/") + "/" + maybe_path


def take_screenshot(path: Path, *, session: str) -> JSON:
    return run_agent_browser(["screenshot", str(path)], session=session, want_json=False)


def get_errors(*, session: str) -> JSON:
    return run_agent_browser(["errors"], session=session, want_json=True)


def get_console(*, session: str) -> JSON:
    return run_agent_browser(["console"], session=session, want_json=True)


@dataclass
class FuzzConfig:
    duration_minutes: float
    seed: int
    safety: str  # read-only | dangerous
    screenshot_every: int = 10
    checkpoint_every: int = 0


@dataclass
class ExplorationSession:
    base_url: str
    output_dir: Path
    goal: str
    session_name: str = "explore"
    log: List[JSON] = field(default_factory=list)
    screenshots: List[str] = field(default_factory=list)
    visited_urls: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)
    checkpoints: List[JSON] = field(default_factory=list)
    last_checkpoint: Optional[JSON] = None

    def record(self, rec: JSON) -> None:
        self.log.append(rec)

    def nav(self, path: str) -> None:
        url = _prefix_url(self.base_url, path)
        self.record(open_url(url, session=self.session_name))
        self.record(wait_for(["--load", "networkidle"], session=self.session_name))
        u = get_url(session=self.session_name)
        if u:
            if not self.visited_urls or self.visited_urls[-1] != u:
                self.visited_urls.append(u)

    def screenshot(self, name: str) -> None:
        safe = re.sub(r"[^a-zA-Z0-9._-]+", "_", name)
        path = self.output_dir / f"{len(self.screenshots):03d}_{safe}.png"
        self.record(take_screenshot(path, session=self.session_name))
        self.screenshots.append(str(path))

    def add_checkpoint(self, checkpoint: JSON) -> None:
        self.checkpoints.append(checkpoint)
        self.last_checkpoint = checkpoint
        shot = (checkpoint.get("artifacts") or {}).get("screenshot")
        if shot and shot not in self.screenshots:
            self.screenshots.append(shot)

    def run_checkpoint(
        self,
        name: str,
        *,
        mode: str,
        probe_cmds: List[str],
        probe_cwd: Optional[str],
        raw_value_patterns: List[str],
        label_terms: List[str],
        tool_payload_patterns: List[str],
        include_screenshot: bool = True,
    ) -> JSON:
        cp = collectors.collect_checkpoint(
            name=name,
            session=self.session_name,
            out_dir=self.output_dir,
            mode=mode,
            probe_cmds=probe_cmds,
            probe_cwd=probe_cwd,
            raw_value_patterns=raw_value_patterns,
            label_terms=label_terms,
            tool_payload_patterns=tool_payload_patterns,
            include_screenshot=include_screenshot,
        )
        self.add_checkpoint(cp)
        return cp

    def to_session_file(self, elements: List[JSON]) -> Path:
        last = self.last_checkpoint or {}
        last_summary = last.get("summary") or {}
        payload = {
            "generated": now(),
            "base_url": self.base_url,
            "goal": self.goal,
            "agent_browser_session": self.session_name,
            "interactive_elements": elements[:200],
            "output_dir": str(self.output_dir),
            "current_url": last.get("url"),
            "last_drupal_messages": last_summary.get("drupal_messages"),
            "last_console_summary": last_summary.get("console"),
            "last_errors_summary": last_summary.get("errors"),
            "last_ai_explorer_summary": last_summary.get("ai_explorer"),
            "last_checkpoint": {
                "name": last.get("name"),
                "time": last.get("time"),
                "artifacts": last.get("artifacts"),
            } if last else None,
        }
        p = self.output_dir / "exploration_session.json"
        p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return p

    def write_report(self, *, duration_minutes: float, mode: str, output_name: str, fuzz: Optional[FuzzConfig] = None) -> Path:
        report = []
        report.append("# Drupal Exploration Report")
        report.append("")
        report.append(f"**Generated:** {now()}")
        report.append(f"**Site:** {self.base_url}")
        report.append(f"**Goal:** {self.goal}")
        report.append(f"**Mode:** {mode}")
        report.append(f"**Duration:** {duration_minutes:.1f} minutes")
        report.append(f"**agent-browser session:** `{self.session_name}`")
        report.append("")
        if fuzz:
            report.append("## Fuzz configuration")
            report.append("")
            report.append(f"- Seed: `{fuzz.seed}`")
            report.append(f"- Safety: `{fuzz.safety}`")
            report.append(f"- Screenshot every: {fuzz.screenshot_every} actions")
            if fuzz.checkpoint_every:
                report.append(f"- Checkpoint every: {fuzz.checkpoint_every} actions")
            report.append("")
        report.append("## Summary")
        report.append("")
        report.append(f"- URLs visited: {len(self.visited_urls)}")
        report.append(f"- Checkpoints: {len(self.checkpoints)}")
        report.append(f"- Screenshots: {len(self.screenshots)}")
        report.append(f"- Logged commands: {len(self.log)}")
        report.append(f"- Issues flagged: {len(self.issues)}")
        report.append("")
        if self.visited_urls:
            report.append("## URLs visited")
            report.append("")
            for u in self.visited_urls:
                report.append(f"- {u}")
            report.append("")
        if self.issues:
            report.append("## Issues flagged")
            report.append("")
            for i in self.issues:
                report.append(f"- {i}")
            report.append("")
        report.append("## Screenshots")
        report.append("")
        for i, s in enumerate(self.screenshots, 1):
            report.append(f"{i}. `{s}`")
        report.append("")
        if self.checkpoints:
            report.append("## Checkpoints")
            report.append("")
            for cp in self.checkpoints:
                name = cp.get("name")
                url = cp.get("url")
                report.append(f"- `{name}` at {url}")
            report.append("")
        report.append("## Last 40 commands")
        report.append("")
        report.append("| Time | Return | Command |")
        report.append("|------|--------|---------|")
        for rec in self.log[-40:]:
            t = rec.get("time", "")[-8:]
            rc = rec.get("returncode", "")
            argv = rec.get("argv", [])
            cmd = " ".join(argv[3:]) if isinstance(argv, list) and len(argv) >= 4 else ""
            report.append(f"| {t} | {rc} | `{cmd[:80]}` |")
        report.append("")
        path = self.output_dir / output_name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(report), encoding="utf-8")
        return path


BLOCKLIST_DESTRUCTIVE = [
    "delete", "remove", "uninstall", "install", "drop", "purge", "rebuild", "clear all", "wipe",
]
BLOCKLIST_ALWAYS = [
    "log out",
]


def allowed_by_safety(name: str, safety: str) -> bool:
    n = name.strip().lower()
    if any(b in n for b in BLOCKLIST_ALWAYS):
        return False
    if safety == "dangerous":
        return True
    if any(b in n for b in BLOCKLIST_DESTRUCTIVE):
        return False
    # read-only: avoid common mutating actions
    if any(b in n for b in ["save", "submit", "apply", "create", "add", "update"]):
        return False
    return True


def fuzz_loop(
    sess: ExplorationSession,
    cfg: FuzzConfig,
    *,
    probe_cmds: List[str],
    probe_cwd: Optional[str],
    raw_value_patterns: List[str],
    label_terms: List[str],
    tool_payload_patterns: List[str],
) -> None:
    rng = random.Random(cfg.seed)
    end_time = datetime.now() + timedelta(minutes=cfg.duration_minutes)
    action_count = 0

    while datetime.now() < end_time:
        parsed, elements = snapshot_interactive(session=sess.session_name)
        sess.record({"time": now(), "kind": "snapshot-meta", "interactive_count": len(elements)})

        # quick error scan
        err = get_errors(session=sess.session_name)
        sess.record(err)
        if err.get("returncode", 0) == 0:
            # heuristic: if there are any errors listed, flag it
            parsed_err = err.get("parsed", {})
            if isinstance(parsed_err, dict):
                data = parsed_err.get("data")
                if data:
                    sess.issues.append(f"Page errors detected at {get_url(session=sess.session_name) or 'unknown URL'}")
                    sess.run_checkpoint(
                        f"error_{action_count}",
                        mode="full",
                        probe_cmds=probe_cmds,
                        probe_cwd=probe_cwd,
                        raw_value_patterns=raw_value_patterns,
                        label_terms=label_terms,
                        tool_payload_patterns=tool_payload_patterns,
                        include_screenshot=True,
                    )

        # pick a random actionable element
        candidates = [e for e in elements if allowed_by_safety(str(e.get("name","")), cfg.safety)]
        if not candidates:
            # try to move somewhere else
            sess.record(run_agent_browser(["press", "Escape"], session=sess.session_name, want_json=True))
            time.sleep(0.5)
            continue

        el = rng.choice(candidates)
        ref = el["ref"]
        role = str(el.get("role", ""))
        name = str(el.get("name", ""))

        # Decide action by role
        if role == "textbox":
            text = f"Fuzz {cfg.seed} #{action_count}"
            sess.record(run_agent_browser(["fill", ref, text], session=sess.session_name, want_json=True))
        elif role in ("checkbox", "radio"):
            # toggle by click
            sess.record(run_agent_browser(["click", ref], session=sess.session_name, want_json=True))
        else:
            sess.record(run_agent_browser(["click", ref], session=sess.session_name, want_json=True))

        # wait a bit for navigation/ajax
        sess.record(wait_for(["--load", "networkidle"], session=sess.session_name))
        u = get_url(session=sess.session_name)
        if u and (not sess.visited_urls or sess.visited_urls[-1] != u):
            sess.visited_urls.append(u)

        action_count += 1
        if action_count % cfg.screenshot_every == 0:
            sess.screenshot(f"step_{action_count}_{role}")
        if cfg.checkpoint_every and action_count % cfg.checkpoint_every == 0:
            sess.run_checkpoint(
                f"checkpoint_{action_count}",
                mode="full",
                probe_cmds=probe_cmds,
                probe_cwd=probe_cwd,
                raw_value_patterns=raw_value_patterns,
                label_terms=label_terms,
                tool_payload_patterns=tool_payload_patterns,
                include_screenshot=True,
            )

    sess.record({"time": now(), "kind": "fuzz-done", "actions": action_count})


def login(sess: ExplorationSession, *, login_path: str, username: str, password: str) -> None:
    sess.nav(login_path)
    # Use semantic locators (label) to fill creds
    sess.record(run_agent_browser(["find", "label", "Username", "fill", username], session=sess.session_name, want_json=True))
    sess.record(run_agent_browser(["find", "label", "Password", "fill", password], session=sess.session_name, want_json=True))
    sess.record(run_agent_browser(["find", "role", "button", "click", "--name", "Log in"], session=sess.session_name, want_json=True))
    sess.record(wait_for(["--load", "networkidle"], session=sess.session_name))
    # If login works, Drupal often shows "Log out" somewhere
    sess.record(wait_for(["--text", "Log out"], session=sess.session_name))
    sess.screenshot("after_login")


def main() -> int:
    parser = argparse.ArgumentParser(description="Exploratory testing for Drupal using agent-browser")
    parser.add_argument("--url", required=True, help="Base URL of the Drupal site")
    parser.add_argument("--duration", default="10m", help="Duration: e.g. 30m, 1h")
    parser.add_argument("--goal", default="Explore the site and report findings", help="Exploration goal")
    parser.add_argument("--output-dir", default="./test_outputs", help="Directory for artifacts")
    parser.add_argument("--output", default="exploration_report.md", help="Report filename")
    parser.add_argument("--mode", choices=["guided", "fuzz"], default="guided")
    parser.add_argument("--session", default="explore", help="agent-browser session name")
    parser.add_argument("--login-path", default="/user/login", help="Login page path")
    parser.add_argument("--username", default=None, help="Login username (or set DRUPAL_TEST_USER)")
    parser.add_argument("--password", default=None, help="Login password (or set DRUPAL_TEST_PASS)")
    # fuzz-specific
    parser.add_argument("--seed", type=int, default=1337, help="Random seed (fuzz mode)")
    parser.add_argument("--safety", choices=["read-only", "dangerous"], default="read-only")
    parser.add_argument("--screenshot-every", type=int, default=10, help="Screenshot every N actions (fuzz mode)")
    parser.add_argument("--checkpoint-every", type=int, default=0, help="Checkpoint every N actions (fuzz mode)")
    parser.add_argument("--probe-cmd", action="append", default=[], help="Optional backend probe command (repeatable, no shell operators)")
    parser.add_argument("--probe-cwd", default=None, help="Working directory for probe commands")
    parser.add_argument("--raw-value-regex", action="append", default=[], help="Regex to flag raw values in AI output (repeatable). Example: --raw-value-regex \"\\\\bhg:\"")
    parser.add_argument("--label-term", action="append", default=[], help="Label term expected in final answer (case-insensitive substring, repeatable)")
    parser.add_argument("--tool-payload-regex", action="append", default=[], help="Regex to detect tool payload blocks (repeatable). Example: --tool-payload-regex \"\\\\bcomponents:\"")
    args = parser.parse_args()

    probe_cmds = args.probe_cmd or []
    probe_cwd = args.probe_cwd or None
    raw_value_patterns = args.raw_value_regex or []
    label_terms = args.label_term or []
    tool_payload_patterns = args.tool_payload_regex or collectors.TOOL_PAYLOAD_PATTERNS

    # Parse duration
    d = args.duration.strip().lower()
    minutes: float
    if d.endswith("h"):
        minutes = float(d[:-1]) * 60.0
    elif d.endswith("m"):
        minutes = float(d[:-1])
    else:
        minutes = float(d)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sess = ExplorationSession(
        base_url=args.url.rstrip("/"),
        output_dir=out_dir,
        goal=args.goal,
        session_name=args.session,
    )

    # Clean session
    sess.record(run_agent_browser(["close"], session=args.session, want_json=False))

    username = args.username or os.environ.get("DRUPAL_TEST_USER")
    password = args.password or os.environ.get("DRUPAL_TEST_PASS")
    if not username or not password:
        print("Missing credentials. Provide --username/--password or set DRUPAL_TEST_USER/DRUPAL_TEST_PASS.")
        return 2

    # Login first
    login(sess, login_path=args.login_path, username=username, password=password)

    # Gather initial snapshot for guided mode handoff
    parsed, elements = snapshot_interactive(session=args.session)
    if args.mode == "guided":
        sess.run_checkpoint(
            "guided_start",
            mode="full",
            probe_cmds=probe_cmds,
            probe_cwd=probe_cwd,
            raw_value_patterns=raw_value_patterns,
            label_terms=label_terms,
            tool_payload_patterns=tool_payload_patterns,
            include_screenshot=True,
        )
        session_file = sess.to_session_file(elements)
        print(f"✅ Guided exploration ready. Session file: {session_file}")
        print(f"👉 Continue with: agent-browser --session {args.session} <command>")
        # Write a minimal report
        report_path = sess.write_report(duration_minutes=0.0, mode="guided", output_name=args.output)
        print(f"📝 Report skeleton: {report_path}")
        return 0

    # Fuzz mode
    cfg = FuzzConfig(
        duration_minutes=minutes,
        seed=args.seed,
        safety=args.safety,
        screenshot_every=args.screenshot_every,
        checkpoint_every=args.checkpoint_every,
    )
    fuzz_loop(
        sess,
        cfg,
        probe_cmds=probe_cmds,
        probe_cwd=probe_cwd,
        raw_value_patterns=raw_value_patterns,
        label_terms=label_terms,
        tool_payload_patterns=tool_payload_patterns,
    )
    report_path = sess.write_report(duration_minutes=minutes, mode="fuzz", output_name=args.output, fuzz=cfg)
    print(f"📝 Fuzz report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
