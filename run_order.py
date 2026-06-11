#!/usr/bin/env python3
"""Utilities for creating, reading, writing, and appending run order JSON files.

Usage:
    run_order.py create <path> --name=<name> --organization=<org> --city=<city> --state=<state> --date=<date> [--schema-version=<version>]
    run_order.py read <path>
    run_order.py write <path> [--output=<output>]
    run_order.py append <path> --timestamp=<timestamp> --json-data=<json> [--location-path=<location>]
    run_order.py (-h | --help)

Options:
    --name=<name>                 Event name.
    --organization=<org>          Event organization.
    --city=<city>                 Event city.
    --state=<state>               Event state.
    --date=<date>                 Event start date (YYYY-MM-DD).
    --schema-version=<version>    Schema version [default: 1.0.0].
    --output=<output>             Optional alternate output path for write.
    --timestamp=<timestamp>       Timestamp slot for append.
    --json-data=<json>            Entry object as JSON for append.
    --location-path=<location>    Dot-separated location path (e.g. pool1 or field) [default: ].
    -h --help                     Show this screen.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docopt import docopt


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


def main(argv: Sequence[str] | None = None) -> int:
    arguments = docopt(__doc__, argv=argv)
    path = Path(arguments["<path>"])

    if arguments["create"]:
        RunOrder.create(
            path,
            event={
                "name": arguments["--name"],
                "org": arguments["--organization"],
                "city": arguments["--city"],
                "state": arguments["--state"],
                "start_date": arguments["--date"],
            },
            schema_version=arguments["--schema-version"],
        )
        print(f"Created {path}")
        return 0

    if arguments["read"]:
        run_order = RunOrder.read(path)
        for location_path, timestamp, entry in run_order:
            location = ".".join(location_path) or "<root>"
            print(f"{location} @ {timestamp}: {json.dumps(entry, sort_keys=True)}")
        print(f"{len(run_order)} entries")
        return 0

    if arguments["write"]:
        run_order = RunOrder.read(path)
        output = arguments["--output"]
        output_path = run_order.write(Path(output) if output else None)
        print(f"Wrote {output_path}")
        return 0

    if arguments["append"]:
        run_order = RunOrder.read(path)
        location_path = tuple(
            part for part in arguments["--location-path"].split(".") if part
        )
        entry = json.loads(arguments["--json-data"])
        if not isinstance(entry, dict):
            raise SystemExit("--json-data must decode to a JSON object")
        run_order.append(
            entry,
            timestamp=arguments["--timestamp"],
            location_path=location_path,
        )
        run_order.write()
        print(f"Appended entry to {path}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
