#!/usr/bin/env python3
"""Intent manifest parsing and validation helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency
    yaml = None

JSON = Dict[str, Any]


def load_manifest(path: str) -> JSON:
    manifest_path = Path(path)
    raw = manifest_path.read_text(encoding="utf-8")
    if manifest_path.suffix.lower() in {".json"}:
        return json.loads(raw)
    if yaml is None:
        raise RuntimeError("PyYAML is required for YAML manifests. Install with: pip install pyyaml")
    return yaml.safe_load(raw)


def normalize_manifest(manifest: JSON) -> JSON:
    normalized = dict(manifest or {})
    normalized.setdefault("issue", {})
    normalized.setdefault("intent_statement", "")
    normalized.setdefault("adr", [])
    normalized.setdefault("environment", {})
    normalized.setdefault("strategy", {})
    normalized.setdefault("steps", [])
    normalized.setdefault("assertions", [])
    normalized.setdefault("guards", [])
    normalized.setdefault("probes", [])

    strategy = normalized.get("strategy", {})
    strategy.setdefault("mode", "single")
    strategy.setdefault("retries", 0)
    strategy.setdefault("timeouts", {})
    timeouts = strategy.get("timeouts", {})
    timeouts.setdefault("page_load_ms", 120000)
    timeouts.setdefault("ai_response_ms", 600000)
    strategy["timeouts"] = timeouts
    normalized["strategy"] = strategy

    env = normalized.get("environment", {})
    if "login_url" not in env and env.get("admin_user"):
        env["login_url"] = "/user/login"
    normalized["environment"] = env

    return normalized


def validate_manifest(manifest: JSON) -> List[str]:
    errors: List[str] = []
    if not isinstance(manifest, dict):
        return ["Manifest must be a mapping/object."]

    issue = manifest.get("issue")
    intent_statement = manifest.get("intent_statement")
    if issue is None:
        issue = {}
    if not isinstance(issue, dict):
        errors.append("issue must be an object")
    else:
        if not issue.get("url") or not issue.get("title"):
            if not intent_statement:
                errors.append("issue.url and issue.title are required unless intent_statement is provided")

    env = manifest.get("environment")
    if not isinstance(env, dict):
        errors.append("environment must be an object")
    else:
        if not env.get("base_url"):
            errors.append("environment.base_url is required")

    strategy = manifest.get("strategy")
    if not isinstance(strategy, dict):
        errors.append("strategy must be an object")
    else:
        mode = strategy.get("mode", "single")
        if mode not in ("single", "compare"):
            errors.append("strategy.mode must be 'single' or 'compare'")

    steps = manifest.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("steps must be a non-empty list")

    assertions = manifest.get("assertions", [])
    if assertions and not isinstance(assertions, list):
        errors.append("assertions must be a list")

    adr = manifest.get("adr", [])
    if adr and not isinstance(adr, list):
        errors.append("adr must be a list of strings")
    elif isinstance(adr, list):
        for entry in adr:
            if not isinstance(entry, str):
                errors.append("adr entries must be strings")
                break

    return errors


def load_and_validate(path: str) -> Tuple[JSON, List[str]]:
    manifest = normalize_manifest(load_manifest(path))
    errors = validate_manifest(manifest)
    return manifest, errors
