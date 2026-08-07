from __future__ import annotations

import re

_QUOTED_DOG_NAME_SEGMENT_RE = re.compile(r'\s*"([^"]*)"\s*')
# Flat managed keywords are written as X-*; DSP-* and pipe forms are still accepted when reading.
FLAT_KEYWORD_PREFIX = "X-"
LEGACY_FLAT_KEYWORD_PREFIX = "DSP-"
FLAT_KEYWORD_COLON_RE = re.compile(r"^(?:DSP|X)-([^:]+):\s*(.+)$", re.IGNORECASE)
FLAT_KEYWORD_HIER_RE = re.compile(r"^(?:DSP|X)-([^|]+)\|(.+)$", re.IGNORECASE)
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

# Short field names used as X-<field>: <value> (and scrubbed on write).
# Only these (plus id-<org>) are managed flat tags; other X-* keywords are kept.
MANAGED_FLAT_FIELDS = frozenset(
	MATCH_X_FIELDS
	| {
		"img",
		"ofn",
		"org",
		"event",
		"club",
		"venue",
		"type",
		"city",
	}
)

# Back-compat alias used by older call sites / docs wording.
LEGACY_X_FIELDS = MANAGED_FLAT_FIELDS

# Default flat Keywords writes: team (else handler), plus event.
# --add-flat can request any managed flat field (or id-<org>).
ADD_FLAT_AVAILABLE_FIELDS = tuple(sorted(MANAGED_FLAT_FIELDS))
ADD_FLAT_USAGE_LIST = ", ".join(ADD_FLAT_AVAILABLE_FIELDS) + ", id-<org>"


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
	match = FLAT_KEYWORD_HIER_RE.match(text)
	if match:
		field = _canonicalize_field_name(match.group(1))
		value = strip_stray_keyword_quotes(match.group(2))
		return field, value
	match = FLAT_KEYWORD_COLON_RE.match(text)
	if match:
		field = _canonicalize_field_name(match.group(1))
		value = strip_stray_keyword_quotes(match.group(2))
		return field, value
	return None, None


def _normalized_keyword_value(field, value):
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
	return text


def format_keyword(field, value):
	"""Return dogsportphoto.com|<write_field>|<value> hierarchical path."""
	text = _normalized_keyword_value(field, value)
	if text is None:
		return None
	return "{}|{}|{}".format(DOGSPORTPHOTO_KEYWORD_ROOT, _write_field_name(field), text)


def format_x_flat_keyword(field, value):
	"""Return X-<short_field>: <value> flat keyword."""
	text = _normalized_keyword_value(field, value)
	if text is None:
		return None
	return "{}{}: {}".format(FLAT_KEYWORD_PREFIX, _canonicalize_field_name(field), text)


def x_flat_keyword_from_managed(keyword):
	"""Convert a managed keyword (any accepted form) to X-<field>: <value>."""
	field, value = parse_x_keyword(keyword)
	if field is None or not value:
		return None
	return format_x_flat_keyword(field, value)


def is_managed_flat_field(field):
	"""True for short fields (or id-<org>) used as managed flat X- tags."""
	if not field:
		return False
	if field.startswith("id-"):
		return True
	return field in MANAGED_FLAT_FIELDS


# Back-compat alias.
is_legacy_x_field = is_managed_flat_field


def normalize_add_flat_tag(tag):
	"""Normalize a --add-flat token to a short field name (or id-<org>)."""
	if tag is None:
		return None
	text = str(tag).strip().lower()
	if not text:
		return None
	if text.startswith("x-"):
		text = text[2:]
	elif text.startswith("dsp-"):
		text = text[4:]
	text = _canonicalize_field_name(text)
	if not is_managed_flat_field(text):
		return None
	return text


def select_flat_keywords_for_write(managed_keywords, add_flat_fields=()):
	"""Build default/extra flat X- keywords: team else handler, plus event, plus --add-flat."""
	by_field = {}
	for keyword in managed_keywords or []:
		field, value = parse_x_keyword(keyword)
		if field and value:
			by_field[field] = value

	selected = set()
	for field in add_flat_fields or ():
		normalized = normalize_add_flat_tag(field)
		if normalized:
			selected.add(normalized)
	if "team" in by_field:
		selected.add("team")
	elif "handler" in by_field:
		selected.add("handler")
	if "event" in by_field:
		selected.add("event")

	result = set()
	for field in selected:
		value = by_field.get(field)
		if not value:
			continue
		flat = format_x_flat_keyword(field, value)
		if flat:
			result.add(flat)
	return result


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
		text = strip_stray_keyword_quotes(keyword)
		if not text:
			continue
		if is_x_keyword(text):
			canon = canonical_x_keyword(text)
			if canon:
				normalized.add(canon)
			continue
		normalized.add(text)
	return normalized


def existing_dogsportphoto_com_keywords(keywords):
	"""Exact dogsportphoto.com|* paths already on the file (flat/legacy forms ignored)."""
	present = set()
	prefix = DOGSPORTPHOTO_KEYWORD_ROOT + "|"
	for keyword in keywords or []:
		if not keyword:
			continue
		text = strip_stray_keyword_quotes(keyword)
		if not text.startswith(prefix):
			continue
		canon = canonical_x_keyword(text)
		if canon:
			present.add(canon)
	return present


def existing_managed_flat_keywords(keywords):
	"""Exact on-file managed flat keywords (X-* / DSP-*, colon or pipe)."""
	present = set()
	for keyword in keywords or []:
		if not keyword:
			continue
		text = strip_stray_keyword_quotes(keyword)
		lower = text.lower()
		if not (lower.startswith("x-") or lower.startswith("dsp-")):
			continue
		field = keyword_x_field(text)
		if is_managed_flat_field(field):
			present.add(text)
	return present


# Back-compat alias.
existing_x_flat_keywords = existing_managed_flat_keywords


def is_obsolete_managed_keyword(keyword):
	"""True for managed X-*/DSP-*, dogsportphoto|* (no .com)."""
	text = strip_stray_keyword_quotes(keyword)
	if not text or not is_x_keyword(text):
		return False
	lower = text.lower()
	if lower.startswith("x-") or lower.startswith("dsp-"):
		return is_managed_flat_field(keyword_x_field(text))
	if lower.startswith("dogsportphoto|"):
		return True
	return False


def obsolete_managed_keywords(keywords):
	"""Exact on-file obsolete managed spellings to remove when writing keywords."""
	present = set()
	for keyword in keywords or []:
		if not keyword:
			continue
		text = strip_stray_keyword_quotes(keyword)
		if is_obsolete_managed_keyword(text):
			present.add(text)
	return present


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
