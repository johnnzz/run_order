#!/usr/bin/env python3
"""Validate example run order JSON files against run_order.schema.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ImportError:
    print("Install jsonschema: pip install jsonschema", file=sys.stderr)
    raise SystemExit(1)


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "run_order.schema.json"
CURRENT_SCHEMA_VERSION = "2.0.0"
EXAMPLE_PATHS = sorted(ROOT.glob("*_example.json"))


def main() -> int:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    failed = False

    for example_path in EXAMPLE_PATHS:
        document = json.loads(example_path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(document), key=lambda err: list(err.path))
        if errors:
            failed = True
            print(f"FAIL {example_path.name}")
            for error in errors:
                location = ".".join(str(part) for part in error.path) or "<root>"
                print(f"  {location}: {error.message}")
        else:
            print(f"OK   {example_path.name}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
