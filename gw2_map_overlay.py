"""Live GW2 map overlay window.

Reads MumbleLink (same as gw2_zerobus.py), pulls the relevant tiles from the
public GW2 tile server, and renders a Tk window that follows the player as
they move around. Run alongside gw2_zerobus.py — both processes can read
MumbleLink concurrently without conflict.

Coordinate math:
    Continent coordinates from MumbleContext.player{X,Y} are pixel offsets
    at the *maximum* zoom level for the continent. To render at a lower
    zoom level Z, divide by 2^(max_zoom - Z). Tiles are fixed at 256x256,
    so the tile that contains a given pixel is at (pixel // 256).

    Tile URL: https://tiles.guildwars2.com/{cont}/{floor}/{zoom}/{x}/{y}.jpg
    Map meta: https://api.guildwars2.com/v2/maps/{map_id}  (continent_id, default_floor)
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import queue
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageTk

import enrich
from mumblelink import LinkedMem, MumbleLinkError, MumbleLinkReader

LOG = logging.getLogger("gw2_map_overlay")

TILE_SIZE = 256
TILE_URL = "https://tiles.guildwars2.com/{continent}/{floor}/{zoom}/{x}/{y}.jpg"
MAP_API_URL = "https://api.guildwars2.com/v2/maps/{map_id}"
CONTINENT_API_URL = "https://api.guildwars2.com/v2/continents/{continent_id}"

# Continents are NOT square 2^zoom on a side. Tyria's pixel canvas at max zoom
# is 81920x114688 — taller than wide — and the Mists is 16384x16384. The valid
# tile range therefore depends on `continent_dims` (fetched at runtime), not
# on `2 ** zoom`. We also fetch min_zoom/max_zoom from the API so a future
# continent revision doesn't silently render blank.

DEFAULT_CACHE_DIR = Path.home() / ".cache" / "gw2_map_overlay"
DEFAULT_ZOOM = 4
DEFAULT_WINDOW = (768, 768)

# Trail defaults — one trail per map_id, capped to keep memory bounded.
DEFAULT_TRAIL_CAP = 5000          # ~8 minutes of unique positions at 10 Hz
DEFAULT_TRAIL_STEP = 5.0          # min continent-units between successive points
TRAIL_LINE_COLOR = (255, 220, 50, 230)
TRAIL_DOT_COLOR = (255, 220, 50, 220)


# ---------------------------------------------------------------------------
# HTTP helpers (used from a worker thread; main UI thread never blocks)
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: float = 6.0) -> Optional[bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.URLError as e:
        LOG.warning("GET %s failed: %s", url, e)
    except Exception as e:  # noqa: BLE001
        LOG.warning("GET %s raised: %s", url, e)
    return None


class _JsonCache:
    """Tiny shared base: in-memory + on-disk JSON-per-key cache."""

    def __init__(self, cache_dir: Path, subdir: str) -> None:
        self._mem: dict[int, Optional[dict]] = {}
        self._dir = cache_dir / subdir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _load_or_fetch(self, key: int, url: str) -> Optional[dict]:
        if key in self._mem:
            return self._mem[key]
        path = self._dir / f"{key}.json"
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self._mem[key] = data
                return data
            except Exception:  # noqa: BLE001 - corrupt cache, refetch
                path.unlink(missing_ok=True)

        body = _http_get(url)
        if body is None:
            self._mem[key] = None
            return None
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self._mem[key] = None
            return None
        path.write_text(json.dumps(data))
        self._mem[key] = data
        return data


class MapMetadata(_JsonCache):
    """Resolves map_id -> {continent_id, default_floor, name, region_name, ...}."""

    def __init__(self, cache_dir: Path) -> None:
        super().__init__(cache_dir, "maps")

    def get(self, map_id: int) -> Optional[dict]:
        return self._load_or_fetch(map_id, MAP_API_URL.format(map_id=map_id))


class ContinentInfo(_JsonCache):
    """Resolves continent_id -> {continent_dims: [w, h], min_zoom, max_zoom, name}.

    Used to compute the valid tile range at any zoom (a continent is not
    necessarily 2^zoom * 2^zoom in tiles).
    """

    def __init__(self, cache_dir: Path) -> None:
        super().__init__(cache_dir, "continents")

    def get(self, continent_id: int) -> Optional[dict]:
        return self._load_or_fetch(
            continent_id, CONTINENT_API_URL.format(continent_id=continent_id)
        )

    def tile_count(self, continent_id: int, zoom: int) -> tuple[int, int]:
        """How many tiles wide and tall this continent is at the given zoom."""
        info = self.get(continent_id)
        if not info:
            # Defensive fallback: assume square 2^zoom * 2^zoom
            n = 2 ** zoom
            return n, n
        max_zoom = int(info.get("max_zoom", 7))
        w, h = info.get("continent_dims", [256 * 2 ** max_zoom] * 2)
        scale = 2 ** (max_zoom - zoom)
        # ceil-div so the final partial tile on the right/bottom is included
        return ((w + scale * TILE_SIZE - 1) // (scale * TILE_SIZE),
                (h + scale * TILE_SIZE - 1) // (scale * TILE_SIZE))


class TileCache:
    """Disk-backed tile cache. PIL.Image objects are returned by `get()`.

    Tiles never change once published, so a cache hit is final — no TTL.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._dir = cache_dir / "tiles"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, continent: int, floor: int, zoom: int, x: int, y: int) -> Path:
        return self._dir / f"{continent}_{floor}_{zoom}_{x}_{y}.jpg"

    def get(self, continent: int, floor: int, zoom: int, x: int, y: int) -> Optional[Image.Image]:
        path = self._path(continent, floor, zoom, x, y)
        if path.exists():
            try:
                return Image.open(path).convert("RGB")
            except Exception:  # noqa: BLE001 - corrupt; refetch
                path.unlink(missing_ok=True)

        url = TILE_URL.format(continent=continent, floor=floor, zoom=zoom, x=x, y=y)
        body = _http_get(url)
        if body is None:
            return None
        path.write_bytes(body)
        try:
            return Image.open(io.BytesIO(body)).convert("RGB")
        except Exception:  # noqa: BLE001
            path.unlink(missing_ok=True)
            return None


