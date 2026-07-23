#!/usr/bin/python3
# Copyright (c) 2026 John Navitsky
#
# SPDX-License-Identifier: MIT
# See LICENSE for the full license text.
"""Match queued event photos to run-order check-ins and write EXIF keywords.

Processes photos present in a queue directory against a time-series file (or two)
and uses the data present in the time-series file to identify the handler, dog
and event data, and then encodes that data in the image using exiftool keywords.

Once the file has been processed, it is renamed and moved to a processed directory,
and optionally an unmodified version is placed in a backup directory.

When --process completes, a photo summary JSON file named <event>-ps.json is written
next to the timeseries file. Each entry records the image name, capture timestamp,
and matched handler and dog.

Each processed file will have EXIF keywords prefixed with X-, such as X-team,
X-handler, X-dog, X-event, X-org.  Results can be reviewed with the 
summarize_dir.py script.

Portable and self-contained: only requires docopt (stdlib otherwise).
Also requires exiftool on PATH for --process.

With no options, prints this help message.

Usage:
  process_queue.py [options]


Options:
  -q, --queue DIR         Queue directory [default: ./queue].
  -p, --processed DIR     Processed output directory [default: ./processed].
  -b, --backup DIR        Backup directory. If omitted, no backup is made.
  -t, --timeline FILE     Timeseries JSON file [default: ./eventname-ts.json].
      --timeline2 FILE    Optional second timeseries file merged into --timeline.
  -r, --rating NUM        Set rating on images that have none (omit to leave ratings unchanged).
  --status                Print directory paths and file counts.
  --process               Process files in the queue directory.
  --log FILE              Write log output to FILE (stdout only when omitted).
  --force                 Always overwrite existing destination files.
  --safe                  Write to _N suffix paths instead of overwriting.
  --output MODE           Output layout: flat or subdir [default: flat].
  --verbosity LEVEL       Console output: quiet or full [default: quiet].
  -h, --help              Show this message.
"""

from __future__ import annotations

import bisect
import copy
import subprocess
import hashlib
import json
import logging
import re
import sys
import uuid
from collections.abc import Mapping, MutableMapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import os
import shutil
import time

try:
	from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
	ZoneInfo = None

from docopt import docopt

import _run_order_timeseries as rot
from _exiftool_session import ExifToolSession
from _graceful_interrupt import abort_if_interrupt_requested, install_graceful_interrupt_handler, interrupt_requested
from stage_into_dirs import (
	UNMATCHED_STAGING_DIR,
	safe_team_dir_name,
	staging_dir_names_from_keywords,
)

logger = logging.getLogger(__name__)

_QUOTED_DOG_NAME_SEGMENT_RE = re.compile(r'\s*"([^"]*)"\s*')


def normalize_quoted_dog_name(value: str) -> str:
	text = str(value).strip()
	if not text or '"' not in text:
		return text
	return _QUOTED_DOG_NAME_SEGMENT_RE.sub(r" - \1", text).strip()

DEFAULT_TIMELINE_FILE = "eventname-ts.json"

IMAGE_DATE_TAGS = (
	"SubSecCreateDate",
	"SubSecDateTimeOriginal",
	"CreateDate",
	"DateTimeOriginal",
	"FileModifyDate",
)

SERIAL_NUMBER_TAGS = (
	"SerialNumber",
	"BodySerialNumber",
	"InternalSerialNumber",
	"CameraSerialNumber",
	"CameraSerialNo",
	"UserSerialNumber",
)

LENS_SERIAL_HINTS = ("lens",)

EXIF_OFFSET_TAGS = (
	"OffsetTime",
	"OffsetTimeOriginal",
	"OffsetTimeDigitized",
)

DATE_TAG_OFFSET_TAG = {
	"SubSecDateTimeOriginal": "OffsetTimeOriginal",
	"DateTimeOriginal": "OffsetTimeOriginal",
	"SubSecCreateDate": "OffsetTime",
	"CreateDate": "OffsetTime",
	"FileModifyDate": "OffsetTime",
}

INSPECT_TAGS = IMAGE_DATE_TAGS + EXIF_OFFSET_TAGS + SERIAL_NUMBER_TAGS + (
	"Keywords",
	"Rating",
	"Model",
	"CameraModelName",
	"Subject",
	"Headline",
	"Creator",
	"Credit",
	"Rights",
	"Copyright",
	"CopyrightNotice",
	"City",
	"State",
	"Location",
	"Source",
	"TransmissionReference",
)
CAMERA_MODEL_TAGS = ("CameraModelName", "Model")
TIMESTAMP_PREFIX_RE = re.compile(r"^\d{14,20}-")
DOCK_LANE_PATTERN = re.compile(r"^Dock\s+(.+?)\s+-\s+Lane\s+(\d+)$", re.IGNORECASE)
DUELING_DOGS_DISCIPLINE = "dueling dogs"

# --- Matching timing baselines ---
#
# Photo bursts (camera capture):
#   Frames in a burst are typically <= 0.2s apart (5 fps). The same jump may
#   include an occasional 1-2s pause before the next frame. Gaps much larger
#   than that are a new run/team.
PHOTO_BURST_TYPICAL_INTERVAL_SECONDS = 0.2
PHOTO_BURST_GAP_SECONDS = 2

# Check-in timestamps (timeseries):
#   Check-ins are device-timestamped and sorted post-facto. Out-of-order entries
#   or clock skew should be small and rare.
CHECK_IN_TIMESTAMP_IMPRECISION_SECONDS = 5

# Wider windows for non-match purposes (discipline selection, runlist batch
# overlap when photos span the full batch run, sequential-vs-timestamp sanity).
CHECK_IN_GRACE_SECONDS = 120

# Forward timestamp match: photo slightly before a logged check-in (QR tap lag).
FORWARD_CHECK_IN_GRACE_SECONDS = 5

# Runlist: first jump photo typically arrives a few seconds after check-in, so a
# photo logged before the next check-in is almost always still the previous run.
# Forward grace applies only when there is no prior check-in (photo before first log).
RUNLIST_FORWARD_CHECK_IN_GRACE_SECONDS = CHECK_IN_TIMESTAMP_IMPRECISION_SECONDS

# Pre-logged runlist batches: consecutive entries seconds apart; larger gap
# starts a new batch cluster or isolates a lead check-in.
CHECK_IN_BATCH_GAP_SECONDS = 60
EXIF_READ_BATCH_SIZE = 100

# Naive timestamps without an offset (Google Sheets export, EXIF without zone suffix) are
# interpreted in this timezone. Camera and sheet are Pacific; matching uses UTC instants.
NAIVE_SOURCE_TIMEZONE_NAME = os.getenv("NAIVE_TIMESTAMP_TIMEZONE", "America/Los_Angeles")

US_STATE_TIMEZONES = {
	"ak": "America/Anchorage",
	"al": "America/Chicago",
	"ar": "America/Chicago",
	"az": "America/Phoenix",
	"ca": "America/Los_Angeles",
	"co": "America/Denver",
	"ct": "America/New_York",
	"dc": "America/New_York",
	"de": "America/New_York",
	"fl": "America/New_York",
	"ga": "America/New_York",
	"hi": "Pacific/Honolulu",
	"ia": "America/Chicago",
	"id": "America/Boise",
	"il": "America/Chicago",
	"in": "America/Indiana/Indianapolis",
	"ks": "America/Chicago",
	"ky": "America/New_York",
	"la": "America/Chicago",
	"ma": "America/New_York",
	"md": "America/New_York",
	"me": "America/New_York",
	"mi": "America/Detroit",
	"mn": "America/Chicago",
	"mo": "America/Chicago",
	"ms": "America/Chicago",
	"mt": "America/Denver",
	"nc": "America/New_York",
	"nd": "America/Chicago",
	"ne": "America/Chicago",
	"nh": "America/New_York",
	"nj": "America/New_York",
	"nm": "America/Denver",
	"nv": "America/Los_Angeles",
	"ny": "America/New_York",
	"oh": "America/New_York",
	"ok": "America/Chicago",
	"or": "America/Los_Angeles",
	"pa": "America/New_York",
	"ri": "America/New_York",
	"sc": "America/New_York",
	"sd": "America/Chicago",
	"tn": "America/Chicago",
	"tx": "America/Chicago",
	"ut": "America/Denver",
	"va": "America/New_York",
	"vt": "America/New_York",
	"wa": "America/Los_Angeles",
	"wi": "America/Chicago",
	"wv": "America/New_York",
	"wy": "America/Denver",
}

def setup_logging(log_path=None, *, quiet=False):
	handlers = []
	if log_path:
		handlers.append(logging.FileHandler(log_path, mode="w"))
	if not quiet:
		handlers.append(logging.StreamHandler())
	elif not log_path:
		stream = logging.StreamHandler()
		stream.setLevel(logging.ERROR)
		handlers.append(stream)
	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s %(levelname)s %(message)s",
		handlers=handlers,
		force=True,
	)

def run_cmd(cmd):
	if isinstance(cmd, str):
		cmd = cmd.split(" ")
	result = subprocess.run(
		cmd,
		capture_output=True,
		text=True,
	)
	return result

def exiftool_error_message(cmd_out):
	return cmd_out.stderr.strip() or "no output"

def normalize_serial_value(value):
	if value is None or isinstance(value, bool):
		return None
	if isinstance(value, (list, tuple)):
		for item in value:
			serial = normalize_serial_value(item)
			if serial:
				return serial
		return None
	if isinstance(value, (int, float)):
		if isinstance(value, float) and not value.is_integer():
			return str(value).strip()
		return str(int(value))
	if isinstance(value, str):
		serial = value.strip()
		return serial if serial else None
	return str(value).strip() or None

def _is_lens_serial_tag(tag):
	tag_lower = tag.lower()
	return any(hint in tag_lower for hint in LENS_SERIAL_HINTS)

def camera_serial_from_exif(exif_json):
	for tag in SERIAL_NUMBER_TAGS:
		serial = normalize_serial_value(exif_json.get(tag))
		if serial:
			return serial

	fallback = []
	for tag, value in exif_json.items():
		if tag == "SourceFile" or "serial" not in tag.lower():
			continue
		serial = normalize_serial_value(value)
		if serial:
			fallback.append((_is_lens_serial_tag(tag), tag, serial))

	if fallback:
		fallback.sort(key=lambda item: (item[0], item[1]))
		return fallback[0][2]

	raise KeyError(
		"No camera serial found in {}; tried {}".format(
			exif_json.get("SourceFile", "image"),
			", ".join(SERIAL_NUMBER_TAGS),
		)
	)

def fetch_exif_tags(filename, tags, *, session=None):
	tag_args = ["-{}".format(tag) for tag in tags]
	if session is not None:
		exif_json = session.read_json(filename, tags)
		if exif_json is None:
			logger.warning("Skipping %s: unreadable or non-image file", filename)
		return exif_json
	cmd = ["exiftool", "-json"] + tag_args + [filename]
	cmd_out = run_cmd(cmd)
	if cmd_out.returncode == 1:
		logger.warning("Skipping %s: %s", filename, exiftool_error_message(cmd_out))
		return None
	if cmd_out.returncode != 0 or not cmd_out.stdout.strip():
		raise RuntimeError(
			"exiftool failed for {}: {}".format(filename, exiftool_error_message(cmd_out))
		)
	return json.loads(cmd_out.stdout)[0]

def first_exif_value(exif_json, *tags):
	for tag in tags:
		value = exif_json.get(tag)
		if value is None:
			continue
		if isinstance(value, (list, tuple, set)):
			parts = [str(item).strip() for item in value if item is not None and str(item).strip()]
			if parts:
				return ", ".join(parts)
			continue
		text = str(value).strip()
		if text:
			return text
	return None

def camera_model_from_exif(exif_json):
	return first_exif_value(exif_json, *CAMERA_MODEL_TAGS)

def fetch_exif_serial_tags(filename, *, session=None):
	tags = ("SerialNumber", "InternalSerialNumber", "BodySerialNumber")
	if session is not None:
		exif_json = session.read_json(filename, tags)
		return exif_json or {}
	cmd = ["exiftool", "-json", "-SerialNumber", "-InternalSerialNumber", "-BodySerialNumber", filename]
	cmd_out = run_cmd(cmd)
	if cmd_out.returncode != 0 or not cmd_out.stdout.strip():
		return {}
	return json.loads(cmd_out.stdout)[0]

def exif_time(exif_formated):
	d, t = exif_formated.split(" ", 1)
	d = "-".join(d.split(":"))
	# Pad fractional seconds to 6 digits; fromisoformat rejects shorter
	# fractions with timezone offsets on Python < 3.11 (e.g. ".04-05:00").
	match = re.match(r"(\d{2}:\d{2}:\d{2})(\.\d+)?(.*)", t)
	if match:
		time_base, frac, suffix = match.groups()
		if frac:
			frac = "." + frac[1:].ljust(6, "0")[:6]
		t = f"{time_base}{frac or ''}{suffix or ''}"
	dt = " ".join([d, t])
	return datetime.fromisoformat(dt)

def parse_run_order_timestamp(timestamp):
	normalized = timestamp.replace(" ", "T", 1)
	if normalized.endswith("Z"):
		normalized = normalized[:-1] + "+00:00"
	return datetime.fromisoformat(normalized)

def resolve_event_timezone(*, city=None, state=None):
	del city, state  # event location is not used for timestamp interpretation
	return timezone.utc

def naive_source_timezone():
	if ZoneInfo is None:
		return timezone.utc
	try:
		return ZoneInfo(NAIVE_SOURCE_TIMEZONE_NAME)
	except Exception:
		logger.warning("Unable to load naive source timezone %s", NAIVE_SOURCE_TIMEZONE_NAME)
		return timezone.utc

def event_timezone_from_time_series(time_series):
	del time_series
	return timezone.utc

def sheet_timezone_from_event(event):
	if not isinstance(event, dict):
		return None
	tz_name = event.get("sheet_timezone")
	if not isinstance(tz_name, str) or not tz_name.strip():
		return None
	if ZoneInfo is None:
		return None
	try:
		return ZoneInfo(tz_name.strip())
	except Exception:
		logger.warning("Unable to load sheet timezone %s", tz_name)
		return None

def sheet_timezone_from_time_series(time_series):
	if not isinstance(time_series, dict):
		return None
	sheet_tz = time_series.get("sheet_timezone")
	if sheet_tz is not None:
		return sheet_tz
	tz_name = time_series.get("sheet_timezone_name")
	if isinstance(tz_name, str) and tz_name.strip():
		return sheet_timezone_from_event({"sheet_timezone": tz_name})
	return None

def comparison_instant(moment, *, naive_tz=None):
	"""Normalize any timestamp to a UTC instant for ordering.

	Aware values use the offset embedded in the timestamp. Naive values are
	interpreted in naive_source_timezone() (Pacific for sheet/camera).
	"""
	if moment is None:
		return None
	if moment.tzinfo is None:
		source_tz = naive_tz or naive_source_timezone()
		moment = moment.replace(tzinfo=source_tz)
	return moment.astimezone(timezone.utc)

def normalize_for_comparison(moment, event_tz=None, naive_tz=None):
	instant = comparison_instant(moment, naive_tz=naive_tz)
	if instant is None:
		return None
	return instant.replace(tzinfo=None)

