# Image processing utilities

Copyright (c) 2026 John Navitsky. Released under the [MIT License](LICENSE).

Standalone command-line tools for building run-order timeseries data, tagging event photos with EXIF keywords, staging output for publish, and inspecting image metadata. Copy a script plus any listed helper modules from this directory to run elsewhere.

| Script | Summary |
|--------|---------|
| `google_qr_to_timeseries.py` | Build a run_order timeseries JSON file from a Google Sheet |
| `process_queue.py` | Match queued event photos to run-order check-ins and write EXIF keywords |
| `stage_into_dirs.py` | Stage tagged photos into team folders for publish and gallery delivery |
| `summarize_dir.py` | Print a human-readable EXIF summary of images in a directory tree |

Typical end-to-end workflow:

```text
google_qr_to_timeseries.py <sheet_url>  →  event-ts.json
        ↓
process_queue.py --process           →  processed/ (tagged EXIF)
        ↓
stage_into_dirs.py                   →  publish/<team>/ + clients.csv
```

For ad-hoc inspection of tagged output, use `summarize_dir.py` on `processed/` or `publish/`.

---

## Installing dependencies

### Python packages

CLI utilities in this directory need **docopt**. Install from here:

```bash
pip install -r requirements.txt
```

Or install only docopt:

```bash
pip install docopt
```

**Python version:** 3.9+ recommended (`zoneinfo` for timezone handling). Older 3.x may work with reduced timezone support.

### exiftool (system dependency)

Several scripts invoke **exiftool** as an external command. It must be installed separately and available on `PATH` (run `exiftool -ver` to verify).

| Script | Uses exiftool for |
|--------|-------------------|
| `process_queue.py` | Reading and writing EXIF keywords, ratings, dates (`--process`) |
| `stage_into_dirs.py` | Reading `Keywords` from processed images |
| `summarize_dir.py` | Reading image metadata for directory summaries |

**macOS (Homebrew):**

```bash
brew install exiftool
```

**Debian / Ubuntu:**

```bash
sudo apt-get update && sudo apt-get install -y libimage-exiftool-perl
```

