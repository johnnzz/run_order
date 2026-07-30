#!/usr/bin/python3
# Copyright (c) 2026 John Navitsky
#
# SPDX-License-Identifier: MIT
# See LICENSE for the full license text.
"""
Stage tagged photos into team folders for publish and gallery delivery.

Script to stage files for bulk upload into tools like Pixieset.

The script utilizes the keyword EXIF data present in the images and is optimized
to make per team directories, but if only handler information is present it 
will create per handler directories.

The script also produces clients.csv file with handler emails looked up 
from the time-series file.  This file can be used as a mail-merge and if
everything is done correctly and luck is with you, can contain a direct 
link to the photos for each team.

Usage:
  stage_into_dirs.py [options]

Options:
  --processed DIR   Processed photos directory [default: ./processed].
  --publish DIR     Publish directory [default: ./publish].
  --timeline FILE   Timeseries JSON file [default: ./eventname-ts.json].
  --download-prefix URL  Pixieset download URL prefix for clients.csv [default: ].
  --level NUM       Staging depth: 1=team folders only, 2=photographer/team [default: 1].
  --force           Always overwrite existing destination files.
  --safe            Write to _N suffix paths instead of overwriting.
  -h, --help        Show this message.
"""

import csv
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys

from docopt import docopt

import _run_order_timeseries as rot
from _graceful_interrupt import abort_if_interrupt_requested, install_graceful_interrupt_handler
from x_keywords import parse_x_keyword

logger = logging.getLogger(__name__)

_QUOTED_DOG_NAME_SEGMENT_RE = re.compile(r'\s*"([^"]*)"\s*')


def normalize_quoted_dog_name(value: str) -> str:
	text = str(value).strip()
	if not text or '"' not in text:
		return text
	return _QUOTED_DOG_NAME_SEGMENT_RE.sub(r" - \1", text).strip()

DEFAULT_PROCESSED_DIR = "./processed"
DEFAULT_PUBLISH_DIR = "./publish"
DEFAULT_TIMELINE_FILE = "eventname-ts.json"
UNKNOWN_PHOTOGRAPHER_DIR = "Unknown"
STAGING_LEVEL_DEFAULT = 1
UNMATCHED_STAGING_DIR = "Unmatched"
QUOTED_KEYWORD_RE = re.compile(r'"([^"]*)"')

def setup_logging():
	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s %(levelname)s %(message)s",
		handlers=[logging.StreamHandler()],
		force=True,
	)

def run_cmd(cmd):
	result = subprocess.run(
		cmd,
		capture_output=True,
		text=True,
	)
	return result

def exiftool_error_message(cmd_out):
	return cmd_out.stderr.strip() or "no output"

def fetch_keywords(filename, *, include_creator=False):
	tags = ["-Keywords"]
	if include_creator:
		tags.append("-Creator")
	cmd = ["exiftool", "-json"] + tags + [filename]
	cmd_out = run_cmd(cmd)
	if cmd_out.returncode == 1:
		logger.warning(
			"Skipping %s: %s",
			filename,
			exiftool_error_message(cmd_out),
		)
		return None
	if cmd_out.returncode != 0 or not cmd_out.stdout.strip():
		raise RuntimeError(
			"exiftool failed for {}: {}".format(filename, exiftool_error_message(cmd_out))
		)
	exif_json = json.loads(cmd_out.stdout)[0]
	if not include_creator:
		return exif_json.get("Keywords")
	return {
		"keywords": exif_json.get("Keywords"),
		"creator": exif_json.get("Creator"),
	}

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

def x_keyword_values(keywords, field_name):
	values = []
	field_name = field_name.strip().lower()
	for keyword in expand_keywords(keywords):
		keyword = strip_keyword_quotes(keyword)
		field, value = parse_x_keyword(keyword)
		if field == field_name and value:
			values.append(value)
	return values

def x_keyword_map(keywords):
	fields = {}
	for keyword in expand_keywords(keywords):
		keyword = strip_keyword_quotes(keyword)
		field, value = parse_x_keyword(keyword)
		if field and value:
			fields[field] = value
	return fields

def teams_from_keywords(keywords):
	seen = set()
	teams = []
	for team in x_keyword_values(keywords, "team"):
		if team not in seen:
			seen.add(team)
			teams.append(team)
	return teams

