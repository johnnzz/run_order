#!/usr/bin/env python3
"""Utilities for creating, reading, writing, and appending run order JSON files."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_SCHEMA_VERSION = "1.0.0"


@dataclass(frozen=True)
class RunOrderEntry:
    """A single run entry and where it lives in the file."""

    location_path: tuple[str, ...]
    timestamp: str
    data: dict[str, Any]

    def __iter__(self) -> Iterator[Any]:
        yield self.location_path
        yield self.timestamp
        yield self.data


class RunOrder:
    """Read and manipulate run order JSON documents."""

    def __init__(self, data: MutableMapping[str, Any], *, path: Path | None = None) -> None:
        self._data = data
        self._path = path

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def data(self) -> MutableMapping[str, Any]:
        return self._data

    @classmethod
    def create(
        cls,
        path: str | Path,
        *,
        event: Mapping[str, Any],
        location: Mapping[str, Any] | None = None,
        schema_version: str = DEFAULT_SCHEMA_VERSION,
    ) -> RunOrder:
        """Create a new run order file on disk."""
        document = {
            "schema_version": schema_version,
            "event": dict(event),
            "location": dict(location or {}),
        }
        run_order = cls(document, path=Path(path))
        run_order.write()
        return run_order

    @classmethod
    def read(cls, path: str | Path) -> RunOrder:
        """Load a run order file from disk."""
        file_path = Path(path)
        with file_path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"Expected a JSON object in {file_path}")
        return cls(data, path=file_path)

    def write(self, path: str | Path | None = None) -> Path:
        """Write the current document to disk."""
        file_path = Path(path) if path is not None else self._path
        if file_path is None:
            raise ValueError("No output path provided and no path was set on this RunOrder")

        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=4)
            handle.write("\n")

        self._path = file_path
        return file_path

    def append(
        self,
        entry: Mapping[str, Any],
        *,
        timestamp: str,
        location_path: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Append an entry under location_path + timestamp."""
        slot = self._resolve_slot(location_path, timestamp, create=True)
        appended = dict(entry)
        slot.append(appended)
        return appended

    def extend(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        timestamp: str,
        location_path: Sequence[str] = (),
    ) -> list[dict[str, Any]]:
        """Append multiple entries to the same timestamp slot."""
        slot = self._resolve_slot(location_path, timestamp, create=True)
        appended_entries = [dict(entry) for entry in entries]
        slot.extend(appended_entries)
        return appended_entries

    def __iter__(self) -> Iterator[RunOrderEntry]:
        location = self._data.get("location")
        if not isinstance(location, dict):
            return iter(())

        for location_path, timestamp, entries in self._walk_location(location):
            for entry in entries:
                if isinstance(entry, dict):
                    yield RunOrderEntry(location_path, timestamp, entry)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __repr__(self) -> str:
        event_name = self._data.get("event", {}).get("name", "<unknown event>")
        return f"RunOrder(event={event_name!r}, entries={len(self)}, path={self._path!r})"

    def _resolve_slot(
        self,
        location_path: Sequence[str],
        timestamp: str,
        *,
        create: bool,
    ) -> list[dict[str, Any]]:
        location = self._data.setdefault("location", {})
        if not isinstance(location, dict):
            raise ValueError("Top-level 'location' must be an object")

        node: MutableMapping[str, Any] = location
        for key in location_path:
            child = node.get(key)
            if child is None:
                if not create:
                    raise KeyError(f"Missing location segment: {key!r}")
                child = {}
                node[key] = child
            if not isinstance(child, dict):
                raise ValueError(f"Location segment {key!r} must be an object")
            node = child

        slot = node.get(timestamp)
        if slot is None:
            if not create:
                raise KeyError(f"Missing timestamp slot: {timestamp!r}")
            slot = []
            node[timestamp] = slot
        if not isinstance(slot, list):
            raise ValueError(f"Timestamp slot {timestamp!r} must be a list")

        return slot

    @staticmethod
    def _walk_location(
        node: Mapping[str, Any],
        location_path: tuple[str, ...] = (),
    ) -> Iterator[tuple[tuple[str, ...], str, list[Any]]]:
        for key, value in node.items():
            if isinstance(value, list):
                yield location_path, key, value
            elif isinstance(value, dict):
                yield from RunOrder._walk_location(value, location_path + (key,))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create", help="Create a new run order JSON file")
    create_parser.add_argument("path", type=Path, help="Output JSON file path")
    create_parser.add_argument("--event-name", required=True)
    create_parser.add_argument("--event-org", required=True)
    create_parser.add_argument("--event-city", required=True)
    create_parser.add_argument("--event-state", required=True)
    create_parser.add_argument("--event-start-date", required=True)
    create_parser.add_argument("--schema-version", default=DEFAULT_SCHEMA_VERSION)

    read_parser = subparsers.add_parser("read", help="Print run order entries")
    read_parser.add_argument("path", type=Path)

    write_parser = subparsers.add_parser("write", help="Write in-memory changes back to disk")
    write_parser.add_argument("path", type=Path)
    write_parser.add_argument("--output", type=Path, help="Optional alternate output path")

    append_parser = subparsers.add_parser("append", help="Append an entry to a timestamp slot")
    append_parser.add_argument("path", type=Path)
    append_parser.add_argument("--timestamp", required=True)
    append_parser.add_argument(
        "--location-path",
        default="",
        help="Dot-separated location path, e.g. pool1 or field",
    )
    append_parser.add_argument("--entry-json", required=True, help="Entry object as JSON")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "create":
        RunOrder.create(
            args.path,
            event={
                "name": args.event_name,
                "org": args.event_org,
                "city": args.event_city,
                "state": args.event_state,
                "start_date": args.event_start_date,
            },
            schema_version=args.schema_version,
        )
        print(f"Created {args.path}")
        return 0

    if args.command == "read":
        run_order = RunOrder.read(args.path)
        for location_path, timestamp, entry in run_order:
            location = ".".join(location_path) or "<root>"
            print(f"{location} @ {timestamp}: {json.dumps(entry, sort_keys=True)}")
        print(f"{len(run_order)} entries")
        return 0

    if args.command == "write":
        run_order = RunOrder.read(args.path)
        output_path = run_order.write(args.output)
        print(f"Wrote {output_path}")
        return 0

    if args.command == "append":
        run_order = RunOrder.read(args.path)
        location_path = tuple(part for part in args.location_path.split(".") if part)
        entry = json.loads(args.entry_json)
        if not isinstance(entry, dict):
            raise SystemExit("--entry-json must decode to a JSON object")
        run_order.append(entry, timestamp=args.timestamp, location_path=location_path)
        run_order.write()
        print(f"Appended entry to {args.path}")
        return 0

    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