def exif_offset_timezone(offset_value):
	if not isinstance(offset_value, str):
		return None
	offset_value = offset_value.strip()
	if not offset_value:
		return None
	if offset_value in ("Z", "+00:00", "-00:00"):
		return timezone.utc
	match = re.match(r"^([+-])(\d{2}):(\d{2})$", offset_value)
	if not match:
		return None
	sign, hours, minutes = match.groups()
	total_minutes = int(hours) * 60 + int(minutes)
	if sign == "-":
		total_minutes = -total_minutes
	return timezone(timedelta(minutes=total_minutes))

def localize_naive_exif_time(exif_json, date_tag, moment):
	if moment.tzinfo is not None:
		return moment
	offset_tag = DATE_TAG_OFFSET_TAG.get(date_tag)
	if offset_tag:
		offset_tz = exif_offset_timezone(exif_json.get(offset_tag))
		if offset_tz is not None:
			return moment.replace(tzinfo=offset_tz)
	return moment.replace(tzinfo=naive_source_timezone())

def image_time_from_exif(exif_json):
	for tag in IMAGE_DATE_TAGS:
		value = exif_json.get(tag)
		if value:
			return localize_naive_exif_time(exif_json, tag, exif_time(value))
	raise KeyError(
		"No creation date found in {}; tried {}".format(
			exif_json.get("SourceFile", "image"),
			", ".join(IMAGE_DATE_TAGS),
		)
	)

def normalize_exif_json(exif_json, filename, *, session=None):
	if exif_json is None:
		return None

	exif_json["image_time"] = image_time_from_exif(exif_json)

	try:
		exif_json["camera_serial"] = camera_serial_from_exif(exif_json)
	except KeyError:
		try:
			exif_json["camera_serial"] = camera_serial_from_exif(
				fetch_exif_serial_tags(filename, session=session)
			)
		except KeyError:
			exif_json["camera_serial"] = None

	if "Keywords" in exif_json:
		image_keywords = exif_json["Keywords"]
		if isinstance(image_keywords, str):
			image_keywords = [image_keywords]
	else:
		image_keywords = list()
	image_keywords = set(image_keywords)
	image_keywords.discard(None)
	exif_json["original_x_keywords"] = x_keywords(image_keywords)
	exif_json["Keywords"] = image_keywords
	exif_json["original_iptc_metadata"] = read_iptc_metadata(exif_json)
	exif_json["Rating"] = exif_json.get("Rating")
	exif_json["log"] = []
	return exif_json

def get_exif(filename, *, session=None):
	exif_json = fetch_exif_tags(filename, INSPECT_TAGS, session=session)
	return normalize_exif_json(exif_json, filename, session=session)

def is_x_keyword(keyword):
	return isinstance(keyword, str) and keyword.startswith("X-")

def x_keywords(keywords):
	return {keyword for keyword in keywords if keyword and is_x_keyword(keyword)}

def preserve_non_x_keywords(keywords):
	return {keyword for keyword in keywords if keyword and not is_x_keyword(keyword)}

def format_keyword(field, value):
	if value is None:
		return None
	text = str(value).strip()
	if not text:
		return None
	if text.lower() in {"none", "unspecified"} and field != "handler":
		return None
	if field == "dog":
		text = normalize_quoted_dog_name(text)
	elif field == "team" and " n " in text:
		handler_part, dog_part = text.split(" n ", 1)
		text = "{} n {}".format(handler_part.strip(), normalize_quoted_dog_name(dog_part.strip()))
	return "X-{}: {}".format(field, text)

def parse_labeled_name(value):
	trimmed = value.strip()
	if trimmed.endswith(")"):
		open_paren = trimmed.rfind(" (")
		if open_paren != -1:
			suffix = trimmed[open_paren + 2 : -1]
			if len(suffix) == 5 and suffix.isdigit():
				return trimmed[:open_paren].strip(), suffix
	return trimmed, None

def format_duel_team_side(check_in):
	if not isinstance(check_in, dict):
		return None

	handler = check_in.get("handler")
	dog = check_in.get("dog")
	team = check_in.get("team")
	if not isinstance(handler, str) or not handler.strip():
		return None
	if not isinstance(dog, str) or not dog.strip():
		return None

	handler_name, _ = parse_labeled_name(handler.strip())
	dog_name, _ = parse_labeled_name(dog.strip())
	dog_name = normalize_quoted_dog_name(dog_name)
	if not handler_name or not dog_name:
		return None

	team_code = None
	if isinstance(team, str) and team.strip():
		_, team_code = parse_labeled_name(team.strip())

	if team_code:
		return "{} n {} ({})".format(handler_name, dog_name, team_code)
	return "{} n {}".format(handler_name, dog_name)

def parse_dock_lane_location_name(location_name):
	if not isinstance(location_name, str):
		return None
	match = DOCK_LANE_PATTERN.match(location_name.strip())
	if not match:
		return None
	lane = int(match.group(2))
	if lane not in (1, 2):
		return None
	return match.group(1).strip().lower(), lane

def is_dueling_dogs_discipline(discipline):
	return isinstance(discipline, str) and discipline.strip().lower() == DUELING_DOGS_DISCIPLINE

def format_dock_location_label(location_path, discipline):
	label = _location_label(location_path)
	if is_dueling_dogs_discipline(discipline) and parse_dock_lane_location_name(label) is not None:
		return re.sub(r"\s*-\s*Lane\s+\d+\s*$", "", label.strip(), flags=re.IGNORECASE)
	return label

def find_other_lane_location_path(location_path, check_ins_by_location):
	label = _location_label(location_path)
	parsed = parse_dock_lane_location_name(label)
	if parsed is None:
		return None

	dock_key, lane = parsed
	other_lane = 2 if lane == 1 else 1
	for path in check_ins_by_location:
		other_label = _location_label(path)
		other_parsed = parse_dock_lane_location_name(other_label)
		if other_parsed is None:
			continue
		other_dock_key, other_lane_number = other_parsed
		if other_dock_key == dock_key and other_lane_number == other_lane:
			return path
	return None

def _dock_lane_paths(dock_key, time_series):
	paths_by_lane = {}
	for location_path in time_series["check_ins_by_location"]:
		parsed = parse_dock_lane_location_name(_location_label(location_path))
		if parsed is None or parsed[0] != dock_key:
			continue
		paths_by_lane[parsed[1]] = location_path

	for entry in time_series["photographer_entries"]:
		location_path = entry["location_path"]
		parsed = parse_dock_lane_location_name(_location_label(location_path))
		if parsed is None or parsed[0] != dock_key:
			continue
		paths_by_lane.setdefault(parsed[1], location_path)

	for location_path in time_series["discipline_entries_by_location"]:
		parsed = parse_dock_lane_location_name(_location_label(location_path))
		if parsed is None or parsed[0] != dock_key:
			continue
		paths_by_lane.setdefault(parsed[1], location_path)

	return paths_by_lane

def resolve_dock_lane_check_ins(image_time, dock_key, time_series, event_tz):
	check_ins_by_location = time_series["check_ins_by_location"]
	sheet_tz = sheet_timezone_from_time_series(time_series)
	paths_by_lane = _dock_lane_paths(dock_key, time_series)
	lane1_path = paths_by_lane.get(1)
	lane2_path = paths_by_lane.get(2)
	lane1_check_in = (
		resolve_check_in(
			image_time,
			lane1_path,
			check_ins_by_location,
			event_tz,
			sheet_tz=sheet_tz,
			time_series=time_series,
		)
		if lane1_path is not None
		else None
	)
	lane2_check_in = (
		resolve_check_in(
			image_time,
			lane2_path,
			check_ins_by_location,
			event_tz,
			sheet_tz=sheet_tz,
			time_series=time_series,
		)
		if lane2_path is not None
		else None
	)
	return lane1_check_in, lane2_check_in

def format_duel_keyword(lane1_check_in, lane2_check_in):
	lane1_team = format_duel_team_side(lane1_check_in)
	lane2_team = format_duel_team_side(lane2_check_in)
	if not lane1_team or not lane2_team:
		return None
	return format_keyword("duel", "{} vs {}".format(lane1_team, lane2_team))

def resolve_dock_discipline(image_time, dock_key, time_series, event_tz):
	if dock_key is None:
		return None

	sheet_tz = sheet_timezone_from_time_series(time_series)
	discipline_entries_by_location = time_series["discipline_entries_by_location"]
	paths_by_lane = _dock_lane_paths(dock_key, time_series)
	lane_entries = []
	for lane in (1, 2):
		location_path = paths_by_lane.get(lane)
		if location_path is None:
			continue
		entry = resolve_discipline_entry(
			image_time,
			location_path,
			discipline_entries_by_location,
			event_tz,
			sheet_tz=sheet_tz,
			discipline_index=time_series.get("discipline_index"),
		)
		if entry is not None and entry.get("discipline"):
			lane_entries.append(entry)

	if not lane_entries:
		return None

	dueling_entries = [
		entry for entry in lane_entries
		if is_dueling_dogs_discipline(entry.get("discipline"))
	]
	if dueling_entries:
		return max(
			dueling_entries,
			key=lambda item: comparison_instant(item["time"], naive_tz=sheet_tz),
		)["discipline"]

	return max(
		lane_entries,
		key=lambda item: comparison_instant(item["time"], naive_tz=sheet_tz),
	)["discipline"]

def resolve_duel_check_ins(image_time, match, time_series):
	dock_key = _duel_dock_key(match.get("location_path"))
	if dock_key is None:
		return None

	event_tz = event_timezone_from_time_series(time_series)
	discipline = match.get("discipline")
	if not is_dueling_dogs_discipline(discipline):
		discipline = resolve_dock_discipline(image_time, dock_key, time_series, event_tz)
	if not is_dueling_dogs_discipline(discipline):
		return None
	lane1_check_in, lane2_check_in = resolve_dock_lane_check_ins(
		image_time,
		dock_key,
		time_series,
		event_tz,
	)
	has_lane1 = lane1_check_in is not None and lane1_check_in.get("dog")
	has_lane2 = lane2_check_in is not None and lane2_check_in.get("dog")
	if not has_lane1 and not has_lane2:
		return None
	return lane1_check_in, lane2_check_in

def keywords_from_org_ids(org_ids):
	keywords = set()
	if not isinstance(org_ids, dict):
		return keywords
	for org, org_id in org_ids.items():
		org_slug = str(org).strip().lower()
		org_id_text = str(org_id).strip()
		if not org_slug or not org_id_text:
			continue
		keyword = format_keyword("id-{}".format(org_slug), org_id_text)
		if keyword:
			keywords.add(keyword)
	return keywords

def keywords_from_check_in(check_in):
	keywords = set()
	if not isinstance(check_in, dict):
		return keywords
	for field, key in (
		("dog", "dog"),
		("handler", "handler"),
		("team", "team"),
	):
		keyword = format_keyword(field, check_in.get(key))
		if keyword:
			keywords.add(keyword)
	keywords.update(keywords_from_org_ids(check_in.get("org_ids")))
	return keywords

def duel_participant_keywords(image_time, match, time_series):
	duel_check_ins = resolve_duel_check_ins(image_time, match, time_series)
	if duel_check_ins is None:
		return set()

	lane1_check_in, lane2_check_in = duel_check_ins
	keywords = set()
	if lane1_check_in is not None and lane1_check_in.get("dog"):
		keywords.update(keywords_from_check_in(lane1_check_in))
	if lane2_check_in is not None and lane2_check_in.get("dog"):
		keywords.update(keywords_from_check_in(lane2_check_in))
	return keywords

def resolve_duel_keyword(image_time, match, time_series):
	duel_check_ins = resolve_duel_check_ins(image_time, match, time_series)
	if duel_check_ins is None:
		return None

	lane1_check_in, lane2_check_in = duel_check_ins
	if (
		lane1_check_in is None
		or not lane1_check_in.get("dog")
		or lane2_check_in is None
		or not lane2_check_in.get("dog")
	):
		return None
	return format_duel_keyword(lane1_check_in, lane2_check_in)

def _normalize_sequence_text(value):
	if not isinstance(value, str):
		return None
	text = value.strip()
	return text.lower() if text else None

def _duel_dock_key(location_path):
	parsed = parse_dock_lane_location_name(_location_label(location_path))
	if parsed is None:
		return None
	return parsed[0]

def _duel_dogs_at_time(dock_key, image_time, time_series, event_tz):
	check_ins_by_location = time_series["check_ins_by_location"]
	sheet_tz = sheet_timezone_from_time_series(time_series)
	dogs = set()
	for location_path in check_ins_by_location:
		parsed = parse_dock_lane_location_name(_location_label(location_path))
		if parsed is None or parsed[0] != dock_key:
			continue
		check_in = resolve_check_in(
			image_time,
			location_path,
			check_ins_by_location,
			event_tz,
			sheet_tz=sheet_tz,
			time_series=time_series,
		)
		dog = check_in.get("dog") if check_in else None
		if isinstance(dog, str) and dog.strip():
			dogs.add(dog.strip())
	return tuple(sorted(dogs))

def sequence_group_key(match, image_time, time_series):
	if match is None:
		return None

	discipline = _normalize_sequence_text(match.get("discipline"))
	location_path = match.get("location_path")

	if is_dueling_dogs_discipline(match.get("discipline")):
		dock_key = _duel_dock_key(location_path)
		if dock_key is None:
			return None
		dogs = _duel_dogs_at_time(
			dock_key,
			image_time,
			time_series,
			event_timezone_from_time_series(time_series),
		)
		if not dogs:
			return None
		return ("duel", dock_key, discipline, dogs)

	dog = _normalize_sequence_text(match.get("dog"))
	if location_path is None or discipline is None or dog is None:
		return None
	return ("solo", location_path, discipline, dog)

def generate_short_id():
	return uuid.uuid4().hex[:8]

def build_sequence_ids(queue_entries, time_series):
	sorted_entries = sorted(queue_entries, key=lambda item: item[1]["image_time"])
	sequence_ids = {}
	current_key = object()
	current_uuid = None

	for queue_file, _image_json in sorted_entries:
		match = resolve_photo_match(
			_image_json["image_time"],
			_image_json.get("camera_serial"),
			time_series,
			queue_file=queue_file,
		)
		key = sequence_group_key(match, _image_json["image_time"], time_series)
		if key != current_key:
			current_key = key
			current_uuid = generate_short_id() if key is not None else None
		sequence_ids[queue_file] = current_uuid

	return sequence_ids

def format_keyword_arg(keyword):
	escaped = str(keyword).replace("\\", "\\\\").replace('"', '\\"')
	return '-Keywords="{}"'.format(escaped)

def format_keyword_remove_arg(keyword):
	escaped = str(keyword).replace("\\", "\\\\").replace('"', '\\"')
	return '-Keywords-="{}"'.format(escaped)

IPTC_SCALAR_FIELDS = (
	"headline",
	"creator",
	"credit",
	"rights",
	"location",
	"city",
	"state",
	"source",
	"transmission_reference",
)

IPTC_FIELD_TAGS = {
	"headline": "Headline",
	"creator": "Creator",
	"credit": "Credit",
	"rights": "Rights",
	"location": "Location",
	"city": "City",
	"state": "State",
	"source": "Source",
	"transmission_reference": "TransmissionReference",
}

