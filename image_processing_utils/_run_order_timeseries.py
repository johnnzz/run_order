"""Internal shared run_order timeseries helpers for schema version 2.0.0.

Not run directly — import as _run_order_timeseries. The leading underscore marks
this as a private helper module co-located with the CLI scripts that need it.

Used by:
  - process_queue.py — load/migrate timeseries, index entries for photo matching
    (handler, dog, team, photographer, discipline)
  - stage_into_dirs.py — handler emails from team_check_in entries
  - google_to_timeseries.py — build v2 timeseries from Google Sheets

Not imported by summarize_dir.py.

Provides:
  - Schema/migration: SCHEMA_VERSION, migrate_document_to_v2 (legacy v1 location tree → v2 entries)
  - Entry model: RunOrder, RunOrderEntry, build_*_entry helpers
  - Parsing: entry_type, parse_entry_at, format_entry_at, display names, photo request,
    discipline, cameras, entries_match
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SCHEMA_VERSION = "2.0.0"

_LEGACY_CHECK_INS_KEY = "check_ins"
_LOCATION_METADATA_KEYS = frozenset({"cameras", "locations", "selected_location_id"})
_TIMESTAMP_KEY_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

UNSPECIFIED_DOG_NAME = "Unspecified"
UNSPECIFIED_HANDLER_NAME = "Unspecified"


def format_entry_at(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat(timespec="seconds")


def parse_entry_at(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    return datetime.fromisoformat(normalized)


def photo_request_to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "Yes":
            return True
        if stripped == "No":
            return False
    return None


def photo_request_from_bool(value: Optional[bool]) -> Optional[bool]:
    if value is None:
        return None
    return bool(value)


def _labeled_name(name: str, code: Optional[str]) -> str:
    base = name.strip()
    if code and str(code).strip():
        return f"{base} ({str(code).strip()})"
    return base


def _parse_labeled_name(value: str) -> tuple[str, Optional[str]]:
    stripped = value.strip()
    open_paren = stripped.rfind(" (")
    if open_paren > 0 and stripped.endswith(")"):
        suffix = stripped[open_paren + 2 : -1]
        if suffix:
            return stripped[:open_paren].strip(), suffix
    return stripped, None


def handler_object(
    *,
    name: str,
    dogsportphoto_code: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name.strip()}
    if dogsportphoto_code and str(dogsportphoto_code).strip():
        payload["dogsportphoto_code"] = str(dogsportphoto_code).strip()
    if email and str(email).strip():
        payload["email"] = str(email).strip()
    if phone and str(phone).strip():
        payload["phone"] = str(phone).strip()
    return payload


def dog_object(
    *,
    name: str,
    dogsportphoto_code: Optional[str] = None,
    breed: Optional[str] = None,
    color: Optional[str] = None,
    org_ids: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name.strip()}
    if dogsportphoto_code and str(dogsportphoto_code).strip():
        payload["dogsportphoto_code"] = str(dogsportphoto_code).strip()
    if breed and str(breed).strip():
        payload["breed"] = str(breed).strip()
    if color and str(color).strip():
        payload["color"] = str(color).strip()
    if org_ids:
        payload["org_ids"] = {str(key): str(value) for key, value in org_ids.items()}
    return payload


def team_object(
    *,
    name: str,
    dogsportphoto_code: Optional[str] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name.strip()}
    if dogsportphoto_code and str(dogsportphoto_code).strip():
        payload["dogsportphoto_code"] = str(dogsportphoto_code).strip()
    return payload


def entry_type(entry: Mapping[str, Any]) -> Optional[str]:
    explicit = entry.get("type")
    if explicit in {"team_check_in", "photographer_check_in", "set_discipline"}:
        return str(explicit)
    if isinstance(entry.get("discipline"), str) and entry.get("discipline", "").strip():
        return "set_discipline"
    photographer = entry.get("photographer")
    if isinstance(photographer, dict) and isinstance(photographer.get("cameras"), list):
        return "photographer_check_in"
    if isinstance(photographer, str) and isinstance(entry.get("cameras"), list):
        return "photographer_check_in"
    if entry.get("handler") is not None:
        return "team_check_in"
    return None


def handler_display_name(entry: Mapping[str, Any]) -> str:
    handler = entry.get("handler")
    if isinstance(handler, dict):
        name = handler.get("name")
        code = handler.get("dogsportphoto_code")
        if isinstance(name, str) and name.strip():
            return _labeled_name(name, code if isinstance(code, str) else None)
    if isinstance(handler, str) and handler.strip():
        return handler.strip()
    return UNSPECIFIED_HANDLER_NAME


def dog_display_name(entry: Mapping[str, Any]) -> str:
    dog = entry.get("dog")
    if isinstance(dog, dict):
        name = dog.get("name", dog.get("call_name"))
        code = dog.get("dogsportphoto_code")
        if isinstance(name, str) and name.strip():
            return _labeled_name(name, code if isinstance(code, str) else None)
    return UNSPECIFIED_DOG_NAME


def team_display_name(entry: Mapping[str, Any]) -> Optional[str]:
    team = entry.get("team")
    if isinstance(team, dict):
        name = team.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    if isinstance(team, str) and team.strip():
        return team.strip()
    handler_name = handler_display_name(entry)
    dog_name = dog_display_name(entry)
    if dog_name != UNSPECIFIED_DOG_NAME:
        handler_base, _ = _parse_labeled_name(handler_name)
        dog_base, _ = _parse_labeled_name(dog_name)
        return f"{handler_base} n {dog_base}"
    return None


def handler_email(entry: Mapping[str, Any]) -> Optional[str]:
    handler = entry.get("handler")
    if isinstance(handler, dict):
        email = handler.get("email")
        if isinstance(email, str) and email.strip():
            return email.strip()
    legacy = entry.get("handler-email")
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip()
    return None


def entry_photo_request(entry: Mapping[str, Any]) -> Optional[bool]:
    event_block = entry.get("event")
    if isinstance(event_block, dict):
        value = photo_request_to_bool(event_block.get("photo_request"))
        if value is not None:
            return value
    return photo_request_to_bool(entry.get("photo_request"))


def entry_message_to_photographer(entry: Mapping[str, Any]) -> Optional[str]:
    event_block = entry.get("event")
    if isinstance(event_block, dict):
        value = event_block.get("message_to_photographer")
        if isinstance(value, str) and value.strip():
            return value.strip().replace("\n", " ")
    legacy = entry.get("message_to_photographer")
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip().replace("\n", " ")
    return None


def build_entry_attendance_block(
    *,
    photo_request: Optional[str] = None,
    message_to_photographer: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    event_block: dict[str, Any] = {}
    photo_bool = photo_request_to_bool(photo_request)
    if photo_bool is not None:
        event_block["photo_request"] = photo_bool
    message = (message_to_photographer or "").strip().replace("\n", " ") or None
    if message:
        event_block["message_to_photographer"] = message
    return event_block or None


def photographer_name(entry: Mapping[str, Any]) -> str:
    photographer = entry.get("photographer")
    if isinstance(photographer, dict):
        name = photographer.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    if isinstance(photographer, str) and photographer.strip():
        return photographer.strip()
    return UNSPECIFIED_HANDLER_NAME


def photographer_cameras(entry: Mapping[str, Any]) -> list[dict[str, str]]:
    photographer = entry.get("photographer")
    cameras_raw: Any = None
    if isinstance(photographer, dict):
        cameras_raw = photographer.get("cameras")
    if cameras_raw is None:
        cameras_raw = entry.get("cameras")
    cameras: list[dict[str, str]] = []
    if isinstance(cameras_raw, list):
        for camera in cameras_raw:
            if not isinstance(camera, dict):
                continue
            model = camera.get("model")
            serial = camera.get("serial", camera.get("serial_number"))
            if isinstance(model, str) and model.strip() and isinstance(serial, str) and serial.strip():
                cameras.append({"model": model.strip(), "serial": serial.strip()})
    cameras.sort(key=lambda item: (item["model"], item["serial"]))
    return cameras


def discipline_name(entry: Mapping[str, Any]) -> Optional[str]:
    discipline = entry.get("discipline")
    if isinstance(discipline, str) and discipline.strip():
        return discipline.strip()
    return None


def legacy_dog_payload(entry: Mapping[str, Any]) -> dict[str, Any]:
    dog = entry.get("dog")
    if not isinstance(dog, dict):
        return {}
    payload: dict[str, Any] = {}
    name = dog.get("name", dog.get("call_name"))
    if isinstance(name, str) and name.strip():
        code = dog.get("dogsportphoto_code")
        payload["call_name"] = _labeled_name(
            name,
            code if isinstance(code, str) else None,
        )
        if isinstance(code, str) and code.strip():
            payload["dogsportphoto_code"] = code.strip()
    for key in ("breed", "color", "org_ids"):
        value = dog.get(key)
        if value is not None:
            payload[key] = value
    return payload


def _legacy_entry_to_v2(
    entry: Mapping[str, Any],
    *,
    at: str,
    location: str,
) -> dict[str, Any]:
    kind = entry_type(entry)
    if kind == "photographer_check_in":
        return {
            "at": at,
            "location": location,
            "type": "photographer_check_in",
            "photographer": {
                "name": photographer_name(entry),
                "cameras": photographer_cameras(entry),
            },
        }
    if kind == "set_discipline":
        return {
            "at": at,
            "location": location,
            "type": "set_discipline",
            "discipline": discipline_name(entry) or "",
        }

    payload: dict[str, Any] = {
        "at": at,
        "location": location,
        "type": "team_check_in",
        "handler": _legacy_handler_to_object(entry),
    }
    dog = entry.get("dog")
    if isinstance(dog, dict) and (
        isinstance(dog.get("name"), str)
        or isinstance(dog.get("call_name"), str)
    ):
        name = dog.get("name") or dog.get("call_name")
        code = dog.get("dogsportphoto_code")
        if not code and isinstance(name, str):
            _, parsed_code = _parse_labeled_name(name)
            code = parsed_code
            if isinstance(name, str):
                base, _ = _parse_labeled_name(name)
                name = base
        payload["dog"] = dog_object(
            name=str(name),
            dogsportphoto_code=code if isinstance(code, str) else None,
            breed=dog.get("breed") if isinstance(dog.get("breed"), str) else None,
            color=dog.get("color") if isinstance(dog.get("color"), str) else None,
            org_ids=dog.get("org_ids") if isinstance(dog.get("org_ids"), dict) else None,
        )
    team_name = team_display_name(entry)
    if team_name:
        team = entry.get("team")
        team_code = team.get("dogsportphoto_code") if isinstance(team, dict) else None
        payload["team"] = team_object(name=team_name, dogsportphoto_code=team_code if isinstance(team_code, str) else None)
    event_block = build_entry_attendance_block(
        photo_request=photo_request_to_bool(entry_photo_request(entry)),
        message_to_photographer=entry_message_to_photographer(entry),
    )
    if event_block is not None:
        payload["event"] = event_block
    return payload


def _legacy_handler_to_object(entry: Mapping[str, Any]) -> dict[str, Any]:
    handler = entry.get("handler")
    if isinstance(handler, dict):
        return handler_object(
            name=str(handler.get("name") or UNSPECIFIED_HANDLER_NAME),
            dogsportphoto_code=handler.get("dogsportphoto_code")
            if isinstance(handler.get("dogsportphoto_code"), str)
            else None,
            email=handler.get("email") if isinstance(handler.get("email"), str) else handler_email(entry),
            phone=handler.get("phone") if isinstance(handler.get("phone"), str) else None,
        )
    name = handler if isinstance(handler, str) else UNSPECIFIED_HANDLER_NAME
    base, code = _parse_labeled_name(name)
    return handler_object(
        name=base,
        dogsportphoto_code=code,
        email=handler_email(entry),
        phone=entry.get("handler-phone") if isinstance(entry.get("handler-phone"), str) else None,
    )


def _walk_location_tree(
    node: Mapping[str, Any],
    location_path: tuple[str, ...] = (),
) -> Iterator[tuple[tuple[str, ...], str, list[Any]]]:
    for key, value in node.items():
        if isinstance(value, list):
            yield location_path, key, value
        elif isinstance(value, dict):
            yield from _walk_location_tree(value, location_path + (key,))


def _location_name_from_path(location_path: tuple[str, ...]) -> str:
    if not location_path:
        return "default"
    if location_path[0] == _LEGACY_CHECK_INS_KEY and len(location_path) > 1:
        return location_path[1]
    return location_path[-1]


def _normalize_timestamp_to_at(timestamp: str) -> str:
    return format_entry_at(parse_entry_at(timestamp))


def migrate_location_tree_to_entries(location: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for location_path, timestamp, slot in _walk_location_tree(location):
        if not _TIMESTAMP_KEY_RE.match(timestamp):
            continue
        at = _normalize_timestamp_to_at(timestamp)
        location_name = _location_name_from_path(location_path)
        for entry in slot:
            if isinstance(entry, dict):
                entries.append(
                    _legacy_entry_to_v2(
                        entry,
                        at=at,
                        location=location_name,
                    )
                )
    entries.sort(key=lambda item: item.get("at", ""))
    return entries


def event_dogsportphoto_code_from_metadata(
    event: Mapping[str, Any],
    *,
    fallback: Optional[str] = None,
) -> Optional[str]:
    for key in ("dogsportphoto_code", "event_code"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(fallback, str) and fallback.strip():
        return fallback.strip()
    return None


def migrate_event_metadata_code_field(event: MutableMapping[str, Any]) -> None:
    if "event_code" in event and "dogsportphoto_code" not in event:
        event["dogsportphoto_code"] = event.pop("event_code")
    event.pop("event_code", None)


def migrate_document_to_v2(
    data: MutableMapping[str, Any],
    *,
    event_code: Optional[str] = None,
) -> None:
    if data.get("schema_version") == SCHEMA_VERSION and isinstance(data.get("entries"), list):
        event = data.setdefault("event", {})
        if isinstance(event, dict):
            migrate_event_metadata_code_field(event)
            if event_code and not event_dogsportphoto_code_from_metadata(event):
                event["dogsportphoto_code"] = event_code
        data.pop("location", None)
        return

    entries: list[dict[str, Any]] = []
    if isinstance(data.get("entries"), list):
        entries.extend(item for item in data["entries"] if isinstance(item, dict))

    location = data.get("location")
    if isinstance(location, dict):
        entries.extend(migrate_location_tree_to_entries(location))

    top_level_bucket = data.pop(_LEGACY_CHECK_INS_KEY, None)
    if isinstance(top_level_bucket, dict):
        entries.extend(migrate_location_tree_to_entries({_LEGACY_CHECK_INS_KEY: top_level_bucket}))

    data.pop("location", None)
    data["entries"] = sorted(entries, key=lambda item: item.get("at", ""))
    data["schema_version"] = SCHEMA_VERSION

    event = data.get("event")
    if not isinstance(event, dict):
        event = {}
        data["event"] = event
    migrate_event_metadata_code_field(event)
    if event_code and not event_dogsportphoto_code_from_metadata(event):
        event["dogsportphoto_code"] = event_code


def build_team_check_in_entry(
    *,
    handler_name: str,
    handler_code: Optional[str] = None,
    handler_email: Optional[str] = None,
    handler_phone: Optional[str] = None,
    photo_request: Optional[str] = None,
    message_to_photographer: Optional[str] = None,
    dog_name: Optional[str] = None,
    dog_code: Optional[str] = None,
    team_name: Optional[str] = None,
    team_code: Optional[str] = None,
    breed: Optional[str] = None,
    color: Optional[str] = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "type": "team_check_in",
        "handler": handler_object(
            name=handler_name,
            dogsportphoto_code=handler_code,
            email=handler_email,
            phone=handler_phone,
        ),
    }
    if dog_name and str(dog_name).strip():
        entry["dog"] = dog_object(
            name=str(dog_name),
            dogsportphoto_code=dog_code,
            breed=breed,
            color=color,
        )
    if team_name and str(team_name).strip():
        entry["team"] = team_object(name=str(team_name), dogsportphoto_code=team_code)
    event_block = build_entry_attendance_block(
        photo_request=photo_request,
        message_to_photographer=message_to_photographer,
    )
    if event_block is not None:
        entry["event"] = event_block
    return entry


def build_photographer_check_in_entry(
    *,
    photographer_name: str,
    cameras: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "type": "photographer_check_in",
        "photographer": {
            "name": photographer_name.strip(),
            "cameras": photographer_cameras({"cameras": list(cameras)}),
        },
    }


def build_set_discipline_entry(*, discipline: str) -> dict[str, Any]:
    return {
        "type": "set_discipline",
        "discipline": discipline.strip(),
    }


def finalize_entry(
    entry: Mapping[str, Any],
    *,
    at: str,
    location: str,
) -> dict[str, Any]:
    payload = dict(entry)
    payload["at"] = at
    payload["location"] = location
    return payload


def entries_match(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_type = entry_type(left)
    right_type = entry_type(right)
    if left_type != right_type:
        return False
    if left_type == "photographer_check_in":
        return (
            photographer_name(left) == photographer_name(right)
            and photographer_cameras(left) == photographer_cameras(right)
        )
    if left_type == "set_discipline":
        return discipline_name(left) == discipline_name(right)
    return (
        handler_display_name(left) == handler_display_name(right)
        and dog_display_name(left) == dog_display_name(right)
        and handler_email(left) == handler_email(right)
        and entry_photo_request(left) == entry_photo_request(right)
        and entry_message_to_photographer(left) == entry_message_to_photographer(right)
    )


@dataclass(frozen=True)
class RunOrderEntry:
    location_path: tuple[str, ...]
    timestamp: str
    data: dict[str, Any]


class RunOrder:
    def __init__(self, data: MutableMapping[str, Any], *, path: Optional[Path] = None) -> None:
        self._data = data
        self._path = path

    @property
    def path(self) -> Optional[Path]:
        return self._path

    @classmethod
    def create(
        cls,
        path: str | Path,
        *,
        event: Mapping[str, Any],
        schema_version: str = SCHEMA_VERSION,
    ) -> RunOrder:
        document = {
            "schema_version": schema_version,
            "event": dict(event),
            "entries": [],
        }
        run_order = cls(document, path=Path(path))
        run_order.write()
        return run_order

    @classmethod
    def read(cls, path: str | Path, *, event_code: Optional[str] = None) -> RunOrder:
        file_path = Path(path)
        with file_path.open(encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError(f"Expected a JSON object in {file_path}")
        migrate_document_to_v2(data, event_code=event_code)
        return cls(data, path=file_path)

    @classmethod
    def load_or_create(cls, path: str | Path, *, event: Mapping[str, Any]) -> RunOrder:
        file_path = Path(path)
        if file_path.exists():
            return cls.read(file_path)
        return cls.create(file_path, event=event)

    def write(
        self,
        path: str | Path | None = None,
        *,
        validate: Optional[Callable[[Mapping[str, Any]], None]] = None,
    ) -> Path:
        file_path = Path(path) if path is not None else self._path
        if file_path is None:
            raise ValueError("No output path provided and no path was set on this RunOrder")

        migrate_document_to_v2(self._data)
        if validate is not None:
            validate(self._data)

        file_path.parent.mkdir(parents=True, exist_ok=True)
        with file_path.open("w", encoding="utf-8") as handle:
            try:
                self._lock_file(handle)
                json.dump(self._data, handle, indent=4)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                self._unlock_file(handle)

        self._path = file_path
        return file_path

    @staticmethod
    def _lock_file(handle) -> None:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except ImportError:
            return

    @staticmethod
    def _unlock_file(handle) -> None:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except ImportError:
            return

    def append(
        self,
        entry: Mapping[str, Any],
        *,
        timestamp: str,
        location_path: Sequence[str] = (),
    ) -> dict[str, Any]:
        location_name = location_path[-1] if location_path else "default"
        at = format_entry_at(parse_entry_at(timestamp))
        finalized = finalize_entry(entry, at=at, location=location_name)
        entries = self._data.setdefault("entries", [])
        if not isinstance(entries, list):
            raise ValueError("Top-level 'entries' must be an array")
        appended = dict(finalized)
        entries.append(appended)
        entries.sort(key=lambda item: item.get("at", ""))
        return appended

    def __iter__(self) -> Iterator[RunOrderEntry]:
        entries = self._data.get("entries")
        if not isinstance(entries, list):
            return iter(())

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            at = str(entry.get("at") or "")
            location = str(entry.get("location") or "default")
            yield RunOrderEntry((location,), at, entry)
