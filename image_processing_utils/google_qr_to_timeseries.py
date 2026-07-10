#!/usr/bin/python3
# Copyright (c) 2026 John Navitsky
#
# SPDX-License-Identifier: MIT
# See LICENSE for the full license text.
"""
Build a run_order timeseries JSON file from a Google Sheet.

This script allows the use of the following Google Sheet QR check process to 
be used with the rest of these processing utilities.

The template for the QR process can be seen here.

https://docs.google.com/spreadsheets/d/1w1tS5-LS1Zc6LBbcNk-QyH06SOGVvw1_lpuwV1fuCAA/

The QR process results in timestamps in a Google Sheet.  This script converts
the resulting Google Sheet into a time-series file that can be used by the rest
of the scripts.

There are some limitations and considerations.  The QR check-in process is typically
by handler, so dogs are not captured.  The rest of the workflow allows for handler
only data.

One must be mindful of the timezone of the timestamps captured in the Google Sheets.
It is suggested you do a test and verify what timezone is in use.  This script will
allow you to use an alternate timezone if needed.  This is also important because
the timestamp format used in the sheet does not include the timezone.

For simple one location events (one pool, one ring, one field) the time-series file
derived from the Google Sheet should be sufficient.  

For more complicated events, a second time-series file that contains information about
which photographer is at which location (photographer A is at pool 1, photographer B
is at pool 2) is needed.  Any tool that writes the standard run_order v2 format can
provide this kind of information.

For more information about the file format, see:

https://github.com/johnnzz/run_order/blob/main/README.md

In cases where there are multiple locations, the heat field must exactly match
the location name used by the other time-series file.  That is to say, if the 
second time-series file uses the locations "Pool 1" and "Pool 2", then the 
heats used in the Google Sheets should be "Pool 1" and "Pool 2".  The heat is
defined in the QR scanner.

This script fetches Setup, Event, Roster, and Log tabs
from a published sheet URL and writes a schema 2.0 time-series file for
process_queue.py to consume. No Google API credentials required.

Usage:
  google_qr_to_timeseries.py [options] <sheet_url>

Options:
  --timezone TZ            IANA timezone for naive Log timestamps [default: America/New_York]
  --file PATH              Output timeseries JSON path (default: <event>-ts.json)
  -h, --help               Show this message.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from docopt import docopt

import _run_order_timeseries as rot

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
    ZoneInfo = None  # type: ignore[misc, assignment]


# ---------------------------------------------------------------------------
# Sheet timezone
# ---------------------------------------------------------------------------

DEFAULT_SHEET_TIMEZONE = "America/New_York"

SHEET_TIMEZONE_CHOICES: tuple[tuple[str, str], ...] = (
    ("America/Los_Angeles", "Pacific"),
    ("America/Denver", "Mountain"),
    ("America/Chicago", "Central"),
    ("America/New_York", "Eastern"),
    ("America/Phoenix", "Arizona"),
    ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"),
)


def _resolve_zone(tz_name: str) -> Any:
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return timezone.utc


def normalize_sheet_timezone_name(tz_name: Optional[str]) -> str:
    candidate = (tz_name or "").strip()
    if not candidate:
        return DEFAULT_SHEET_TIMEZONE
    if ZoneInfo is None:
        return DEFAULT_SHEET_TIMEZONE
    try:
        ZoneInfo(candidate)
    except Exception:
        return DEFAULT_SHEET_TIMEZONE
    return candidate


def resolve_sheet_timezone(tz_name: Optional[str]) -> Any:
    return _resolve_zone(normalize_sheet_timezone_name(tz_name))


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

GOOGLE_SHEETS_ID_PATTERN = re.compile(
    r"docs\.google\.com/spreadsheets/d/([a-zA-Z0-9-_]+)",
)

HEADERLESS_LOG_FIELDS = ("timestamp", "sheet_id", "handler", "heat", "location", "source")


class GoogleSheetError(ValueError):
    pass


def parse_spreadsheet_id(sheet_url: str) -> str:
    match = GOOGLE_SHEETS_ID_PATTERN.search(sheet_url.strip())
    if not match:
        raise GoogleSheetError("Invalid Google Sheet URL")
    return match.group(1)


def _fetch_sheet_csv(
    spreadsheet_id: str,
    sheet_name: str,
    *,
    timeout: float = 15.0,
) -> str:
    url = (
        f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq"
        f"?tqx=out:csv&sheet={quote(sheet_name)}"
    )
    request = Request(url, headers={"User-Agent": "dogsport-photo-tools/1.0"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8-sig")
    except HTTPError as exc:
        raise GoogleSheetError(f"Unable to read sheet ({exc.code})") from exc
    except URLError as exc:
        raise GoogleSheetError("Unable to reach Google Sheets") from exc

    if raw.strip().startswith("<!DOCTYPE") or raw.strip().startswith("<html"):
        raise GoogleSheetError(
            "Unable to read sheet. Share it as 'Anyone with the link can view'.",
        )
    return raw


def fetch_sheet_table(
    spreadsheet_id: str,
    sheet_name: str,
    *,
    timeout: float = 15.0,
) -> list[list[str]]:
    raw = _fetch_sheet_csv(spreadsheet_id, sheet_name, timeout=timeout)
    reader = csv.reader(io.StringIO(raw))
    rows: list[list[str]] = []
    for row in reader:
        cleaned = [cell.strip() for cell in row]
        if any(cleaned):
            rows.append(cleaned)
    return rows


def _normalize_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _row_looks_like_header(row: list[str]) -> bool:
    if not row:
        return False
    if parse_timestamp(row[0].strip()):
        return False
    normalized_cells = {_normalize_header(cell) for cell in row if cell.strip()}
    header_names = {
        "handler",
        "timestamp",
        "time",
        "date",
        "email",
        "heat",
        "location",
        "dock",
        "lane",
        "pool",
        "id",
        "phone",
    }
    return len(normalized_cells & header_names) >= 2


def _rows_from_headerless_table(
    table: list[list[str]],
    field_names: tuple[str, ...],
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row in table:
        mapped = {
            field_names[index]: (row[index].strip() if index < len(row) else "")
            for index in range(len(field_names))
        }
        if any(mapped.values()):
            rows.append(mapped)
    return rows


def fetch_sheet_rows(
    spreadsheet_id: str,
    sheet_name: str,
    *,
    timeout: float = 15.0,
) -> list[dict[str, str]]:
    table = fetch_sheet_table(spreadsheet_id, sheet_name, timeout=timeout)
    if not table:
        return []

    if _row_looks_like_header(table[0]):
        headers = table[0]
        data_rows = table[1:]
    else:
        return _rows_from_headerless_table(table, HEADERLESS_LOG_FIELDS)

    rows: list[dict[str, str]] = []
    for row in data_rows:
        cleaned = {
            headers[index].strip(): (row[index].strip() if index < len(row) else "")
            for index in range(len(headers))
            if headers[index].strip()
        }
        if any(cleaned.values()):
            rows.append(cleaned)
    return rows


def fetch_log_rows(
    spreadsheet_id: str,
    *,
    timeout: float = 15.0,
) -> list[dict[str, str]]:
    table = fetch_sheet_table(spreadsheet_id, "Log", timeout=timeout)
    if not table:
        return []
    if _row_looks_like_header(table[0]):
        headers = table[0]
        return [
            {
                headers[index].strip(): (row[index].strip() if index < len(row) else "")
                for index in range(len(headers))
                if headers[index].strip()
            }
            for row in table[1:]
            if any(cell.strip() for cell in row)
        ]
    return _rows_from_headerless_table(table, HEADERLESS_LOG_FIELDS)


def _find_column(row_keys: list[str], *candidates: str) -> Optional[str]:
    normalized = {_normalize_header(key): key for key in row_keys}
    for candidate in candidates:
        key = normalized.get(_normalize_header(candidate))
        if key:
            return key
    return None


def _normalize_handler_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def parse_roster(
    rows: list[dict[str, str]],
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    if not rows:
        return {}, {}

    keys = list(rows[0].keys())
    name_col = _find_column(keys, "handler", "name", "handler name", "handler_name")
    email_col = _find_column(keys, "email", "handler email", "handler_email", "e-mail")
    phone_col = _find_column(keys, "phone", "handler phone", "handler_phone", "cell")
    id_col = _find_column(keys, "id", "handler id", "handler_id", "sheet_id")
    if name_col is None:
        raise GoogleSheetError('Roster tab must include a "Handler" or "Name" column')

    roster_by_name: dict[str, dict[str, str]] = {}
    roster_by_id: dict[str, dict[str, str]] = {}
    for row in rows:
        name = row.get(name_col, "").strip()
        if not name:
            continue
        email = row.get(email_col or "", "").strip() if email_col else ""
        phone = row.get(phone_col or "", "").strip() if phone_col else ""
        entry = {"name": name, "email": email, "phone": phone}
        roster_by_name[_normalize_handler_key(name)] = entry
        if id_col:
            sheet_id = row.get(id_col, "").strip()
            if sheet_id:
                roster_by_id[sheet_id] = entry
    return roster_by_name, roster_by_id


def parse_timestamp(value: str) -> Optional[datetime]:
    raw = value.strip()
    if not raw:
        return None

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def match_heat_to_location_name(heat: str, location_names: list[str]) -> Optional[str]:
    heat_norm = heat.strip()
    if not heat_norm or not location_names:
        return None

    lowered_names = {name.strip().lower(): name for name in location_names}

    if heat_norm.lower() in lowered_names:
        return lowered_names[heat_norm.lower()]

    for name in location_names:
        name_norm = name.strip().lower()
        if heat_norm.lower() in name_norm or name_norm in heat_norm.lower():
            return name

    digits = re.search(r"\d+", heat_norm)
    if digits:
        index = int(digits.group()) - 1
        if 0 <= index < len(location_names):
            return location_names[index]

    return None


def _resolve_location_name(
    *,
    location_value: str,
    heat_value: str,
    location_names: list[str],
    unmapped_heats: set[str],
    default_location_name: str | None = None,
) -> Optional[str]:
    for candidate in (location_value, heat_value):
        if not candidate:
            continue
        location_name = match_heat_to_location_name(candidate, location_names)
        if location_name:
            return location_name

    if location_value:
        unmapped_heats.add(location_value)
    elif heat_value:
        unmapped_heats.add(heat_value)
    elif default_location_name:
        return default_location_name
    return None


def _lookup_roster_entry(
    handler_name: str,
    sheet_id: str,
    roster_by_name: Mapping[str, Mapping[str, str]],
    roster_by_id: Mapping[str, Mapping[str, str]],
) -> dict[str, str]:
    if sheet_id and sheet_id in roster_by_id:
        return dict(roster_by_id[sheet_id])
    return dict(roster_by_name.get(_normalize_handler_key(handler_name), {}))


def parse_log_entries(
    rows: list[dict[str, str]],
    *,
    roster_by_name: Mapping[str, Mapping[str, str]],
    roster_by_id: Mapping[str, Mapping[str, str]] | None = None,
    location_names: list[str],
    default_location_name: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    if not rows:
        return [], []

    roster_by_id = roster_by_id or {}
    keys = list(rows[0].keys())
    timestamp_col = _find_column(
        keys,
        "timestamp",
        "time",
        "date",
        "datetime",
        "date/time",
        "checked in",
        "check in",
    )
    handler_col = _find_column(keys, "handler", "name", "handler name", "handler_name")
    heat_col = _find_column(keys, "heat")
    location_col = _find_column(keys, "location", "dock", "lane", "pool")
    sheet_id_col = _find_column(keys, "id", "sheet_id", "handler id", "handler_id")
    if handler_col is None:
        raise GoogleSheetError('Log tab must include a "Handler" column')
    if heat_col is None and location_col is None:
        raise GoogleSheetError('Log tab must include a "Location" or "Heat" column')

    entries: list[dict[str, Any]] = []
    unmapped_heats: set[str] = set()

    for row in rows:
        handler_name = row.get(handler_col, "").strip()
        if not handler_name:
            continue

        heat_value = row.get(heat_col or "", "").strip() if heat_col else ""
        location_value = row.get(location_col or "", "").strip() if location_col else ""
        sheet_id = row.get(sheet_id_col or "", "").strip() if sheet_id_col else ""
        timestamp_raw = row.get(timestamp_col or "", "").strip() if timestamp_col else ""
        roster_entry = _lookup_roster_entry(handler_name, sheet_id, roster_by_name, roster_by_id)
        location_name = _resolve_location_name(
            location_value=location_value,
            heat_value=heat_value,
            location_names=location_names,
            unmapped_heats=unmapped_heats,
            default_location_name=default_location_name,
        )

        entries.append(
            {
                "timestamp": timestamp_raw or None,
                "sheet_id": sheet_id or None,
                "handler_name": roster_entry.get("name") or handler_name,
                "handler_email": roster_entry.get("email") or None,
                "handler_phone": roster_entry.get("phone") or None,
                "heat": heat_value or location_value,
                "location_name": location_name,
            },
        )

    entries.sort(
        key=lambda item: item["timestamp"] or "",
        reverse=True,
    )
    return entries, sorted(unmapped_heats)


def fetch_optional_sheet_rows(
    spreadsheet_id: str,
    sheet_name: str,
    *,
    timeout: float = 15.0,
) -> list[dict[str, str]]:
    try:
        return fetch_sheet_rows(spreadsheet_id, sheet_name, timeout=timeout)
    except GoogleSheetError:
        return []


def _parse_key_value_metadata_rows(rows: list[dict[str, str]]) -> dict[str, str]:
    if not rows:
        return {}

    keys = list(rows[0].keys())
    if len(keys) < 2:
        return {}

    field_col = keys[0]
    value_col = keys[1]
    metadata: dict[str, str] = {}
    for row in rows:
        field = row.get(field_col, "").strip()
        value = row.get(value_col, "").strip()
        if field and value:
            metadata[_normalize_header(field)] = value
    return metadata


def _parse_tabular_metadata_row(rows: list[dict[str, str]]) -> dict[str, str]:
    if not rows:
        return {}

    metadata: dict[str, str] = {}
    row = rows[0]
    for key, value in row.items():
        normalized_key = _normalize_header(key)
        if not normalized_key:
            continue
        stripped = value.strip()
        if stripped:
            metadata[normalized_key] = stripped
    return metadata


def _metadata_value(metadata: Mapping[str, str], *candidates: str) -> Optional[str]:
    for candidate in candidates:
        value = metadata.get(_normalize_header(candidate))
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_event_metadata_rows(rows: list[dict[str, str]]) -> dict[str, str]:
    event = parse_optional_event_metadata_rows(rows)
    if not event:
        raise GoogleSheetError(
            'Spreadsheet must include event details on the "Setup" or "Event" tab',
        )
    if not event.get("name"):
        raise GoogleSheetError(
            'Spreadsheet must include an event name on the "Setup" tab ("Event Name") '
            'or "Event" tab ("Name" or "Event")',
        )
    return event


def parse_optional_event_metadata_rows(rows: list[dict[str, str]]) -> dict[str, str]:
    if not rows:
        return {}

    raw = _parse_key_value_metadata_rows(rows)
    if not raw:
        raw = _parse_tabular_metadata_row(rows)
    if not raw:
        return {}

    event: dict[str, str] = {}
    name = _metadata_value(raw, "name", "event", "event name", "event_name")
    if name:
        event["name"] = name
    for target, candidates in (
        ("org", ("org", "organization", "sanctioning body")),
        ("city", ("city",)),
        ("state", ("state",)),
        ("start_date", ("start date", "start_date", "start")),
        ("end_date", ("end date", "end_date", "end")),
        ("club", ("club",)),
        ("venue", ("venue",)),
        ("org_type", ("org type", "org_type", "organization type")),
        ("event_code", ("code", "event code", "event_code")),
    ):
        value = _metadata_value(raw, *candidates)
        if value:
            event[target] = value
    return event


def _merge_event_metadata(*sources: Mapping[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for source in sources:
        for key, value in source.items():
            if isinstance(value, str) and value.strip() and key not in merged:
                merged[key] = value.strip()
    return merged


def fetch_event_metadata(
    spreadsheet_id: str,
    *,
    timeout: float = 15.0,
) -> dict[str, str]:
    setup_metadata: dict[str, str] = {}
    for sheet_name in ("Setup", "setup"):
        rows = fetch_optional_sheet_rows(spreadsheet_id, sheet_name, timeout=timeout)
        setup_metadata = _merge_event_metadata(
            setup_metadata,
            parse_optional_event_metadata_rows(rows),
        )

    event_metadata: dict[str, str] = {}
    for sheet_name in ("Event", "Events", "event"):
        rows = fetch_optional_sheet_rows(spreadsheet_id, sheet_name, timeout=timeout)
        event_metadata = _merge_event_metadata(
            event_metadata,
            parse_optional_event_metadata_rows(rows),
        )

    merged = _merge_event_metadata(setup_metadata, event_metadata)
    if setup_metadata.get("name"):
        merged["name"] = setup_metadata["name"]

    if not merged.get("name"):
        raise GoogleSheetError(
            'Spreadsheet must include an event name on the "Setup" tab ("Event Name") '
            'or "Event" tab ("Name" or "Event")',
        )
    return merged


def infer_default_location_name(location_names: list[str]) -> str:
    if location_names:
        return location_names[0]
    return "default"


def parse_location_names(rows: list[dict[str, str]]) -> list[str]:
    if not rows:
        return []

    keys = list(rows[0].keys())
    name_col = _find_column(keys, "location", "name", "dock", "lane", "pool", "heat")
    if name_col is None:
        return []

    names: list[str] = []
    seen: set[str] = set()
    for row in rows:
        value = row.get(name_col, "").strip()
        lowered = value.lower()
        if value and lowered not in seen:
            seen.add(lowered)
            names.append(value)
    return names


def fetch_location_names(
    spreadsheet_id: str,
    *,
    log_rows: list[dict[str, str]] | None = None,
    timeout: float = 15.0,
) -> list[str]:
    for sheet_name in ("Locations", "Location", "Heats", "Docks"):
        rows = fetch_optional_sheet_rows(spreadsheet_id, sheet_name, timeout=timeout)
        names = parse_location_names(rows)
        if names:
            return names

    if not log_rows:
        return []

    keys = list(log_rows[0].keys())
    heat_col = _find_column(keys, "heat")
    location_col = _find_column(keys, "location", "dock", "lane", "pool")
    names: list[str] = []
    seen: set[str] = set()
    for row in log_rows:
        for col in (location_col, heat_col):
            if not col:
                continue
            value = row.get(col, "").strip()
            lowered = value.lower()
            if value and lowered not in seen:
                seen.add(lowered)
                names.append(value)
    return names


def localize_sheet_timestamp(
    value: str,
    *,
    event_tz: Any = None,
) -> Optional[datetime]:
    parsed = parse_timestamp(value)
    if parsed is None:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    if event_tz is None:
        event_tz = _resolve_zone(DEFAULT_SHEET_TIMEZONE)
    return parsed.replace(tzinfo=event_tz).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Run order store (minimal)
# ---------------------------------------------------------------------------

DEFAULT_SCHEMA_VERSION = rot.SCHEMA_VERSION
RunOrderEntry = rot.RunOrderEntry


def parse_labeled_name(value: str) -> tuple[str, str | None]:
    trimmed = value.strip()
    if trimmed.endswith(")"):
        open_paren = trimmed.rfind(" (")
        if open_paren != -1:
            suffix = trimmed[open_paren + 2 : -1]
            if len(suffix) == 5 and suffix.isdigit():
                return trimmed[:open_paren].strip(), suffix
    return trimmed, None


def sanitize_event_name_for_filename(name: str) -> str:
    stripped = name.strip()
    if not stripped:
        return "Event"
    with_underscores = re.sub(r"\s+", "_", stripped)
    sanitized = re.sub(r"[^A-Za-z0-9_]", "", with_underscores)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    return sanitized or "Event"


def default_event_metadata() -> dict[str, str]:
    today = datetime.now(timezone.utc).date().isoformat()
    return {
        "name": "Dog Registry Check-ins",
        "org": "Dog Registry",
        "dogsportphoto_code": "0000",
        "city": "Unspecified",
        "state": "Unspecified",
        "start_date": today,
        "end_date": today,
    }


def is_specified_event_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    return bool(stripped) and stripped.lower() != "unspecified"


def _normalize_event_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip()
    if len(code) == 4 and code.isdigit():
        return code
    return None


def sanitize_event_metadata(metadata: Mapping[str, Any]) -> dict[str, str]:
    """Build event metadata for timeseries storage, omitting unspecified optional fields."""
    defaults = default_event_metadata()
    sanitized: dict[str, str] = {}
    for key in ("name", "start_date"):
        value = metadata.get(key)
        sanitized[key] = value.strip() if is_specified_event_value(value) else defaults[key]
    org = metadata.get("org")
    sanitized["org"] = org.strip() if is_specified_event_value(org) else defaults["org"]
    event_code = _normalize_event_code(
        metadata.get("dogsportphoto_code") or metadata.get("event_code")
    )
    sanitized["dogsportphoto_code"] = event_code or defaults["dogsportphoto_code"]
    end_date = metadata.get("end_date")
    if is_specified_event_value(end_date):
        sanitized["end_date"] = end_date.strip()
    elif is_specified_event_value(sanitized.get("start_date")):
        sanitized["end_date"] = sanitized["start_date"]
    else:
        sanitized["end_date"] = defaults["end_date"]
    for key in ("city", "state", "club", "venue", "org_type", "sheet_timezone"):
        value = metadata.get(key)
        if is_specified_event_value(value):
            sanitized[key] = value.strip()
    return sanitized


def validate_run_order_document(data: Mapping[str, Any]) -> None:
    try:
        import jsonschema
    except ImportError:
        return

    schema_path = Path(__file__).resolve().parent.parent / "run_order.schema.json"
    if not schema_path.exists():
        return

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(instance=data, schema=schema)


class RunOrder(rot.RunOrder):
    def write(self, path: str | Path | None = None) -> Path:
        rot.migrate_document_to_v2(self._data)
        event = self._data.get("event")
        if isinstance(event, dict):
            self._data["event"] = sanitize_event_metadata(event)
        return super().write(path, validate=validate_run_order_document)


def build_check_in_entry(
    *,
    handler_name: str,
    handler_code: Optional[str] = None,
    handler_email: Optional[str] = None,
    handler_phone: Optional[str] = None,
    photo_request: Optional[str] = None,
) -> dict[str, Any]:
    return rot.build_team_check_in_entry(
        handler_name=handler_name,
        handler_code=handler_code,
        handler_email=handler_email,
        handler_phone=handler_phone,
        photo_request=photo_request,
    )


# ---------------------------------------------------------------------------
# Sheet entry dedup and timeseries write
# ---------------------------------------------------------------------------


def _normalize_checked_in_at(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def _team_check_in_signature(
    *,
    location_name: str,
    handler_name: str,
    checked_in_at: datetime,
    sheet_id: str | None = None,
) -> str:
    moment = _normalize_checked_in_at(checked_in_at)
    handler_key = _normalize_handler_key(parse_labeled_name(handler_name.strip())[0] or handler_name)
    location_key = location_name.strip().lower()
    if sheet_id:
        return f"{sheet_id}|{location_key}|{moment.isoformat()}"
    return f"{location_key}|{handler_key}|{moment.isoformat()}"


def _signature_variants(
    *,
    location_name: str,
    handler_name: str,
    checked_in_at: datetime,
    sheet_id: str | None = None,
) -> set[str]:
    variants = {
        _team_check_in_signature(
            location_name=location_name,
            handler_name=handler_name,
            checked_in_at=checked_in_at,
        ),
    }
    if sheet_id:
        variants.add(
            _team_check_in_signature(
                location_name=location_name,
                handler_name=handler_name,
                checked_in_at=checked_in_at,
                sheet_id=sheet_id,
            ),
        )
    return variants


def _checked_in_at_for_entry(
    entry: Mapping[str, Any],
    *,
    source_tz,
) -> datetime | None:
    timestamp = entry.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp.strip():
        return None

    parsed = parse_timestamp(timestamp)
    if parsed is None:
        return localize_sheet_timestamp(timestamp, event_tz=source_tz)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=source_tz).astimezone(timezone.utc)
    return parsed.astimezone(timezone.utc)


def write_sheet_entries_to_timeseries_file(
    output_path: Path,
    *,
    event_metadata: Mapping[str, str],
    entries: list[Mapping[str, Any]],
    sheet_timezone: str | None = None,
    persist_sheet_timezone: bool = True,
) -> int:
    resolved_timezone_name = normalize_sheet_timezone_name(sheet_timezone)
    source_tz = resolve_sheet_timezone(resolved_timezone_name)
    metadata_input = dict(event_metadata)
    if persist_sheet_timezone:
        metadata_input["sheet_timezone"] = resolved_timezone_name
    metadata = sanitize_event_metadata(metadata_input)

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    run_order = RunOrder.create(path, event=metadata)
    existing_signatures: set[str] = set()
    synced_count = 0

    ordered_entries = sorted(
        entries,
        key=lambda item: str(item.get("timestamp") or ""),
    )

    for entry in ordered_entries:
        location_name = entry.get("location_name")
        handler_name = entry.get("handler_name")
        handler_email = entry.get("handler_email")
        handler_phone = entry.get("handler_phone")
        sheet_id = entry.get("sheet_id")
        if not isinstance(location_name, str) or not location_name.strip():
            continue
        if not isinstance(handler_name, str) or not handler_name.strip():
            continue

        checked_in_at = _checked_in_at_for_entry(entry, source_tz=source_tz)
        if checked_in_at is None:
            continue

        normalized_sheet_id = sheet_id if isinstance(sheet_id, str) and sheet_id.strip() else None
        candidate_signatures = _signature_variants(
            location_name=location_name,
            handler_name=handler_name,
            checked_in_at=checked_in_at,
            sheet_id=normalized_sheet_id,
        )
        if candidate_signatures & existing_signatures:
            continue

        check_in_entry = build_check_in_entry(
            handler_name=handler_name.strip(),
            handler_email=handler_email if isinstance(handler_email, str) else None,
            handler_phone=handler_phone if isinstance(handler_phone, str) else None,
        )
        run_order.append(
            check_in_entry,
            timestamp=rot.format_entry_at(checked_in_at),
            location_path=(location_name.strip(),),
        )
        existing_signatures.update(candidate_signatures)
        synced_count += 1

    run_order.write()
    return synced_count


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------


def resolve_timezone_option(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return normalize_sheet_timezone_name(None)
    lowered = candidate.lower()
    for iana_name, label in SHEET_TIMEZONE_CHOICES:
        if lowered == label.lower():
            return iana_name
    return normalize_sheet_timezone_name(candidate)


def default_output_path(event_name: str) -> Path:
    safe_name = sanitize_event_name_for_filename(event_name)
    return Path(f"{safe_name}-ts.json")


def event_date_range_from_check_ins(
    entries: list[dict],
    *,
    timezone_name: str,
) -> tuple[str, str]:
    source_tz = resolve_sheet_timezone(timezone_name)
    dates = []
    for entry in entries:
        checked_in_at = _checked_in_at_for_entry(entry, source_tz=source_tz)
        if checked_in_at is not None:
            dates.append(checked_in_at.astimezone(source_tz).date())
    if not dates:
        today = datetime.now(timezone.utc).astimezone(source_tz).date().isoformat()
        return today, today
    return min(dates).isoformat(), max(dates).isoformat()


def build_output_event_metadata(
    sheet_metadata: dict[str, str],
    entries: list[dict],
    *,
    timezone_name: str,
) -> dict[str, str]:
    name = (sheet_metadata.get("name") or "").strip()
    if not name:
        raise GoogleSheetError(
            'Spreadsheet must include an event name on the "Setup" tab ("Event Name") '
            'or "Event" tab ("Name" or "Event")',
        )

    defaults = default_event_metadata()
    start_date, end_date = event_date_range_from_check_ins(entries, timezone_name=timezone_name)
    metadata = {
        "name": name,
        "start_date": start_date,
        "end_date": end_date,
        "org": defaults["org"],
        "dogsportphoto_code": _normalize_event_code(
            sheet_metadata.get("dogsportphoto_code") or sheet_metadata.get("event_code")
        )
        or defaults["dogsportphoto_code"],
    }
    for key in ("city", "state", "club", "venue", "org_type"):
        value = sheet_metadata.get(key)
        if is_specified_event_value(value):
            metadata[key] = value.strip()
    org = sheet_metadata.get("org")
    if is_specified_event_value(org):
        metadata["org"] = org.strip()
    return sanitize_event_metadata(metadata)


def build_timeseries_from_sheet(
    sheet_url: str,
    *,
    timezone_name: str,
    output_path: Path | None = None,
) -> Path:
    spreadsheet_id = parse_spreadsheet_id(sheet_url)
    sheet_metadata = fetch_event_metadata(spreadsheet_id)
    roster_rows = fetch_sheet_rows(spreadsheet_id, "Roster")
    log_rows = fetch_log_rows(spreadsheet_id)
    roster_by_name, roster_by_id = parse_roster(roster_rows)
    location_names = fetch_location_names(spreadsheet_id, log_rows=log_rows)
    default_location_name = infer_default_location_name(location_names)

    raw_entries, unmapped_heats = parse_log_entries(
        log_rows,
        roster_by_name=roster_by_name,
        roster_by_id=roster_by_id,
        location_names=location_names,
        default_location_name=default_location_name,
    )
    syncable_entries = [
        entry for entry in raw_entries if (entry.get("location_name") or "").strip()
    ]
    if raw_entries and not syncable_entries:
        skipped = len(raw_entries)
        raise GoogleSheetError(
            f"{skipped} sheet check-in{'s' if skipped != 1 else ''} missing Heat/Location. "
            "Add Heat values to the Log tab or include location/heat names on the sheet."
        )

    destination = output_path or default_output_path(sheet_metadata["name"])
    event_metadata = build_output_event_metadata(
        sheet_metadata,
        syncable_entries,
        timezone_name=timezone_name,
    )
    synced_count = write_sheet_entries_to_timeseries_file(
        destination,
        event_metadata=event_metadata,
        entries=syncable_entries,
        sheet_timezone=timezone_name,
        persist_sheet_timezone=False,
    )

    if unmapped_heats:
        print(
            "Warning: unmapped heats/locations: {}".format(", ".join(unmapped_heats)),
            file=sys.stderr,
        )
    print(f"Wrote {synced_count} check-in{'s' if synced_count != 1 else ''} to {destination}")
    return destination


def main(argv: list[str] | None = None) -> int:
    if argv is None and len(sys.argv) == 1:
        print(__doc__)
        return 0
    arguments = docopt(__doc__, argv=argv)
    sheet_url = str(arguments["<sheet_url>"]).strip()
    timezone_name = resolve_timezone_option(str(arguments["--timezone"]))
    output_path = Path(arguments["--file"]).expanduser() if arguments["--file"] else None

    try:
        build_timeseries_from_sheet(
            sheet_url,
            timezone_name=timezone_name,
            output_path=output_path,
        )
    except GoogleSheetError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