def handlers_from_keywords(keywords):
	seen = set()
	handlers = []
	for handler in x_keyword_values(keywords, "handler"):
		if handler not in seen:
			seen.add(handler)
			handlers.append(handler)
	return handlers

def dogs_from_keywords(keywords):
	seen = set()
	dogs = []
	for dog in x_keyword_values(keywords, "dog"):
		if dog not in seen:
			seen.add(dog)
			dogs.append(dog)
	return dogs

def _iptc_text(value):
	if isinstance(value, str):
		return value.strip()
	if isinstance(value, list):
		for item in value:
			if isinstance(item, str) and item.strip():
				return item.strip()
	return ""

def photographer_name_from_exif(keywords, creator=None):
	photographers = x_keyword_values(keywords, "photog")
	if photographers:
		return photographers[0].strip()
	creator_text = _iptc_text(creator)
	if creator_text:
		return creator_text
	return None

def safe_photographer_dir_name(photographer_name):
	safe_name = safe_team_dir_name(photographer_name)
	if safe_name:
		return safe_name
	return UNKNOWN_PHOTOGRAPHER_DIR

def publish_team_dir(publish_dir, team_dir_name, *, level=1, photographer_dir_name=None):
	if level >= 2:
		photographer = photographer_dir_name or UNKNOWN_PHOTOGRAPHER_DIR
		return os.path.join(publish_dir, photographer, team_dir_name)
	return os.path.join(publish_dir, team_dir_name)

def parse_staging_level(value):
	if value is None:
		return STAGING_LEVEL_DEFAULT
	try:
		level = int(str(value).strip())
	except ValueError:
		raise SystemExit("Invalid --level value: {} (expected 1 or 2)".format(value))
	if level not in (1, 2):
		raise SystemExit("Invalid --level value: {} (expected 1 or 2)".format(value))
	return level

def staging_dir_names_from_keywords(keywords):
	teams = teams_from_keywords(keywords)
	if teams:
		return teams
	handlers = handlers_from_keywords(keywords)
	if handlers:
		return handlers
	return [UNMATCHED_STAGING_DIR]

def team_from_keywords(keywords):
	teams = teams_from_keywords(keywords)
	return teams[0] if teams else None

def parse_labeled_name(value):
	trimmed = value.strip()
	if trimmed.endswith(")"):
		open_paren = trimmed.rfind(" (")
		if open_paren != -1:
			suffix = trimmed[open_paren + 2 : -1]
			if len(suffix) == 5 and suffix.isdigit():
				return trimmed[:open_paren].strip(), suffix
	return trimmed, None

def team_handler_name(team_name):
	parts = team_name.split(" n ", 1)
	return parts[0].strip() if parts else team_name.strip()

def handler_for_team(team_name, handler_values):
	expected = team_handler_name(team_name)
	for handler in handler_values:
		base, _ = parse_labeled_name(handler)
		if base == expected:
			return handler
	return expected

def load_handler_emails_from_timeline(timeline_path):
	if not os.path.isfile(timeline_path):
		logger.warning("Time series file not found: %s", timeline_path)
		return {}

	with open(timeline_path, encoding="utf-8") as handle:
		data = json.load(handle)

	if not isinstance(data, dict):
		logger.warning("Time series file %s is not a JSON object", timeline_path)
		return {}

	rot.migrate_document_to_v2(data)
	handler_emails = {}
	entries = data.get("entries")
	if not isinstance(entries, list):
		logger.warning("Time series file %s has no entries array", timeline_path)
		return handler_emails

	for entry in entries:
		if not isinstance(entry, dict) or rot.entry_type(entry) != "team_check_in":
			continue
		handler = rot.handler_display_name(entry)
		handler_email = rot.handler_email(entry) or ""
		if handler and handler_email:
			handler_emails[handler] = handler_email
	return handler_emails

def lookup_handler_email(handler_emails, handler_name, keyword_email=""):
	handler_name = handler_name.strip()
	if handler_name and handler_name in handler_emails:
		return handler_emails[handler_name]
	if isinstance(keyword_email, str):
		return keyword_email.strip()
	return ""

def client_from_keywords(keywords, handler_emails):
	clients = clients_from_keywords(keywords, handler_emails)
	if not clients:
		return None
	return next(iter(clients.values()))

