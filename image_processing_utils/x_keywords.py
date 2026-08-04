from __future__ import annotations

import re

_QUOTED_DOG_NAME_SEGMENT_RE = re.compile(r'\s*"([^"]*)"\s*')
X_KEYWORD_LEGACY_RE = re.compile(r"^X-([^:]+):\s*(.+)$", re.IGNORECASE)
X_KEYWORD_HIER_RE = re.compile(r"^X-([^|]+)\|(.+)$", re.IGNORECASE)
DOGSPORTPHOTO_HIER_RE = re.compile(
	r"^(dogsportphoto(?:\.com)?)\|([^|]+)\|(.+)$",
	re.IGNORECASE,
)

DOGSPORTPHOTO_KEYWORD_ROOT = "dogsportphoto.com"
LEGACY_DOGSPORTPHOTO_KEYWORD_ROOT = "dogsportphoto"

# Short internal field names → names written under dogsportphoto.com|
FIELD_WRITE_NAMES = {
	"dis": "discipline",
	"img": "image_uuid",
	"loc": "location",
	"ofn": "original_filename",
	"org": "organization",
	"photog": "photographer",
	"seq": "sequence",
}

# Written / legacy long names → short internal field names used by matching/staging
FIELD_READ_ALIASES = {written: short for short, written in FIELD_WRITE_NAMES.items()}

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


def strip_stray_keyword_quotes(text: str) -> str:
	"""Strip balanced or one-sided quotes left by older exiftool argv quoting."""
	value = str(text).strip()
	if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
		return value[1:-1].strip()
	if value.endswith('"') and not value.startswith('"'):
		return value[:-1].rstrip()
	if value.startswith('"') and not value.endswith('"'):
		return value[1:].lstrip()
	return value


def _canonicalize_field_name(field: str) -> str:
	normalized = field.strip().lower()
	return FIELD_READ_ALIASES.get(normalized, normalized)


def _write_field_name(field: str) -> str:
	normalized = field.strip().lower()
	return FIELD_WRITE_NAMES.get(normalized, normalized)


def parse_x_keyword(keyword):
	if not isinstance(keyword, str):
		return None, None
	text = strip_stray_keyword_quotes(keyword)
	if not text:
		return None, None
	match = DOGSPORTPHOTO_HIER_RE.match(text)
	if match:
		field = _canonicalize_field_name(match.group(2))
		value = strip_stray_keyword_quotes(match.group(3))
		return field, value
	match = X_KEYWORD_HIER_RE.match(text)
	if match:
		field = _canonicalize_field_name(match.group(1))
		value = strip_stray_keyword_quotes(match.group(2))
		return field, value
	match = X_KEYWORD_LEGACY_RE.match(text)
	if match:
		field = _canonicalize_field_name(match.group(1))
		value = strip_stray_keyword_quotes(match.group(2))
		return field, value
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
	return "{}|{}|{}".format(DOGSPORTPHOTO_KEYWORD_ROOT, _write_field_name(field), text)


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
	return format_keyword(field, value)


def dogsportphoto_hierarchical_path(keyword):
	"""Return dogsportphoto.com|<field>|<value> path for a managed keyword."""
	return canonical_x_keyword(keyword)


def _legacy_field_names(field: str):
	"""Short and written field names used across current and legacy trees."""
	short = _canonicalize_field_name(field)
	written = _write_field_name(short)
	names = {short, written}
	return names


def hierarchical_subject_entries(keyword):
	"""Return canonical path and Subject nodes to scrub on reprocess.

	Includes legacy X-* / dogsportphoto|* parents plus the current
	dogsportphoto.com|* tree so reprocessing can scrub flattened leftovers.
	"""
	canonical = canonical_x_keyword(keyword)
	if canonical is None:
		return None, set()
	field, value = parse_x_keyword(canonical)
	if field is None or not value:
		return None, set()

	subject_nodes = {
		value,
		DOGSPORTPHOTO_KEYWORD_ROOT,
		LEGACY_DOGSPORTPHOTO_KEYWORD_ROOT,
		canonical,
	}
	for name in _legacy_field_names(field):
		subject_nodes.add("X-{}".format(name))
		subject_nodes.add("{}|{}".format(DOGSPORTPHOTO_KEYWORD_ROOT, name))
		subject_nodes.add("{}|{}".format(LEGACY_DOGSPORTPHOTO_KEYWORD_ROOT, name))
		for path in (
			"X-{}|{}".format(name, value),
			"{}|{}|{}".format(LEGACY_DOGSPORTPHOTO_KEYWORD_ROOT, name, value),
			"{}|{}|{}".format(DOGSPORTPHOTO_KEYWORD_ROOT, name, value),
		):
			subject_nodes.add(path)
			# Older writes may have stored a trailing quote in the leaf value.
			subject_nodes.add('{}"'.format(path))
	return canonical, subject_nodes


def keyword_removal_forms(keyword):
	"""All keyword-tag representations to delete when scrubbing legacy metadata."""
	if not isinstance(keyword, str):
		return set()
	field, value = parse_x_keyword(keyword)
	if field is None or not value:
		return {strip_stray_keyword_quotes(keyword)}
	forms = {
		keyword.strip(),
		strip_stray_keyword_quotes(keyword),
	}
	for name in _legacy_field_names(field):
		forms.update(
			{
				"X-{}|{}".format(name, value),
				"X-{}: {}".format(name, value),
				"{}|{}|{}".format(LEGACY_DOGSPORTPHOTO_KEYWORD_ROOT, name, value),
				"{}|{}|{}".format(DOGSPORTPHOTO_KEYWORD_ROOT, name, value),
				# Older writes may have stored a trailing quote in the value.
				'X-{}|{}"'.format(name, value),
				'{}|{}|{}"'.format(LEGACY_DOGSPORTPHOTO_KEYWORD_ROOT, name, value),
				'{}|{}|{}"'.format(DOGSPORTPHOTO_KEYWORD_ROOT, name, value),
			}
		)
	return {form for form in forms if form}


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
