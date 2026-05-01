"""Static lookup tables and decoders for GW2 MumbleLink enum-ish fields.

All sources: https://wiki.guildwars2.com/wiki/API:MumbleLink and the public GW2 API.
Values default to None when out of range so the table accepts them as NULL.
"""

from __future__ import annotations

import socket
import struct
from typing import Optional

PROFESSIONS = {
    1: "Guardian",
    2: "Warrior",
    3: "Engineer",
    4: "Ranger",
    5: "Thief",
    6: "Elementalist",
    7: "Mesmer",
    8: "Necromancer",
    9: "Revenant",
}

RACES = {
    0: "Asura",
    1: "Charr",
    2: "Human",
    3: "Norn",
    4: "Sylvari",
}

TEAM_COLORS = {
    0: "None",
    1: "Red",
    2: "Green",
    3: "Blue",
}

# Per https://wiki.guildwars2.com/wiki/API:MumbleLink#mapType
MAP_TYPES = {
    0: "AutoRedirect",
    1: "CharacterCreation",
    2: "PvP",
    3: "GvG",
    4: "Instance",
    5: "Public",
    6: "Tournament",
    7: "Tutorial",
    8: "UserTournament",
    9: "EternalBattlegrounds",
    10: "BlueBorderlands",
    11: "GreenBorderlands",
    12: "RedBorderlands",
    13: "FortunesVale",
    14: "ObsidianSanctum",
    15: "EOTM",
    16: "PublicMini",
    17: "BigBattle",
    18: "WvWLounge",
}

MOUNTS = {
    0: "None",
    1: "Jackal",
    2: "Griffon",
    3: "Springer",
    4: "Skimmer",
    5: "Raptor",
    6: "RollerBeetle",
    7: "Warclaw",
    8: "Skyscale",
    9: "Skiff",
    10: "SiegeTurtle",
}

# uiState bitmask bits, per the wiki
UI_STATE_BITS = {
    "ui_state_map_open": 0x01,
    "ui_state_compass_top_right": 0x02,
    "ui_state_compass_rotation_enabled": 0x04,
    "ui_state_game_focus": 0x08,
    "ui_state_competitive_mode": 0x10,
    "ui_state_textbox_focus": 0x20,
    "ui_state_in_combat": 0x40,
}


def profession_name(value: Optional[int]) -> Optional[str]:
    return PROFESSIONS.get(value) if value is not None else None


def race_name(value: Optional[int]) -> Optional[str]:
    return RACES.get(value) if value is not None else None


def team_color_name(value: Optional[int]) -> Optional[str]:
    return TEAM_COLORS.get(value) if value is not None else None


def map_type_name(value: Optional[int]) -> Optional[str]:
    return MAP_TYPES.get(value) if value is not None else None


def mount_name(value: Optional[int]) -> Optional[str]:
    return MOUNTS.get(value) if value is not None else None


def decode_ui_state(ui_state: int) -> dict:
    return {flag: bool(ui_state & mask) for flag, mask in UI_STATE_BITS.items()}


# Windows AF_INET6 differs from POSIX (Linux=10, Windows=23). Accept both so the
# script works whether GW2 runs on native Windows or Wine/Proton on Linux.
_AF_INET = 2
_AF_INET6_VALUES = {10, 23}


def parse_server_address(addr_bytes: bytes) -> tuple[Optional[str], Optional[int]]:
    """Decode the 28-byte sockaddr_in/sockaddr_in6 stored by GW2.

    Returns (ip, port) or (None, None) if the buffer is empty/unparseable.
    Port comes off the wire in network (big-endian) order.
    """
    if not addr_bytes or len(addr_bytes) < 8 or addr_bytes[:2] == b"\x00\x00":
        return None, None

    family = struct.unpack_from("<H", addr_bytes, 0)[0]
    port = struct.unpack_from(">H", addr_bytes, 2)[0]

    if family == _AF_INET:
        ip = socket.inet_ntop(socket.AF_INET, addr_bytes[4:8])
        return ip, port
    if family in _AF_INET6_VALUES:
        ip = socket.inet_ntop(socket.AF_INET6, addr_bytes[8:24])
        return ip, port
    return None, None
