# Run Order

JSON format for exchanging dog sport running orders and event timeseries between event software, photographers, and workflow tools such as [dogsport-photo-tools](https://github.com/johnnzz/dogsport-photo-tools).

This repository defines the canonical schema and example documents for the format.

## Document structure

A run order file is a single JSON object with three top-level fields:

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | string | Format version, currently `"2.0.0"` |
| `event` | object | Event metadata |
| `entries` | array | Chronological log of timeseries records |

Version 2.0.0 replaces the older nested `location` tree (1.x) with a flat, append-only `entries` array sorted by `at`.

### Event object

Required fields:

| Field | Type | Example |
| --- | --- | --- |
| `name` | string | `"World Championships 2024"` |

Optional fields (used by the teams app when available):

| Field | Type | Example |
| --- | --- | --- |
| `dogsportphoto_code` | string | `"1234"` (four digits) |
| `org` | string | `"Dueling Dogs"` |
| `start_date` | date string | `"2024-03-19"` |
| `end_date` | date string | `"2024-03-21"` |
| `city` | string | `"Dubuque"` |
| `state` | string | `"IA"` |
| `club` | string | `"Kickass Disc Dogs"` |
| `venue` | string | `"Evergreen State Fairgrounds"` |
| `org_type` | string | `"dock"` |

When present, the four-digit `dogsportphoto_code` matches the DogSportPhoto event code and appears in on-disk filenames such as `Summer_Splash-1234-ts.json`. Timeseries files do not carry sheet timezone settings; those live in event app configuration instead.

### Entries

Each entry is one chronological record with a shared envelope:

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `at` | yes | ISO 8601 date-time | When the entry was recorded |
| `location` | yes | string | Location or lane name (`Dock 1`, `Dock 1 Lane 2`, `default`, etc.) |
| `type` | yes | string | Entry type (see below) |

Entry types:

#### `team_check_in`

Records a handler check-in, optionally with a dog and team details.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `handler` | yes | object | Handler identity (`name` required) |
| `dog` | no | object | Dog identity (`name` required when present) |
| `team` | no | object | Team label for photo keywording |
| `event` | no | object | Attendance metadata (`photo_request`, `message_to_photographer`) |

Handler, dog, and team objects may include a `dogsportphoto_code` when registered in DogSportPhoto.

#### `photographer_check_in`

Records a photographer and their cameras at a location.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `photographer` | yes | object | Photographer `name` and `cameras` array |

Each camera object requires `model` and `serial` strings.

#### `set_discipline`

Records the active discipline at a location. The teams app writes this when a photographer or assistant selects a discipline for a shoot location.

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `discipline` | yes | string | Discipline name, such as `Distance Jump` or `Speed Retrieve` |

Example:

```json
{
  "at": "2026-06-23T14:05:12-06:00",
  "location": "Dock 1",
  "type": "set_discipline",
  "discipline": "Distance Jump"
}
```

A location may have many `set_discipline` entries over time. Consumers should treat the latest entry at a location as the current discipline unless they have another source of truth.

### Handler object

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `name` | yes | string | Handler display name |
| `dogsportphoto_code` | no | string | Registered handler entity code |
| `email` | no | string | Handler email |
| `phone` | no | string | Handler phone |

### Dog object

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `name` | yes | string | Dog call name |
| `dogsportphoto_code` | no | string | Registered dog entity code |
| `breed` | no | string | Dog breed |
| `color` | no | string | Dog color |
| `org_ids` | no | object | Map of org slug → registration number |

### Attendance object (`event` on a team check-in)

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `photo_request` | no | boolean | Whether the handler requested event photos |
| `message_to_photographer` | no | string | One-line note to the photographer (max 200 chars) |

An empty `entries` array (`[]`) is valid for a newly created file with no records yet.

## Examples

| File | Description |
| --- | --- |
| [`minimal_event_example.json`](minimal_event_example.json) | New file with only required event metadata and empty entries |
| [`simple_example.json`](simple_example.json) | Minimal event with two team check-ins |
| [`dueling_example.json`](dueling_example.json) | Dueling Dogs example with org IDs |
| [`teams_app_example.json`](teams_app_example.json) | All three entry types: photographer check-in, set discipline, and team check-in |

### Minimal example

```json
{
  "schema_version": "2.0.0",
  "event": {
    "name": "Kickass Disc Dogs",
    "dogsportphoto_code": "1001",
    "org": "K9 Frisbee Worldwide League",
    "city": "Everett",
    "state": "WA",
    "start_date": "2024-03-19",
    "end_date": "2024-03-19"
  },
  "entries": [
    {
      "at": "2024-03-19T16:00:00-07:00",
      "location": "field",
      "type": "team_check_in",
      "handler": { "name": "Joe Smith" },
      "dog": { "name": "Sugar" },
      "team": { "name": "Joe Smith n Sugar" }
    }
  ]
}
```

## Schema

The machine-readable schema is [`run_order.schema.json`](run_order.schema.json) (JSON Schema draft 2020-12).

Validate example files:

```bash
pip install jsonschema
python3 validate_examples.py
```

Or validate any file with the [jsonschema](https://python-jsonschema.readthedocs.io/) CLI:

```bash
jsonschema --schema run_order.schema.json --instance my_run_order.json
```

Online validators that support draft 2020-12 can also load the schema directly from:

```text
https://raw.githubusercontent.com/johnnzz/run_order/main/run_order.schema.json
```

## Versioning

The current schema version is **2.0.0**, used by dogsport-photo-tools. Older 1.x documents used a nested `location` tree keyed by timestamp strings instead of an `entries` array.

| Version | Changes |
| --- | --- |
| **2.0.0** | Chronological `entries` log; structured handler/dog/team objects; optional event metadata used by the teams app; entry types `team_check_in`, `photographer_check_in`, `set_discipline` |
| **1.1.0** | Allow empty `location` objects (legacy tree format) |
| **1.0.0** | Initial nested `location` tree format |

Increment `schema_version` in documents when making incompatible format changes.

## Related tools

- **dogsport-photo-tools** — writes and reads 2.0.0 timeseries files for event check-ins and photo workflow
- **run_order_timeseries.py** — shared helpers in dogsport-photo-tools for reading, writing, and migrating timeseries files
