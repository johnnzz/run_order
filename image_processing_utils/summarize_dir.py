#!/usr/bin/python3
"""
Print a human-readable EXIF summary of images in a directory tree.

Inspection utility for verifying process_queue output or reviewing staged
publish/ folders. Not part of the required processing pipeline.

Usage:
  summarize_dir.py [<dir>]
  summarize_dir.py -h | --help

Arguments:
  <dir>  Root directory to scan [default: .].

Options:
  -h, --help  Show this message.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from docopt import docopt

try:
	from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9
	ZoneInfo = None

NAIVE_SOURCE_TIMEZONE_NAME = os.getenv("NAIVE_TIMESTAMP_TIMEZONE", "America/Los_Angeles")

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

DATE_TAG_OFFSET_TAG = {
	"SubSecDateTimeOriginal": "OffsetTimeOriginal",
	"DateTimeOriginal": "OffsetTimeOriginal",
	"SubSecCreateDate": "OffsetTime",
	"CreateDate": "OffsetTime",
	"FileModifyDate": "OffsetTime",
}

SUMMARY_TAGS = (
	"Copyright",
	"Rights",
	"CopyrightNotice",
	"Headline",
	"Creator",
	"Credit",
	"Source",
	"TransmissionReference",
	"Location",
	"City",
	"State",
	"Subject",
	"Model",
	"CameraModelName",
	"LensModel",
	"Lens",
	"FocalLength",
	"ShutterSpeed",
	"ExposureTime",
	"ISO",
	"FNumber",
	"Keywords",
	"Rating",
) + IMAGE_DATE_TAGS + SERIAL_NUMBER_TAGS + tuple(DATE_TAG_OFFSET_TAG.values())

QUOTED_KEYWORD_RE = re.compile(r'"([^"]*)"')

def run_cmd(cmd):
	return subprocess.run(
		cmd,
		capture_output=True,
		text=True,
	)

def exiftool_error_message(cmd_out):
	return cmd_out.stderr.strip() or "no output"

def fetch_exif(filename):
	cmd = ["exiftool", "-json"]
	cmd.extend("-{}".format(tag) for tag in SUMMARY_TAGS)
	cmd.append(filename)
	cmd_out = run_cmd(cmd)
	if cmd_out.returncode == 1:
		return None
	if cmd_out.returncode != 0 or not cmd_out.stdout.strip():
		raise RuntimeError(
			"exiftool failed for {}: {}".format(filename, exiftool_error_message(cmd_out))
		)
	payload = json.loads(cmd_out.stdout)
	if not payload:
		return None
	return payload[0]

def exif_time(exif_formated):
	d, t = exif_formated.split(" ", 1)
	d = "-".join(d.split(":"))
	match = re.match(r"(\d{2}:\d{2}:\d{2})(\.\d+)?(.*)", t)
	if match:
		time_base, frac, suffix = match.groups()
		if frac:
			frac = "." + frac[1:].ljust(6, "0")[:6]
		t = f"{time_base}{frac or ''}{suffix or ''}"
	dt = " ".join([d, t])
	return datetime.fromisoformat(dt)

def naive_source_timezone():
	if ZoneInfo is None:
		return timezone.utc
	try:
		return ZoneInfo(NAIVE_SOURCE_TIMEZONE_NAME)
	except Exception:
		return timezone.utc

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
	return None

def format_timeseries_timestamp(moment):
	if moment is None:
		return "(no timestamp)"
	if moment.tzinfo is None:
		moment = moment.replace(tzinfo=naive_source_timezone())
	base = moment.strftime("%Y-%m-%d %H:%M:%S")
	offset = moment.strftime("%z")
	if offset:
		offset = "{}:{}".format(offset[:3], offset[3:])
	return "{}{}".format(base, offset)

def first_value(exif_json, *tags):
	for tag in tags:
		value = exif_json.get(tag)
		if value is None:
			continue
		if isinstance(value, str) and not value.strip():
			continue
		return value
	return None

def normalize_serial(value):
	if value is None or isinstance(value, bool):
		return None
	if isinstance(value, (list, tuple)):
		for item in value:
			serial = normalize_serial(item)
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

def camera_serial_from_exif(exif_json):
	for tag in SERIAL_NUMBER_TAGS:
		serial = normalize_serial(exif_json.get(tag))
		if serial:
			return serial
	for tag, value in exif_json.items():
		if tag == "SourceFile" or "serial" not in tag.lower():
			continue
		serial = normalize_serial(value)
		if serial:
			return serial
	return None

def strip_keyword_quotes(keyword):
	text = str(keyword).strip()
	if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
		return text[1:-1].strip()
	return text

def expand_keywords(keywords):
	if keywords is None:
		return []

	if isinstance(keywords, str):
		candidates = [keywords]
	else:
		candidates = list(keywords)

	expanded = []
	for candidate in candidates:
		if not isinstance(candidate, str):
			continue
		text = candidate.strip()
		if not text:
			continue

		quoted = QUOTED_KEYWORD_RE.findall(text)
		if quoted:
			expanded.extend(quoted)
			continue

		expanded.append(strip_keyword_quotes(text))

	return expanded

def format_keywords(value):
	return expand_keywords(value)

KEYWORD_LINE_MAX_LENGTH = 80
FILENAME_INDENT = "    "
DETAIL_INDENT = "        "

def format_keyword_lines(keywords, max_length=KEYWORD_LINE_MAX_LENGTH):
	if not keywords:
		return []

	separator = ", "
	lines = []
	current = ""

	for keyword in keywords:
		if not current:
			candidate = keyword
		else:
			candidate = current + separator + keyword

		if len(candidate) <= max_length:
			current = candidate
			continue

		if current:
			lines.append(current)
			current = keyword
		else:
			lines.append(keyword)
			current = ""

	if current:
		lines.append(current)

	return lines

def format_aperture(value):
	if value is None:
		return ""
	text = str(value).strip()
	if not text:
		return ""
	if text.lower().startswith("f/"):
		return text
	return "f/{}".format(text)

def format_camera_line(exif_json):
	model = first_value(exif_json, "Model", "CameraModelName") or "Unknown camera"
	serial = camera_serial_from_exif(exif_json) or "?"
	lens = first_value(exif_json, "LensModel", "Lens") or ""
	camera = "{} ({})".format(model, serial)
	if lens:
		return "{} {}".format(camera, lens)
	return camera

def format_focal_length_suffix(exif_json):
	focal = first_value(exif_json, "FocalLength")
	if not focal:
		return ""
	return "@ {}".format(focal)

def format_exposure_line(exif_json):
	shutter = first_value(exif_json, "ShutterSpeed", "ExposureTime") or ""
	iso = first_value(exif_json, "ISO")
	iso_text = "ISO {}".format(iso) if iso is not None else ""
	aperture = format_aperture(first_value(exif_json, "FNumber"))
	focal = format_focal_length_suffix(exif_json)
	return " ".join(part for part in (shutter, iso_text, aperture, focal) if part)

def format_copyright(exif_json):
	return first_value(exif_json, "Copyright", "Rights", "CopyrightNotice") or ""

def normalize_subject_list(value):
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
	return []

def format_iptc_location(exif_json):
	location = first_value(exif_json, "Location")
	city = first_value(exif_json, "City")
	state = first_value(exif_json, "State")
	parts = []
	if location:
		parts.append(location)
	if city and state:
		parts.append("{}, {}".format(city, state))
	elif city:
		parts.append(city)
	elif state:
		parts.append(state)
	return ", ".join(parts)

def format_iptc_lines(exif_json):
	lines = []
	for tag, label in (
		("Headline", "Headline"),
		("Creator", "Creator"),
		("Credit", "Credit"),
		("Source", "Source"),
		("TransmissionReference", "Transmission Reference"),
	):
		value = first_value(exif_json, tag)
		if value:
			lines.append("{}: {}".format(label, value))

	location = format_iptc_location(exif_json)
	if location:
		lines.append("Location: {}".format(location))

	subjects = normalize_subject_list(exif_json.get("Subject"))
	if subjects:
		subject_lines = format_keyword_lines(subjects)
		lines.append("Subject: {}".format(subject_lines[0]))
		lines.extend(subject_lines[1:])
	return lines

def format_star_rating(exif_json):
	rating = exif_json.get("Rating")
	if rating is None:
		return None
	try:
		rating = int(round(float(rating)))
	except (TypeError, ValueError):
		return None
	rating = max(0, min(5, rating))
	return ("★" * rating) + ("☆" * (5 - rating))

def iter_files(root_dir):
	for dirpath, dirnames, filenames in os.walk(root_dir):
		dirnames.sort(key=str.lower)
		for name in sorted(filenames, key=str.lower):
			path = os.path.join(dirpath, name)
			if os.path.isfile(path):
				yield dirpath, name, path

def summarize_directory(root_dir):
	root_dir = os.path.abspath(root_dir)
	if not os.path.isdir(root_dir):
		raise FileNotFoundError("Directory not found: {}".format(root_dir))

	current_dir = None
	for dirpath, name, path in iter_files(root_dir):
		try:
			exif_json = fetch_exif(path)
		except (RuntimeError, json.JSONDecodeError):
			continue
		if exif_json is None:
			continue

		if dirpath != current_dir:
			current_dir = dirpath
			print("{}/".format(dirpath))

		timestamp = format_timeseries_timestamp(image_time_from_exif(exif_json))
		rating = format_star_rating(exif_json)
		filename_line = "{}{}   {}".format(FILENAME_INDENT, name, timestamp)
		if rating:
			filename_line = "{}   {}".format(filename_line, rating)
		print(filename_line)
		copyright = format_copyright(exif_json)
		if copyright:
			print("{}{}".format(DETAIL_INDENT, copyright))
		print("{}{}".format(DETAIL_INDENT, format_camera_line(exif_json)))
		exposure = format_exposure_line(exif_json)
		if exposure:
			print("{}{}".format(DETAIL_INDENT, exposure))
		iptc_lines = format_iptc_lines(exif_json)
		for line in iptc_lines:
			print("{}{}".format(DETAIL_INDENT, line))
		keywords = format_keywords(exif_json.get("Keywords"))
		for line in format_keyword_lines(keywords):
			print("{}{}".format(DETAIL_INDENT, line))

def main():
	if len(sys.argv) == 1:
		print(__doc__)
		return
	args = docopt(__doc__)
	root_dir = args["<dir>"] or "."
	summarize_directory(root_dir)

if __name__ == "__main__":
	main()