# ---------------------------------------------------------------------------
# Coordinate math
# ---------------------------------------------------------------------------

def continent_to_pixel(coord: float, max_zoom: int, zoom: int) -> float:
    """Convert a continent coordinate to its pixel position at the given zoom.

    Continent coords are defined at max_zoom (1 unit == 1 pixel there); each
    step down halves the resolution.
    """
    return coord / (2 ** (max_zoom - zoom))


# ---------------------------------------------------------------------------
# Background tile loader: avoids freezing the UI thread on map changes.
# ---------------------------------------------------------------------------

class TileLoader(threading.Thread):
    """Worker that fetches tiles requested by the UI and posts them back."""

    def __init__(self, tile_cache: TileCache, result_q: "queue.Queue") -> None:
        super().__init__(daemon=True)
        self._cache = tile_cache
        self._req_q: "queue.Queue" = queue.Queue()
        self._result_q = result_q
        self._stop = threading.Event()

    def request(self, key: tuple) -> None:
        # key = (continent, floor, zoom, x, y)
        self._req_q.put(key)

    def stop(self) -> None:
        self._stop.set()
        self._req_q.put(None)

    def run(self) -> None:
        while not self._stop.is_set():
            key = self._req_q.get()
            if key is None:
                return
            try:
                img = self._cache.get(*key)
            except Exception as e:  # noqa: BLE001
                LOG.warning("tile fetch %s failed: %s", key, e)
                img = None
            self._result_q.put((key, img))


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

