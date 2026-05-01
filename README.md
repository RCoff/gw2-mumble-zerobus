# GW2 → Databricks ZeroBus

Streams Guild Wars 2 player position (and every other field exposed by the
[MumbleLink](https://wiki.guildwars2.com/wiki/API:MumbleLink) shared-memory
API) into a Unity Catalog table via the
[Databricks Zerobus Ingest SDK](https://github.com/databricks/zerobus-sdk-py).

```
GW2 (writes shared mem) ──► mumblelink.py ──► flatten ──► ZeroBus stream ──► UC table
                            (parse C struct)              (JSON record type)
```

Works on Windows (where GW2 actually runs) and on Linux when GW2 is launched
under Wine/Proton. macOS is supported only as a development target — GW2 has
no native Mac client, so on macOS you can run `_smoketest.py` and `--dry-run`
mode but you cannot read a live game.

---

## Files

| File              | What it is                                                 |
|-------------------|------------------------------------------------------------|
| `mumblelink.py`     | Reader + binary parser for the 5460-byte LinkedMem block   |
| `enrich.py`         | Decoders for profession/race/mount/map_type/uiState/sockaddr |
| `gw2_zerobus.py`    | CLI: poll → flatten → ingest                               |
| `gw2_map_overlay.py`| Tk window — draws the live player position on GW2 map tiles |
| `create_table.sql`  | Unity Catalog DDL (every MumbleLink field + enrichment)    |
| `requirements.txt`| Python deps                                                |
| `.env.example`    | Config template — copy to `.env`                           |
| `_smoketest.py`   | Offline parser/flatten/SQL parity check                    |

---

## Setup

### 1. Install Python deps

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Create the target Unity Catalog table

In a Databricks SQL editor (or via `databricks sql`):

```sql
-- Edit create_table.sql to set <catalog>.<schema>, then run it.
```

The table is `gw2_player_position` with 65 columns covering every
MumbleLink field plus decoded enums.

### 3. Create a service principal and grant it permission

In your workspace UI:

1. **Settings → Identity and access → Service principals → Manage → Add service principal → Add new**
2. Generate and **save the client ID and client secret** (you can't see the secret again)
3. Copy the **Application Id** (UUID) from the Configurations tab

Grant write access:

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<application-id>`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema> TO `<application-id>`;
GRANT MODIFY, SELECT ON TABLE <catalog>.<schema>.gw2_player_position TO `<application-id>`;
```

### 4. Find your ZeroBus endpoint

The endpoint URL has the form:

```
https://<workspace-id>.zerobus.<region>.cloud.databricks.com
```

Where `<workspace-id>` is the numeric ID from your workspace URL and
`<region>` matches your workspace cloud region (e.g. `us-west-2`,
`eu-west-1`). If you're not sure, see
[Get started with Zerobus Ingest](https://docs.databricks.com/aws/en/ingestion/zerobus-overview).

### 5. Configure environment

```bash
cp .env.example .env
$EDITOR .env
```

Fill in:

- `DATABRICKS_WORKSPACE_URL` — `https://dbc-...cloud.databricks.com`
- `ZEROBUS_ENDPOINT`         — see step 4
- `DATABRICKS_CLIENT_ID`     — from step 3
- `DATABRICKS_CLIENT_SECRET` — from step 3
- `ZEROBUS_TABLE`            — `<catalog>.<schema>.gw2_player_position`

`.env` is git-ignored.

---

## Run

On the same machine that runs Guild Wars 2:

```bash
.venv/bin/python gw2_zerobus.py
```

Stop with Ctrl-C — the stream is flushed and closed gracefully so in-flight
records are acknowledged before exit.

### Useful flags

| Flag                                | Effect                                                       |
|-------------------------------------|--------------------------------------------------------------|
| `--poll-hz 5`                       | Poll rate (default 10 Hz; MumbleLink updates ~60 Hz)         |
| `--no-dedupe`                       | Send every sample even when `uiTick` hasn't advanced         |
| `--dry-run`                         | Parse + log the full record, skip the ZeroBus stream         |
| `--pretty`                          | With `--dry-run`, format the JSON record indented + sorted   |
| `--mumblelink-path /dev/shm/...`    | Override shared-memory path (Linux) or tagname (Windows)     |
| `-v / --verbose`                    | DEBUG logging                                                |

Sample output:

```
2026-05-01 15:42:01 INFO  starting: table=main.gw2.gw2_player_position endpoint=https://… poll_hz=10.00 dedupe=True dry_run=False
2026-05-01 15:42:01 INFO  ZeroBus stream open
2026-05-01 15:42:11 INFO  sent=100 skipped_dupes=0 last_tick=812334
…
^C
2026-05-01 15:43:22 INFO  flushing & closing ZeroBus stream (sent=712)
2026-05-01 15:43:22 INFO  done. sent=712 skipped_dupes=0
```

---

## Dry-run mode

`--dry-run` reads MumbleLink, parses, and flattens exactly as the live mode
does, but writes the record to the log instead of to ZeroBus. No SDK calls
are made and no Databricks credentials are required, so this works even
with an empty `.env`.

Each polled tick produces two log lines: a one-line summary plus the full
JSON record that *would* have been sent. Use `--pretty` to get an indented,
key-sorted version of the JSON instead.

```bash
# Compact (one log line per record, easy to pipe into a file or jq):
.venv/bin/python gw2_zerobus.py --dry-run

# Pretty (indented JSON, easy to skim with your eyes):
.venv/bin/python gw2_zerobus.py --dry-run --pretty
```

Sample compact output:

```
INFO  starting: table=<dry-run> endpoint=<dry-run> poll_hz=10.00 dedupe=True dry_run=True
INFO  DRY-RUN: no records will be sent to ZeroBus
INFO  [dry-run] tick=812334 player='Ridgeward' prof=Guardian map_id=38 map_type=Public mount=Raptor in_combat=False pos=(100.50, 200.50, -50.25)
INFO  [dry-run] {"event_timestamp": "2026-05-01T...", "ui_version": 2, "ui_tick": 812334, ...65 fields total...}
```

Pipe to `jq` for ad-hoc filtering — e.g. extract the position trail:

```bash
.venv/bin/python gw2_zerobus.py --dry-run 2>&1 \
  | sed -n 's/.*\[dry-run\] \({.*\)/\1/p' \
  | jq -c '{ui_tick, x: .player_continent_x, y: .player_continent_y, map_id}'
```

## Map overlay window

`gw2_map_overlay.py` is a separate Tk app that reads MumbleLink (just like
the streamer does) and renders the live player position on top of the
official GW2 map tiles. Run it standalone, or in parallel with
`gw2_zerobus.py` — both processes can read MumbleLink concurrently without
conflict.

```bash
.venv/bin/python gw2_map_overlay.py
```

What it does:

- Resolves the current `map_id` to `continent_id` + `default_floor` via
  `https://api.guildwars2.com/v2/maps/{id}`.
- Pulls map tiles from `https://tiles.guildwars2.com/{cont}/{floor}/{zoom}/{x}/{y}.jpg`.
- Composites a viewport centered on the player and draws a marker at the
  player's continent coordinates.
- Caches everything to disk under `~/.cache/gw2_map_overlay/` (tiles
  never change once published, so a cache hit is final).
- Background thread does HTTP fetches; the UI thread never blocks waiting
  for tiles, so a map change isn't a freeze.

All sampled positions on the current map are drawn as a yellow trail
behind the player marker. There's one trail kept per `map_id` (re-visiting
a map preserves history; switching to a new map starts fresh). The trail
is bounded — when full the oldest point drops off — and points closer than
`--trail-step` continent-units to the previous one are skipped, so
standing still doesn't fill the buffer.

In-window keyboard shortcuts:

| Key   | Effect                            |
|-------|-----------------------------------|
| `+`   | Zoom in                           |
| `-`   | Zoom out                          |
| `c`   | Clear the current map's trail     |
| `q`   | Quit                              |

CLI flags: `--zoom N` (initial zoom 0..max-for-continent),
`--width W --height H`, `--poll-hz N`, `--mumblelink-path PATH`,
`--cache-dir DIR`, `--trail-cap N` (max points per map; default 5000),
`--trail-step UNITS` (min movement before a new point; default 5),
`--no-trail`, `-v`.

### Tk dependency

Tkinter is a *system* package, not a pip package. Most Python installs
include it, but Homebrew's macOS Python doesn't:

| Platform                         | Install                            |
|----------------------------------|------------------------------------|
| Windows (python.org installer)   | included                           |
| Linux (Debian/Ubuntu)            | `sudo apt-get install python3-tk`  |
| macOS (Homebrew Python 3.14)     | `brew install python-tk@3.14`      |
| macOS (python.org installer)     | included                           |

Pillow is in `requirements.txt`; that one's a normal pip install.

## Verifying the parser without GW2

`_smoketest.py` builds a synthetic MumbleLink buffer and round-trips it
through the parser, the enum decoders, and the row flattener — and asserts
the emitted column names match the SQL DDL exactly.

```bash
.venv/bin/python _smoketest.py
# OK: parser + enrichment + flatten + SQL column parity
#   record keys: 65
```

You can also run `--dry-run` against a synthetic file (so you can see
output without GW2 running at all):

```bash
.venv/bin/python -c "from _smoketest import build_buffer; open('/tmp/ml.bin','wb').write(build_buffer())"
.venv/bin/python gw2_zerobus.py --dry-run --pretty --mumblelink-path /tmp/ml.bin --poll-hz 1
```

---

## Field map

Each row written to `gw2_player_position` has these groups of columns
(see `create_table.sql` for the full list):

| Group       | Columns                                                                 |
|-------------|-------------------------------------------------------------------------|
| Metadata    | `event_timestamp`, `ui_version`, `ui_tick`                              |
| Game info   | `game_name`, `description`                                              |
| Avatar      | `avatar_pos_{x,y,z}`, `avatar_front_{x,y,z}`, `avatar_top_{x,y,z}`      |
| Camera      | `camera_pos_{x,y,z}`, `camera_front_{x,y,z}`, `camera_top_{x,y,z}`      |
| Identity    | `player_name`, `profession`(+`_name`), `spec`, `race`(+`_name`), `identity_map_id`, `world_id`, `team_color_id`(+`_name`), `commander`, `fov`, `uisz` |
| GW2 context | `map_id`, `map_type`(+`_name`), `shard_id`, `instance`, `build_id`, `process_id`, `mount_index`(+`_name`) |
| Server      | `server_address_raw` (hex), `server_ip`, `server_port`                  |
| UI state    | `ui_state` (raw bitmask) + 7 boolean flags (`map_open`, `compass_top_right`, `compass_rotation_enabled`, `game_focus`, `competitive_mode`, `textbox_focus`, `in_combat`) |
| Compass/map | `compass_width`, `compass_height`, `compass_rotation`, `player_continent_x/y`, `map_center_x/y`, `map_scale` |

`avatar_*` and `camera_*` values are MumbleLink units (meters).
`player_continent_*` are in GW2 continent coordinates.

---

## Notes & gotchas

- **uiTick=0** frames are skipped — GW2 sets the tick to 0 until the player
  has loaded into a map.
- **NaN/Inf floats** are converted to `null` (e.g. on character select
  before the world finishes loading).
- **Windows tagname**: Python's `mmap.mmap(-1, size, "MumbleLink")` opens
  the existing section if GW2 is running, or creates an empty one if not —
  so reading immediately after launch may show `uiVersion=0`. Wait until
  in-game.
- **Wine/Proton**: GW2 maps the section to `/dev/shm/MumbleLink`. If your
  Wine prefix maps it elsewhere, set `MUMBLELINK_PATH` in `.env`.
- **Throughput**: At 10 Hz with `dedupe_by_tick=true`, expect ≈600 rows/min
  while in-game, less while idle. The ZeroBus SDK ack-asynchronously and
  buffers internally; one Python process can comfortably do 1000+ records/s
  if you crank `--poll-hz` and `--no-dedupe`.
- **Protobuf**: This sample uses `RecordType.JSON` for clarity. For
  production / high throughput, swap to `RecordType.PROTO` — the SDK ships
  a `python -m zerobus.tools.generate_proto` helper that auto-generates the
  `.proto` from the UC table.
