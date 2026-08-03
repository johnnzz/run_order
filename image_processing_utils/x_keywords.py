from __future__ import annotations

import re

_QUOTED_DOG_NAME_SEGMENT_RE = re.compile(r'\s*"([^"]*)"\s*')
X_KEYWORD_LEGACY_RE = re.compile(r"^X-([^:]+):\s*(.+)$", re.IGNORECASE)
X_KEYWORD_HIER_RE = re.compile(r"^X-([^|]+)\|(.+)$", re.IGNORECASE)

MATCH_X_FIELDS = frozenset(
	{
		"dog",
		"handler",
		"team",
		"photog",
		"photoreq",
		"msg",
		"dis",
		"loc",
		"seq",
		"duel",
	}
)


def normalize_quoted_dog_name(value: str) -> str:
	text = str(value).strip()
	if not text or '"' not in text:
		return text
	return _QUOTED_DOG_NAME_SEGMENT_RE.sub(r" - \1", text).strip()


def parse_x_keyword(keyword):
	if not isinstance(keyword, str):
		return None, None
	text = keyword.strip()
	if not text:
		return None, None
	match = X_KEYWORD_HIER_RE.match(text)
	if match:
		return match.group(1).strip().lower(), match.group(2).strip()
	match = X_KEYWORD_LEGACY_RE.match(text)
	if match:
		return match.group(1).strip().lower(), match.group(2).strip()
	return None, None


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
	return "X-{}|{}".format(field, text)


def keyword_x_field(keyword):
	field, _value = parse_x_keyword(keyword)
	return field


def keyword_x_value(keyword):
	_field, value = parse_x_keyword(keyword)
	return value or None


def is_x_keyword(keyword):
	return keyword_x_field(keyword) is not None


def canonical_x_keyword(keyword):
	field, value = parse_x_keyword(keyword)
	if field is None or not value:
		return None
	return "X-{}|{}".format(field, value)


DOGSPORTPHOTO_KEYWORD_ROOT = "dogsportphoto"


def dogsportphoto_hierarchical_path(keyword):
	"""Return dogsportphoto|<field>|<value> path mirroring an X-* keyword."""
	field, value = parse_x_keyword(keyword)
	if field is None or not value:
		return None
	return "{}|{}|{}".format(DOGSPORTPHOTO_KEYWORD_ROOT, field, value)


def hierarchical_subject_entries(keyword):
	"""Return hierarchical path and legacy dc:Subject nodes to scrub on reprocess.

	Also includes the parallel dogsportphoto|<field>|<value> tree and its parent
	nodes so reprocessing can scrub flattened Subject leftovers.
	"""
	canonical = canonical_x_keyword(keyword)
	if canonical is None:
		return None, set()
	field, value = parse_x_keyword(canonical)
	if field is None or not value:
		return None, set()
	parent = "X-{}".format(field)
	dsp_path = dogsportphoto_hierarchical_path(canonical)
	dsp_mid = "{}|{}".format(DOGSPORTPHOTO_KEYWORD_ROOT, field)
	subject_nodes = {parent, value, canonical, DOGSPORTPHOTO_KEYWORD_ROOT, dsp_mid}
	if dsp_path:
		subject_nodes.add(dsp_path)
	return canonical, subject_nodes


def keyword_removal_forms(keyword):
	"""All keyword-tag representations to delete when scrubbing legacy metadata."""
	if not isinstance(keyword, str):
		return set()
	field, value = parse_x_keyword(keyword)
	if field is None or not value:
		return {keyword.strip()}
	return {
		keyword.strip(),
		"X-{}|{}".format(field, value),
		"X-{}: {}".format(field, value),
	}


def is_match_x_keyword(keyword):
	field = keyword_x_field(keyword)
	if field is None:
		return False
	if field.startswith("id-"):
		return True
	return field in MATCH_X_FIELDS


def strip_match_x_keywords(keywords):
	stripped = set()
	for keyword in keywords:
		if not keyword:
			continue
		if is_match_x_keyword(keyword):
			continue
		stripped.add(keyword)
	return stripped