def _normalize_exif_list(value):
	if value is None:
		return []
	if isinstance(value, str):
		text = value.strip()
		return [text] if text else []
	if isinstance(value, (list, tuple, set)):
		return [
			str(item).strip()
			for item in value
			if item is not None and str(item).strip()
		]
	text = str(value).strip()
	return [text] if text else []

def read_iptc_metadata(exif_json):
	metadata = {}
	for field, tag in IPTC_FIELD_TAGS.items():
		value = first_exif_value(exif_json, tag)
		if value:
			metadata[field] = value
	subjects = _normalize_exif_list(exif_json.get("Subject"))
	if subjects:
		metadata["subjects"] = subjects
	return metadata

def existing_copyright(exif_json):
	return first_exif_value(exif_json, "Copyright", "Rights", "CopyrightNotice")

def keyword_x_value(keyword):
	if not isinstance(keyword, str) or ":" not in keyword:
		return None
	value = keyword.split(":", 1)[1].strip()
	return value or None

def image_id_from_keywords(keywords):
	for keyword in keywords:
		if isinstance(keyword, str) and keyword.startswith("X-img:"):
			return keyword_x_value(keyword)
	return None

def _iptc_text(value):
	if value is None:
		return None
	text = str(value).strip()
	if not text or text.lower() in {"none", "unspecified"}:
		return None
	return text

def _iptc_dog_name(value):
	text = _iptc_text(value)
	if text is None:
		return None
	return normalize_quoted_dog_name(text)

def build_iptc_headline(match, time_series, duel_keyword):
	event_name = _iptc_text(time_series.get("event_name"))
	if duel_keyword:
		duel_value = keyword_x_value(duel_keyword)
		discipline = _iptc_text(match.get("discipline"))
		if duel_value and discipline and event_name:
			return "{} in {} at {}".format(duel_value, discipline, event_name)
		return None
	team = _iptc_text(match.get("team"))
	org = _iptc_text(time_series.get("event_org"))
	if team and org and event_name:
		return "{} compete in {} {}".format(team, org, event_name)
	return None

def build_iptc_subjects(time_series, match):
	match = match or {}
	candidates = (
		_iptc_text(time_series.get("event_name")),
		_iptc_text(time_series.get("event_org")),
		_iptc_text(time_series.get("event_club")),
		_iptc_text(time_series.get("event_venue")),
		_iptc_text(match.get("discipline")),
		_iptc_dog_name(match.get("dog")),
		_iptc_text(match.get("handler")),
		_iptc_text(match.get("team")),
		_iptc_text(time_series.get("event_org_type")),
	)
	subjects = []
	for text in candidates:
		if text and text not in subjects:
			subjects.append(text)
	return subjects

def build_iptc_source(time_series):
	event_name = _iptc_text(time_series.get("event_name"))
	code = _iptc_text(time_series.get("event_dogsportphoto_code"))
	if event_name and code:
		return "{} ({})".format(event_name, code)
	return event_name

def build_iptc_metadata(time_series, match, image_json, duel_keyword):
	metadata = {}

	if match is not None:
		headline = build_iptc_headline(match, time_series, duel_keyword)
		if headline:
			metadata["headline"] = headline

		photographer = _iptc_text(match.get("photographer"))
		if photographer:
			metadata["creator"] = photographer
			metadata["credit"] = photographer

		if not existing_copyright(image_json) and photographer:
			image_time = image_json.get("image_time")
			year = image_time.year if image_time is not None else datetime.now().year
			metadata["rights"] = "© {} {}".format(year, photographer)

	image_id = image_id_from_keywords(image_json.get("Keywords", set()))
	if image_id:
		metadata["transmission_reference"] = image_id

	source = build_iptc_source(time_series)
	if source:
		metadata["source"] = source

	venue = _iptc_text(time_series.get("event_venue"))
	if venue:
		metadata["location"] = venue

	city = _iptc_text(time_series.get("event_city"))
	if city:
		metadata["city"] = city

	state = _iptc_text(time_series.get("event_state"))
	if state:
		metadata["state"] = state

	subjects = build_iptc_subjects(time_series, match)
	if subjects:
		metadata["subjects"] = subjects

	return metadata or None

def format_exiftool_set_arg(tag, value):
	escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
	return '-{}="{}"'.format(tag, escaped)

def format_exiftool_remove_arg(tag, value):
	escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
	return '-{}-="{}"'.format(tag, escaped)

def format_exiftool_clear_arg(tag):
	return "-{}=".format(tag)

def iptc_metadata_args(original_iptc, final_iptc, *, replace_all=False, force_headline_refresh=False):
	if not final_iptc:
		return []

	original_iptc = original_iptc or {}
	args = []
	if replace_all:
		for field, tag in IPTC_FIELD_TAGS.items():
			old_value = original_iptc.get(field)
			new_value = final_iptc.get(field)
			if field == "headline":
				if new_value:
					args.append(format_exiftool_clear_arg(tag))
					args.append(format_exiftool_set_arg(tag, new_value))
				elif old_value:
					args.append(format_exiftool_remove_arg(tag, old_value))
				continue
			if old_value:
				args.append(format_exiftool_remove_arg(tag, old_value))
			if new_value:
				args.append(format_exiftool_set_arg(tag, new_value))
		for subject in sorted(set(original_iptc.get("subjects", []))):
			args.append(format_exiftool_remove_arg("Subject", subject))
		for subject in sorted(set(final_iptc.get("subjects", []))):
			args.append(format_exiftool_set_arg("Subject+", subject))
		return args

	for field, tag in IPTC_FIELD_TAGS.items():
		new_value = final_iptc.get(field)
		old_value = original_iptc.get(field)
		if not new_value:
			continue
		if field == "headline" and force_headline_refresh:
			args.append(format_exiftool_clear_arg(tag))
			args.append(format_exiftool_set_arg(tag, new_value))
			continue
		if new_value == old_value:
			continue
		if old_value:
			args.append(format_exiftool_remove_arg(tag, old_value))
		args.append(format_exiftool_set_arg(tag, new_value))

	original_subjects = set(original_iptc.get("subjects", []))
	final_subjects = set(final_iptc.get("subjects", []))
	for subject in sorted(original_subjects - final_subjects):
		args.append(format_exiftool_remove_arg("Subject", subject))
	for subject in sorted(final_subjects - original_subjects):
		args.append(format_exiftool_set_arg("Subject+", subject))
	return args

def assign_iptc_metadata(image_json, time_series, match, duel_keyword):
	metadata = build_iptc_metadata(time_series, match, image_json, duel_keyword)
	if not metadata:
		return
	image_json["iptc_metadata"] = metadata
	image_json["log"].append("add IPTC metadata")

def strip_timestamp_prefix(filename):
	return TIMESTAMP_PREFIX_RE.sub("", filename, count=1)

def format_processed_timestamp(image_time):
	stamp = image_time.strftime("%Y%m%d%H%M%S")
	centiseconds = image_time.microsecond // 10000
	return "{}{:02d}".format(stamp, centiseconds)

def build_processed_filename(image_time, original_filename):
	timestamp = format_processed_timestamp(image_time)
	base_name = strip_timestamp_prefix(original_filename)
	return "{}-{}".format(timestamp, base_name)

OUTPUT_MODE_FLAT = "flat"
OUTPUT_MODE_SUBDIR = "subdir"
OUTPUT_MODES = (OUTPUT_MODE_FLAT, OUTPUT_MODE_SUBDIR)

VERBOSITY_QUIET = "quiet"
VERBOSITY_FULL = "full"
VERBOSITY_MODES = (VERBOSITY_QUIET, VERBOSITY_FULL)

def parse_output_mode(value):
	if value is None or value == OUTPUT_MODE_FLAT:
		return OUTPUT_MODE_FLAT
	if value == OUTPUT_MODE_SUBDIR:
		return OUTPUT_MODE_SUBDIR
	raise SystemExit(
		"Invalid --output value: {} (expected flat or subdir)".format(value)
	)

def parse_verbosity(value):
	if value is None or value == VERBOSITY_QUIET:
		return VERBOSITY_QUIET
	if value == VERBOSITY_FULL:
		return VERBOSITY_FULL
	raise SystemExit(
		"Invalid --verbosity value: {} (expected quiet or full)".format(value)
	)

def strip_match_x_keywords(keywords):
	prefixes = (
		"X-dog:",
		"X-handler:",
		"X-team:",
		"X-photog:",
		"X-photoreq:",
		"X-msg:",
		"X-dis:",
		"X-loc:",
		"X-seq:",
		"X-duel:",
	)
	stripped = set()
	for keyword in keywords:
		if not keyword:
			continue
		if keyword.startswith("X-id-"):
			continue
		if any(keyword.startswith(prefix) for prefix in prefixes):
			continue
		stripped.add(keyword)
	return stripped


def processed_output_subdirectory(keywords, output_mode, *, match=None):
	if output_mode != OUTPUT_MODE_SUBDIR:
		return None
	primary_dir = primary_staging_dir_from_match(match)
	if primary_dir is not None:
		return primary_dir
	dir_names = staging_dir_names_from_keywords(list(keywords))
	dir_name = dir_names[0] if dir_names else UNMATCHED_STAGING_DIR
	return safe_team_dir_name(dir_name) or UNMATCHED_STAGING_DIR


def primary_staging_dir_from_match(match):
	if not isinstance(match, dict):
		return None
	for field in ("team", "handler"):
		value = match.get(field)
		if not isinstance(value, str) or not value.strip():
			continue
		safe_name = safe_team_dir_name(value.strip())
		if safe_name:
			return safe_name
	return None


def resolve_processed_paths(processed_dir, backup_dir, filename, keywords, *, output_mode, safe, match=None):
	subdir = processed_output_subdirectory(keywords, output_mode, match=match)
	if subdir:
		processed_base = os.path.join(processed_dir, subdir)
		backup_base = os.path.join(backup_dir, subdir) if backup_dir else None
		os.makedirs(processed_base, exist_ok=True)
		if backup_base:
			os.makedirs(backup_base, exist_ok=True)
	else:
		processed_base = processed_dir
		backup_base = backup_dir
	if safe:
		return unique_output_names(processed_base, backup_base, filename)
	processed_file = os.path.join(processed_base, filename)
	backup_file = os.path.join(backup_base, filename) if backup_base else None
	return filename, processed_file, backup_file


def copy_duel_photo_to_additional_staging_subdirs(
	source_path,
	filename,
	processed_dir,
	backup_dir,
	keywords,
	primary_subdir,
	*,
	force=False,
	safe=False,
):
	for subdir in photo_summary_staging_dirs(keywords):
		if subdir == primary_subdir:
			continue
		processed_base = os.path.join(processed_dir, subdir)
		os.makedirs(processed_base, exist_ok=True)
		dest_path = os.path.join(processed_base, filename)
		backup_base = os.path.join(backup_dir, subdir) if backup_dir else None
		if backup_base:
			os.makedirs(backup_base, exist_ok=True)
		if safe:
			filename, dest_path, backup_path = unique_output_names(
				processed_base,
				backup_base,
				os.path.basename(dest_path),
			)
		else:
			backup_path = os.path.join(backup_base, filename) if backup_base else None
		dest_path, copied = copy_destination(
			source_path,
			dest_path,
			force=force,
			safe=False,
		)
		if copied:
			logger.info("* Duel copy to %s", dest_path)
		if backup_path:
			copy_destination(source_path, backup_path, force=force, safe=safe)


def put_exif(exif_json, filename, output_path=None, *, session=None):

	# if log is empty, we didn't do anything
	if not exif_json["log"]:
		logger.info("* No changes for %s", filename)
		return

	cmd = ["-m", "-overwrite_original"]

	original_x_keywords = exif_json.get("original_x_keywords", set())
	final_x_keywords = x_keywords(exif_json.get("Keywords", set()))
	if original_x_keywords != final_x_keywords:
		for keyword in original_x_keywords:
			cmd.append(format_keyword_remove_arg(keyword))
		for keyword in final_x_keywords:
			cmd.append(format_keyword_arg(keyword))

	cmd.extend(
		iptc_metadata_args(
			exif_json.get("original_iptc_metadata"),
			exif_json.get("iptc_metadata"),
			replace_all=exif_json.get("iptc_metadata") is not None,
		)
	)

	if "add default rating" in exif_json["log"]:
		cmd.append("-rating={}".format(exif_json["Rating"]))

	logger.info("* Running: exiftool %s %s", " ".join(cmd), filename)
	try:
		if session is not None:
			result = session.write(cmd, filename)
			if result.returncode != 0:
				raise RuntimeError(
					"exiftool failed for {}: {}".format(filename, result.stderr or result.stdout)
				)
		else:
			run_cmd(["exiftool"] + cmd + [filename])
	except SystemExit:
		raise
	except RuntimeError:
		if interrupt_requested():
			abort_if_interrupt_requested()
		raise
	if output_path:
		logger.info("* Output: %s", output_path)

def _is_photographer_entry(entry):
	return rot.entry_type(entry) == "photographer_check_in"

def _is_check_in_entry(entry):
	return rot.entry_type(entry) == "team_check_in"

def _camera_serials(entry):
	return {camera["serial"] for camera in rot.photographer_cameras(entry)}

def _dog_call_name(entry):
	dog_name = rot.dog_display_name(entry)
	if dog_name == rot.UNSPECIFIED_DOG_NAME:
		return None
	if normalize_quoted_dog_name(dog_name).lower() == "dog":
		return None
	return normalize_quoted_dog_name(dog_name)

def _org_ids_from_entry(entry):
	dog = entry.get("dog")
	if not isinstance(dog, dict):
		return None
	org_ids = dog.get("org_ids")
	if not isinstance(org_ids, dict) or not org_ids:
		return None
	normalized = {}
	for org, org_id in org_ids.items():
		org_slug = str(org).strip().lower()
		org_id_text = str(org_id).strip()
		if org_slug and org_id_text:
			normalized[org_slug] = org_id_text
	return normalized or None

def _synthesize_team_name(handler, dog):
	if not isinstance(handler, str) or not handler.strip():
		return None
	if not isinstance(dog, str) or not dog.strip():
		return None

	handler_base, _ = parse_labeled_name(handler.strip())
	dog_base, _ = parse_labeled_name(dog.strip())
	dog_base = normalize_quoted_dog_name(dog_base)
	if not handler_base or not dog_base:
		return None
	if handler_base.lower() == "unspecified" or dog_base.lower() == "unspecified":
		return None
	return "{} n {}".format(handler_base, dog_base)

def _team_from_entry(entry):
	team = rot.team_display_name(entry)
	if team:
		return team
	return _synthesize_team_name(_handler_label(entry), _dog_call_name(entry))

def _team_from_check_in(check_in):
	if not isinstance(check_in, dict):
		return None
	team = check_in.get("team")
	if isinstance(team, str) and team.strip():
		return team.strip()
	return _synthesize_team_name(check_in.get("handler"), check_in.get("dog"))

def _is_discipline_entry(entry):
	return rot.entry_type(entry) == "set_discipline"

def _discipline_name(entry):
	return rot.discipline_name(entry)

def _photo_request_label(entry):
	value = rot.entry_photo_request(entry)
	if value is True:
		return "Yes"
	if value is False:
		return "No"
	return None

def _message_to_photographer_label(entry):
	return rot.entry_message_to_photographer(entry)

def _handler_label(entry):
	return rot.handler_display_name(entry)