**Other platforms:** Download from [ExifTool by Phil Harvey](https://exiftool.org/) or use your package manager’s `exiftool` / `perl-Image-ExifTool` package.

### Copying scripts to another machine

Each script documents which helper modules it imports. Copy these files together:

| Script | Copy with |
|--------|-----------|
| `process_queue.py` | `_run_order_timeseries.py` |
| `google_qr_to_timeseries.py` | `_run_order_timeseries.py` |
| `stage_into_dirs.py` | `_run_order_timeseries.py` |
| `summarize_dir.py` | *(none — standalone)* |

---

## Helper scripts (shared modules)

These are not run directly in the normal workflow; they provide shared logic imported by the CLI tools above.

### `_run_order_timeseries.py`

Internal [run_order](https://github.com/johnnzz/run_order) timeseries library (not run directly; leading `_` marks it as a helper). Copy alongside any script that imports it.

- Load and migrate legacy **1.0** location-tree JSON to v2 chronological `entries`
- Parse entry types: `photographer_check_in`, `team_check_in`, `set_discipline`
- Extract handler, dog, team, email, photo request, cameras, discipline, and location fields
- `team_display_name()` — synthesizes `"<handler> n <dog>"` when team is missing

Used by `process_queue.py`, `google_qr_to_timeseries.py`, and `stage_into_dirs.py`.

`process_queue.py --timeline2` merges a secondary timeseries into the primary inline (primary wins on conflicts; secondary fills gaps).

---

## google_qr_to_timeseries.py

Build a run_order timeseries JSON file from a Google Sheet.

First step in the offline workflow: fetches Setup, Event, Roster, and Log tabs from a published sheet URL and writes a schema 2.0 timeseries file for `process_queue.py` to consume. No Google API credentials required.

```text
google_qr_to_timeseries.py [options] <sheet_url>
```

| Option | Default | Description |
|--------|---------|-------------|
| `--timezone TZ` | `America/New_York` | IANA timezone for naive Log tab timestamps |
| `--file PATH` | `<event-name>-ts.json` | Output JSON path |
| `-h`, `--help` | | Show usage |

With no options, prints usage (same as `-h` / `--help`). A `<sheet_url>` argument is required to build a timeseries file.

### Sheet tabs read

| Tab name(s) | Purpose |
|-------------|---------|
| `Setup` / `setup` | Event name and optional metadata |
| `Event` / `Events` / `event` | Event metadata (name, org, venue, city, etc.) |
| `Roster` | Handler/dog/team roster for Log row resolution |
| `Log` | Check-in log rows → timeseries entries |
| `Locations` / `Location` / `Heats` / `Docks` | Location/heat names (also inferred from Log when absent) |

The event name must appear on **Setup** (`Event Name`) or **Event** (`Name` or `Event`). Log rows without a resolvable heat/location are skipped; if every row lacks location, the script exits with an error.

### Output

Writes a v2 timeseries file with `schema_version`, `event` metadata (including `sheet_timezone`), and chronological `entries` derived from the Log tab. Unmapped heat/location values print a warning to stderr.

### Examples

```bash
google_qr_to_timeseries.py "https://docs.google.com/spreadsheets/d/ABC123/edit"
```

```bash
google_qr_to_timeseries.py --timezone America/Los_Angeles \
  --file "Summer Splash 2026-ts.json" \
  "https://docs.google.com/spreadsheets/d/ABC123/edit"
```

**Dependencies:** `docopt`, `_run_order_timeseries.py`. No exiftool.

---

## process_queue.py

Match queued event photos to run-order check-ins and write EXIF keywords.

First step after timeseries export: reads photos from a queue directory, matches each image to photographer location, team check-in, and discipline using a local timeseries JSON file, writes hierarchical keywords (and optional flat `X-*` keywords / rating), then renames and moves tagged files into `processed/`.

```text
process_queue.py [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-q`, `--queue DIR` | `./queue` | Input directory of photos to process |
| `-p`, `--processed DIR` | `./processed` | Output directory for tagged, renamed photos |
| `-b`, `--backup DIR` | *(none)* | Backup directory. **Omitted = no backup is made** |
| `-t`, `--timeline FILE` | `./eventname-ts.json` | Primary run_order timeseries JSON file |
| `--timeline2 FILE` | *(none)* | Optional second timeseries merged into `--timeline` |
| `-r`, `--rating NUM` | *(none)* | Set star rating on images that have none; omit to leave ratings unchanged |
| `--status` | | Print queue/processed/backup paths and file counts |
| `--process` | | Process all files in the queue |
| `--in-place` | | Write EXIF/IPTC on the queue file; do not rename, move, or backup |
| `--log FILE` | `process_queue-<pid>.log` | Log file path |
| `--force` | | Always overwrite existing destination files, even when MD5 matches |
| `--safe` | | Write to `_N` suffix paths instead of overwriting |
| `--flat-keyword MODE` | `on` | Write flat `X-*` Keywords: `on` or `off` |
| `--add-flat TAGS` | | Extra flat `X-*` tags when `--flat-keyword` is on (comma-separated). Available: `city`, `club`, `dis`, `dog`, `duel`, `event`, `handler`, `img`, `loc`, `msg`, `ofn`, `org`, `photog`, `photoreq`, `seq`, `team`, `type`, `venue`, `id-<org>` |
| `-h`, `--help` | | Show usage |

`--force` and `--safe` cannot be used together. `--in-place` cannot be combined with `--force` or `--safe`.

### Operating modes

**Default (no options)** — Prints usage (same as `-h` / `--help`).

**`--status`** — Prints directory status: path and file count for queue, processed, and backup (backup listed only when `-b` was provided).

**`--process`** — Main workflow:

1. Load and index the timeseries file(s)
2. Walk the queue directory recursively for image files
3. Read EXIF from each file via exiftool
4. Match photo time + camera serial to run-order data
5. Write keywords via exiftool (and rating only when `-r` / `--rating` is set)
6. Optionally back up the original (only when `-b` is set; skipped with `--in-place`)
7. Rename and move the file into processed (skipped with `--in-place`)

**`--process --in-place`** — Same matching and EXIF/IPTC writes, but the queue file keeps its path and name. Pass-through and before-first-check-in files are also left in the queue unchanged. Console output still defaults to `--verbosity quiet`, but the log (`--log` or `--verbosity full`) includes the same per-image match details as a normal full-verbosity run.

### Destination file behavior (`--force` / `--safe`)

When **neither** `--force` nor `--safe` is given (the default):

- If the destination file does not exist, the file is written and moved into place.
- If the destination already exists with **identical content** (same MD5), EXIF is not re-written and the existing file is left unchanged, but the queue file is still removed.
- If the destination exists with **different content**, it is overwritten.

| Flag | Behavior |
|------|----------|
| *(default)* | Skip rewrite when MD5 matches; overwrite when content differs; always dequeue |
| **`--force`** | Always re-write EXIF and overwrite the destination, even when MD5 matches |
| **`--safe`** | Never overwrite; pick `_1`, `_2`, … suffixed filenames when the target name exists |

### Directory workflow

```text
queue/          →  read images, apply EXIF, rename
    ↓
processed/      →  final tagged output (YYYYMMDDHHMMSScc-original.ext)
backup/         →  optional copy of pre-tag originals (only with -b)
```

- Queue files may live in subdirectories; the script walks the tree and removes empty subdirs after processing.
- Processed filenames are prefixed with capture time: `YYYYMMDDHHMMSS` + 2-digit centiseconds + `-` + original basename (any existing timestamp prefix on the original name is stripped first).
- With `--safe`, if a destination filename already exists, `_1`, `_2`, … suffixes are used for both processed and backup paths.
- Without `--force`, files with identical MD5 at the destination are skipped (logged as unchanged); the queue copy is still removed on move when unchanged.

### Timeseries input

Accepts [run_order](https://github.com/johnnzz/run_order) JSON:

- **Schema 2.0** — `schema_version`, `event`, and chronological `entries`
- **Legacy 1.0** — nested `location` tree; auto-migrated to v2 on load

Entry types indexed:

| Type | Purpose |
|------|---------|
| `photographer_check_in` | Photographer name + camera serials at a location |
| `team_check_in` | Handler, dog, team, org IDs, photo request, message |
| `set_discipline` | Active discipline at a location |

Event metadata from the `event` block becomes `dogsportphoto.com|event|`, `dogsportphoto.com|organization|`, `dogsportphoto.com|club|`, `dogsportphoto.com|venue|`, `dogsportphoto.com|type|`, and `dogsportphoto.com|city|` keywords on every processed image.

**Optional merge (`--timeline2`)** — Merges a secondary file into the primary (primary wins on conflicts; secondary fills gaps).

**Single-location inference** — If the timeseries has exactly one location across check-ins and discipline entries, that location is inferred when no photographer match exists.

**Team name synthesis** — When handler and dog are present but team is absent: `<handler> n <dog>` (parenthetical codes stripped; embedded quotes in dog names rewritten as ` - `).

### Photo matching

1. **Image time** — From EXIF (`SubSecCreateDate`, `SubSecDateTimeOriginal`, `CreateDate`, `DateTimeOriginal`, `FileModifyDate`) with offset tags; naive timestamps use `NAIVE_TIMESTAMP_TIMEZONE` (default `America/Los_Angeles`). When the timeseries includes a per-camera **`offset`** on the matching photographer check-in, that many seconds are added to EXIF time before matching check-ins.
2. **Camera serial** — Body serial tags, excluding lens serials.
3. **Photographer location** — Latest `photographer_check_in` at or before photo time where serial matches; ties prefer lower dock lane number.
4. **Team check-in** — Latest `team_check_in` at or before photo time for that location (QR mode and self check-in). Runlist mode (`event_mode_checkin: Runlist`, or legacy timeseries with a lead check-in >120s before a batch cluster) groups photos into bursts (~3 s apart) and matches sequentially: first burst to the lead check-in, then each later burst to the next check-in in batch order.
5. **Discipline** — Latest `set_discipline` with 120-second grace window; Dueling Dogs resolves across both lanes of a dock.
6. **Handler metadata** — `photo_request` and `message_to_photographer` from check-in or per-handler maps.

### EXIF keywords written

Hierarchical `dogsportphoto.com|<field>|<value>` paths are always written to `XMP-lr:HierarchicalSubject`.

Flat `X-<field>: <value>` Keywords are controlled by `--flat-keyword` (default `on`):

| Flat default | Writes |
|--------------|--------|
| Primary | `X-team: …` when team is present, otherwise `X-handler: …` |
| Event | `X-event: …` when event is present |
| Extra | Any tags listed in `--add-flat` that have values on the image |

Existing non-managed keywords are preserved. Hierarchical paths are always rewritten. When flat keywords are enabled, managed flat spellings (`X-*` / `DSP-*` for known fields) are removed and the selected `X-*` set is rewritten. Obsolete `dogsportphoto|*` entries are removed. Unrelated `X-*` keywords are left alone.

When a photo match is found, the script also writes **IPTC Core** metadata via exiftool:

| IPTC field | Source |
|------------|--------|
| Title | `{team} - {image_uuid}` (Lightroom Title / `dc:title`) |
| Alt Text | Same text as Title (`XMP-iptcCore:AltTextAccessibility`) |
| Headline | `{team} compete in {org} {event}`; duel → `{duel} in {discipline} at {event}` |
| Caption | Same text as Headline (Lightroom Caption / `dc:description`) |
| Extended Description | Same text as Caption (`XMP-iptcCore:ExtDescrAccessibility`) |
| Location | `event.venue` when present |
| City / State | from event metadata |
| Creator / Credit | photographer name |
| Copyright | existing image copyright, or `© {year} {photographer}` |
| Source | `{event} ({dogsportphoto_code})` |
| Transmission Reference | `image_uuid` |

Hierarchical managed keywords are always written to `XMP-lr:HierarchicalSubject`. Flat `X-field: value` keywords are written to `Keywords` when `--flat-keyword` is on. Parsers still accept `DSP-*`, pipe forms, and `dogsportphoto|*` when reading older files.

**Event keywords:** `event`, `organization`, `club`, `venue`, `type`, `city`

**Match keywords:** `photographer`, `dog`, `handler`, `team`, `photoreq`, `msg`, `discipline`, `location`, `id-<org>`

**Per-image keywords:** `image_uuid` (unique ID), `original_filename`, `sequence` (sequence group)

**Dueling Dogs:** `duel` plus per-lane dog/handler/team keywords when both dock lanes have dogs checked in.

### Sequence grouping (`sequence`)

Queue images sorted by capture time; consecutive images sharing the same location, discipline, and dog (or duel dock + dogs) receive the same `sequence` value derived from the matched check-in:

`<yy><mm><dd>-<hh><mm>.<ss><ms>-<photographer initials>`

Example: `dogsportphoto.com|sequence|260731-1614.30512-JN` for a 2026-07-31 16:14:30.512 check-in by John Navitsky.

### Safety and idempotency

| Feature | Behavior |
|---------|----------|
| **No backup by default** | Originals moved to processed unless `-b` is set |
| **`--force`** | Overwrite even when MD5 matches |
| **`--safe`** | Allocate `_N` suffix filenames |
| **MD5 skip** | Without `--force`, identical destination content is not re-written |
| **Keyword overwrite** | Hierarchical paths always rewritten; flat `X-*` rewritten when enabled |

### Re-processing

In the event of problems processing files, files can be re-processed. Hierarchical keywords are fully rewritten; flat `X-*` keywords follow `--flat-keyword` / `--add-flat`; obsolete managed spellings are removed.

### Rating (`--rating`)

The script can optionally set a rating for all images that do not currently contain one. This can facilitate the use of a rating system while also allowing a photographer to tag notable images in-camera.

The author uses a system where all images are set to two stars by default. In this scheme, two stars means "not rated". Image ratings then can be moved up or down. Once there are no more two star ratings you know you have graded all the files.

| Stars | Meaning |
|-------|---------|
| 0 | Delete |
| 1 | Bad, but keep as backup |
| 2 | Unrated |
| 3 | Adequate |
| 4 | Good |
| 5 | Excellent |

### Examples

```bash
process_queue.py --process --timeline MyEvent-1234-ts.json
```

```bash
process_queue.py --process \
  --timeline primary-ts.json \
  --timeline2 supplemental-ts.json \
  --backup ./backup
```

```bash
process_queue.py --process --safe --timeline event-ts.json
process_queue.py --status -q ./queue -p ./processed
```

**Dependencies:** `docopt`, `_run_order_timeseries.py`, **exiftool**.

Use `summarize_dir.py` to inspect EXIF on processed output.

---

## stage_into_dirs.py

Stage tagged photos into team folders for publish and gallery delivery.

Second step after `process_queue.py`: copies files from `processed/` into `publish/` subdirectories named by `team` (or `handler`) keywords, and writes `clients.csv` with handler emails looked up from the timeseries file.

```text
stage_into_dirs.py [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--processed DIR` | `./processed` | Source directory of tagged photos |
| `--publish DIR` | `./publish` | Destination root for team/handler folders |
| `--timeline FILE` | `./eventname-ts.json` | Timeseries JSON for handler email lookup |
| `--download-prefix URL` | *(empty)* | Pixieset-style download URL prefix for `clients.csv` |
| `--level NUM` | `1` | `1` = team/handler folders under publish root; `2` = `publish/<photographer>/<team>/` using `X-photog:` or IPTC Creator |
| `--force` | | Always overwrite existing destination files |
| `--safe` | | Write to `_N` suffix paths instead of overwriting |
| `-h`, `--help` | | Show usage |

`--force` and `--safe` cannot be used together.

With no options, prints usage (same as `-h` / `--help`).

### Staging rules

1. Read `Keywords` from each file in `--processed` via exiftool.
2. Determine target folder name(s):
   - Prefer all distinct `team` values (a file with multiple teams is copied into each folder).
   - If no team but `dog` is present, skip (dog-only images are not staged).
   - Otherwise use `handler` values.
3. Copy files into `publish/<team-or-handler>/` (slashes in names become hyphens).
   - With `--level 2`, copy into `publish/<photographer>/<team-or-handler>/` instead. Photographer comes from `photographer` keywords, then IPTC `Creator`; missing values use `Unknown`.
4. Build `clients.csv` with columns `handler_name`, `handler_email`, `team_name`, `download`.
   - Handler emails come from `team_check_in` entries in the timeseries, with optional `email` keyword fallback.
   - Download URLs are built from `--download-prefix` and a slug derived from the team name.
5. Merge with any existing `clients.csv` in the publish directory.
6. Clear the processed directory after successful staging.

Without `--force`, files with identical MD5 at the destination are skipped. With `--safe`, conflicting filenames get `_1`, `_2`, … suffixes.

### Examples

```bash
stage_into_dirs.py --processed ./processed --publish ./publish \
  --timeline "Summer Splash 2026-ts.json"
```

```bash
stage_into_dirs.py --download-prefix "https://example.pixieset.com/gallery/" \
  --timeline event-ts.json
```

**Dependencies:** `docopt`, `_run_order_timeseries.py`, **exiftool**.

---

## summarize_dir.py

Print a human-readable EXIF summary of images in a directory tree.

Inspection utility for verifying `process_queue.py` output or reviewing staged `publish/` folders. Not part of the required processing pipeline.

```text
summarize_dir.py [<dir>]
```

| Argument / option | Default | Description |
|-------------------|---------|-------------|
| `<dir>` | *(required to scan)* | Root directory to scan |
| `-h`, `--help` | | Show usage |

With no options, prints usage (same as `-h` / `--help`).

Output is grouped by subdirectory. For each image:

- Filename, capture timestamp (same priority as `process_queue.py`), optional star rating
- Copyright line when present
- Camera model, serial, and lens
- Shutter, ISO, aperture, focal length
- IPTC Core fields when present (Headline, Creator, Credit, Source, Transmission Reference, Location, Subject)
- Keywords wrapped across lines (long keyword lists split for readability)

Non-image files and files exiftool cannot read are skipped silently. Naive EXIF timestamps use `NAIVE_TIMESTAMP_TIMEZONE` (default `America/Los_Angeles`).

### Examples

```bash
summarize_dir.py ./processed
summarize_dir.py ./publish
```

**Dependencies:** `docopt`, **exiftool**. No helper modules.

---

## Related files

| File | Role |
|------|------|
| `README.md` | This document |
| `google_qr_to_timeseries.py` | Google Sheet → timeseries JSON |
| `process_queue.py` | Queue → tagged processed photos |
| `stage_into_dirs.py` | Processed → publish folders + `clients.csv` |
| `summarize_dir.py` | Directory EXIF summary |
| `_run_order_timeseries.py` | Internal timeseries schema, migration, entry parsing |
| `../run_order.schema.json` | JSON Schema for v2 timeseries documents (optional validation) |
| `requirements.txt` | Python deps (`docopt`) |
