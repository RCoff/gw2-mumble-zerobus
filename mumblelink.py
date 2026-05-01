"""Read & parse the GW2 MumbleLink shared-memory block.

Layout reference: https://wiki.guildwars2.com/wiki/API:MumbleLink

LinkedMem (5460 bytes total):
    uint32_t  uiVersion
    uint32_t  uiTick
    float     fAvatarPosition[3]
    float     fAvatarFront[3]
    float     fAvatarTop[3]
    wchar_t   name[256]                # 512 bytes (UTF-16-LE)
    float     fCameraPosition[3]
    float     fCameraFront[3]
    float     fCameraTop[3]
    wchar_t   identity[256]            # 512 bytes, JSON
    uint32_t  context_len
    uint8_t   context[256]             # GW2 MumbleContext + padding
    wchar_t   description[2048]        # 4096 bytes

MumbleContext (85 bytes inside context[256]):
    uint8_t   serverAddress[28]        # sockaddr_in / sockaddr_in6
    uint32_t  mapId, mapType, shardId, instance, buildId, uiState
    uint16_t  compassWidth, compassHeight
    float     compassRotation
    float     playerX, playerY, mapCenterX, mapCenterY, mapScale
    uint32_t  processId
    uint8_t   mountIndex
"""

from __future__ import annotations

import json
import math
import mmap
import struct
import sys
from dataclasses import dataclass
from typing import Optional

LINKED_MEM_FORMAT = "<II3f3f3f512s3f3f3f512sI256s4096s"
LINKED_MEM_SIZE = struct.calcsize(LINKED_MEM_FORMAT)  # 5460

MUMBLE_CONTEXT_FORMAT = "<28s6I2H6fIB"
MUMBLE_CONTEXT_SIZE = struct.calcsize(MUMBLE_CONTEXT_FORMAT)  # 85

DEFAULT_WINDOWS_TAGNAME = "MumbleLink"
DEFAULT_LINUX_PATH = "/dev/shm/MumbleLink"


class MumbleLinkError(RuntimeError):
    pass


def _decode_wchar(buf: bytes) -> str:
    """Decode a UTF-16-LE wchar_t buffer, trimming at the first NUL pair."""
    end = len(buf)
    for i in range(0, len(buf) - 1, 2):
        if buf[i] == 0 and buf[i + 1] == 0:
            end = i
            break
    return buf[:end].decode("utf-16-le", errors="replace")


def _safe_float(value: float) -> Optional[float]:
    """Map NaN/Inf to None so the value JSON-serialises as null."""
    return value if math.isfinite(value) else None


def _safe_floats(values) -> list[Optional[float]]:
    return [_safe_float(v) for v in values]


@dataclass
class LinkedMem:
    ui_version: int
    ui_tick: int
    avatar_position: list[Optional[float]]
    avatar_front: list[Optional[float]]
    avatar_top: list[Optional[float]]
    name: str
    camera_position: list[Optional[float]]
    camera_front: list[Optional[float]]
    camera_top: list[Optional[float]]
    identity: dict
    context_len: int
    context: "MumbleContext"
    description: str


@dataclass
class MumbleContext:
    server_address: bytes  # raw 28 bytes; decoded via enrich.parse_server_address
    map_id: int
    map_type: int
    shard_id: int
    instance: int
    build_id: int
    ui_state: int
    compass_width: int
    compass_height: int
    compass_rotation: Optional[float]
    player_x: Optional[float]
    player_y: Optional[float]
    map_center_x: Optional[float]
    map_center_y: Optional[float]
    map_scale: Optional[float]
    process_id: int
    mount_index: int