def clients_from_keywords(keywords, handler_emails):
	teams = teams_from_keywords(keywords)
	handler_values = x_keyword_values(keywords, "handler")
	email_values = x_keyword_values(keywords, "email")
	keyword_email = email_values[0] if len(email_values) == 1 else ""

	clients = {}
	if teams:
		for team_name in teams:
			team_dir_name = safe_team_dir_name(team_name)
			if team_dir_name is None:
				continue
			handler_name = handler_for_team(team_name, handler_values)
			clients[team_dir_name] = {
				"handler_name": handler_name,
				"handler_email": lookup_handler_email(
					handler_emails,
					handler_name,
					keyword_email,
				),
				"team_name": team_name,
			}
		return clients

	if dogs_from_keywords(keywords):
		return {}

	for handler_name in handlers_from_keywords(keywords):
		dir_name = safe_team_dir_name(handler_name)
		if dir_name is None:
			continue
		clients[dir_name] = {
			"handler_name": handler_name,
			"handler_email": lookup_handler_email(
				handler_emails,
				handler_name,
				keyword_email,
			),
			"team_name": handler_name,
		}
	return clients

def safe_team_dir_name(team):
	team = team.strip()
	if not team:
		return None
	return team.replace("/", "-").replace("\0", "")

def strip_trailing_parenthetical_code(value):
	trimmed = value.strip()
	if trimmed.endswith(")"):
		open_paren = trimmed.rfind(" (")
		if open_paren != -1:
			suffix = trimmed[open_paren + 2 : -1]
			if suffix.isdigit() and len(suffix) in (4, 5):
				return trimmed[:open_paren].strip()
	return trimmed

def team_name_for_download_url(team_name):
	team_name = team_name.strip()
	if not team_name:
		return ""

	without_team_code = strip_trailing_parenthetical_code(team_name)
	parts = without_team_code.split(" n ", 1)
	if len(parts) == 2:
		handler = strip_trailing_parenthetical_code(parts[0].strip())
		dog = normalize_quoted_dog_name(strip_trailing_parenthetical_code(parts[1].strip()))
		return "{} n {}".format(handler, dog)

	return strip_trailing_parenthetical_code(without_team_code)

def team_download_slug(team_name):
	clean_name = team_name_for_download_url(team_name)
	if not clean_name:
		return ""
	return clean_name.lower().replace(" ", "")

def build_download_url(download_prefix, team_name):
	prefix = (download_prefix or "").strip()
	if not prefix:
		return ""
	slug = team_download_slug(team_name)
	if not slug:
		return ""
	if not prefix.endswith("/"):
		prefix += "/"
	return prefix + slug + "/"

