#!/usr/bin/env python3
# Copyright (c) 2026 John Navitsky
#
# SPDX-License-Identifier: MIT
# See LICENSE for the full license text.
"""Validate run order JSON files against run_order.schema.json.

Usage:
    validate.py [--file=<path>]
    validate.py (-h | --help)

Options:
    --file=<path>    Validate a single JSON file [default: ].
    -h --help        Show this screen.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from docopt import docopt

try:
    from jsonschema import Draft202012Validator
except ImportError:
    print("Install jsonschema: pip install jsonschema", file=sys.stderr)
    raise SystemExit(1)


ROOT = Path(__file__).resolve().parent
SCHEMA_PATH = ROOT / "run_order.schema.json"
CURRENT_SCHEMA_VERSION = "2.0.0"


def validate_file(path: Path, validator: Draft202012Validator) -> bool:
    document = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(validator.iter_errors(document), key=lambda err: list(err.path))
    if errors:
        print(f"FAIL {path.name}")
        for error in errors:
            location = ".".join(str(part) for part in error.path) or "<root>"
            print(f"  {location}: {error.message}")
        return False

    print(f"OK   {path.name}")
    return True


def target_paths(file_arg: str) -> list[Path]:
    if file_arg:
        return [Path(file_arg)]
    return sorted(ROOT.glob("*_example.json"))


def main(argv: list[str] | None = None) -> int:
    arguments = docopt(__doc__, argv=argv)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    paths = target_paths(arguments["--file"])
    if not paths:
        print("No example files found.", file=sys.stderr)
        return 1

    failed = False
    for path in paths:
        if not path.is_file():
            print(f"FAIL {path}: file not found", file=sys.stderr)
            failed = True
            continue
        if not validate_file(path, validator):
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
