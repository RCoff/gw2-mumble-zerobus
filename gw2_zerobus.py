"""GW2 → Databricks ZeroBus streaming bridge.

Reads the MumbleLink shared memory at a configurable rate, flattens each
sample to a JSON row matching the Unity Catalog table in create_table.sql,
and ingests it via the official Databricks ZeroBus Python SDK.

Run:
    python gw2_zerobus.py
Stop with Ctrl+C; the stream is closed cleanly so in-flight records are
acknowledged before exit.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

import enrich
from mumblelink import LinkedMem, MumbleLinkError, MumbleLinkReader
from session import SessionInfo, SessionTracker

LOGGER = logging.getLogger("gw2_zerobus")


def flatten_sample(
    sample: LinkedMem,
    *,
    session: Optional[SessionInfo] = None,
) -> dict:
    """Convert a parsed LinkedMem into a flat dict matching the UC table.

    `session`, when provided, contributes the synthetic character-session
    columns. It's optional so older callers / tests can still flatten
    without instantiating a SessionTracker.
    """
    identity = sample.identity or {}
    ctx = sample.context

    server_ip, server_port = enrich.parse_server_address(ctx.server_address)
    ui_state_flags = enrich.decode_ui_state(ctx.ui_state)

    record = {
        "event_timestamp": datetime.now(timezone.utc).isoformat(),
        "character_session_id": session.session_id if session else None,
        "character_session_start_ts": session.session_start_iso if session else None,
        "ui_version": sample.ui_version,
        "ui_tick": sample.ui_tick,

        "game_name": sample.name or None,
        "description": sample.description or None,

        "avatar_pos_x": sample.avatar_position[0],
        "avatar_pos_y": sample.avatar_position[1],
        "avatar_pos_z": sample.avatar_position[2],
        "avatar_front_x": sample.avatar_front[0],
        "avatar_front_y": sample.avatar_front[1],
        "avatar_front_z": sample.avatar_front[2],
        "avatar_top_x": sample.avatar_top[0],
        "avatar_top_y": sample.avatar_top[1],
        "avatar_top_z": sample.avatar_top[2],

        "camera_pos_x": sample.camera_position[0],
        "camera_pos_y": sample.camera_position[1],
        "camera_pos_z": sample.camera_position[2],
        "camera_front_x": sample.camera_front[0],
        "camera_front_y": sample.camera_front[1],
        "camera_front_z": sample.camera_front[2],
        "camera_top_x": sample.camera_top[0],
        "camera_top_y": sample.camera_top[1],
        "camera_top_z": sample.camera_top[2],

        "player_name": identity.get("name"),
        "profession": identity.get("profession"),
        "profession_name": enrich.profession_name(identity.get("profession")),
        "spec": identity.get("spec"),
        "race": identity.get("race"),
        "race_name": enrich.race_name(identity.get("race")),
        "identity_map_id": identity.get("map_id"),
        "world_id": identity.get("world_id"),
        "team_color_id": identity.get("team_color_id"),
        "team_color_name": enrich.team_color_name(identity.get("team_color_id")),
        "commander": identity.get("commander"),
        "fov": identity.get("fov"),
        "uisz": identity.get("uisz"),

        "context_len": sample.context_len,
        "server_address_raw": ctx.server_address.hex(),
        "server_ip": server_ip,
        "server_port": server_port,
        "map_id": ctx.map_id,
        "map_type": ctx.map_type,
        "map_type_name": enrich.map_type_name(ctx.map_type),
        "shard_id": ctx.shard_id,
        "instance": ctx.instance,
        "build_id": ctx.build_id,
        "ui_state": ctx.ui_state,
        **ui_state_flags,
        "compass_width": ctx.compass_width,
        "compass_height": ctx.compass_height,
        "compass_rotation": ctx.compass_rotation,
        "player_continent_x": ctx.player_x,
        "player_continent_y": ctx.player_y,
        "map_center_x": ctx.map_center_x,
        "map_center_y": ctx.map_center_y,
        "map_scale": ctx.map_scale,
        "process_id": ctx.process_id,
        "mount_index": ctx.mount_index,
        "mount_name": enrich.mount_name(ctx.mount_index),
    }
    return record


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(
            f"Missing required env var {name}. Copy .env.example to .env and fill it in."
        )
    return value


def _record_summary(record: dict) -> str:
    """One-line at-a-glance summary of a flattened record."""
    px = record.get("avatar_pos_x")
    py = record.get("avatar_pos_y")
    pz = record.get("avatar_pos_z")
    pos = f"({px:.2f}, {py:.2f}, {pz:.2f})" if None not in (px, py, pz) else "(?, ?, ?)"
    sid = record.get("character_session_id") or "-"
    return (
        f"tick={record.get('ui_tick')} "
        f"sid={sid} "
        f"player={record.get('player_name')!r} "
        f"prof={record.get('profession_name')} "
        f"map_id={record.get('map_id')} "
        f"map_type={record.get('map_type_name')} "
        f"mount={record.get('mount_name')} "
        f"in_combat={record.get('ui_state_in_combat')} "
        f"pos={pos}"
    )


class _ShutdownFlag:
    def __init__(self) -> None:
        self._stop = False

    def trigger(self, *_args) -> None:
        self._stop = True

    def __bool__(self) -> bool:
        return self._stop


def _open_output_file(path: Optional[str]):
    """Open a JSONL output sink. Returns (file_obj, label) or (None, None).

    `path == "-"` returns sys.stdout (so JSONL pipes cleanly into jq, etc.).
    Any real path is opened in append mode + line-buffered so a crash never
    drops the most recent record.
    """
    if not path:
        return None, None
    if path == "-":
        return sys.stdout, "stdout"
    fp = open(path, "a", buffering=1, encoding="utf-8")
    return fp, path


def run(
    *,
    poll_hz: float,
    dedupe_by_tick: bool,
    mumblelink_path: Optional[str],
    workspace_url: str,
    zerobus_endpoint: str,
    client_id: str,
    client_secret: str,
    table: str,
    dry_run: bool = False,
    pretty: bool = False,
    output_file: Optional[str] = None,
) -> None:
    # Imported lazily so --dry-run works without the SDK installed.
    if not dry_run:
        from zerobus.sdk.shared import RecordType, StreamConfigurationOptions, TableProperties
        from zerobus.sdk.sync import ZerobusSdk

    poll_interval = 1.0 / poll_hz if poll_hz > 0 else 0.0
    shutdown = _ShutdownFlag()
    signal.signal(signal.SIGINT, shutdown.trigger)
    signal.signal(signal.SIGTERM, shutdown.trigger)

    output_fp, output_label = _open_output_file(output_file)

    LOGGER.info(
        "starting: table=%s endpoint=%s poll_hz=%.2f dedupe=%s dry_run=%s output=%s",
        table or "<dry-run>",
        zerobus_endpoint or "<dry-run>",
        poll_hz, dedupe_by_tick, dry_run,
        output_label or "<none>",
    )
    if dry_run:
        LOGGER.info("DRY-RUN: no records will be sent to ZeroBus")

    last_tick: Optional[int] = None
    sent = 0
    skipped_dupes = 0
    tracker = SessionTracker()

    with MumbleLinkReader(mumblelink_path) as reader:
        stream = None
        try:
            if not dry_run:
                sdk = ZerobusSdk(zerobus_endpoint, workspace_url)
                table_properties = TableProperties(table)
                options = StreamConfigurationOptions(record_type=RecordType.JSON)
                stream = sdk.create_stream(
                    client_id, client_secret, table_properties, options
                )
                LOGGER.info("ZeroBus stream open")

            while not shutdown:
                loop_start = time.monotonic()

                try:
                    sample = reader.read()
                except MumbleLinkError as e:
                    LOGGER.warning("read failed: %s", e)
                    time.sleep(min(2.0, max(poll_interval, 0.5)))
                    continue

                # Always run the session tracker, even on uiTick=0, so it can
                # observe the logout/login boundary; it returns None for those
                # frames and we skip downstream work.
                session = tracker.update(sample)
                if session is None:
                    if poll_interval:
                        time.sleep(poll_interval)
                    continue

                if dedupe_by_tick and sample.ui_tick == last_tick:
                    skipped_dupes += 1
                    if poll_interval:
                        time.sleep(poll_interval)
                    continue
                last_tick = sample.ui_tick

                record = flatten_sample(sample, session=session)

                # Write to file BEFORE sending — that way a failed ZeroBus
                # call still leaves an authoritative local record we can
                # replay later.
                if output_fp is not None:
                    output_fp.write(json.dumps(record, default=str) + "\n")

                if dry_run:
                    summary = _record_summary(record)
                    payload = json.dumps(
                        record,
                        default=str,
                        indent=2 if pretty else None,
                        sort_keys=pretty,
                    )
                    if pretty:
                        LOGGER.info("[dry-run] %s\n%s", summary, payload)
                    else:
                        LOGGER.info("[dry-run] %s", summary)
                        LOGGER.info("[dry-run] %s", payload)
                else:
                    stream.ingest_record_offset(record)
                sent += 1
                if sent % 100 == 0:
                    LOGGER.info("sent=%d skipped_dupes=%d last_tick=%s",
                                sent, skipped_dupes, last_tick)

                elapsed = time.monotonic() - loop_start
                remaining = poll_interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)

        finally:
            if stream is not None:
                LOGGER.info("flushing & closing ZeroBus stream (sent=%d)", sent)
                try:
                    stream.close()
                except Exception as e:  # noqa: BLE001 - best-effort shutdown
                    LOGGER.warning("stream.close() raised: %s", e)
            if output_fp is not None and output_fp is not sys.stdout:
                try:
                    output_fp.close()
                except Exception as e:  # noqa: BLE001
                    LOGGER.warning("output_file.close() raised: %s", e)
            LOGGER.info("done. sent=%d skipped_dupes=%d", sent, skipped_dupes)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream GW2 MumbleLink samples to a Databricks ZeroBus table.",
    )
    parser.add_argument("--poll-hz", type=float, default=None,
                        help="Poll rate in Hz (default: $POLL_HZ or 10)")
    parser.add_argument("--no-dedupe", action="store_true",
                        help="Send every sample, even if uiTick has not advanced")
    parser.add_argument("--dry-run", action="store_true",
                        help="Read MumbleLink and log the full record; skip ZeroBus")
    parser.add_argument("--pretty", action="store_true",
                        help="In --dry-run, log the JSON record indented & sorted")
    parser.add_argument("--mumblelink-path", default=None,
                        help="Override shared-memory path (Linux) or tagname (Windows)")
    parser.add_argument("-o", "--output-file", default=None,
                        help="Write each record as a JSONL line to PATH "
                             "(append mode). Use '-' for stdout. Works with "
                             "or without --dry-run; written before ZeroBus "
                             "send so failed ingests still leave a record.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable DEBUG logging")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    args = parse_args(argv if argv is not None else sys.argv[1:])

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    poll_hz = args.poll_hz
    if poll_hz is None:
        poll_hz = float(os.environ.get("POLL_HZ", "10"))

    dedupe = not args.no_dedupe and (
        os.environ.get("DEDUPE_BY_TICK", "true").lower() != "false"
    )

    mumblelink_path = args.mumblelink_path or os.environ.get("MUMBLELINK_PATH") or None

    if args.dry_run:
        run(
            poll_hz=poll_hz,
            dedupe_by_tick=dedupe,
            mumblelink_path=mumblelink_path,
            workspace_url="",
            zerobus_endpoint="",
            client_id="",
            client_secret="",
            table="",
            dry_run=True,
            pretty=args.pretty,
            output_file=args.output_file,
        )
        return 0

    if args.pretty:
        LOGGER.warning("--pretty has no effect without --dry-run; ignoring")

    run(
        poll_hz=poll_hz,
        dedupe_by_tick=dedupe,
        mumblelink_path=mumblelink_path,
        workspace_url=_require_env("DATABRICKS_WORKSPACE_URL"),
        zerobus_endpoint=_require_env("ZEROBUS_ENDPOINT"),
        client_id=_require_env("DATABRICKS_CLIENT_ID"),
        client_secret=_require_env("DATABRICKS_CLIENT_SECRET"),
        table=_require_env("ZEROBUS_TABLE"),
        output_file=args.output_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