def file_md5(path):
	digest = hashlib.md5()
	with open(path, "rb") as handle:
		for chunk in iter(lambda: handle.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()

def should_write_destination(source_path, dest_path, *, force=False):
	if not os.path.exists(dest_path):
		return True
	if force:
		return True
	return file_md5(source_path) != file_md5(dest_path)

def copy_destination(source_path, dest_path, *, force=False, safe=False):
	if safe:
		dest_path = unique_destination_path(
			os.path.dirname(dest_path),
			os.path.basename(dest_path),
		)
	if not safe and not should_write_destination(source_path, dest_path, force=force):
		logger.info("Destination unchanged (same MD5): %s", dest_path)
		return dest_path, False
	shutil.copy2(source_path, dest_path)
	return dest_path, True

def list_files(directory):
	return sorted(
		name for name in os.listdir(directory)
		if not name.startswith(".") and os.path.isfile(os.path.join(directory, name))
	)

def iter_staging_source_files(processed_dir):
	for root, _dirs, files in os.walk(processed_dir):
		for name in sorted(files):
			if name.startswith("."):
				continue
			yield os.path.join(root, name)

def clear_processed_directory(processed_dir):
	if not os.path.isdir(processed_dir):
		return

	removed_count = 0
	for name in os.listdir(processed_dir):
		if name.startswith("."):
			continue
		path = os.path.join(processed_dir, name)
		if os.path.isfile(path) or os.path.islink(path):
			os.remove(path)
			removed_count += 1
		elif os.path.isdir(path):
			shutil.rmtree(path)
			removed_count += 1

	if removed_count:
		logger.info("Cleared %d item(s) from processed directory %s", removed_count, processed_dir)

def unique_destination_path(directory, filename):
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

def load_existing_clients(publish_dir):
	csv_path = os.path.join(publish_dir, "clients.csv")
	if not os.path.isfile(csv_path):
		return {}

	existing = {}
	with open(csv_path, encoding="utf-8", newline="") as handle:
		reader = csv.DictReader(handle)
		for row in reader:
			team_name = (row.get("team_name") or "").strip()
			team_dir_name = safe_team_dir_name(team_name)
			if not team_dir_name:
				continue
			existing[team_dir_name] = {
				"handler_name": (row.get("handler_name") or "").strip(),
				"handler_email": (row.get("handler_email") or "").strip(),
				"team_name": team_name,
			}
	return existing

def write_clients_csv(publish_dir, clients, download_prefix=""):
	csv_path = os.path.join(publish_dir, "clients.csv")
	with open(csv_path, "w", encoding="utf-8", newline="") as handle:
		writer = csv.writer(handle)
		writer.writerow(["handler_name", "handler_email", "team_name", "download"])
		for team_dir_name in sorted(clients):
			client = clients[team_dir_name]
			team_name = client["team_name"]
			writer.writerow([
				client["handler_name"],
				client["handler_email"],
				team_name,
				build_download_url(download_prefix, team_name),
			])
	logger.info("Wrote %s", csv_path)

def stage_processed(processed_dir, publish_dir, timeline_path, *, force=False, safe=False, download_prefix="", level=STAGING_LEVEL_DEFAULT):
	if not os.path.isdir(processed_dir):
		logger.info("Processed directory %s not found; nothing to stage", processed_dir)
		return

	os.makedirs(publish_dir, exist_ok=True)
	handler_emails = load_handler_emails_from_timeline(timeline_path)

	source_files = list(iter_staging_source_files(processed_dir))
	if not source_files:
		logger.info("Processed directory %s is empty; nothing to stage", processed_dir)
		return

	logger.info("Staging %d file(s) from %s to %s", len(source_files), processed_dir, publish_dir)

	clients = {}

	install_graceful_interrupt_handler()

	for source_path in source_files:
		name = os.path.basename(source_path)
		if level >= 2:
			staging_exif = fetch_keywords(source_path, include_creator=True)
			if staging_exif is None:
				abort_if_interrupt_requested(completed_item=name)
				continue
			keywords = staging_exif.get("keywords")
			photographer_dir_name = safe_photographer_dir_name(
				photographer_name_from_exif(keywords, staging_exif.get("creator"))
			)
		else:
			keywords = fetch_keywords(source_path)
			if keywords is None:
				abort_if_interrupt_requested(completed_item=name)
				continue
			photographer_dir_name = None
		dir_names = staging_dir_names_from_keywords(keywords)

		staged_any = False
		for dir_name in dir_names:
			safe_dir_name = safe_team_dir_name(dir_name)
			if safe_dir_name is None:
				logger.warning("Skipping %s: empty staging keyword value", name)
				continue

			target_dir = publish_team_dir(
				publish_dir,
				safe_dir_name,
				level=level,
				photographer_dir_name=photographer_dir_name,
			)
			os.makedirs(target_dir, exist_ok=True)
			dest_path = os.path.join(target_dir, name)
			dest_path, copied = copy_destination(
				source_path,
				dest_path,
				force=force,
				safe=safe,
			)
			if copied:
				logger.info("Staged %s -> %s", name, dest_path)
				staged_any = True
			else:
				logger.info("Skipped unchanged destination for %s -> %s", name, dest_path)

		if not staged_any:
			abort_if_interrupt_requested(completed_item=name)
			continue

		clients.update(clients_from_keywords(keywords, handler_emails))
		abort_if_interrupt_requested(completed_item=name)

	if clients:
		merged_clients = load_existing_clients(publish_dir)
		merged_clients.update(clients)
		write_clients_csv(publish_dir, merged_clients, download_prefix=download_prefix)

	clear_processed_directory(processed_dir)

def main():
	if len(sys.argv) == 1:
		print(__doc__)
		return
	args = docopt(__doc__)
	if args["--force"] and args["--safe"]:
		raise SystemExit("Cannot use --force and --safe together")
	setup_logging()

	stage_processed(
		processed_dir=args["--processed"],
		publish_dir=args["--publish"],
		timeline_path=args["--timeline"] or DEFAULT_TIMELINE_FILE,
		force=args["--force"],
		safe=args["--safe"],
		download_prefix=args["--download-prefix"] or "",
		level=parse_staging_level(args["--level"]),
	)

if __name__ == "__main__":
	main()
