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

Unique, name-spaced entries in the format of `<application>_<field>` such as `dogsportphoto_code` may be added for application specific purposes.  

### Entries

Each entry is one chronological record with a shared envelope:

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `at` | yes | ISO 8601 date-time | When the entry was recorded |
| `location` | yes | string | Location or lane name (`Dock 1`, `Dock 1 Lane 2`, `default`, etc.) |
| `type` | yes | string | Entry type (see below) |

#### Entry ordering

Entries are a chronological log sorted by `at`. When a time-series file includes setup records for a location, write them before the first `team_check_in` for that location:

- If the file uses discipline information, append a `set_discipline` entry before team check-ins at that location.
- If the file uses photographer information, append a `photographer_check_in` entry before team check-ins at that location.

`photographer_check_in` is normally written by a photographer-centric app such as the DogSportPhoto teams app. Third-party, handler-oriented time-series sources (for example run lists or registration exports) typically emit `team_check_in` entries only and do not include photographer setup records.

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

Photographer check in entries are typically provided by photography specific applications and can be used to tie the a given image back to the location it was taken by correlating image to photographer (via the camera serial number) to where the photographer was at that time based on their check in and thus back to the dog.

This type of arrangement is only necessary when there are multiple locations (fields, docks, rings, arenas) and this allows coordinated coverage by multiple photographers across the various locations. 

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `photographer` | yes | object | Photographer `name` and `cameras` array |

Each camera object requires `model` and `serial` strings.

#### `set_discipline`

Records the active discipline or activity at a location.  

When used, a set_discipline entry should be made before any team check ins, so the discipline of the subsequent check ins is known.

The goal of the discipline is provide additional descriptive organization information to categorize images that will be added to the image as metadata for later use.

The discipline value is user defined, but it is recommended to follow the concepts of the event organization. 

For example, Dockdogs has disciplines of "Big Air", "Extreme Vertical", "Speed Retrieve" and "Dueling Dogs".  

Updog has games like "Time Warp", "ThrowNGo", "Spaced Out", "4WayPlay", "Far Out", "Greedy", "Boom!", "Fireball" and "Freestyle".  

In agility this could be something like "Jumpers" vs "Standard".

These all provide meaningful organizational information.  

In cases where there aren't meaningful differences in the activity being performed, it could reflect skill categories ("Expert", "Novice") or broad time categories ("Morning", "Evening") or some combination of the above.

That said discipline is only useful if it is something that is happening at an location (ring, pool, arena) at a given time-frame.  

A discipline is not strictly necessary, but highly desirable.

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

A location may have many `set_discipline` entries over time. Consumers should treat the latest entry at a location as the current discipline.

### Handler object

| Field | Required | Type | Description |
| --- | --- | --- | --- |
| `name` | yes | string | Handler display name |
| `dogsportphoto_code` | no | string | Registered handler entity code |
| `email` | no | string | Handler email |
| `phone` | no | string | Handler phone |

### Dog object

While the dog entry is not strictly required, it is highly desirable.  It is not a required field since some tracking mechanisms such as QR codes make it impractical to track the specific dog.

Information such as breed, color or organizational IDs can be provided if the information is available and can be useful to photographers to help identify dogs in the former cases, or to provide cross event tracking information in the case of the IDs.

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
| [`minimal_example.json`](minimal_example.json) | Required fields only |
| [`full_example.json`](full_example.json) | All optional event, entry, handler, dog, team, and attendance fields |

### Minimal example

Only required fields: `schema_version`, `event.name`, and one `team_check_in` with `at`, `location`, `type`, and `handler.name`.

```json
{
  "schema_version": "2.0.0",
  "event": {
    "name": "New Event"
  },
  "entries": [
    {
      "at": "2024-03-19T16:00:00-07:00",
      "location": "field",
      "type": "team_check_in",
      "handler": {
        "name": "Joe Smith"
      }
    }
  ]
}
```

An empty `entries` array is also valid when no records have been written yet.

See [`full_example.json`](full_example.json) for all three entry types with optional event metadata, handler contact fields, dog breed and org IDs, team codes, and attendance notes.

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