def _index_timeseries_entry(entry, photographer_entries, check_ins_by_location, discipline_entries_by_location, photo_requests_by_handler, messages_by_handler):
	try:
		entry_time = rot.parse_entry_at(str(entry.get("at", "")))
	except ValueError:
		logger.warning("Skipping invalid entry timestamp: %s", entry.get("at"))
		return

	location_name = str(entry.get("location") or "default")
	location_path = (location_name,)
	kind = rot.entry_type(entry)
	if kind == "photographer_check_in":
		photographer_entries.append({
			"time": entry_time,
			"location_path": location_path,
			"photographer": rot.photographer_name(entry),
			"serials": _camera_serials(entry),
		})
		return
	if kind == "set_discipline":
		discipline_entries_by_location.setdefault(location_path, []).append({
			"time": entry_time,
			"discipline": _discipline_name(entry),
		})
		return
	if kind != "team_check_in":
		return

	handler = _handler_label(entry)
	photo_request = _photo_request_label(entry)
	message_to_photographer = _message_to_photographer_label(entry)
	if handler:
		if photo_request in {"Yes", "No"}:
			photo_requests_by_handler[handler] = photo_request
		if message_to_photographer:
			messages_by_handler[handler] = message_to_photographer
	org_ids = _org_ids_from_entry(entry)
	check_in_entry = {
		"time": entry_time,
		"dog": _dog_call_name(entry),
		"handler": handler,
		"team": _team_from_entry(entry),
		"group": entry.get("group"),
		"photo_request": photo_request,
		"message_to_photographer": message_to_photographer,
	}
	if org_ids:
		check_in_entry["org_ids"] = org_ids
	check_ins_by_location.setdefault(location_path, []).append(check_in_entry)

def _is_timestamp_slot_map(node):
	if not isinstance(node, dict) or not node:
		return False
	for key, value in node.items():
		if not isinstance(key, str):
			return False
		try:
			parse_run_order_timestamp(key)
		except ValueError:
			return False
		if not isinstance(value, list):
			return False
	return True

def _collect_location_tree_paths(node, location_path=()):
	paths = set()
	if not isinstance(node, dict):
		return paths
	for key, value in node.items():
		if not isinstance(key, str) or not key.strip():
			continue
		child_path = location_path + (key.strip(),)
		if isinstance(value, list):
			if location_path:
				paths.add(location_path)
			else:
				paths.add(child_path)
			continue
		if isinstance(value, dict):
			if _is_timestamp_slot_map(value):
				if location_path:
					paths.add(location_path)
				else:
					paths.add(child_path)
				continue
			paths.add(child_path)
			paths.update(_collect_location_tree_paths(value, child_path))
	return paths

def collect_time_series_locations(time_series, *, location_tree=None):
	paths = set()
	paths.update(time_series["check_ins_by_location"])
	paths.update(time_series["discipline_entries_by_location"])
	for entry in time_series["photographer_entries"]:
		paths.add(tuple(entry["location_path"]))
	if isinstance(location_tree, dict):
		paths.update(_collect_location_tree_paths(location_tree))
	return paths

def infer_default_location_path(time_series, *, location_tree=None):
	paths = collect_time_series_locations(time_series, location_tree=location_tree)
	if len(paths) == 1:
		return next(iter(paths))
	if not paths:
		return ("default",)
	return None

def apply_inferred_location(time_series, *, location_tree=None):
	time_series["inferred_location_path"] = infer_default_location_path(
		time_series,
		location_tree=location_tree,
	)

def finalize_time_series(time_series, *, location_tree=None):
	apply_inferred_location(time_series, location_tree=location_tree)
	return time_series

def _optional_event_metadata_value(event, key):
	value = event.get(key)
	if value is None:
		return None
	if not isinstance(value, str):
		return None
	stripped = value.strip()
	if not stripped or stripped.lower() == "unspecified":
		return None
	return stripped


def load_raw_timeseries(path: str | Path) -> dict[str, Any]:
	file_path = Path(path)
	with file_path.open(encoding="utf-8") as handle:
		data = json.load(handle)
	if not isinstance(data, dict):
		raise ValueError("Expected a JSON object in {}".format(file_path))
	rot.migrate_document_to_v2(data)
	return data

def merge_event_metadata(
	primary: MutableMapping[str, Any],
	secondary: Mapping[str, Any],
) -> MutableMapping[str, Any]:
	merged = copy.deepcopy(primary)
	for key, value in secondary.items():
		if key not in merged:
			merged[key] = copy.deepcopy(value)
	return merged

def _entry_key(entry: Mapping[str, Any]) -> tuple[str, str, str]:
	return (
		str(entry.get("at") or ""),
		str(entry.get("location") or ""),
		str(entry.get("type") or rot.entry_type(entry) or ""),
	)

