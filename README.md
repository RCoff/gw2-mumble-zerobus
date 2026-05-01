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
| `mumblelink.py`   | Reader + binary parser for the 5460-byte LinkedMem block   |
| `enrich.py`       | Decoders for profession/race/mount/map_type/uiState/sockaddr |
| `gw2_zerobus.py`  | CLI: poll → flatten → ingest                               |
| `create_table.sql`| Unity Catalog DDL (every MumbleLink field + enrichment)    |
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
| `--dry-run`                         | Parse + log records, skip the ZeroBus stream                 |
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

## Verifying the parser without GW2

`_smoketest.py` builds a synthetic MumbleLink buffer and round-trips it
through the parser, the enum decoders, and the row flattener — and asserts
the emitted column names match the SQL DDL exactly.

```bash
.venv/bin/python _smoketest.py
# OK: parser + enrichment + flatten + SQL column parity
#   record keys: 65
```

You can also run `--dry-run` against a synthetic file:

```bash
.venv/bin/python -c "from _smoketest import build_buffer; open('/tmp/ml.bin','wb').write(build_buffer())"
.venv/bin/python gw2_zerobus.py --dry-run --mumblelink-path /tmp/ml.bin --poll-hz 5
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
