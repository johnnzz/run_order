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
  --log FILE              Log file (default: process_queue-<pid>.log).
  --force                 Always overwrite existing destination files.
  --safe                  Write to _N suffix paths instead of overwriting.
  -h, --help              Show this message.
"""

from __future__ import annotations

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

try:
	from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
	ZoneInfo = None

from docopt import docopt

import _run_order_timeseries as rot

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
DEFAULT_CAMERA_SAMPLES_DIR = os.path.join(
	os.path.dirname(os.path.abspath(__file__)),
	"camera_samples",
)
TIMESTAMP_PREFIX_RE = re.compile(r"^\d{14,20}-")
DOCK_LANE_PATTERN = re.compile(r"^Dock\s+(.+?)\s+-\s+Lane\s+(\d+)$", re.IGNORECASE)
DUELING_DOGS_DISCIPLINE = "dueling dogs"
# Small grace for discipline selection when camera clock and check-in clocks differ slightly.
CHECK_IN_GRACE_SECONDS = 120
# Photos within this gap are treated as one run/burst (typically 2 frames).
PHOTO_BURST_GAP_SECONDS = 3
# Check-ins separated by more than this are a pre-batch lead vs a runlist batch cluster.
CHECK_IN_BATCH_GAP_SECONDS = 120

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

def setup_logging(log_path):
	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s %(levelname)s %(message)s",
		handlers=[
			logging.FileHandler(log_path, mode="w"),
			logging.StreamHandler(),
		],
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

def fetch_exif_tags(filename, tags):
	tag_args = ["-{}".format(tag) for tag in tags]
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

def camera_sample_basename(model, source_path):
	ext = os.path.splitext(source_path)[1].lstrip(".")
	if not ext:
		ext = "jpg"
	safe_model = model.replace(" ", "_")
	return "{}.{}".format(safe_model, ext.lower())

def maybe_save_camera_sample(source_path, model, samples_dir=None):
	if not model or not source_path:
		return False

	target_dir = samples_dir or DEFAULT_CAMERA_SAMPLES_DIR
	os.makedirs(target_dir, exist_ok=True)
	dest_path = os.path.join(target_dir, camera_sample_basename(model, source_path))
	if os.path.exists(dest_path):
		return False

	try:
		shutil.copy2(source_path, dest_path)
		logger.info("Saved camera sample %s -> %s", model, dest_path)
		return True
	except OSError as exc:
		logger.warning("Unable to save camera sample for %s: %s", model, exc)
		return False

def fetch_exif_serial_tags(filename):
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

def get_exif(filename):

	exif_json = fetch_exif_tags(filename, INSPECT_TAGS)
	if exif_json is None:
		return None

	# save the parsed time
	exif_json["image_time"] = image_time_from_exif(exif_json)

	try:
		exif_json["camera_serial"] = camera_serial_from_exif(exif_json)
	except KeyError:
		try:
			exif_json["camera_serial"] = camera_serial_from_exif(fetch_exif_serial_tags(filename))
		except KeyError:
			exif_json["camera_serial"] = None
 
 	# ensure Keywords is a set
	if "Keywords" in exif_json:
		image_keywords = exif_json["Keywords"]
		if isinstance(image_keywords, str):
			image_keywords = [ image_keywords ]
		else:
			image_keywords = image_keywords
	else:
		# if there are no keywords, stub it out
		image_keywords = list()
	image_keywords = set(image_keywords)
	image_keywords.discard(None)
	exif_json["original_x_keywords"] = x_keywords(image_keywords)
	exif_json["Keywords"] = image_keywords
	exif_json["original_iptc_metadata"] = read_iptc_metadata(exif_json)

	# make sure Rating exists
	if "Rating" in exif_json:
		image_rating = exif_json["Rating"]
	else:
		image_rating = None
	exif_json["Rating"] = image_rating

	exif_json["log"] = []

	return exif_json

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
	if not text or text.lower() in {"none", "unspecified"}:
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
		resolve_check_in(image_time, lane1_path, check_ins_by_location, event_tz, sheet_tz=sheet_tz)
		if lane1_path is not None
		else None
	)
	lane2_check_in = (
		resolve_check_in(image_time, lane2_path, check_ins_by_location, event_tz, sheet_tz=sheet_tz)
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

def _duel_dogs_at_time(dock_key, image_time, check_ins_by_location, event_tz):
	dogs = set()
	for location_path in check_ins_by_location:
		parsed = parse_dock_lane_location_name(_location_label(location_path))
		if parsed is None or parsed[0] != dock_key:
			continue
		check_in = resolve_check_in(image_time, location_path, check_ins_by_location, event_tz)
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
			time_series["check_ins_by_location"],
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
	if match is None:
		return None

	metadata = {}
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

def iptc_metadata_args(original_iptc, final_iptc):
	if not final_iptc:
		return []

	original_iptc = original_iptc or {}
	args = []
	for field, tag in IPTC_FIELD_TAGS.items():
		new_value = final_iptc.get(field)
		old_value = original_iptc.get(field)
		if not new_value or new_value == old_value:
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

def put_exif(exif_json, filename, output_path=None):

	# if log is empty, we didn't do anything
	if not exif_json["log"]:
		logger.info("* No changes for %s", filename)
		return

	cmd = ["exiftool", "-m", "-overwrite_original"]

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
		)
	)

	if "add default rating" in exif_json["log"]:
		cmd.append("-rating={}".format(exif_json["Rating"]))
	cmd.append(filename)

	logger.info("* Running: %s", " ".join(cmd))
	run_cmd(cmd)
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
	return finalize_time_series(time_series)

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

def resolve_photographer_entry(image_time, camera_serial, photographer_entries, event_tz=None, sheet_tz=None):
	del event_tz
	if not camera_serial:
		return None

	comparison_time = comparison_instant(image_time)
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

def event_mode_checkin_from_time_series(time_series):
	mode = time_series.get("event_mode_checkin")
	if isinstance(mode, str) and mode.strip():
		return mode.strip()
	return None

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

def _photos_overlap_runlist_batch(bursts, lead, batch, sheet_tz=None):
	if not bursts or not batch:
		return False

	first_photo = comparison_instant(bursts[0][0][1]["image_time"])
	last_photo = comparison_instant(bursts[-1][-1][1]["image_time"])
	grace = timedelta(seconds=CHECK_IN_GRACE_SECONDS)
	if lead is not None:
		sequence_start = _check_in_instant(lead, sheet_tz)
	else:
		sequence_start = _check_in_instant(batch[0], sheet_tz)
	sequence_end = _check_in_instant(batch[-1], sheet_tz)
	window_start = sequence_start - grace
	window_end = sequence_end + grace
	return first_photo <= window_end and last_photo >= window_start

def build_sequential_check_in_assignments(queue_entries, time_series):
	sheet_tz = sheet_timezone_from_time_series(time_series)
	event_tz = event_timezone_from_time_series(time_series)
	assignments = {}
	photos_by_location = {}

	for queue_file, image_json in queue_entries:
		photographer_entry = resolve_photographer_entry(
			image_json["image_time"],
			image_json.get("camera_serial"),
			time_series["photographer_entries"],
			event_tz,
			sheet_tz=sheet_tz,
		)
		if photographer_entry is None:
			location_path = time_series.get("inferred_location_path")
		else:
			location_path = photographer_entry["location_path"]
		if location_path is None:
			continue
		photos_by_location.setdefault(location_path, []).append((queue_file, image_json))

	for location_path, photos in photos_by_location.items():
		entries = time_series["check_ins_by_location"].get(location_path, [])
		lead, batch = _split_runlist_lead_and_batch(entries, sheet_tz=sheet_tz)
		if len(batch) < 2:
			continue
		if not sequential_check_in_matching_enabled(time_series, lead=lead):
			continue

		sorted_photos = sorted(photos, key=lambda item: item[1]["image_time"])
		bursts = _group_photo_bursts(sorted_photos)
		if len(bursts) < 2 and lead is None:
			continue
		if not _photos_overlap_runlist_batch(bursts, lead, batch, sheet_tz=sheet_tz):
			continue

		runlist_sequence = ([lead] if lead is not None else []) + batch
		for index, burst in enumerate(bursts):
			if index >= len(runlist_sequence):
				break
			check_in = runlist_sequence[index]
			for queue_file, _image_json in burst:
				assignments[queue_file] = check_in

	time_series["sequential_check_in_by_queue_file"] = assignments
	return assignments

def resolve_check_in(image_time, location_path, check_ins_by_location, event_tz=None, sheet_tz=None):
	del event_tz
	entries = check_ins_by_location.get(location_path, [])
	if not entries:
		return None

	def entry_time(entry):
		return _check_in_instant(entry, sheet_tz)

	comparison_time = comparison_instant(image_time)
	# Use the latest check-in at or before the photo. A photo before Laurel's 22:22
	# check-in still matches the previous handler at that location.
	at_or_before = [
		entry for entry in entries
		if entry_time(entry) <= comparison_time
	]
	if not at_or_before:
		return None
	return max(at_or_before, key=entry_time)

def resolve_discipline_entry(image_time, location_path, discipline_entries_by_location, event_tz=None, sheet_tz=None):
	del event_tz
	entries = discipline_entries_by_location.get(location_path, [])
	comparison_time = comparison_instant(image_time)
	grace_cutoff = comparison_time + timedelta(seconds=CHECK_IN_GRACE_SECONDS)
	matches = [
		entry for entry in entries
		if entry.get("discipline") and comparison_instant(entry["time"], naive_tz=sheet_tz) <= grace_cutoff
	]
	if not matches:
		return None

	at_or_before = [
		entry for entry in matches
		if comparison_instant(entry["time"], naive_tz=sheet_tz) <= comparison_time
	]
	if at_or_before:
		return max(at_or_before, key=lambda item: comparison_instant(item["time"], naive_tz=sheet_tz))

	after_photo = [
		entry for entry in matches
		if comparison_instant(entry["time"], naive_tz=sheet_tz) > comparison_time
	]
	return min(after_photo, key=lambda item: comparison_instant(item["time"], naive_tz=sheet_tz))

def resolve_discipline(image_time, location_path, discipline_entries_by_location, event_tz, sheet_tz=None):
	entry = resolve_discipline_entry(
		image_time,
		location_path,
		discipline_entries_by_location,
		event_tz,
		sheet_tz=sheet_tz,
	)
	if entry is None:
		return None
	return entry.get("discipline")

def resolve_photo_match(image_time, camera_serial, time_series, *, queue_file=None):
	event_tz = event_timezone_from_time_series(time_series)
	sheet_tz = sheet_timezone_from_time_series(time_series)
	photographer_entry = resolve_photographer_entry(
		image_time,
		camera_serial,
		time_series["photographer_entries"],
		event_tz,
		sheet_tz=sheet_tz,
	)
	if photographer_entry is None:
		location_path = time_series.get("inferred_location_path")
		if location_path is None:
			return None
		photographer = None
	else:
		location_path = photographer_entry["location_path"]
		photographer = photographer_entry.get("photographer")
		if isinstance(photographer, str) and photographer.strip():
			photographer = photographer.strip()
		else:
			photographer = None

	check_in = None
	if queue_file:
		check_in = time_series.get("sequential_check_in_by_queue_file", {}).get(queue_file)
	if check_in is None:
		check_in = resolve_check_in(
			image_time,
			location_path,
			time_series["check_ins_by_location"],
			sheet_tz=sheet_tz,
		)
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
	return file_md5(source_path) != file_md5(dest_path)

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

def process_queue(queue_dir, processed_dir, backup_dir, time_series, default_rating=None, *, force=False, safe=False):
	queue_files = list(iter_queue_files(queue_dir))
	if not queue_files:
		logger.info("Queue directory %s is empty; nothing to do", queue_dir)
		return

	inferred_location = time_series.get("inferred_location_path")
	if inferred_location is not None:
		logger.info(
			"Timeseries location scan selected %s",
			_location_label(inferred_location),
		)

	event_keywords = event_metadata_keywords(time_series)
	queue_entries = []
	for queue_file in queue_files:
		image_json = get_exif(queue_file)
		if image_json is None:
			continue
		model = camera_model_from_exif(image_json)
		if model:
			maybe_save_camera_sample(queue_file, model)
		queue_entries.append((queue_file, image_json))

	if not queue_entries:
		logger.info("No readable images in queue directory %s", queue_dir)
		return

	build_sequential_check_in_assignments(queue_entries, time_series)
	sequence_ids = build_sequence_ids(queue_entries, time_series)

	for queue_file, image_json in queue_entries:
		file = os.path.basename(queue_file)
		queue_relative = os.path.relpath(queue_file, queue_dir)
		if queue_relative != file:
			logger.info("Processing %s from queue subdirectory %s", file, os.path.dirname(queue_relative))

		log_processing_start(file, image_json)

		if default_rating is not None and not image_json["Rating"]:
			image_json["Rating"] = default_rating
			image_json["log"].append("add default rating")

		if event_keywords:
			image_json["Keywords"] = preserve_non_x_keywords(image_json["Keywords"])
			image_json["Keywords"].update(event_keywords)
			image_json["log"].append("add event metadata keywords")

		match = resolve_photo_match(
			image_json["image_time"],
			image_json.get("camera_serial"),
			time_series,
			queue_file=queue_file,
		)
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
				if not event_keywords:
					image_json["Keywords"] = preserve_non_x_keywords(image_json["Keywords"])
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
		if safe:
			output_name, processed_file, backup_file = unique_output_names(
				processed_dir,
				backup_dir,
				new_name,
			)
		else:
			output_name = new_name
			processed_file = os.path.join(processed_dir, output_name)
			backup_file = os.path.join(backup_dir, output_name) if backup_dir else None
		logger.info("* Renaming %s", output_name)
		if backup_dir and backup_file:
			backup_file, backed_up = copy_destination(
				queue_file,
				backup_file,
				force=force,
				safe=safe,
			)
			if backed_up:
				logger.info("* Backing up to %s", backup_file)
		assign_image_keyword(image_json)
		assign_original_filename_keyword(image_json, file)
		if match is not None:
			assign_iptc_metadata(image_json, time_series, match, duel_keyword)
		put_exif(image_json, queue_file, processed_file)
		processed_file, processed = move_destination(
			queue_file,
			processed_file,
			force=force,
			safe=safe,
		)
		if processed:
			logger.info("* Output: %s", processed_file)
		logger.info("")

	remove_empty_queue_dirs(queue_dir)

def main():
	args = docopt(__doc__)
	if not args["--process"] and not args["--status"]:
		print(__doc__)
		return

	log_file = args["--log"] or "process_queue-{}.log".format(os.getpid())
	setup_logging(log_file)
	logger.info("Logging to %s", log_file)

	queue_dir = args["--queue"]
	processed_dir = args["--processed"]
	backup_dir = args["--backup"] or None

	if args["--process"]:
		if args["--force"] and args["--safe"]:
			raise SystemExit("Cannot use --force and --safe together")
		timeline_path = args["--timeline"] or DEFAULT_TIMELINE_FILE
		merge_path = args["--timeline2"] or None
		process_queue(
			queue_dir=queue_dir,
			processed_dir=processed_dir,
			backup_dir=backup_dir,
			time_series=load_time_series(timeline_path, merge_path=merge_path),
			default_rating=int(args["--rating"]) if args["--rating"] is not None else None,
			force=args["--force"],
			safe=args["--safe"],
		)
		return

	print_status(queue_dir, processed_dir, backup_dir)

if __name__ == "__main__":
	main()
