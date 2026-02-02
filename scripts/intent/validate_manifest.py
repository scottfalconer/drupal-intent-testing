#!/usr/bin/env python3
"""Validate an intent manifest."""

import argparse
import sys
from pathlib import Path

if __package__ is None and __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.intent import manifest as manifest_lib


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate an intent manifest")
    parser.add_argument("manifest", help="Path to manifest (YAML or JSON)")
    args = parser.parse_args()

    manifest, errors = manifest_lib.load_and_validate(args.manifest)
    if errors:
        print("Manifest validation failed:")
        for err in errors:
            print(f"- {err}")
        return 2

    print("Manifest is valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
