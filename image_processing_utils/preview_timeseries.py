#!/usr/bin/python3
# Copyright (c) 2026 John Navitsky
#
# SPDX-License-Identifier: MIT
# See LICENSE for the full license text.
"""
Preview team check-ins from a timeseries file.

Lists each team_check_in using the same X-team|... keyword format that
process_queue.py writes into HierarchicalSubject.

Fixit corrections are written 0.01s after the original (wrong or Unspecified)
check-in at the same location. When a correction exists, the original entry is
omitted from the preview.

Usage:
  preview_timeseries.py <timeseries>
  preview_timeseries.py -h | --help

Arguments:
  <timeseries>  Path to a run_order timeseries JSON file.

Options:
  -h, --help  Show this message.
"""

from __future__ import annotations

import sys
from datetime import timedelta, timezone

from docopt import docopt

from process_queue import load_time_series
from x_keywords import format_keyword

# Matches dogsport-photo-tools run_order_store.FIXIT_CHECK_IN_OFFSET_SECONDS.
FIXIT_CHECK_IN_OFFSET = timedelta(seconds=0.01)


def _location_label(location_path) -> str:
	if not location_path:
		return "default"
	return " / ".join(str(part) for part in location_path if part)


def _normalize_moment(moment):
	if moment.tzinfo is None:
		return moment.replace(tzinfo=timezone.utc)
	return moment.astimezone(timezone.utc)


def _format_check_in_time(moment) -> str:
	if not hasattr(moment, "isoformat"):
		return str(moment)
	normalized = _normalize_moment(moment)
	if normalized.microsecond:
		return normalized.isoformat(timespec="milliseconds")
	return normalized.isoformat(timespec="seconds")


def _fixit_times_by_location(check_ins_by_location):
	"""Map location_path -> set of timestamps that are Fixit corrections (+0.01s)."""
	fixit_times = {}
	for location_path, entries in check_ins_by_location.items():
		moments = {_normalize_moment(entry["time"]) for entry in entries}
		superseded = set()
		for moment in moments:
			correction = moment + FIXIT_CHECK_IN_OFFSET
			if correction in moments:
				superseded.add(moment)
		fixit_times[location_path] = superseded
	return fixit_times


def iter_team_check_ins(time_series):
	check_ins_by_location = time_series.get("check_ins_by_location", {})
	superseded_by_location = _fixit_times_by_location(check_ins_by_location)
	rows = []
	for location_path, entries in check_ins_by_location.items():
		location = _location_label(location_path)
		superseded = superseded_by_location.get(location_path, set())
		for check_in in entries:
			if _normalize_moment(check_in["time"]) in superseded:
				continue
			keyword = format_keyword("team", check_in.get("team"))
			if not keyword:
				continue
			rows.append((check_in["time"], location, keyword))
	rows.sort(key=lambda item: (item[0], item[1], item[2]))
	return rows


def preview_timeseries(path: str) -> int:
	time_series = load_time_series(path)
	rows = iter_team_check_ins(time_series)

	event_name = time_series.get("event_name") or "(unnamed event)"
	unique_teams = sorted({keyword for _time, _location, keyword in rows})

	print("Event: {}".format(event_name))
	print("File: {}".format(path))
	print(
		"{} team check-in{}, {} unique team{}".format(
			len(rows),
			"" if len(rows) == 1 else "s",
			len(unique_teams),
			"" if len(unique_teams) == 1 else "s",
		)
	)
	print("")

	if not rows:
		print("No team check-ins found.")
		return 0

	for moment, location, keyword in rows:
		print(
			"{}\t{}\t{}".format(
				_format_check_in_time(moment),
				location,
				keyword,
			)
		)

	print("")
	print("Unique teams:")
	for keyword in unique_teams:
		print(keyword)
	return 0


def main(argv=None) -> int:
	args = docopt(__doc__, argv=argv)
	return preview_timeseries(args["<timeseries>"])


if __name__ == "__main__":
	sys.exit(main())
