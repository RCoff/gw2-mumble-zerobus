"""Build a synthetic LinkedMem buffer and verify parser + flatten round-trip."""

import json
import re
import struct
import sys
import types
from pathlib import Path

# Shim python-dotenv so we can import gw2_zerobus without installing it.
if "dotenv" not in sys.modules:
    stub = types.ModuleType("dotenv")
    stub.load_dotenv = lambda *a, **k: None
    sys.modules["dotenv"] = stub

import enrich
import gw2_zerobus
import mumblelink as ml


def build_buffer() -> bytes:
    identity = json.dumps({
        "name": "Ridgeward",
        "profession": 1,
        "spec": 27,
        "race": 3,
        "map_id": 38,
        "world_id": 268435511,
        "team_color_id": 0,
        "commander": False,
        "fov": 0.873,
        "uisz": 1,
    })
    identity_bytes = identity.encode("utf-16-le").ljust(512, b"\x00")[:512]
    name_bytes = "Guild Wars 2".encode("utf-16-le").ljust(512, b"\x00")[:512]
    description_bytes = b"\x00" * 4096

    server_addr = struct.pack("<H", 2) + struct.pack(">H", 6112) + bytes([97, 105, 110, 111]) + b"\x00" * 20
    assert len(server_addr) == 28

    context = struct.pack(
        ml.MUMBLE_CONTEXT_FORMAT,
        server_addr,
        38, 5, 12, 0, 180000, 0x09,
        362, 362,
        0.0,
        25896.4, 19112.7, 25000.0, 19000.0, 1.0,
        4242,
        5,
    )
    context_padded = context.ljust(256, b"\x00")[:256]

    return struct.pack(
        ml.LINKED_MEM_FORMAT,
        2,
        123456,
        100.5, 200.5, -50.25,
        0.0, 0.0, 1.0,
        0.0, 1.0, 0.0,
        name_bytes,
        110.0, 215.0, -45.0,
        0.0, -0.1, 0.99,
        0.0, 1.0, 0.0,
        identity_bytes,
        ml.MUMBLE_CONTEXT_SIZE,
        context_padded,
        description_bytes,
    )


def columns_from_sql(path: Path) -> set[str]:
    text = path.read_text()
    # Strip line comments first so parens in header comments don't confuse the
    # body regex below.
    text = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    body = re.search(
        r"CREATE\s+TABLE[^()]*\((.*?)\)\s*USING\s+DELTA", text, re.S | re.I
    ).group(1)
    cols = set()
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s+", line)
        if m:
            cols.add(m.group(1).lower())
    return cols


def main() -> None:
    assert ml.LINKED_MEM_SIZE == 5460
    assert ml.MUMBLE_CONTEXT_SIZE == 85

    buf = build_buffer()
    parsed = ml.parse_linked_mem(buf)

    assert parsed.ui_version == 2
    assert parsed.ui_tick == 123456
    assert parsed.name == "Guild Wars 2", repr(parsed.name)
    assert parsed.identity["name"] == "Ridgeward"
    assert parsed.context.map_id == 38
    assert parsed.context.mount_index == 5

    flags = enrich.decode_ui_state(parsed.context.ui_state)
    assert flags["ui_state_map_open"] is True
    assert flags["ui_state_game_focus"] is True
    assert flags["ui_state_in_combat"] is False

    ip, port = enrich.parse_server_address(parsed.context.server_address)
    assert ip == "97.105.110.111"
    assert port == 6112

    record = gw2_zerobus.flatten_sample(parsed)
    expected = {
        "player_name": "Ridgeward",
        "profession_name": "Guardian",
        "race_name": "Norn",
        "map_type_name": "Public",
        "mount_name": "Raptor",
        "server_ip": "97.105.110.111",
        "server_port": 6112,
        "avatar_pos_x": 100.5,
        "ui_state_map_open": True,
    }
    for k, v in expected.items():
        assert record[k] == v, f"{k}: got {record[k]!r} want {v!r}"

    # JSON-serializable
    serialized = json.dumps(record, default=str)
    assert "Ridgeward" in serialized

    # Column-name consistency between SQL DDL and emitted record
    sql_cols = columns_from_sql(Path(__file__).with_name("create_table.sql"))
    record_cols = {k.lower() for k in record.keys()}
    extra_in_record = record_cols - sql_cols
    missing_in_record = sql_cols - record_cols
    assert not extra_in_record, f"record has columns not in SQL: {extra_in_record}"
    assert not missing_in_record, f"SQL has columns not in record: {missing_in_record}"

    print("OK: parser + enrichment + flatten + SQL column parity")
    print(f"  record keys: {len(record)}")


if __name__ == "__main__":
    main()
