# Run Order

JSON format for exchanging dog sport running orders between event software, photographers, and workflow tools such as [dogsport-photo-tools](https://github.com/johnnzz/dogsport-photo-tools).

This repository defines the canonical schema and example documents for the format.

## Document structure

A run order file is a single JSON object with three top-level fields:

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | string | Format version in semver form, currently `2.0.0` |
| `event` | object | Event metadata (name, dates, optional org and venue details) |
| `location` | object | Recursive tree of venue locations and timeseries entries |

### Event object

Required fields:

| Field | Type | Example |
| --- | --- | --- |
| `name` | string | `"World Championships 2024"` |
| `start_date` | date string | `"2024-03-19"` |
| `end_date` | date string | `"2024-03-21"` |

Optional fields:

| Field | Type | Example |
| --- | --- | --- |
| `org` | string | `"Dueling Dogs"` |
| `city` | string | `"Dubuque"` |
| `state` | string | `"IA"` |
| `club` | string | `"Kickass Disc Dogs"` |
| `venue` | string | `"Evergreen State Fairgrounds"` |
| `org_type` | string | `"dock"` |
| `sheet_timezone` | string | `"America/Los_Angeles"` |

### Location tree

The `location` object is a recursive map. Each key is either:

1. **A nested location name** — value is another location object. Use names that match the venue (`pool1`, `ring2`, `Dock 1`, and so on).
2. **A timestamp slot** — value is an array of run entries for that location at that time.

Timestamp keys use this format:

```text
YYYY-MM-DD HH:MM:SS±HH:MM
```

Examples:

- `2024-03-19 16:00:00-07:00`
- `2024-03-19 16:05:13-07:00`

The offset is a numeric timezone offset (`-07:00`, `+00:00`), not a named zone like `PDT`.

An empty `location` object (`{}`) is valid for a newly created file with no entries yet.

#### Location tree example

```json
{
  "location": {
    "pool1": {
      "2024-03-19 16:00:00-07:00": [ /* entries in pool 1 */ ]
    },
    "2024-03-19 16:05:13-07:00": [ /* entries at the root location level */ ]
  }
}
```

In this example, `pool1` is a nested location. The timestamp at the root level schedules entries that are not under a named sub-location.

### Run entry

Each timestamp slot contains an array of run entry objects. Version 2.0.0 defines four entry types:

#### Team check-in

A handler-and-dog pair checked in at a location.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `dog` | yes | object | Dog identity (see below) |
| `handler` | yes | string | Handler name |
| `group` | no | string | Class, round, or group identifier |
| `handler-email` | no | string | Handler email address |
| `handler-phone` | no | string | Handler phone number |
| `photo_request` | no | string | `"Yes"` or `"No"` |
| `team` | no | string | Formatted handler-and-dog label for photo keywording |
| `user_id` | no | uuid string | Registered handler user id when known |

#### Handler metadata

Handler information without a dog check-in (for example proxy check-ins).

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `handler` | yes | string | Handler name |
| `group` | no | string | Class, round, or group identifier |
| `handler-email` | no | string | Handler email address |
| `handler-phone` | no | string | Handler phone number |
| `photo_request` | no | string | `"Yes"` or `"No"` |
| `user_id` | no | uuid string | Registered handler user id when known |

#### Photographer setup

Photographer and camera registration at a location.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `photographer` | yes | string | Photographer name |
| `cameras` | yes | array | Cameras registered at this location |

Each camera object requires `model` and `serial` strings.

#### Discipline selection

The active discipline at a location.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `discipline` | yes | string | Discipline name |

### Dog object

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `call_name` | yes | string | Dog's call name |
| `breed` | no | string | Dog breed |
| `color` | no | string | Dog color |
| `org_ids` | no | object | Map of org abbreviation → registration/entry number |

Organization abbreviations are short lowercase keys such as `akc`, `ddww`, or `aac`. Values are strings so leading zeros and alphanumeric IDs are preserved.

```json
{
  "call_name": "Sugar",
  "breed": "Border Collie",
  "color": "Black and White",
  "org_ids": {
    "akc": "1222",
    "ddww": "432"
  }
}
```

## Examples

This repository includes three example files:

| File | Description |
| --- | --- |
| [`simple_example.json`](simple_example.json) | Minimal event with a single field and two timestamp slots |
| [`dueling_example.json`](dueling_example.json) | Dueling Dogs world championship example with nested pool location, groups, and org IDs |
| [`teams_app_example.json`](teams_app_example.json) | dogsport-photo-tools teams app entries: photographer setup, discipline selection, team check-in, and handler metadata |

### Minimal example

```json
{
  "schema_version": "2.0.0",
  "event": {
    "name": "Kickass Disc Dogs",
    "org": "K9 Frisbee Worldwide League",
    "city": "Everett",
    "state": "WA",
    "start_date": "2024-03-19",
    "end_date": "2024-03-19"
  },
  "location": {
    "field": {
      "2024-03-19 16:00:00-07:00": [
        {
          "dog": { "call_name": "Sugar" },
          "handler": "Joe Smith"
        }
      ]
    }
  }
}
```

## Schema

The machine-readable schema is [`run_order.schema.json`](run_order.schema.json) (JSON Schema draft 2020-12).

Validate example files:

```bash
pip install jsonschema
python validate_examples.py
```

Or validate any file with the [jsonschema](https://python-jsonschema.readthedocs.io/) CLI:

```bash
jsonschema --schema run_order.schema.json --instance my_run_order.json
```

Online validators that support draft 2020-12 can also load the schema directly from:

```text
https://raw.githubusercontent.com/johnnzz/run_order/main/run_order.schema.json
```

## Working with location paths

Tools that read or append runs typically address a slot using a dot-separated location path and a timestamp:

| Location path | Timestamp | Meaning |
| --- | --- | --- |
| *(empty)* | `2024-03-19 16:05:13-07:00` | Root-level slot |
| `pool1` | `2024-03-19 16:00:00-07:00` | Slot under `location.pool1` |
| `ring2.masters` | `2024-03-20 09:30:00-07:00` | Nested location `ring2` → `masters` |

## Versioning

The current schema version is **2.0.0**, used by dogsport-photo-tools. Older versions remain valid for legacy documents.

| Version | Changes |
| --- | --- |
| **2.0.0** | Timeseries entry variants (team check-in, handler metadata, photographer setup, discipline selection); `end_date` required on event; optional event fields for club, venue, org type, and sheet timezone; dog `breed` and `color` fields |
| **1.1.0** | Allow empty `location` objects |
| **1.0.0** | Initial format |

Increment `schema_version` in documents when making incompatible format changes.

## Related tools

- **dogsport-photo-tools** — consumes run order files to match photos to scheduled runs and store event check-ins
- **run_order.py** — optional Python helper (available in git history) for create/read/append operations