def merge_entries(
	primary_entries: list[dict[str, Any]],
	secondary_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
	merged: dict[tuple[str, str, str], dict[str, Any]] = {}
	for entry in primary_entries:
		if isinstance(entry, dict):
			merged[_entry_key(entry)] = copy.deepcopy(entry)
	for entry in secondary_entries:
		if not isinstance(entry, dict):
			continue
		key = _entry_key(entry)
		if key not in merged:
			merged[key] = copy.deepcopy(entry)
	return sorted(merged.values(), key=lambda item: item.get("at", ""))

def merge_timeseries(primary: Mapping[str, Any], secondary: Mapping[str, Any]) -> dict[str, Any]:
	primary_data = copy.deepcopy(primary)
	secondary_data = copy.deepcopy(secondary)
	rot.migrate_document_to_v2(primary_data)
	rot.migrate_document_to_v2(secondary_data)

	result: dict[str, Any] = {
		"schema_version": rot.SCHEMA_VERSION,
		"event": {},
		"entries": [],
	}

	primary_event = primary_data.get("event")
	secondary_event = secondary_data.get("event")
	if isinstance(primary_event, dict) and isinstance(secondary_event, dict):
		result["event"] = merge_event_metadata(primary_event, secondary_event)
	elif isinstance(primary_event, dict):
		result["event"] = copy.deepcopy(primary_event)
	elif isinstance(secondary_event, dict):
		result["event"] = copy.deepcopy(secondary_event)

	primary_entries = primary_data.get("entries")
	secondary_entries = secondary_data.get("entries")
	result["entries"] = merge_entries(
		list(primary_entries) if isinstance(primary_entries, list) else [],
		list(secondary_entries) if isinstance(secondary_entries, list) else [],
	)
	return result

def load_time_series(path, merge_path=None):
	if merge_path:
		primary = load_raw_timeseries(path)
		secondary = load_raw_timeseries(merge_path)
		data = merge_timeseries(primary, secondary)
	else:
		with open(path, encoding="utf-8") as handle:
			data = json.load(handle)

	if not isinstance(data, dict):
		raise ValueError("Time series file must contain a JSON object")

	rot.migrate_document_to_v2(data)

	event = data.get("event", {})
	if not isinstance(event, dict):
		event = {}

	photographer_entries = []
	check_ins_by_location = {}
	discipline_entries_by_location = {}
	photo_requests_by_handler = {}
	messages_by_handler = {}
	entries = data.get("entries")
	if isinstance(entries, list):
		for entry in entries:
			if isinstance(entry, dict):
				_index_timeseries_entry(
					entry,
					photographer_entries,
					check_ins_by_location,
					discipline_entries_by_location,
					photo_requests_by_handler,
					messages_by_handler,
				)

	photographer_entries.sort(key=lambda item: item["time"])
	for entries in check_ins_by_location.values():
		entries.sort(key=lambda item: item["time"])
	for entries in discipline_entries_by_location.values():
		entries.sort(key=lambda item: item["time"])

	sheet_tz = sheet_timezone_from_event(event)

	time_series = {
		"event_name": _optional_event_metadata_value(event, "name"),
		"event_org": _optional_event_metadata_value(event, "org"),
		"event_club": _optional_event_metadata_value(event, "club"),
		"event_venue": _optional_event_metadata_value(event, "venue"),
		"event_org_type": _optional_event_metadata_value(event, "org_type"),
		"event_dogsportphoto_code": _optional_event_metadata_value(event, "dogsportphoto_code"),
		"event_city": _optional_event_metadata_value(event, "city"),
		"event_state": _optional_event_metadata_value(event, "state"),
		"event_mode_checkin": _optional_event_metadata_value(event, "event_mode_checkin"),
		"event_timezone": timezone.utc,
		"sheet_timezone_name": event.get("sheet_timezone"),
		"sheet_timezone": sheet_timezone_from_event(event),
		"photographer_entries": photographer_entries,
		"check_ins_by_location": check_ins_by_location,
		"discipline_entries_by_location": discipline_entries_by_location,
		"photo_requests_by_handler": photo_requests_by_handler,
		"messages_by_handler": messages_by_handler,
		"inferred_location_path": None,
	}
	time_series["check_in_index"] = _build_check_in_index(
		check_ins_by_location,
		sheet_tz=sheet_tz,
	)
	time_series["discipline_index"] = _build_discipline_index(
		discipline_entries_by_location,
		sheet_tz=sheet_tz,
	)
	time_series["photographer_by_serial"] = _build_photographer_by_serial(
		photographer_entries,
		sheet_tz=sheet_tz,
	)
	time_series["first_team_check_in_instant"] = _first_team_check_in_from_index(
		time_series["check_in_index"]
	)
	return finalize_time_series(time_series)

def _build_check_in_index(check_ins_by_location, *, sheet_tz=None):
	index = {}
	for location_path, entries in check_ins_by_location.items():
		instants = [_check_in_instant(entry, sheet_tz) for entry in entries]
		index[location_path] = {
			"entries": entries,
			"instants": instants,
		}
	return index

def _build_discipline_index(discipline_entries_by_location, *, sheet_tz=None):
	index = {}
	for location_path, entries in discipline_entries_by_location.items():
		filtered = [entry for entry in entries if entry.get("discipline")]
		instants = [
			comparison_instant(entry["time"], naive_tz=sheet_tz)
			for entry in filtered
		]
		index[location_path] = {
			"entries": filtered,
			"instants": instants,
		}
	return index

def _build_photographer_by_serial(photographer_entries, *, sheet_tz=None):
	by_serial = {}
	for entry in photographer_entries:
		instant = comparison_instant(entry["time"], naive_tz=sheet_tz)
		entry["_lookup_instant"] = instant
		for serial in entry["serials"]:
			by_serial.setdefault(serial, []).append(entry)
	for entries in by_serial.values():
		entries.sort(key=lambda item: item["_lookup_instant"])
	return by_serial

def _first_team_check_in_from_index(check_in_index):
	first = None
	for bucket in check_in_index.values():
		for instant in bucket.get("instants") or []:
			if first is None or instant < first:
				first = instant
	return first

def _location_lookup_bucket(index, location_path):
	if index is None:
		return None
	return index.get(location_path)

def event_metadata_keywords(time_series):
	keywords = set()
	for field, key in (
		("event", "event_name"),
		("org", "event_org"),
		("club", "event_club"),
		("venue", "event_venue"),
		("type", "event_org_type"),
	):
		keyword = format_keyword(field, time_series.get(key))
		if keyword:
			keywords.add(keyword)

	city_raw = time_series.get("event_city")
	state_raw = time_series.get("event_state")
	city = city_raw.strip() if isinstance(city_raw, str) else ""
	state = state_raw.strip() if isinstance(state_raw, str) else ""
	if city and state:
		city_value = "{}, {}".format(city, state)
	elif city:
		city_value = city
	elif state:
		city_value = state
	else:
		city_value = None

	keyword = format_keyword("city", city_value)
	if keyword:
		keywords.add(keyword)
	return keywords

def _location_label(location_path):
	return location_path[-1] if location_path else ""

FREESHOOT_LOCATION_PREFIX = "Freeshoot"

def _is_freeshoot_location_path(location_path):
	label = _location_label(location_path)
	return isinstance(label, str) and label.startswith(FREESHOOT_LOCATION_PREFIX)

def _location_activity_window(entries, sheet_tz=None):
	if not entries:
		return None
	times = [_check_in_instant(entry, sheet_tz) for entry in entries]
	grace = timedelta(seconds=CHECK_IN_GRACE_SECONDS)
	return min(times) - grace, max(times) + grace

def _check_in_handler_name(check_in):
	handler = check_in.get("handler") if isinstance(check_in, dict) else None
	if not isinstance(handler, str):
		return ""
	return handler.strip()

def _is_placeholder_check_in(check_in):
	name = _check_in_handler_name(check_in).lower()
	return name in {"unspecified", "inactive"}

def _is_assignable_team_check_in(check_in):
	if not isinstance(check_in, dict):
		return False
	if _is_placeholder_check_in(check_in):
		return False
	dog = check_in.get("dog")
	return isinstance(dog, str) and bool(dog.strip())

def _primary_team_location_path(time_series):
	best_path = None
	best_count = 0
	for location_path, entries in time_series.get("check_ins_by_location", {}).items():
		if _is_freeshoot_location_path(location_path):
			continue
		assignable = sum(1 for entry in entries if _is_assignable_team_check_in(entry))
		if assignable > best_count:
			best_count = assignable
			best_path = location_path
	return best_path

def _active_dock_location_path_for_photo(image_time, time_series, *, sheet_tz=None):
	comparison_time = comparison_instant(image_time)
	grace = timedelta(seconds=CHECK_IN_GRACE_SECONDS)
	best_path = None
	best_instant = None
	for location_path, entries in time_series.get("check_ins_by_location", {}).items():
		if _is_freeshoot_location_path(location_path):
			continue
		for entry in entries:
			if not _is_assignable_team_check_in(entry):
				continue
			instant = _check_in_instant(entry, sheet_tz)
			if instant - grace <= comparison_time <= instant + grace:
				if best_instant is None or instant > best_instant:
					best_instant = instant
					best_path = location_path
	return best_path

def resolve_match_location_path(image_time, camera_serial, time_series, *, sheet_tz=None):
	photographer_entry = resolve_photographer_entry(
		image_time,
		camera_serial,
		time_series.get("photographer_entries", []),
		sheet_tz=sheet_tz,
		photographer_by_serial=time_series.get("photographer_by_serial"),
	)
	if photographer_entry is None:
		return time_series.get("inferred_location_path")

	location_path = photographer_entry["location_path"]
	if not _is_freeshoot_location_path(location_path):
		return location_path

	dock_path = _active_dock_location_path_for_photo(
		image_time,
		time_series,
		sheet_tz=sheet_tz,
	)
	if dock_path is not None:
		return dock_path

	# Photographer check-in defines freeshoot until dock team activity or a check-in elsewhere.
	return location_path

def resolve_photographer_entry(
	image_time,
	camera_serial,
	photographer_entries,
	event_tz=None,
	sheet_tz=None,
	*,
	photographer_by_serial=None,
):
	del event_tz
	if not camera_serial:
		return None

	comparison_time = comparison_instant(image_time)
	if photographer_by_serial is not None:
		entries = photographer_by_serial.get(camera_serial, [])
		if not entries:
			return None
		instants = [entry["_lookup_instant"] for entry in entries]
		idx = bisect.bisect_right(instants, comparison_time) - 1
		if idx < 0:
			return None
		max_time = instants[idx]
		start = idx
		while start > 0 and instants[start - 1] == max_time:
			start -= 1
		tied = entries[start:idx + 1]
		if len(tied) == 1:
			return tied[0]

		def lane_sort_key(entry):
			parsed = parse_dock_lane_location_name(_location_label(entry["location_path"]))
			return parsed[1] if parsed is not None else 99

		return min(tied, key=lane_sort_key)

	matches = [
		entry for entry in photographer_entries
		if comparison_instant(entry["time"], naive_tz=sheet_tz) <= comparison_time
		and camera_serial in entry["serials"]
	]
	if not matches:
		return None

	max_time = max(comparison_instant(entry["time"], naive_tz=sheet_tz) for entry in matches)
	tied = [
		entry for entry in matches
		if comparison_instant(entry["time"], naive_tz=sheet_tz) == max_time
	]
	if len(tied) == 1:
		return tied[0]

	def lane_sort_key(entry):
		parsed = parse_dock_lane_location_name(_location_label(entry["location_path"]))
		return parsed[1] if parsed is not None else 99

	return min(tied, key=lane_sort_key)

def resolve_photographer_location(image_time, camera_serial, photographer_entries, event_tz):
	entry = resolve_photographer_entry(image_time, camera_serial, photographer_entries, event_tz)
	if entry is None:
		return None
	return entry["location_path"]

def _check_in_instant(entry, sheet_tz=None):
	return comparison_instant(entry["time"], naive_tz=sheet_tz)

def first_team_check_in_instant(time_series, sheet_tz=None):
	first = None
	for entries in time_series.get("check_ins_by_location", {}).values():
		for entry in entries:
			instant = _check_in_instant(entry, sheet_tz)
			if first is None or instant < first:
				first = instant
	return first

def is_before_first_team_check_in(image_time, time_series):
	first = time_series.get("first_team_check_in_instant")
	if first is None:
		sheet_tz = sheet_timezone_from_time_series(time_series)
		first = first_team_check_in_instant(time_series, sheet_tz=sheet_tz)
	if first is None:
		return False
	return comparison_instant(image_time) < first

def event_mode_checkin_from_time_series(time_series):
	mode = time_series.get("event_mode_checkin")
	if isinstance(mode, str) and mode.strip():
		return mode.strip()
	return None

def forward_check_in_grace_seconds(time_series=None):
	if time_series and event_mode_checkin_from_time_series(time_series) == "Runlist":
		return RUNLIST_FORWARD_CHECK_IN_GRACE_SECONDS
	return FORWARD_CHECK_IN_GRACE_SECONDS

def _prefer_runlist_forward_check_in(gap_before, gap_after, *, forward_grace_seconds):
	if forward_grace_seconds <= FORWARD_CHECK_IN_GRACE_SECONDS:
		return gap_after < gap_before
	# Runlist: photo before next check-in is still the previous team's run.
	del gap_before, gap_after
	return False

def _check_in_time_gap(image_time, check_in, sheet_tz=None):
	if check_in is None:
		return None
	return abs(
		(
			comparison_instant(image_time)
			- _check_in_instant(check_in, sheet_tz)
		).total_seconds()
	)

def _photo_is_before_check_in(image_time, check_in, sheet_tz=None):
	if check_in is None:
		return False
	return comparison_instant(image_time) < _check_in_instant(check_in, sheet_tz)

def sequential_check_in_matching_enabled(time_series, *, lead):
	"""Runlist burst matching is for pre-logged batches; QR/Self use timestamp matching."""
	mode = event_mode_checkin_from_time_series(time_series)
	if mode == "Runlist":
		return True
	if mode in {"QRmode", "Self"}:
		return False
	# Legacy timeseries without mode: only when a lead check-in precedes a batch cluster.
	return lead is not None

def _split_runlist_lead_and_batch(entries, sheet_tz=None):
	if not entries:
		return None, []

	batch_start = len(entries)
	for index in range(len(entries) - 1, 0, -1):
		gap = (
			_check_in_instant(entries[index], sheet_tz)
			- _check_in_instant(entries[index - 1], sheet_tz)
		).total_seconds()
		if gap > CHECK_IN_BATCH_GAP_SECONDS:
			batch_start = index
			break
	else:
		batch_start = 0

	batch = entries[batch_start:]
	if len(batch) < 2:
		return None, entries

	if batch_start == 0:
		return None, batch

	return entries[batch_start - 1], batch

def _cluster_check_in_batches(entries, sheet_tz=None):
	if not entries:
		return []

	clusters = [[entries[0]]]
	for index in range(1, len(entries)):
		gap = (
			_check_in_instant(entries[index], sheet_tz)
			- _check_in_instant(entries[index - 1], sheet_tz)
		).total_seconds()
		if gap > CHECK_IN_BATCH_GAP_SECONDS:
			clusters.append([entries[index]])
		else:
			clusters[-1].append(entries[index])
	return clusters

def _runlist_batch_sequences(entries, sheet_tz=None):
	clusters = _cluster_check_in_batches(entries, sheet_tz=sheet_tz)
	sequences = []
	for index, cluster in enumerate(clusters):
		lead = None
		if index > 0 and len(clusters[index - 1]) == 1:
			lead = clusters[index - 1][0]
		lead_from_split, batch = _split_runlist_lead_and_batch(cluster, sheet_tz=sheet_tz)
		if lead is None:
			lead = lead_from_split
		if len(batch) < 2:
			continue
		runlist_sequence = ([lead] if lead is not None else []) + batch
		sequences.append((lead, batch, runlist_sequence))
	return sequences

def _group_photo_bursts(sorted_items):
	if not sorted_items:
		return []

	bursts = [[sorted_items[0]]]
	for item in sorted_items[1:]:
		prev_time = comparison_instant(bursts[-1][-1][1]["image_time"])
		this_time = comparison_instant(item[1]["image_time"])
		if (this_time - prev_time).total_seconds() <= PHOTO_BURST_GAP_SECONDS:
			bursts[-1].append(item)
		else:
			bursts.append([item])
	return bursts

def _runlist_batch_time_window(lead, batch, sheet_tz=None):
	grace = timedelta(seconds=CHECK_IN_GRACE_SECONDS)
	if lead is not None:
		sequence_start = _check_in_instant(lead, sheet_tz)
	else:
		sequence_start = _check_in_instant(batch[0], sheet_tz)
	sequence_end = _check_in_instant(batch[-1], sheet_tz)
	return sequence_start - grace, sequence_end + grace

def _photos_overlap_runlist_batch(bursts, lead, batch, sheet_tz=None):
	if not bursts or not batch:
		return False

	first_photo = comparison_instant(bursts[0][0][1]["image_time"])
	last_photo = comparison_instant(bursts[-1][-1][1]["image_time"])
	window_start, window_end = _runlist_batch_time_window(lead, batch, sheet_tz=sheet_tz)
	return first_photo <= window_end and last_photo >= window_start

def _burst_overlaps_runlist_batch(burst, lead, batch, sheet_tz=None):
	if not burst or not batch:
		return False

	first_photo = comparison_instant(burst[0][1]["image_time"])
	last_photo = comparison_instant(burst[-1][1]["image_time"])
	window_start, window_end = _runlist_batch_time_window(lead, batch, sheet_tz=sheet_tz)
	return first_photo <= window_end and last_photo >= window_start

def _burst_representative_instant(burst):
	times = [comparison_instant(item[1]["image_time"]) for item in burst]
	return times[len(times) // 2]

def _pick_runlist_check_in_for_burst(burst, runlist_sequence, used_indices, sheet_tz):
	burst_time = _burst_representative_instant(burst)

	def entry_instant(index):
		return _check_in_instant(runlist_sequence[index], sheet_tz)

	unused = [index for index in range(len(runlist_sequence)) if index not in used_indices]
	if not unused:
		return None

	at_or_before = [
		index for index in unused
		if entry_instant(index) <= burst_time
	]
	if at_or_before:
		return max(at_or_before, key=lambda index: (entry_instant(index), index))

	return min(unused, key=lambda index: (entry_instant(index), index))

def _assign_runlist_bursts_to_sequence(
	bursts,
	runlist_sequence,
	assignments,
	used_files,
	sheet_tz,
):
	if len(runlist_sequence) > len(bursts):
		used_indices = set()
		for burst in bursts:
			pick = _pick_runlist_check_in_for_burst(
				burst,
				runlist_sequence,
				used_indices,
				sheet_tz=sheet_tz,
			)
			if pick is None:
				break
			used_indices.add(pick)
			check_in = runlist_sequence[pick]
			for queue_file, _image_json in burst:
				if queue_file in used_files:
					continue
				assignments[queue_file] = check_in
				used_files.add(queue_file)
	else:
		for index, burst in enumerate(bursts):
			if index >= len(runlist_sequence):
				break
			check_in = runlist_sequence[index]
			for queue_file, _image_json in burst:
				if queue_file in used_files:
					continue
				assignments[queue_file] = check_in
				used_files.add(queue_file)

def build_sequential_check_in_assignments(queue_entries, time_series):
	sheet_tz = sheet_timezone_from_time_series(time_series)
	assignments = {}
	used_files = set()
	photos_by_location = {}

	for queue_file, image_json in queue_entries:
		location_path = resolve_match_location_path(
			image_json["image_time"],
			image_json.get("camera_serial"),
			time_series,
			sheet_tz=sheet_tz,
		)
		if location_path is None:
			continue
		photos_by_location.setdefault(location_path, []).append((queue_file, image_json))

	for location_path, photos in photos_by_location.items():
		entries = time_series["check_ins_by_location"].get(location_path, [])
		sorted_photos = sorted(photos, key=lambda item: item[1]["image_time"])
		bursts = _group_photo_bursts(sorted_photos)
		for lead, batch, runlist_sequence in _runlist_batch_sequences(entries, sheet_tz=sheet_tz):
			if not sequential_check_in_matching_enabled(time_series, lead=lead):
				continue

			overlapping_bursts = [
				burst for burst in bursts
				if _burst_overlaps_runlist_batch(burst, lead, batch, sheet_tz=sheet_tz)
			]
			if len(overlapping_bursts) < 2 and lead is None:
				continue
			if not overlapping_bursts:
				continue

			_assign_runlist_bursts_to_sequence(
				overlapping_bursts,
				runlist_sequence,
				assignments,
				used_files,
				sheet_tz,
			)

	time_series["sequential_check_in_by_queue_file"] = assignments
	return assignments

def _lookup_check_in_bracket(
	image_time,
	location_path,
	time_series,
	sheet_tz,
	*,
	forward_grace_seconds,
):
	bucket = _location_lookup_bucket(time_series.get("check_in_index"), location_path)
	if bucket is None:
		entries = time_series.get("check_ins_by_location", {}).get(location_path, [])
		if not entries:
			return None, None, None, None
		instants = [_check_in_instant(entry, sheet_tz) for entry in entries]
	else:
		entries = bucket["entries"]
		instants = bucket["instants"]

	comparison_time = comparison_instant(image_time)
	prior_index = bisect.bisect_right(instants, comparison_time) - 1
	prior = entries[prior_index] if prior_index >= 0 else None

	next_check_in = None
	following_check_in = None
	next_index = bisect.bisect_right(instants, comparison_time)
	if next_index < len(instants) and instants[next_index] > comparison_time:
		following_check_in = entries[next_index]
		grace_end = comparison_time + timedelta(seconds=forward_grace_seconds)
		if instants[next_index] <= grace_end:
			next_check_in = following_check_in

	chosen = None
	if prior is not None and next_check_in is not None:
		gap_before = comparison_time - instants[prior_index]
		gap_after = instants[next_index] - comparison_time
		if event_mode_checkin_from_time_series(time_series) == "Runlist":
			# Pre-logged runlist check-ins: a photo before the next team still
			# belongs to the previous team even when it is within forward grace.
			chosen = prior
		elif _prefer_runlist_forward_check_in(
			gap_before,
			gap_after,
			forward_grace_seconds=forward_grace_seconds,
		):
			chosen = next_check_in
		else:
			chosen = prior
	elif prior is not None:
		chosen = prior
	elif next_check_in is not None:
		chosen = next_check_in
	return chosen, prior, next_check_in, following_check_in

def resolve_check_in(
	image_time,
	location_path,
	check_ins_by_location,
	event_tz=None,
	sheet_tz=None,
	*,
	forward_grace_seconds=FORWARD_CHECK_IN_GRACE_SECONDS,
	time_series=None,
):
	del event_tz, check_ins_by_location
	if time_series is None:
		raise ValueError("resolve_check_in requires time_series")
	chosen, _prior, _next, _following = _lookup_check_in_bracket(
		image_time,
		location_path,
		time_series,
		sheet_tz,
		forward_grace_seconds=forward_grace_seconds,
	)
	return chosen

def resolve_discipline_entry(
	image_time,
	location_path,
	discipline_entries_by_location,
	event_tz=None,
	sheet_tz=None,
	*,
	discipline_index=None,
):
	del event_tz, discipline_entries_by_location
	bucket = _location_lookup_bucket(discipline_index, location_path)
	if bucket is None:
		return None

	entries = bucket["entries"]
	instants = bucket["instants"]
	if not entries:
		return None

	comparison_time = comparison_instant(image_time)
	grace_cutoff = comparison_time + timedelta(seconds=CHECK_IN_GRACE_SECONDS)
	grace_index = bisect.bisect_right(instants, grace_cutoff)
	if grace_index <= 0:
		return None

	entries = entries[:grace_index]
	instants = instants[:grace_index]
	prior_index = bisect.bisect_right(instants, comparison_time) - 1
	if prior_index >= 0:
		return entries[prior_index]

	next_index = bisect.bisect_right(instants, comparison_time)
	if next_index < len(entries):
		return entries[next_index]
	return None

def resolve_discipline(image_time, location_path, discipline_entries_by_location, event_tz, sheet_tz=None, *, time_series=None):
	discipline_index = None
	if time_series is not None:
		discipline_index = time_series.get("discipline_index")
	entry = resolve_discipline_entry(
		image_time,
		location_path,
		discipline_entries_by_location,
		event_tz,
		sheet_tz=sheet_tz,
		discipline_index=discipline_index,
	)
	if entry is None:
		return None
	return entry.get("discipline")

def _select_photo_check_in(
	image_time,
	location_path,
	time_series,
	*,
	queue_file=None,
	sheet_tz=None,
):
	forward_grace_seconds = forward_check_in_grace_seconds(time_series)
	sequential_check_in = None
	if queue_file:
		sequential_check_in = time_series.get("sequential_check_in_by_queue_file", {}).get(queue_file)
	timestamp_check_in, prior_check_in, next_check_in, following_check_in = _lookup_check_in_bracket(
		image_time,
		location_path,
		time_series,
		sheet_tz,
		forward_grace_seconds=forward_grace_seconds,
	)

	method = "timestamp"
	check_in = timestamp_check_in
	if (
		sequential_check_in is not None
		and timestamp_check_in is not None
		and event_mode_checkin_from_time_series(time_series) == "Runlist"
	):
		if _photo_is_before_check_in(image_time, sequential_check_in, sheet_tz=sheet_tz):
			check_in = timestamp_check_in
			method = "timestamp_over_sequential"
		else:
			sequential_gap = _check_in_time_gap(image_time, sequential_check_in, sheet_tz=sheet_tz)
			timestamp_gap = _check_in_time_gap(image_time, timestamp_check_in, sheet_tz=sheet_tz)
			if (
				sequential_gap is not None
				and timestamp_gap is not None
				and (
					sequential_gap > CHECK_IN_GRACE_SECONDS
					or sequential_gap > timestamp_gap + 10
				)
			):
				check_in = timestamp_check_in
				method = "timestamp_over_sequential"
			else:
				check_in = sequential_check_in
				method = "sequential"
	elif sequential_check_in is not None:
		check_in = sequential_check_in
		method = "sequential"

	return {
		"check_in": check_in,
		"method": method,
		"sequential_check_in": sequential_check_in,
		"timestamp_check_in": timestamp_check_in,
		"prior_check_in": prior_check_in,
		"next_check_in": next_check_in,
		"following_check_in": following_check_in,
		"forward_grace_seconds": forward_grace_seconds,
	}

def _photo_summary_check_in_ref(check_in, image_time=None, sheet_tz=None):
	if check_in is None:
		return None
	instant = _check_in_instant(check_in, sheet_tz)
	ref = {
		"check_in_timestamp": photo_summary_timestamp(instant),
		"handler": check_in.get("handler"),
		"dog": check_in.get("dog"),
	}
	if image_time is not None:
		gap_seconds = (comparison_instant(image_time) - instant).total_seconds()
		rounded = round(abs(gap_seconds), 3)
		if rounded == 0:
			ref["photo_at_same_time_as_check_in"] = True
		elif gap_seconds > 0:
			ref["photo_after_check_in_seconds"] = rounded
		else:
			ref["photo_before_check_in_seconds"] = rounded
	return ref

def _check_in_timing_label(check_in, image_time, sheet_tz=None):
	if check_in is None:
		return None
	gap_seconds = (comparison_instant(image_time) - _check_in_instant(check_in, sheet_tz)).total_seconds()
	rounded = int(round(abs(gap_seconds)))
	if rounded == 0:
		return "photo taken at check-in time"
	if gap_seconds > 0:
		return "photo taken {}s after this check-in".format(rounded)
	return "photo taken {}s before this check-in".format(rounded)

def _describe_photo_match_logic(selection, image_time, time_series, sheet_tz=None):
	check_in = selection.get("check_in")
	if check_in is None:
		prior = selection.get("prior_check_in")
		next_check_in = selection.get("next_check_in")
		if prior is None and next_check_in is None:
			return "no team check-in within match window"
		parts = []
		if prior is not None:
			parts.append(
				"previous check-in for {} ({})".format(
					_check_in_handler_name(prior) or "team",
					_check_in_timing_label(prior, image_time, sheet_tz=sheet_tz),
				)
			)
		if next_check_in is not None:
			parts.append(
				"next check-in for {} ({})".format(
					_check_in_handler_name(next_check_in) or "team",
					_check_in_timing_label(next_check_in, image_time, sheet_tz=sheet_tz),
				)
			)
		return "no team check-in matched; nearest were {}".format(", ".join(parts))

	method = selection.get("method")
	if method == "sequential":
		return "Runlist sequential burst match ({})".format(
			_check_in_timing_label(check_in, image_time, sheet_tz=sheet_tz),
		)
	if method == "timestamp_over_sequential":
		sequential_check_in = selection.get("sequential_check_in")
		return "Runlist rejected sequential match for {} ({}); used timestamp match for {} ({})".format(
			_check_in_handler_name(sequential_check_in) or "team",
			_check_in_timing_label(sequential_check_in, image_time, sheet_tz=sheet_tz),
			_check_in_handler_name(check_in) or "team",
			_check_in_timing_label(check_in, image_time, sheet_tz=sheet_tz),
		)

	prior = selection.get("prior_check_in")
	next_check_in = selection.get("next_check_in")
	mode = event_mode_checkin_from_time_series(time_series) or "timestamp"
	if prior is not None and next_check_in is not None:
		if check_in is next_check_in or check_in == next_check_in:
			return "{}: matched next check-in for {} ({}) instead of previous check-in for {} ({})".format(
				mode,
				_check_in_handler_name(next_check_in) or "team",
				_check_in_timing_label(next_check_in, image_time, sheet_tz=sheet_tz),
				_check_in_handler_name(prior) or "team",
				_check_in_timing_label(prior, image_time, sheet_tz=sheet_tz),
			)
		return "{}: matched previous check-in for {} ({}) instead of next check-in for {} ({})".format(
			mode,
			_check_in_handler_name(prior) or "team",
			_check_in_timing_label(prior, image_time, sheet_tz=sheet_tz),
			_check_in_handler_name(next_check_in) or "team",
			_check_in_timing_label(next_check_in, image_time, sheet_tz=sheet_tz),
		)
	if prior is not None and (check_in is prior or check_in == prior):
		return "{}: matched latest check-in at or before photo ({})".format(
			mode,
			_check_in_timing_label(prior, image_time, sheet_tz=sheet_tz),
		)
	if next_check_in is not None and (check_in is next_check_in or check_in == next_check_in):
		return "{}: matched earliest check-in after photo ({})".format(
			mode,
			_check_in_timing_label(next_check_in, image_time, sheet_tz=sheet_tz),
		)
	return "{} match".format(mode)

def _photo_match_explanation_from_selection(selection, image_time, time_series, sheet_tz=None):
	check_ins = {}
	matched = _photo_summary_check_in_ref(selection["check_in"], image_time, sheet_tz=sheet_tz)
	if matched is not None:
		check_ins["matched_check_in"] = matched
	summary_next_check_in = selection.get("following_check_in") or selection.get("next_check_in")
	for key, candidate in (
		("previous_check_in", selection.get("prior_check_in")),
		("next_check_in", summary_next_check_in),
		("sequential_burst_match", selection.get("sequential_check_in")),
		("timestamp_match", selection.get("timestamp_check_in")),
	):
		if candidate is None:
			continue
		if key == "timestamp_match" and candidate is selection.get("check_in"):
			continue
		ref = _photo_summary_check_in_ref(candidate, image_time, sheet_tz=sheet_tz)
		if ref is not None:
			check_ins[key] = ref

	return {
		"match_logic": _describe_photo_match_logic(selection, image_time, time_series, sheet_tz=sheet_tz),
		"check_ins": check_ins,
	}

def _build_photo_match_result(
	image_time,
	location_path,
	photographer,
	selection,
	time_series,
	event_tz,
	sheet_tz,
):
	check_in = selection["check_in"]
	dock_key = _duel_dock_key(location_path)
	if dock_key is not None:
		discipline = resolve_dock_discipline(
			image_time,
			dock_key,
			time_series,
			event_tz,
		)
	else:
		discipline = resolve_discipline(
			image_time,
			location_path,
			time_series["discipline_entries_by_location"],
			event_tz,
			sheet_tz=sheet_tz,
			time_series=time_series,
		)
	location = format_dock_location_label(location_path, discipline)
	event_name = time_series.get("event_name")
	if check_in is None:
		return {
			"location_path": location_path,
			"location": location,
			"photographer": photographer,
			"dog": None,
			"handler": None,
			"team": None,
			"photo_request": None,
			"message_to_photographer": None,
			"discipline": discipline,
			"event": event_name,
		}

	handler_name = check_in.get("handler")
	photo_request = check_in.get("photo_request")
	if not photo_request and isinstance(handler_name, str) and handler_name.strip():
		photo_request = time_series.get("photo_requests_by_handler", {}).get(handler_name.strip())
	message_to_photographer = check_in.get("message_to_photographer")
	if not message_to_photographer and isinstance(handler_name, str) and handler_name.strip():
		message_to_photographer = time_series.get("messages_by_handler", {}).get(handler_name.strip())

	return {
		"location_path": location_path,
		"location": location,
		"photographer": photographer,
		"dog": check_in.get("dog"),
		"handler": handler_name,
		"team": _team_from_check_in(check_in),
		"org_ids": check_in.get("org_ids"),
		"photo_request": photo_request,
		"message_to_photographer": message_to_photographer,
		"discipline": discipline,
		"event": event_name,
	}

def resolve_photo_match_with_explanation(image_time, camera_serial, time_series, *, queue_file=None):
	if is_before_first_team_check_in(image_time, time_series):
		return None, {
			"match_logic": "before first team check-in",
			"check_ins": {},
		}

	event_tz = event_timezone_from_time_series(time_series)
	sheet_tz = sheet_timezone_from_time_series(time_series)
	photographer_entry = resolve_photographer_entry(
		image_time,
		camera_serial,
		time_series["photographer_entries"],
		event_tz,
		sheet_tz=sheet_tz,
		photographer_by_serial=time_series.get("photographer_by_serial"),
	)
	if photographer_entry is None:
		location_path = time_series.get("inferred_location_path")
		if location_path is None:
			return None, {
				"match_logic": "no photographer location for camera",
				"check_ins": {},
			}
		photographer = None
	else:
		location_path = resolve_match_location_path(
			image_time,
			camera_serial,
			time_series,
			sheet_tz=sheet_tz,
		)
		photographer = photographer_entry.get("photographer")
		if isinstance(photographer, str) and photographer.strip():
			photographer = photographer.strip()
		else:
			photographer = None
	if location_path is None:
		return None, {
			"match_logic": "no photographer location for camera",
			"check_ins": {},
		}

	selection = _select_photo_check_in(
		image_time,
		location_path,
		time_series,
		queue_file=queue_file,
		sheet_tz=sheet_tz,
	)
	match = _build_photo_match_result(
		image_time,
		location_path,
		photographer,
		selection,
		time_series,
		event_tz,
		sheet_tz,
	)
	explanation = _photo_match_explanation_from_selection(
		selection,
		image_time,
		time_series,
		sheet_tz=sheet_tz,
	)
	return match, explanation

def explain_photo_match(image_time, camera_serial, time_series, *, queue_file=None):
	_match, explanation = resolve_photo_match_with_explanation(
		image_time,
		camera_serial,
		time_series,
		queue_file=queue_file,
	)
	return explanation

def resolve_photo_match(image_time, camera_serial, time_series, *, queue_file=None):
	match, _explanation = resolve_photo_match_with_explanation(
		image_time,
		camera_serial,
		time_series,
		queue_file=queue_file,
	)
	return match

def count_files(directory):
	if not os.path.isdir(directory):
		return 0
	return sum(
		1
		for root, _dirs, files in os.walk(directory)
		for name in files
		if os.path.isfile(os.path.join(root, name))
	)

def iter_queue_files(queue_dir):
	for root, _dirs, files in os.walk(queue_dir):
		for name in sorted(files):
			path = os.path.join(root, name)
			if os.path.isfile(path):
				yield path

def is_macos_metadata_file(filename):
	name = os.path.basename(filename)
	return name.startswith("._")

def delete_macos_metadata_file(queue_file):
	file = os.path.basename(queue_file)
	logger.info("Deleting %s (macOS metadata file)", file)
	os.remove(queue_file)

def passthrough_queue_file_reason(filename):
	del filename
	return None

def move_queue_file_unmodified(
	queue_file,
	processed_dir,
	backup_dir,
	*,
	force=False,
	safe=False,
	reason=None,
):
	file = os.path.basename(queue_file)
	if reason:
		logger.info("* %s; moving to processed without modification", reason)
	else:
		logger.info("* Moving to processed without modification")
	output_name = file
	if safe:
		output_name, processed_file, backup_file = unique_output_names(
			processed_dir,
			backup_dir,
			output_name,
		)
	else:
		processed_file = os.path.join(processed_dir, output_name)
		backup_file = os.path.join(backup_dir, output_name) if backup_dir else None
	if backup_dir and backup_file:
		backup_file, backed_up = copy_destination(
			queue_file,
			backup_file,
			force=force,
			safe=safe,
		)
		if backed_up:
			logger.info("* Backing up to %s", backup_file)
	processed_file, processed = move_destination(
		queue_file,
		processed_file,
		force=force,
		safe=safe,
	)
	if processed:
		logger.info("* Output: %s", processed_file)
	return processed_file, processed

def log_batch_progress(label, index, total):
	if total <= 0:
		return
	if index == 1 or index == total or index % max(1, total // 20) == 0:
		logger.info("%s (%d/%d)...", label, index, total)

def file_md5(path):
	digest = hashlib.md5()
	with open(path, "rb") as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()

def numbered_destination_path(directory, filename):
	candidate = os.path.join(directory, filename)
	if not os.path.exists(candidate):
		return candidate

	stem, ext = os.path.splitext(filename)
	counter = 1
	while True:
		alt_name = "{}_{}{}".format(stem, counter, ext)
		alt_path = os.path.join(directory, alt_name)
		if not os.path.exists(alt_path):
			return alt_path
		counter += 1

def should_write_destination(source_path, dest_path, *, force=False):
	if not os.path.exists(dest_path):
		return True
	if force:
		return True
	source_stat = os.stat(source_path)
	dest_stat = os.stat(dest_path)
	if source_stat.st_size != dest_stat.st_size:
		return True
	if source_stat.st_mtime != dest_stat.st_mtime:
		return True
	return False

def copy_destination(source_path, dest_path, *, force=False, safe=False):
	if safe:
		dest_path = numbered_destination_path(
			os.path.dirname(dest_path),
			os.path.basename(dest_path),
		)
	if not safe and not should_write_destination(source_path, dest_path, force=force):
		logger.info("* Destination unchanged (same MD5): %s", dest_path)
		return dest_path, False
	shutil.copy2(source_path, dest_path)
	return dest_path, True

def move_destination(source_path, dest_path, *, force=False, safe=False):
	if safe:
		dest_path = numbered_destination_path(
			os.path.dirname(dest_path),
			os.path.basename(dest_path),
		)
	if not safe and not should_write_destination(source_path, dest_path, force=force):
		logger.info("* Destination unchanged (same MD5): %s", dest_path)
		os.remove(source_path)
		return dest_path, False
	shutil.move(source_path, dest_path)
	return dest_path, True

def unique_output_names(processed_dir, backup_dir, filename):
	candidate = filename
	counter = 0
	while True:
		processed_path = os.path.join(processed_dir, candidate)
		backup_path = os.path.join(backup_dir, candidate) if backup_dir else None
		backup_exists = backup_path and os.path.exists(backup_path)
		if not os.path.exists(processed_path) and not backup_exists:
			return candidate, processed_path, backup_path
		counter += 1
		stem, ext = os.path.splitext(filename)
		candidate = "{}_{}{}".format(stem, counter, ext)

def remove_empty_queue_dirs(queue_dir):
	for root, _dirs, _files in os.walk(queue_dir, topdown=False):
		if root == queue_dir:
			continue
		try:
			if not os.listdir(root):
				os.rmdir(root)
		except OSError:
			pass

def print_status(queue_dir, processed_dir, backup_dir=None):
	logger.info("Directory status:")
	entries = (
		("queue", queue_dir),
		("processed", processed_dir),
	)
	if backup_dir:
		entries = entries + (("backup", backup_dir),)
	for label, path in entries:
		if os.path.isdir(path):
			logger.info("  %s: %s (%d files)", label, path, count_files(path))
		else:
			logger.info("  %s: %s (not found)", label, path)

def log_processing_start(file, image_json):
	logger.info("Processing: %s", file)
	logger.info(" ** Camera SN: %s", image_json.get("camera_serial"))
	logger.info(" ** Existing keywords: %s", image_json["Keywords"])
	logger.info(" ** Existing Rating: %s", image_json["Rating"])
	logger.info(" ** Image time: %s", image_json["image_time"])

def format_quiet_photo_time(image_time):
	if image_time is None:
		return ""
	if hasattr(image_time, "strftime"):
		stamp = image_time.strftime("%Y-%m-%d %H:%M:%S")
		microsecond = getattr(image_time, "microsecond", 0)
		if microsecond:
			stamp += ".{:06d}".format(microsecond)
		return stamp
	return str(image_time)

def format_quiet_photo_line(queue_file, image_json, match):
	fields = (
		format_quiet_photo_time(image_json.get("image_time")),
		os.path.basename(queue_file),
		(match or {}).get("handler") or "",
		(match or {}).get("dog") or "",
		(match or {}).get("discipline") or "",
	)
	return "\t".join(str(field) for field in fields)

def print_quiet_photo_line(queue_file, image_json, match):
	print(format_quiet_photo_line(queue_file, image_json, match), flush=True)

def print_quiet_status(message, *, verbosity=VERBOSITY_QUIET):
	if verbosity == VERBOSITY_QUIET:
		print(message, flush=True)

def log_match_details(match):
	logger.info(" ** Matched Location: %s", match.get("location") or _location_label(match["location_path"]))
	if match.get("photographer"):
		logger.info(" ** Matched Photographer: %s", match.get("photographer"))
	if match.get("dog"):
		logger.info(" ** Matched Dog: %s", match.get("dog"))
	if match.get("handler"):
		logger.info(" ** Matched Handler: %s", match.get("handler"))
	if match.get("discipline"):
		logger.info(" ** Matched Discipline: %s", match.get("discipline"))

def log_sequence_keyword(sequence_id):
	if sequence_id:
		logger.info(" ** Matched Sequence: %s", format_keyword("seq", sequence_id))

def assign_image_keyword(image_json):
	image_json["Keywords"] = {
		keyword
		for keyword in image_json["Keywords"]
		if not (isinstance(keyword, str) and keyword.startswith("X-img:"))
	}
	img_keyword = format_keyword("img", generate_short_id())
	image_json["Keywords"].add(img_keyword)
	image_json["log"].append("add image keyword")
	logger.info(" ** Image ID: %s", img_keyword)

def assign_original_filename_keyword(image_json, original_filename):
	image_json["Keywords"] = {
		keyword
		for keyword in image_json["Keywords"]
		if not (isinstance(keyword, str) and keyword.startswith("X-ofn:"))
	}
	ofn_keyword = format_keyword("ofn", strip_timestamp_prefix(original_filename))
	if ofn_keyword:
		image_json["Keywords"].add(ofn_keyword)
		image_json["log"].append("add original filename keyword")
		logger.info(" ** Original filename: %s", ofn_keyword)

PHOTO_SUMMARY_SCHEMA_VERSION = "1.6.0"


def _parse_summary_timestamp(value):
	if value is None:
		return None
	if isinstance(value, datetime):
		return value
	text = str(value).strip()
	if not text:
		return None
	try:
		return datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError:
		return None


def _round_stat(value, places=3):
	if value is None:
		return None
	return round(float(value), places)


def _numeric_summary(values, *, exclude_outliers=False):
	cleaned = [float(value) for value in values if value is not None]
	if not cleaned:
		return {"sample_count": 0}

	outliers_removed = 0
	if exclude_outliers and len(cleaned) >= 4:
		sorted_values = sorted(cleaned)
		q1_index = len(sorted_values) // 4
		q3_index = (3 * len(sorted_values)) // 4
		q1 = sorted_values[q1_index]
		q3 = sorted_values[q3_index]
		iqr = q3 - q1
		lower = q1 - 1.5 * iqr
		upper = q3 + 1.5 * iqr
		filtered = [value for value in cleaned if lower <= value <= upper]
		outliers_removed = len(cleaned) - len(filtered)
		if filtered:
			cleaned = filtered

	cleaned.sort()
	count = len(cleaned)
	if count == 1:
		median = cleaned[0]
	else:
		mid = count // 2
		median = cleaned[mid] if count % 2 else (cleaned[mid - 1] + cleaned[mid]) / 2

	summary = {
		"sample_count": count,
		"minimum": _round_stat(cleaned[0]),
		"maximum": _round_stat(cleaned[-1]),
		"average": _round_stat(sum(cleaned) / count),
		"median": _round_stat(median),
	}
	if exclude_outliers and outliers_removed:
		summary["outliers_removed"] = outliers_removed
	return summary


def _photo_run_key(photo):
	check_ins = photo.get("check_ins") or {}
	matched = check_ins.get("matched_check_in") or {}
	handler = photo.get("handler")
	if not handler:
		return None
	return (
		photo.get("location"),
		handler,
		photo.get("dog"),
		matched.get("check_in_timestamp"),
	)


def _group_photos_into_runs(photos):
	sorted_photos = sorted(photos, key=lambda item: item.get("timestamp") or "")
	runs = []
	current_run = []
	current_key = None
	for photo in sorted_photos:
		key = _photo_run_key(photo)
		if key is None:
			if current_run:
				runs.append(current_run)
				current_run = []
			current_key = None
			continue
		if key != current_key:
			if current_run:
				runs.append(current_run)
			current_run = [photo]
			current_key = key
		else:
			current_run.append(photo)
	if current_run:
		runs.append(current_run)
	return runs


def _match_logic_category(match_logic):
	if not match_logic:
		return "unknown"
	if match_logic == "before first team check-in":
		return "before_first_check_in"
	if match_logic == "Before first team check-in":
		return "before_first_check_in"
	if match_logic == "unrecognized or non-image file":
		return "passthrough"
	if "no photographer location" in match_logic:
		return "no_photographer_location"
	if match_logic.startswith("no team check-in"):
		return "no_team_check_in"
	if "Runlist sequential burst match" in match_logic:
		return "runlist_sequential"
	if "Runlist rejected sequential" in match_logic:
		return "runlist_timestamp_override"
	if "matched next check-in" in match_logic:
		return "forward_to_next_check_in"
	if "matched previous check-in" in match_logic or "matched latest check-in" in match_logic:
		return "prior_check_in"
	if "matched earliest check-in after photo" in match_logic:
		return "next_check_in"
	return "other"


def _check_in_to_first_photo_seconds(photo):
	check_ins = photo.get("check_ins") or {}
	matched = check_ins.get("matched_check_in") or {}
	if matched.get("photo_at_same_time_as_check_in"):
		return 0.0
	if "photo_after_check_in_seconds" in matched:
		return matched["photo_after_check_in_seconds"]
	if "photo_before_check_in_seconds" in matched:
		return -matched["photo_before_check_in_seconds"]
	first_time = _parse_summary_timestamp(photo.get("timestamp"))
	check_in_time = _parse_summary_timestamp(matched.get("check_in_timestamp"))
	if first_time is None or check_in_time is None:
		return None
	return (first_time - check_in_time).total_seconds()


def _last_photo_to_next_check_in_seconds(photo, *, next_run=None):
	check_ins = photo.get("check_ins") or {}
	next_check_in = check_ins.get("next_check_in") or {}
	if "photo_before_check_in_seconds" in next_check_in:
		return next_check_in["photo_before_check_in_seconds"]
	if next_run:
		next_matched = (next_run[0].get("check_ins") or {}).get("matched_check_in") or {}
		next_time = _parse_summary_timestamp(next_matched.get("check_in_timestamp"))
		last_time = _parse_summary_timestamp(photo.get("timestamp"))
		if last_time is not None and next_time is not None:
			gap = (next_time - last_time).total_seconds()
			return gap if gap >= 0 else None
	last_time = _parse_summary_timestamp(photo.get("timestamp"))
	next_time = _parse_summary_timestamp(next_check_in.get("check_in_timestamp"))
	if last_time is None or next_time is None:
		return None
	gap = (next_time - last_time).total_seconds()
	return gap if gap >= 0 else None


def build_photo_summary_statistics(photos, *, processing_seconds=None):
	photos = photos or []
	stats = {
		"photo_count": len(photos),
	}

	if processing_seconds is not None:
		processing = {
			"wall_seconds": _round_stat(processing_seconds),
		}
		if processing_seconds > 0 and photos:
			processing["photos_per_second"] = _round_stat(len(photos) / processing_seconds)
		stats["processing"] = processing

	matched_photos = [photo for photo in photos if photo.get("handler")]
	match_logic_counts = {}
	location_counts = {}
	for photo in photos:
		category = _match_logic_category(photo.get("match_logic"))
		match_logic_counts[category] = match_logic_counts.get(category, 0) + 1
		location = photo.get("location")
		if location:
			location_counts[location] = location_counts.get(location, 0) + 1

	stats["matching"] = {
		"matched_photo_count": len(matched_photos),
		"unmatched_photo_count": len(photos) - len(matched_photos),
		"match_logic_categories": dict(sorted(match_logic_counts.items())),
		"photos_by_location": dict(sorted(location_counts.items())),
	}

	runs = _group_photos_into_runs(photos)
	run_durations = []
	check_in_to_first = []
	last_photo_to_next = []
	burst_gaps = []
	photos_per_run = []
	inter_run_gaps = []
	previous_run_end = None
	previous_run_location = None

	for index, run in enumerate(runs):
		photos_per_run.append(len(run))
		times = [
			_parse_summary_timestamp(photo.get("timestamp"))
			for photo in run
		]
		times = [value for value in times if value is not None]
		if times:
			if len(times) >= 2:
				run_durations.append((times[-1] - times[0]).total_seconds())
				for gap_index in range(1, len(times)):
					burst_gaps.append((times[gap_index] - times[gap_index - 1]).total_seconds())
			else:
				run_durations.append(0.0)

			check_in_gap = _check_in_to_first_photo_seconds(run[0])
			if check_in_gap is not None:
				check_in_to_first.append(check_in_gap)

			next_run = None
			for candidate in runs[index + 1:]:
				if candidate[0].get("location") == run[0].get("location"):
					next_run = candidate
					break
			next_gap = _last_photo_to_next_check_in_seconds(
				run[-1],
				next_run=next_run,
			)
			if next_gap is not None:
				last_photo_to_next.append(next_gap)

			run_location = run[0].get("location")
			if previous_run_end is not None and run_location == previous_run_location:
				inter_run_gaps.append((times[0] - previous_run_end).total_seconds())
			previous_run_end = times[-1]
			previous_run_location = run_location

	stats["runs"] = {
		"run_count": len(runs),
		"photos_per_run": _numeric_summary(photos_per_run),
		"duration_seconds": _numeric_summary(run_durations, exclude_outliers=True),
		"check_in_to_first_photo_seconds": _numeric_summary(check_in_to_first),
		"last_photo_to_next_check_in_seconds": _numeric_summary(last_photo_to_next),
		"inter_run_gap_seconds": _numeric_summary(inter_run_gaps),
	}

	burst_summary = _numeric_summary(burst_gaps)
	burst_summary["gaps_over_burst_threshold_count"] = sum(
		1 for gap in burst_gaps if gap > PHOTO_BURST_GAP_SECONDS
	)
	burst_summary["burst_gap_threshold_seconds"] = PHOTO_BURST_GAP_SECONDS
	stats["burst_gaps_seconds"] = burst_summary

	return stats


def _attach_photo_summary_statistics(summary, *, processing_seconds=None):
	summary["statistics"] = build_photo_summary_statistics(
		summary.get("photos", []),
		processing_seconds=processing_seconds,
	)
	return summary


def sanitize_event_name_for_filename(name):
	stripped = str(name or "").strip()
	if not stripped:
		return "Event"
	with_underscores = re.sub(r"\s+", "_", stripped)
	sanitized = re.sub(r"[^A-Za-z0-9_]", "", with_underscores)
	sanitized = re.sub(r"_+", "_", sanitized).strip("_")
	return sanitized or "Event"


def photo_summary_path(timeline_path, time_series):
	path = Path(timeline_path)
	if path.name.endswith("-ts.json"):
		return path.with_name("{}-ps.json".format(path.name[:-8]))
	event_name = time_series.get("event_name") or "Event"
	event_code = time_series.get("event_dogsportphoto_code")
	safe_name = sanitize_event_name_for_filename(event_name)
	if event_code:
		filename = "{}-{}-ps.json".format(safe_name, event_code)
	else:
		filename = "{}-ps.json".format(safe_name)
	if path.parent and str(path.parent) not in {".", ""}:
		return path.parent / filename
	return Path(filename)


def unique_photo_summary_path(path):
	target = Path(path)
	if not target.exists():
		return target
	counter = 1
	stem = target.stem
	suffix = target.suffix
	while True:
		candidate = target.with_name("{}_{}{}".format(stem, counter, suffix))
		if not candidate.exists():
			return candidate
		counter += 1


def photo_summary_timestamp(image_time):
	if image_time is None:
		return None
	if hasattr(image_time, "isoformat"):
		return image_time.isoformat()
	return str(image_time)


def photo_summary_relative_processed_path(processed_file, processed_dir):
	relative = os.path.relpath(processed_file, processed_dir)
	if relative in {".", os.curdir}:
		return os.path.basename(processed_file)
	return relative.replace(os.sep, "/")


def photo_summary_staging_dirs(keywords, *, match=None):
	primary = primary_staging_dir_from_match(match)
	if primary:
		return [primary]
	dir_names = staging_dir_names_from_keywords(list(keywords))
	staging_dirs = []
	for dir_name in dir_names:
		safe_name = safe_team_dir_name(dir_name) or UNMATCHED_STAGING_DIR
		if safe_name not in staging_dirs:
			staging_dirs.append(safe_name)
	return staging_dirs


def build_photo_summary_entry(
	queue_file,
	image_json,
	match,
	match_explanation=None,
	*,
	processed_image=None,
	processed_path=None,
	staging_dirs=None,
	disposition=None,
):
	entry = {
		"image": os.path.basename(queue_file),
		"timestamp": photo_summary_timestamp(image_json.get("image_time")),
		"photographer": match.get("photographer") if match else None,
		"location": match.get("location") if match else None,
		"discipline": match.get("discipline") if match else None,
		"handler": match.get("handler") if match else None,
		"dog": match.get("dog") if match else None,
	}
	if processed_image:
		entry["processed_image"] = processed_image
	if processed_path:
		entry["processed_path"] = processed_path
	if staging_dirs:
		entry["staging_dirs"] = staging_dirs
	if disposition:
		entry["disposition"] = disposition
	if match_explanation:
		if match_explanation.get("check_ins"):
			entry["check_ins"] = match_explanation["check_ins"]
		if match_explanation.get("match_logic"):
			entry["match_logic"] = match_explanation["match_logic"]
	return entry


def build_passthrough_photo_summary_entry(queue_file, reason, processed_file, *, processed_dir):
	return {
		"image": os.path.basename(queue_file),
		"timestamp": None,
		"photographer": None,
		"location": None,
		"discipline": None,
		"handler": None,
		"dog": None,
		"match_logic": reason,
		"disposition": "passthrough",
		"processed_image": os.path.basename(processed_file),
		"processed_path": photo_summary_relative_processed_path(processed_file, processed_dir),
	}


def build_photo_summary(time_series, queue_entries):
	build_sequential_check_in_assignments(queue_entries, time_series)
	photos = []
	for queue_file, image_json in sorted(queue_entries, key=lambda item: item[1]["image_time"]):
		match, match_explanation = resolve_photo_match_with_explanation(
			image_json["image_time"],
			image_json.get("camera_serial"),
			time_series,
			queue_file=queue_file,
		)
		photos.append(build_photo_summary_entry(queue_file, image_json, match, match_explanation))
	summary = {
		"schema_version": PHOTO_SUMMARY_SCHEMA_VERSION,
		"event": time_series.get("event_name"),
		"event_code": time_series.get("event_dogsportphoto_code"),
		"photos": photos,
	}
	return _attach_photo_summary_statistics(summary)


def _assemble_photo_summary(time_series, photo_summary_entries, *, processing_seconds=None):
	summary = {
		"schema_version": PHOTO_SUMMARY_SCHEMA_VERSION,
		"event": time_series.get("event_name"),
		"event_code": time_series.get("event_dogsportphoto_code"),
		"photos": sorted(
			photo_summary_entries,
			key=lambda item: item.get("timestamp") or "",
		),
	}
	return _attach_photo_summary_statistics(
		summary,
		processing_seconds=processing_seconds,
	)


def write_photo_summary(path, summary):
	target = unique_photo_summary_path(path)
	target.parent.mkdir(parents=True, exist_ok=True)
	with open(target, "w", encoding="utf-8") as handle:
		json.dump(summary, handle, indent=2)
		handle.write("\n")
	return target


def process_queue(queue_dir, processed_dir, backup_dir, time_series, default_rating=None, *, force=False, safe=False, timeline_path=None, output_mode=OUTPUT_MODE_FLAT, verbosity=VERBOSITY_QUIET):
	install_graceful_interrupt_handler()
	processing_started = time.perf_counter()
	logger.info("Scanning queue directory %s", queue_dir)
	queue_files = list(iter_queue_files(queue_dir))
	if not queue_files:
		message = "Queue directory {} is empty; nothing to do".format(queue_dir)
		logger.info(message)
		print_quiet_status(message, verbosity=verbosity)
		return None

	logger.info("Found %d file(s) in queue", len(queue_files))
	print_quiet_status("Found {} file(s) in queue".format(len(queue_files)), verbosity=verbosity)

	inferred_location = time_series.get("inferred_location_path")
	if inferred_location is not None:
		logger.info(
			"Timeseries location scan selected %s",
			_location_label(inferred_location),
		)

	event_keywords = event_metadata_keywords(time_series)
	passthrough_files = []
	candidate_files = []
	deleted_metadata_files = 0
	for queue_file in queue_files:
		if is_macos_metadata_file(queue_file):
			delete_macos_metadata_file(queue_file)
			deleted_metadata_files += 1
			continue
		candidate_files.append(queue_file)

	if deleted_metadata_files:
		logger.info(
			"Deleted %d macOS metadata file(s) from queue",
			deleted_metadata_files,
		)
	queue_files = candidate_files
	if timeline_path:
		summary_path = photo_summary_path(timeline_path, time_series)
	else:
		summary_path = photo_summary_path(DEFAULT_TIMELINE_FILE, time_series)
	photo_summary_entries = []
	written_summary_path = None

	try:
		with ExifToolSession() as exif_session:
			raw_exif_by_file = {}
			logger.info("Examining queue files and reading EXIF metadata...")
			print_quiet_status("Reading EXIF metadata...", verbosity=verbosity)
			for batch_start in range(0, len(candidate_files), EXIF_READ_BATCH_SIZE):
				batch = candidate_files[batch_start:batch_start + EXIF_READ_BATCH_SIZE]
				if not batch:
					continue
				log_batch_progress(
					"Reading EXIF metadata",
					min(batch_start + len(batch), len(candidate_files)),
					len(candidate_files),
				)
				batch_results = exif_session.read_json_batch(batch, INSPECT_TAGS)
				for path, raw in zip(batch, batch_results):
					raw_exif_by_file[path] = raw

			queue_entries = []
			for index, queue_file in enumerate(queue_files, start=1):
				log_batch_progress("Examining queue files", index, len(queue_files))
				file = os.path.basename(queue_file)
				raw_exif = raw_exif_by_file.get(queue_file)
				image_json = normalize_exif_json(raw_exif, queue_file, session=exif_session)
				if image_json is None:
					passthrough_files.append((queue_file, "unrecognized or non-image file"))
					logger.info("Skipping EXIF for %s (unrecognized or non-image file)", file)
					abort_if_interrupt_requested(completed_item=file)
					continue
				queue_entries.append((queue_file, image_json))
				abort_if_interrupt_requested(completed_item=file)

			logger.info(
				"Queue scan complete: %d image(s) to process, %d file(s) to pass through unmodified",
				len(queue_entries),
				len(passthrough_files),
			)
			print_quiet_status(
				"Queue scan complete: {} image(s) to process, {} pass-through".format(
					len(queue_entries),
					len(passthrough_files),
				),
				verbosity=verbosity,
			)

			if passthrough_files:
				logger.info("Moving pass-through files to processed without modification...")
				print_quiet_status(
					"Moving {} pass-through file(s)...".format(len(passthrough_files)),
					verbosity=verbosity,
				)
				for index, (queue_file, reason) in enumerate(passthrough_files, start=1):
					file = os.path.basename(queue_file)
					log_batch_progress("Moving pass-through files", index, len(passthrough_files))
					logger.info("Pass-through %d/%d: %s", index, len(passthrough_files), file)
					processed_file, _moved = move_queue_file_unmodified(
						queue_file,
						processed_dir,
						backup_dir,
						force=force,
						safe=safe,
						reason=reason,
					)
					photo_summary_entries.append(
						build_passthrough_photo_summary_entry(
							queue_file,
							reason,
							processed_file,
							processed_dir=processed_dir,
						)
					)
					logger.info("")
					abort_if_interrupt_requested(completed_item=file)

			if not queue_entries:
				if passthrough_files:
					logger.info(
						"No processable images in queue; moved %d pass-through file(s) to processed",
						len(passthrough_files),
					)
				else:
					logger.info("No readable images in queue directory %s", queue_dir)
			else:
				logger.info("Building sequential check-in assignments...")
				build_sequential_check_in_assignments(queue_entries, time_series)
				logger.info("Assigning sequence IDs...")
				sequence_ids = build_sequence_ids(queue_entries, time_series)
				logger.info("Processing %d image(s)...", len(queue_entries))
				print_quiet_status(
					"Processing {} image(s)...".format(len(queue_entries)),
					verbosity=verbosity,
				)

				for index, (queue_file, image_json) in enumerate(queue_entries, start=1):
					file = os.path.basename(queue_file)
					queue_relative = os.path.relpath(queue_file, queue_dir)
					if verbosity == VERBOSITY_FULL:
						log_batch_progress("Processing images", index, len(queue_entries))
						logger.info("Processing image %d/%d: %s", index, len(queue_entries), file)
						if queue_relative != file:
							logger.info("Processing %s from queue subdirectory %s", file, os.path.dirname(queue_relative))

						log_processing_start(file, image_json)

					match, match_explanation = resolve_photo_match_with_explanation(
						image_json["image_time"],
						image_json.get("camera_serial"),
						time_series,
						queue_file=queue_file,
					)
					if verbosity == VERBOSITY_QUIET:
						print_quiet_photo_line(queue_file, image_json, match)

					if is_before_first_team_check_in(image_json["image_time"], time_series):
						processed_file, _moved = move_queue_file_unmodified(
							queue_file,
							processed_dir,
							backup_dir,
							force=force,
							safe=safe,
							reason="Before first team check-in",
						)
						photo_summary_entries.append(
							build_photo_summary_entry(
								queue_file,
								image_json,
								match,
								match_explanation,
								processed_image=os.path.basename(processed_file),
								processed_path=photo_summary_relative_processed_path(
									processed_file,
									processed_dir,
								),
								staging_dirs=photo_summary_staging_dirs(
									image_json.get("Keywords") or [],
									match=match,
								),
								disposition="before_first_check_in",
							)
						)
						if verbosity == VERBOSITY_FULL:
							logger.info("")
						abort_if_interrupt_requested(completed_item=file)
						continue

					if default_rating is not None and not image_json["Rating"]:
						image_json["Rating"] = default_rating
						image_json["log"].append("add default rating")

					if event_keywords:
						image_json["Keywords"] = preserve_non_x_keywords(image_json["Keywords"])
						image_json["Keywords"].update(event_keywords)
						image_json["log"].append("add event metadata keywords")

					duel_keyword = None
					if match is None:
						if image_json.get("camera_serial"):
							logger.warning(
								"No photographer location found for serial %s at %s",
								image_json["camera_serial"],
								image_json["image_time"],
							)
						else:
							logger.warning("No camera serial found in %s", queue_relative)
					else:
						match_keywords = set()
						for field, value in (
							("photog", match.get("photographer")),
							("dog", match.get("dog")),
							("handler", match.get("handler")),
							("team", match.get("team")),
							("photoreq", match.get("photo_request")),
							("msg", match.get("message_to_photographer")),
							("dis", match.get("discipline")),
							("event", match.get("event")),
							("loc", match.get("location")),
						):
							keyword = format_keyword(field, value)
							if keyword:
								match_keywords.add(keyword)
						match_keywords.update(keywords_from_org_ids(match.get("org_ids")))
						duel_keywords = duel_participant_keywords(
							image_json["image_time"],
							match,
							time_series,
						)
						if duel_keywords:
							match_keywords = {
								keyword
								for keyword in match_keywords
								if not keyword.startswith(("X-dog:", "X-handler:", "X-team:"))
							}
							match_keywords.update(duel_keywords)
						if match_keywords:
							image_json["Keywords"] = strip_match_x_keywords(image_json["Keywords"])
							image_json["Keywords"].update(match_keywords)
						duel_keyword = resolve_duel_keyword(
							image_json["image_time"],
							match,
							time_series,
						)
						if duel_keyword:
							image_json["Keywords"].add(duel_keyword)
							image_json["log"].append("add dueling dogs duel keyword")
							logger.info(" ** Matched Duel: %s", duel_keyword)
						sequence_id = sequence_ids.get(queue_file)
						seq_keyword = format_keyword("seq", sequence_id)
						if seq_keyword:
							if not event_keywords and not match_keywords:
								image_json["Keywords"] = preserve_non_x_keywords(image_json["Keywords"])
							image_json["Keywords"].add(seq_keyword)
							image_json["log"].append("add sequence keyword")
							log_sequence_keyword(sequence_id)
						if match.get("dog"):
							image_json["log"].append(
								"add matching dog from {} via serial {}".format(
									_location_label(match["location_path"]),
									image_json.get("camera_serial"),
								)
							)
							log_match_details(match)
						elif match.get("photographer"):
							image_json["log"].append(
								"add photographer from {} via serial {}".format(
									_location_label(match["location_path"]),
									image_json.get("camera_serial"),
								)
							)
							log_match_details(match)
						else:
							logger.warning(
								"No check-in found at location %s for %s",
								_location_label(match["location_path"]),
								image_json["image_time"],
							)

					new_name = build_processed_filename(image_json["image_time"], file)
					output_name, processed_file, backup_file = resolve_processed_paths(
						processed_dir,
						backup_dir,
						new_name,
						image_json["Keywords"],
						output_mode=output_mode,
						safe=safe,
						match=match,
					)
					logger.info("* Renaming %s", output_name)
					assign_image_keyword(image_json)
					assign_original_filename_keyword(image_json, file)
					assign_iptc_metadata(image_json, time_series, match, duel_keyword)
					put_exif(image_json, queue_file, processed_file, session=exif_session)
					if backup_dir and backup_file:
						backup_file, backed_up = copy_destination(
							queue_file,
							backup_file,
							force=force,
							safe=safe,
						)
						if backed_up:
							logger.info("* Backing up to %s", backup_file)
					processed_file, processed = move_destination(
						queue_file,
						processed_file,
						force=force,
						safe=safe,
					)
					if processed:
						logger.info("* Output: %s", processed_file)
					if output_mode == OUTPUT_MODE_SUBDIR and duel_keyword is not None:
						copy_duel_photo_to_additional_staging_subdirs(
							processed_file,
							os.path.basename(processed_file),
							processed_dir,
							backup_dir,
							image_json["Keywords"],
							primary_staging_dir_from_match(match),
							force=force,
							safe=safe,
						)
					photo_summary_entries.append(
						build_photo_summary_entry(
							queue_file,
							image_json,
							match,
							match_explanation,
							processed_image=os.path.basename(processed_file),
							processed_path=photo_summary_relative_processed_path(
								processed_file,
								processed_dir,
							),
							staging_dirs=photo_summary_staging_dirs(
								image_json["Keywords"],
								match=match,
							),
							disposition="tagged",
						)
					)
					if verbosity == VERBOSITY_FULL:
						logger.info("")
					abort_if_interrupt_requested(completed_item=file)
	finally:
		if photo_summary_entries:
			summary = _assemble_photo_summary(
				time_series,
				photo_summary_entries,
				processing_seconds=time.perf_counter() - processing_started,
			)
			written_summary_path = write_photo_summary(summary_path, summary)
			logger.info("Wrote photo summary to %s", written_summary_path)

	remove_empty_queue_dirs(queue_dir)
	return written_summary_path

def main():
	args = docopt(__doc__)
	if not args["--process"] and not args["--status"]:
		print(__doc__)
		return

	log_file = args["--log"]
	verbosity = parse_verbosity(args["--verbosity"])
	setup_logging(log_file, quiet=(verbosity == VERBOSITY_QUIET))
	if log_file:
		logger.info("Logging to %s", log_file)

	queue_dir = args["--queue"]
	processed_dir = args["--processed"]
	backup_dir = args["--backup"] or None

	if args["--process"]:
		if args["--force"] and args["--safe"]:
			raise SystemExit("Cannot use --force and --safe together")
		timeline_path = args["--timeline"] or DEFAULT_TIMELINE_FILE
		merge_path = args["--timeline2"] or None
		if verbosity == VERBOSITY_FULL:
			logger.info("Queue directory: %s", queue_dir)
			logger.info("Processed directory: %s", processed_dir)
			if backup_dir:
				logger.info("Backup directory: %s", backup_dir)
			logger.info("Loading time series from %s", timeline_path)
			if merge_path:
				logger.info("Merging secondary time series from %s", merge_path)
		else:
			print_quiet_status("Loading time series from {}".format(timeline_path), verbosity=verbosity)
			if merge_path:
				print_quiet_status(
					"Merging secondary time series from {}".format(merge_path),
					verbosity=verbosity,
				)
		time_series = load_time_series(timeline_path, merge_path=merge_path)
		output_mode = parse_output_mode(args["--output"])
		if verbosity == VERBOSITY_FULL:
			logger.info("Output layout: %s", output_mode)
			logger.info("Starting queue processing...")
		process_queue(
			queue_dir=queue_dir,
			processed_dir=processed_dir,
			backup_dir=backup_dir,
			time_series=time_series,
			default_rating=int(args["--rating"]) if args["--rating"] is not None else None,
			force=args["--force"],
			safe=args["--safe"],
			timeline_path=timeline_path,
			output_mode=output_mode,
			verbosity=verbosity,
		)
		if verbosity == VERBOSITY_FULL:
			logger.info("Queue processing complete")
		return

	print_status(queue_dir, processed_dir, backup_dir)

if __name__ == "__main__":
	try:
		main()
	except SystemExit as exc:
		if exc.code == 130:
			raise SystemExit(130) from None
		raise
