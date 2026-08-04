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


def canonicalize_managed_keywords(keywords):
	"""Preserve non-managed keywords; collapse managed ones to dogsportphoto.com|*."""
	normalized = set()
	for keyword in keywords or []:
		if not keyword:
			continue
		if is_x_keyword(keyword):
			canon = canonical_x_keyword(keyword)
			if canon:
				normalized.add(canon)
			continue
		normalized.add(keyword)
	return normalized


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