class MapOverlay:
    def __init__(
        self,
        *,
        cache_dir: Path,
        zoom: int,
        window_size: tuple[int, int],
        poll_hz: float,
        mumblelink_path: Optional[str],
        trail_cap: int = DEFAULT_TRAIL_CAP,
        trail_step: float = DEFAULT_TRAIL_STEP,
    ) -> None:
        self._zoom = zoom
        self._poll_ms = max(16, int(1000 / poll_hz))
        self._mumblelink_path = mumblelink_path

        self._meta = MapMetadata(cache_dir)
        self._continents = ContinentInfo(cache_dir)
        self._tiles = TileCache(cache_dir)
        self._tile_results: "queue.Queue" = queue.Queue()
        self._loader = TileLoader(self._tiles, self._tile_results)
        # In-memory tile cache so the UI doesn't reopen JPEGs every frame.
        self._mem_tiles: dict[tuple, Optional[Image.Image]] = {}
        self._pending: set[tuple] = set()

        # Per-map position history. We segregate by map_id because continent
        # coordinates are not comparable across maps in the Mists.
        # `trail_cap = 0` disables the trail entirely.
        self._trail_cap = max(0, trail_cap)
        self._trail_step_sq = float(trail_step) ** 2  # compare squared distances
        self._trails: dict[int, deque] = {}

        self._reader: Optional[MumbleLinkReader] = None
        self._last_sample: Optional[LinkedMem] = None

        self._root = tk.Tk()
        self._root.title("GW2 Player Position")
        self._root.geometry(f"{window_size[0]}x{window_size[1] + 60}")
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._canvas = tk.Canvas(
            self._root,
            width=window_size[0],
            height=window_size[1],
            bg="#0d0d10",
            highlightthickness=0,
        )
        self._canvas.pack(fill=tk.BOTH, expand=True)

        self._status_var = tk.StringVar(value="waiting for MumbleLink…")
        self._status = tk.Label(
            self._root,
            textvariable=self._status_var,
            anchor="w",
            font=("Menlo", 11),
            bg="#161620",
            fg="#d0d0d0",
            padx=8,
            pady=4,
            justify="left",
        )
        self._status.pack(fill=tk.X, side=tk.BOTTOM)

        self._photo: Optional[ImageTk.PhotoImage] = None
        self._image_id: Optional[int] = None

        # Keyboard shortcuts: +/- to zoom, c to clear trail, q to quit
        self._root.bind("<plus>", lambda _e: self._set_zoom(self._zoom + 1))
        self._root.bind("<KP_Add>", lambda _e: self._set_zoom(self._zoom + 1))
        self._root.bind("<equal>", lambda _e: self._set_zoom(self._zoom + 1))
        self._root.bind("<minus>", lambda _e: self._set_zoom(self._zoom - 1))
        self._root.bind("<KP_Subtract>", lambda _e: self._set_zoom(self._zoom - 1))
        self._root.bind("<c>", lambda _e: self._clear_current_trail())
        self._root.bind("<q>", lambda _e: self._on_close())

    # ------- lifecycle -------

    def run(self) -> None:
        try:
            self._reader = MumbleLinkReader(self._mumblelink_path)
            self._reader.open()
        except MumbleLinkError as e:
            # Drop the reader so _tick stops trying to read from it; status
            # stays visible and the user can still close the window cleanly.
            self._reader = None
            self._status_var.set(f"MumbleLink error: {e}")
            LOG.error("MumbleLink open failed: %s", e)

        self._loader.start()
        self._root.after(0, self._tick)
        self._root.after(50, self._drain_tile_results)
        try:
            self._root.mainloop()
        finally:
            self._on_close()

    def _on_close(self) -> None:
        try:
            self._loader.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._reader is not None:
            try:
                self._reader.close()
            except Exception:  # noqa: BLE001
                pass
            self._reader = None
        try:
            self._root.destroy()
        except tk.TclError:
            pass

    # ------- main poll loop (runs on UI thread) -------

    def _tick(self) -> None:
        try:
            if self._reader is not None:
                sample = self._reader.read()
                if sample.ui_tick != 0:
                    self._last_sample = sample
                    self._render(sample)
        except MumbleLinkError as e:
            LOG.warning("read failed: %s", e)
        finally:
            self._root.after(self._poll_ms, self._tick)

    def _drain_tile_results(self) -> None:
        """Move freshly-fetched tiles from the worker into the UI cache."""
        try:
            had_new = False
            while True:
                key, img = self._tile_results.get_nowait()
                self._mem_tiles[key] = img
                self._pending.discard(key)
                had_new = True
        except queue.Empty:
            pass
        if had_new and self._last_sample is not None:
            self._render(self._last_sample)
        self._root.after(50, self._drain_tile_results)

    # ------- rendering -------

    def _set_zoom(self, new_zoom: int) -> None:
        # Clamp on the next render once we know which continent we're on.
        self._zoom = max(0, min(8, new_zoom))
        if self._last_sample is not None:
            self._render(self._last_sample)

    def _clear_current_trail(self) -> None:
        if self._last_sample is not None:
            map_id = self._last_sample.context.map_id
            if map_id in self._trails:
                self._trails[map_id].clear()
                LOG.info("cleared trail for map_id=%d", map_id)
                self._render(self._last_sample)

    def _update_trail(self, map_id: int, x: float, y: float) -> "deque":
        """Append (x, y) to the trail for `map_id` if we've moved enough."""
        trail = self._trails.get(map_id)
        if trail is None:
            trail = deque(maxlen=self._trail_cap if self._trail_cap else None)
            self._trails[map_id] = trail
        if not trail:
            trail.append((x, y))
        else:
            lx, ly = trail[-1]
            dx, dy = x - lx, y - ly
            if dx * dx + dy * dy >= self._trail_step_sq:
                trail.append((x, y))
        return trail

    def _render(self, sample: LinkedMem) -> None:
        img = self._compose_image(sample)
        if img is None:
            return
        self._photo = ImageTk.PhotoImage(img)
        if self._image_id is None:
            self._image_id = self._canvas.create_image(0, 0, anchor=tk.NW, image=self._photo)
        else:
            self._canvas.itemconfig(self._image_id, image=self._photo)

    def _compose_image(
        self,
        sample: LinkedMem,
        *,
        canvas_size: Optional[tuple[int, int]] = None,
    ) -> Optional[Image.Image]:
        """Build the full overlay image (tiles + trail + player). Pure function
        from `(sample, internal caches)` to a PIL.Image — used by `_render`
        and the headless tests in `_smoketest.py`.
        """
        ctx = sample.context

        meta = self._meta.get(ctx.map_id) if ctx.map_id else None
        if meta is None:
            self._set_status(sample, meta=None, message="map metadata pending")
            return self._placeholder_image(canvas_size)

        continent = int(meta.get("continent_id") or 1)
        floor = int(meta.get("default_floor") or 1)
        cinfo = self._continents.get(continent) or {}
        min_zoom = int(cinfo.get("min_zoom", 0))
        max_zoom = int(cinfo.get("max_zoom", 7))
        zoom = max(min_zoom, min(max_zoom, self._zoom))
        tiles_w, tiles_h = self._continents.tile_count(continent, zoom)

        if ctx.player_x is None or ctx.player_y is None:
            self._set_status(sample, meta=meta, message="no player position yet")
            return None

        canvas_w, canvas_h = self._resolve_canvas_size(canvas_size)

        # Append to the per-map trail before drawing.
        trail = (
            self._update_trail(ctx.map_id, ctx.player_x, ctx.player_y)
            if self._trail_cap > 0 else deque()
        )

        # Player position in pixels at this zoom.
        ppx = continent_to_pixel(ctx.player_x, max_zoom, zoom)
        ppy = continent_to_pixel(ctx.player_y, max_zoom, zoom)

        # Top-left of the viewport in pixel-at-zoom space.
        view_x0 = ppx - canvas_w / 2
        view_y0 = ppy - canvas_h / 2

        # Tile range we need to cover the viewport.
        tx_min = int(view_x0 // TILE_SIZE)
        ty_min = int(view_y0 // TILE_SIZE)
        tx_max = int((view_x0 + canvas_w) // TILE_SIZE)
        ty_max = int((view_y0 + canvas_h) // TILE_SIZE)

        composite = Image.new("RGB", (canvas_w, canvas_h), (13, 13, 16))
        for ty in range(ty_min, ty_max + 1):
            for tx in range(tx_min, tx_max + 1):
                if tx < 0 or ty < 0 or tx >= tiles_w or ty >= tiles_h:
                    continue
                key = (continent, floor, zoom, tx, ty)
                tile = self._mem_tiles.get(key)
                if tile is None and key not in self._pending:
                    self._pending.add(key)
                    self._loader.request(key)
                if tile is not None:
                    px = int(tx * TILE_SIZE - view_x0)
                    py = int(ty * TILE_SIZE - view_y0)
                    composite.paste(tile, (px, py))

        draw = ImageDraw.Draw(composite, "RGBA")

        # Trail polyline. Convert each continent-coord point to canvas pixels.
        if len(trail) >= 2:
            scale = 2 ** (max_zoom - zoom)
            pts = [
                (int(x / scale - view_x0), int(y / scale - view_y0))
                for (x, y) in trail
            ]
            draw.line(pts, fill=TRAIL_LINE_COLOR, width=2, joint="curve")
            # Mark every Nth point so the polyline reads as a path of samples,
            # not just a smooth curve. Skip the last (it'll be under the player).
            for px, py in pts[:-1:8]:
                draw.ellipse((px - 2, py - 2, px + 2, py + 2),
                             fill=TRAIL_DOT_COLOR)

        # Player marker (always at the canvas center because we follow them).
        cx, cy = canvas_w // 2, canvas_h // 2
        draw.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), fill=(255, 255, 255, 200))
        draw.ellipse((cx - 6, cy - 6, cx + 6, cy + 6), fill=(220, 50, 47, 255))
        draw.ellipse((cx - 1, cy - 1, cx + 1, cy + 1), fill=(255, 255, 255, 255))

        self._set_status(
            sample, meta=meta, zoom=zoom, max_zoom=max_zoom,
            trail_len=len(trail),
        )
        return composite

    def _resolve_canvas_size(
        self, override: Optional[tuple[int, int]]
    ) -> tuple[int, int]:
        if override is not None:
            return override
        # winfo_width returns 1 before the canvas has been laid out — fall
        # back to the configured size in that case.
        w = max(1, self._canvas.winfo_width())
        h = max(1, self._canvas.winfo_height())
        if w <= 1 or h <= 1:
            w = int(self._canvas.cget("width") or 1)
            h = int(self._canvas.cget("height") or 1)
        return w, h

    def _placeholder_image(
        self, canvas_size: Optional[tuple[int, int]]
    ) -> Image.Image:
        w, h = self._resolve_canvas_size(canvas_size)
        composite = Image.new("RGB", (w, h), (13, 13, 16))
        ImageDraw.Draw(composite, "RGBA").text(
            (10, 10), "loading map metadata…", fill=(180, 180, 180, 255)
        )
        return composite

    def _set_status(
        self,
        sample: LinkedMem,
        *,
        meta: Optional[dict] = None,
        zoom: Optional[int] = None,
        max_zoom: Optional[int] = None,
        message: Optional[str] = None,
        trail_len: int = 0,
    ) -> None:
        identity = sample.identity or {}
        ctx = sample.context
        player = identity.get("name") or "?"
        prof = enrich.profession_name(identity.get("profession")) or "?"
        race = enrich.race_name(identity.get("race")) or "?"
        mount = enrich.mount_name(ctx.mount_index) or "?"
        map_name = (meta or {}).get("name") or f"map {ctx.map_id}"
        region = (meta or {}).get("region_name") or "?"
        in_combat = bool(ctx.ui_state & enrich.UI_STATE_BITS["ui_state_in_combat"])

        zoom_part = f" zoom={zoom}/{max_zoom}" if zoom is not None else ""
        msg = f" — {message}" if message else ""
        px = ctx.player_x if ctx.player_x is not None else 0.0
        py = ctx.player_y if ctx.player_y is not None else 0.0

        trail_part = f" · trail={trail_len}" if trail_len else ""
        self._status_var.set(
            f"{player} ({race} {prof}) · {map_name} ({region}) · "
            f"({px:.0f}, {py:.0f}) · mount={mount} · combat={in_combat}"
            f"{trail_part}{zoom_part}{msg}"
        )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live GW2 map overlay — draws the player on the in-game map.",
    )
    parser.add_argument("--zoom", type=int, default=DEFAULT_ZOOM,
                        help=f"Initial tile zoom 0..max (default {DEFAULT_ZOOM}); +/- in-window")
    parser.add_argument("--width", type=int, default=DEFAULT_WINDOW[0])
    parser.add_argument("--height", type=int, default=DEFAULT_WINDOW[1])
    parser.add_argument("--poll-hz", type=float, default=10.0)
    parser.add_argument("--mumblelink-path", default=None,
                        help="Override shared-memory location (testing)")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                        help=f"Tile/metadata cache directory (default {DEFAULT_CACHE_DIR})")
    parser.add_argument("--trail-cap", type=int, default=DEFAULT_TRAIL_CAP,
                        help=f"Max trail points retained per map (default {DEFAULT_TRAIL_CAP}). "
                             "Older points drop off when full. 0 disables the trail.")
    parser.add_argument("--trail-step", type=float, default=DEFAULT_TRAIL_STEP,
                        help=f"Min continent-units of movement before a new trail point is recorded "
                             f"(default {DEFAULT_TRAIL_STEP})")
    parser.add_argument("--no-trail", action="store_true",
                        help="Disable the position trail (equivalent to --trail-cap 0)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    import sys
    args = parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    trail_cap = 0 if args.no_trail else args.trail_cap
    overlay = MapOverlay(
        cache_dir=Path(args.cache_dir),
        zoom=args.zoom,
        window_size=(args.width, args.height),
        poll_hz=args.poll_hz,
        mumblelink_path=args.mumblelink_path,
        trail_cap=trail_cap,
        trail_step=args.trail_step,
    )
    overlay.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