def parse_mumble_context(buf: bytes) -> MumbleContext:
    if len(buf) < MUMBLE_CONTEXT_SIZE:
        raise MumbleLinkError(
            f"context buffer too small: {len(buf)} < {MUMBLE_CONTEXT_SIZE}"
        )
    (
        server_address,
        map_id,
        map_type,
        shard_id,
        instance,
        build_id,
        ui_state,
        compass_width,
        compass_height,
        compass_rotation,
        player_x,
        player_y,
        map_center_x,
        map_center_y,
        map_scale,
        process_id,
        mount_index,
    ) = struct.unpack(MUMBLE_CONTEXT_FORMAT, buf[:MUMBLE_CONTEXT_SIZE])
    return MumbleContext(
        server_address=server_address,
        map_id=map_id,
        map_type=map_type,
        shard_id=shard_id,
        instance=instance,
        build_id=build_id,
        ui_state=ui_state,
        compass_width=compass_width,
        compass_height=compass_height,
        compass_rotation=_safe_float(compass_rotation),
        player_x=_safe_float(player_x),
        player_y=_safe_float(player_y),
        map_center_x=_safe_float(map_center_x),
        map_center_y=_safe_float(map_center_y),
        map_scale=_safe_float(map_scale),
        process_id=process_id,
        mount_index=mount_index,
    )


def parse_linked_mem(buf: bytes) -> LinkedMem:
    if len(buf) < LINKED_MEM_SIZE:
        raise MumbleLinkError(
            f"shared memory too small: {len(buf)} < {LINKED_MEM_SIZE}"
        )
    (
        ui_version,
        ui_tick,
        ap_x, ap_y, ap_z,
        af_x, af_y, af_z,
        at_x, at_y, at_z,
        name_buf,
        cp_x, cp_y, cp_z,
        cf_x, cf_y, cf_z,
        ct_x, ct_y, ct_z,
        identity_buf,
        context_len,
        context_buf,
        description_buf,
    ) = struct.unpack(LINKED_MEM_FORMAT, buf[:LINKED_MEM_SIZE])

    identity_str = _decode_wchar(identity_buf)
    if identity_str:
        try:
            identity = json.loads(identity_str)
            if not isinstance(identity, dict):
                identity = {"raw": identity_str}
        except json.JSONDecodeError:
            identity = {"raw": identity_str}
    else:
        identity = {}

    return LinkedMem(
        ui_version=ui_version,
        ui_tick=ui_tick,
        avatar_position=_safe_floats([ap_x, ap_y, ap_z]),
        avatar_front=_safe_floats([af_x, af_y, af_z]),
        avatar_top=_safe_floats([at_x, at_y, at_z]),
        name=_decode_wchar(name_buf),
        camera_position=_safe_floats([cp_x, cp_y, cp_z]),
        camera_front=_safe_floats([cf_x, cf_y, cf_z]),
        camera_top=_safe_floats([ct_x, ct_y, ct_z]),
        identity=identity,
        context_len=context_len,
        context=parse_mumble_context(context_buf),
        description=_decode_wchar(description_buf),
    )


class MumbleLinkReader:
    """Open the MumbleLink shared memory and read snapshots.

    Use as a context manager so resources are released on exit:

        with MumbleLinkReader() as reader:
            data = reader.read()
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path
        self._mm: Optional[mmap.mmap] = None
        self._file = None

    def __enter__(self) -> "MumbleLinkReader":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        if sys.platform == "win32":
            tagname = self._path or DEFAULT_WINDOWS_TAGNAME
            try:
                # On Windows, an existing section is opened (or one is created
                # at this size) when fileno is -1.
                self._mm = mmap.mmap(-1, LINKED_MEM_SIZE, tagname)
            except OSError as e:
                raise MumbleLinkError(
                    f"failed to open Windows shared memory '{tagname}': {e}. "
                    "Is GW2 running with MumbleLink active?"
                ) from e
        else:
            path = self._path or DEFAULT_LINUX_PATH
            try:
                self._file = open(path, "rb")
                self._mm = mmap.mmap(
                    self._file.fileno(), 0, access=mmap.ACCESS_READ
                )
            except FileNotFoundError as e:
                raise MumbleLinkError(
                    f"MumbleLink shared memory not found at {path}. "
                    "Is GW2 running (under Wine/Proton on Linux)?"
                ) from e
            except OSError as e:
                raise MumbleLinkError(
                    f"failed to mmap {path}: {e}"
                ) from e

    def close(self) -> None:
        if self._mm is not None:
            try:
                self._mm.close()
            finally:
                self._mm = None
        if self._file is not None:
            try:
                self._file.close()
            finally:
                self._file = None

    def read(self) -> LinkedMem:
        if self._mm is None:
            raise MumbleLinkError("reader is not open")
        self._mm.seek(0)
        buf = self._mm.read(LINKED_MEM_SIZE)
        return parse_linked_mem(buf)
