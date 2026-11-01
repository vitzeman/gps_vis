"""
Render an NMEA GPS trace onto OpenStreetMap tiles as a PNG image.

Single self-contained script: run it with uv, which installs the dependencies
declared in the header above into a cached environment.

    uv run trace_visualization.py output.nmea            # -> output.png
    uv run trace_visualization.py output.nmea -o map.png --width 800 --height 800

Map tiles are cached on disk (default .cache/tiles next to this script) and never expire,
so repeated runs only download tiles they have not seen before and work offline.
"""

from __future__ import annotations

# Standard library
import argparse
import hashlib
import io
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from urllib.parse import urlparse

# Third-party
import pynmea2
from PIL import Image, ImageColor, ImageDraw, ImageFont

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Tiles
TILE_CACHE = ".cache/tiles"  # tile cache dir, relative to this script
DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "trace_visualization/1.0 (personal NMEA trace renderer; python-urllib)"
TILE_TIMEOUT_S = 15.0

# Web Mercator
TILE_SIZE = 256
MAX_ZOOM = 19
POINT_ZOOM = 17  # zoom used when the track has no extent (single fix / standing still)
EARTH_CIRCUMFERENCE_M = 40075016.686

# Drawing
SS = 2  # supersampling factor for anti-aliased overlay drawing
OUTLINE = (34, 34, 34, 255)
WHITE = (255, 255, 255, 255)
START_COLOR = (46, 160, 67, 255)
END_COLOR = (215, 48, 39, 255)
BASEMAP_FILL = (221, 221, 221)
LABEL_MIN_DIST_PX = 25
ATTRIBUTION = "© OpenStreetMap contributors"

# CLI defaults
DEFAULT_WIDTH = 1600
DEFAULT_HEIGHT = 1000
DEFAULT_PADDING = 60
DEFAULT_LINE_COLOR = "#3399ff"
DEFAULT_LINE_WIDTH = 5
DEFAULT_ARROW_SPACING = 80

# --------------------------------------------------------------------------- #
# NMEA parsing
# --------------------------------------------------------------------------- #


@dataclass
class Fix:
    """A single position fix, merged from RMC (+ GGA when available)."""

    timestamp: datetime | time
    lat: float
    lon: float
    speed_knots: float | None = None
    course_deg: float | None = None
    altitude_m: float | None = None
    num_sats: int | None = None
    fix_quality: int | None = None
    hdop: float | None = None


@dataclass
class Track:
    """Ordered list of fixes read from one NMEA source."""

    fixes: list[Fix] = field(default_factory=list)
    source: str | None = None

    def __len__(self) -> int:
        """Number of fixes in the track."""
        return len(self.fixes)

    @property
    def points(self) -> list[tuple[float, float]]:
        """(lat, lon) of every fix, in order."""
        return [(f.lat, f.lon) for f in self.fixes]

    @property
    def bounds(self) -> list[list[float]]:
        """Bounding box as [[lat_min, lon_min], [lat_max, lon_max]]."""
        lats = [f.lat for f in self.fixes]
        lons = [f.lon for f in self.fixes]
        return [[min(lats), min(lons)], [max(lats), max(lons)]]

    @property
    def start(self) -> Fix:
        """First fix."""
        return self.fixes[0]

    @property
    def end(self) -> Fix:
        """Last fix."""
        return self.fixes[-1]


def _key(t: time) -> str:
    """Key used to match RMC and GGA sentences belonging to the same fix."""
    return t.isoformat()


def parse_lines(lines, source: str | None = None, check_checksum: bool = True) -> Track:
    """Parse an iterable of NMEA lines. Malformed sentences are skipped."""
    fixes: dict[str, Fix] = {}
    order: list[str] = []
    gga: dict[str, pynmea2.GGA] = {}

    for raw in lines:
        raw = raw.strip()
        if not raw.startswith("$"):
            continue
        try:
            msg = pynmea2.parse(raw, check=check_checksum)
        except pynmea2.ParseError:
            continue

        if isinstance(msg, pynmea2.RMC):
            if msg.status != "A" or msg.timestamp is None or not msg.lat:
                continue
            k = _key(msg.timestamp)
            ts: datetime | time = msg.timestamp
            if isinstance(msg.datestamp, date):
                ts = datetime.combine(msg.datestamp, msg.timestamp).replace(tzinfo=timezone.utc)
            if k not in fixes:
                order.append(k)
            fixes[k] = Fix(
                timestamp=ts,
                lat=msg.latitude,
                lon=msg.longitude,
                speed_knots=float(msg.spd_over_grnd) if msg.spd_over_grnd is not None else None,
                course_deg=float(msg.true_course) if msg.true_course is not None else None,
            )
        elif isinstance(msg, pynmea2.GGA) and msg.timestamp is not None:
            gga[_key(msg.timestamp)] = msg

    for k, f in fixes.items():
        g = gga.get(k)
        if g is None:
            continue
        f.altitude_m = float(g.altitude) if g.altitude is not None else None
        f.num_sats = int(g.num_sats) if g.num_sats else None
        f.fix_quality = int(g.gps_qual) if g.gps_qual is not None else None
        f.hdop = float(g.horizontal_dil) if g.horizontal_dil else None

    return Track(fixes=[fixes[k] for k in order], source=source)


def load_track(path: str | Path, check_checksum: bool = True) -> Track:
    """Read an NMEA file into a Track; raises ValueError if it has no valid fixes."""
    path = Path(path)
    with path.open(encoding="ascii", errors="replace") as fh:
        track = parse_lines(fh, source=str(path), check_checksum=check_checksum)
    if not track.fixes:
        raise ValueError(f"No valid RMC fixes found in {path}")
    return track


# --------------------------------------------------------------------------- #
# Web Mercator helpers
# --------------------------------------------------------------------------- #


def latlon_to_world_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Global pixel coordinates of (lat, lon) at zoom z (origin top-left of tile 0/0)."""
    scale = TILE_SIZE * 2**z
    lat_r = math.radians(max(min(lat, 85.05112878), -85.05112878))
    x = (lon + 180.0) / 360.0 * scale
    y = (1.0 - math.log(math.tan(lat_r) + 1.0 / math.cos(lat_r)) / math.pi) / 2.0 * scale
    return x, y


def choose_zoom(bounds: list[list[float]], width: int, height: int, padding: int) -> int:
    """Highest zoom at which the bounding box fits inside the image minus padding."""
    (lat_min, lon_min), (lat_max, lon_max) = bounds
    if lat_min == lat_max and lon_min == lon_max:
        return POINT_ZOOM
    avail_w, avail_h = max(1, width - 2 * padding), max(1, height - 2 * padding)
    for z in range(MAX_ZOOM, -1, -1):
        x0, y0 = latlon_to_world_px(lat_max, lon_min, z)
        x1, y1 = latlon_to_world_px(lat_min, lon_max, z)
        if x1 - x0 <= avail_w and y1 - y0 <= avail_h:
            return z
    return 0


def meters_per_px(lat: float, z: int) -> float:
    """Ground distance covered by one pixel at latitude `lat` and zoom z."""
    return EARTH_CIRCUMFERENCE_M * math.cos(math.radians(lat)) / (TILE_SIZE * 2**z)


# --------------------------------------------------------------------------- #
# Tile cache
# --------------------------------------------------------------------------- #


def default_cache_dir() -> Path:
    """TILE_CACHE, resolved relative to this script's directory (not the cwd)."""
    return Path(__file__).resolve().parent / TILE_CACHE


class TileError(RuntimeError):
    """Raised when a tile is neither cached nor downloadable."""


class TileCache:
    """Disk cache of map tiles; cached tiles never expire."""

    def __init__(self, url_template: str, cache_dir: Path, timeout_s: float = TILE_TIMEOUT_S):
        """Set up a cache for `url_template`, namespaced by its host and a short hash."""
        self.url_template = url_template
        host = re.sub(r"[^A-Za-z0-9.-]", "_", urlparse(url_template).netloc) or "tiles"
        digest = hashlib.sha1(url_template.encode()).hexdigest()[:8]
        self.root = Path(cache_dir) / f"{host}-{digest}"
        self.timeout_s = timeout_s
        self.hits = 0
        self.fetched = 0

    def _path(self, z: int, x: int, y: int) -> Path:
        """On-disk location of tile z/x/y."""
        return self.root / str(z) / str(x) / f"{y}.png"

    def get(self, z: int, x: int, y: int) -> Image.Image:
        """Return tile z/x/y from disk, downloading and caching it if missing."""
        x %= 2**z
        path = self._path(z, x, y)
        if path.is_file():
            try:
                with Image.open(path) as img:
                    tile = img.convert("RGB")
                self.hits += 1
                return tile
            except OSError:
                path.unlink(missing_ok=True)  # corrupt cache entry -> refetch

        url = self.url_template.format(z=z, x=x, y=y, s="a")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = resp.read()
            tile = Image.open(io.BytesIO(data)).convert("RGB")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TileError(f"tile {z}/{x}/{y} is not cached and could not be downloaded from {url}: {exc}") from exc

        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        self.fetched += 1
        return tile


# --------------------------------------------------------------------------- #
# Basemap
# --------------------------------------------------------------------------- #


def compose_basemap(tiles: TileCache, center_lat: float, center_lon: float, z: int, width: int, height: int):
    """Stitch tiles covering a width x height viewport centered on (lat, lon).

    Returns (image, project) where project(lat, lon) -> image pixel (x, y).
    """
    cx, cy = latlon_to_world_px(center_lat, center_lon, z)
    left, top = cx - width / 2, cy - height / 2
    n = 2**z

    canvas = Image.new("RGB", (width, height), BASEMAP_FILL)
    for ty in range(math.floor(top / TILE_SIZE), math.floor((top + height - 1) / TILE_SIZE) + 1):
        if not 0 <= ty < n:
            continue  # beyond the poles: leave grey
        for tx in range(math.floor(left / TILE_SIZE), math.floor((left + width - 1) / TILE_SIZE) + 1):
            tile = tiles.get(z, tx, ty)
            canvas.paste(tile, (round(tx * TILE_SIZE - left), round(ty * TILE_SIZE - top)))

    def project(lat: float, lon: float) -> tuple[float, float]:
        """Map (lat, lon) to pixel coordinates within the stitched image."""
        x, y = latlon_to_world_px(lat, lon, z)
        return x - left, y - top

    return canvas, project


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #


@dataclass
class Style:
    """User-configurable appearance of the track."""

    line_color: tuple[int, int, int, int]
    line_width: int
    arrow_spacing: int


def _font(size: int) -> ImageFont.ImageFont:
    """Pillow's bundled font at `size` px, scaled for the supersampled overlay."""
    return ImageFont.load_default(size=size * SS)


def _arrow_positions(pts: list[tuple[float, float]], spacing: float):
    """Yield (x, y, angle) every `spacing` px along the polyline, starting half a spacing in."""
    target = spacing / 2
    walked = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        if seg == 0:
            continue
        angle = math.atan2(y1 - y0, x1 - x0)
        while target <= walked + seg:
            t = (target - walked) / seg
            yield x0 + t * (x1 - x0), y0 + t * (y1 - y0), angle
            target += spacing
        walked += seg


def _draw_arrow(draw: ImageDraw.ImageDraw, x: float, y: float, angle: float, size: float) -> None:
    """Draw a triangular arrowhead centered on (x, y) pointing along `angle` (radians)."""
    dx, dy = math.cos(angle), math.sin(angle)
    px, py = -dy, dx
    half_len, half_w = size * 0.6, size * 0.45
    tip = (x + dx * half_len, y + dy * half_len)
    base = (x - dx * half_len, y - dy * half_len)
    left = (base[0] + px * half_w, base[1] + py * half_w)
    right = (base[0] - px * half_w, base[1] - py * half_w)
    draw.polygon([tip, left, right], fill=WHITE, outline=OUTLINE, width=SS)


def _draw_tag(draw: ImageDraw.ImageDraw, x: float, y: float, text: str, font) -> None:
    """Small rounded label box whose bottom-left corner sits up-right of (x, y)."""
    pad = 2 * SS
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    ox, oy = x + 5 * SS, y - 5 * SS - (b - t) - 2 * pad
    box = (ox, oy, ox + (r - l) + 2 * pad, oy + (b - t) + 2 * pad)
    draw.rounded_rectangle(box, radius=3 * SS, fill=(255, 255, 255, 230), outline=OUTLINE, width=1 * SS)
    draw.text((ox + pad - l, oy + pad - t), text, fill=OUTLINE, font=font)


def _draw_endpoint(draw: ImageDraw.ImageDraw, x: float, y: float, color, text: str, font) -> None:
    """Filled circle with a white outline and a centered letter (start/end marker)."""
    r = 11 * SS
    draw.ellipse((x - r, y - r, x + r, y + r), fill=color, outline=WHITE, width=3 * SS)
    draw.text((x, y), text, fill=WHITE, font=font, anchor="mm")


def _nice_length(max_m: float) -> float:
    """Largest 1/2/5 x 10^n not exceeding max_m."""
    exp = 10 ** math.floor(math.log10(max_m))
    for mult in (5, 2, 1):
        if mult * exp <= max_m:
            return mult * exp
    return exp


def _draw_scale_bar(draw: ImageDraw.ImageDraw, height: int, lat: float, z: int, font) -> None:
    """Metric scale bar with a rounded length in the bottom-left corner."""
    mpp = meters_per_px(lat, z)
    length_m = _nice_length(180 * mpp)
    bar_px = length_m / mpp
    label = f"{length_m / 1000:g} km" if length_m >= 1000 else f"{length_m:g} m"

    margin, pad = 12, 6
    _, t, r, b = draw.textbbox((0, 0), label, font=font)
    text_h = (b - t) / SS
    box_w = max(bar_px, r / SS) + 2 * pad
    box_h = text_h + 10 + 3 * pad
    x0, y1 = margin, height - margin
    y0 = y1 - box_h
    draw.rectangle([v * SS for v in (x0, y0, x0 + box_w, y1)], fill=(255, 255, 255, 200))
    draw.text(((x0 + pad) * SS, (y0 + pad) * SS - t), label, fill=OUTLINE, font=font)

    bx0, bx1, by = x0 + pad, x0 + pad + bar_px, y1 - pad
    draw.line([(bx0 * SS, by * SS), (bx1 * SS, by * SS)], fill=OUTLINE, width=2 * SS)
    for bx in (bx0, bx1):
        draw.line([(bx * SS, (by - 8) * SS), (bx * SS, by * SS)], fill=OUTLINE, width=2 * SS)


def _draw_attribution(draw: ImageDraw.ImageDraw, width: int, height: int, font) -> None:
    """Map data attribution in a semi-transparent box in the bottom-right corner."""
    pad = 4 * SS
    l, t, r, b = draw.textbbox((0, 0), ATTRIBUTION, font=font)
    x1, y1 = width * SS, height * SS
    x0, y0 = x1 - (r - l) - 2 * pad, y1 - (b - t) - 2 * pad
    draw.rectangle((x0, y0, x1, y1), fill=(255, 255, 255, 190))
    draw.text((x0 + pad - l, y0 + pad - t), ATTRIBUTION, fill=OUTLINE, font=font)


def draw_overlay(base: Image.Image, track: Track, project, z: int, style: Style) -> Image.Image:
    """Draw the track, arrows, fix dots, labels, markers, scale bar and attribution onto `base`."""
    width, height = base.size
    overlay = Image.new("RGBA", (width * SS, height * SS), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    pts = [project(lat, lon) for lat, lon in track.points]
    spts = [(x * SS, y * SS) for x, y in pts]

    if len(spts) > 1:
        lw = style.line_width * SS
        draw.line(spts, fill=OUTLINE, width=lw + 3 * SS, joint="curve")
        draw.line(spts, fill=style.line_color, width=lw, joint="curve")
        for x, y, angle in _arrow_positions(spts, style.arrow_spacing * SS):
            _draw_arrow(draw, x, y, angle, size=max(12, style.line_width * 2.6) * SS)

    r = 3.5 * SS
    for x, y in spts[1:-1]:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=WHITE, outline=OUTLINE, width=SS)

    tag_font = _font(11)
    if len(pts) > 2:
        last = pts[0]
        for i in range(1, len(pts) - 1):
            p = pts[i]
            if math.dist(p, last) >= LABEL_MIN_DIST_PX and math.dist(p, pts[-1]) >= LABEL_MIN_DIST_PX:
                _draw_tag(draw, p[0] * SS, p[1] * SS, str(i), tag_font)
                last = p

    endpoint_font = _font(13)
    _draw_endpoint(draw, *spts[0], START_COLOR, "S", endpoint_font)
    if len(spts) > 1:
        _draw_endpoint(draw, *spts[-1], END_COLOR, "E", endpoint_font)

    small_font = _font(12)
    _draw_scale_bar(draw, height, track.start.lat, z, small_font)
    _draw_attribution(draw, width, height, small_font)

    overlay = overlay.resize((width, height), Image.Resampling.LANCZOS)
    return Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")


def render_track(track: Track, tiles: TileCache, width: int, height: int, padding: int, style: Style) -> Image.Image:
    """Render the full PNG image: pick the zoom, stitch the basemap, draw the overlay."""
    (lat_min, lon_min), (lat_max, lon_max) = track.bounds
    z = choose_zoom(track.bounds, width, height, padding)
    # Center in projected space so the track is visually centered.
    x0, y0 = latlon_to_world_px(lat_max, lon_min, z)
    x1, y1 = latlon_to_world_px(lat_min, lon_max, z)
    n_px = TILE_SIZE * 2**z
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    center_lon = cx / n_px * 360.0 - 180.0
    center_lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * cy / n_px))))

    base, project = compose_basemap(tiles, center_lat, center_lon, z, width, height)
    return draw_overlay(base, track, project, z, style)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    p = argparse.ArgumentParser(description="Render an NMEA GPS trace on OpenStreetMap tiles as a PNG.")
    p.add_argument("input", type=Path, help="NMEA file (e.g. output.nmea)")
    p.add_argument("-o", "--output", type=Path, help="output PNG (default: <input stem>.png next to the input)")
    p.add_argument("--width", type=int, default=DEFAULT_WIDTH, help=f"image width in px (default: {DEFAULT_WIDTH})")
    p.add_argument("--height", type=int, default=DEFAULT_HEIGHT,
                   help=f"image height in px (default: {DEFAULT_HEIGHT})")
    p.add_argument("--padding", type=int, default=DEFAULT_PADDING,
                   help=f"min px between the track and the image edge (default: {DEFAULT_PADDING})")
    p.add_argument("--line-color", default=DEFAULT_LINE_COLOR,
                   help=f"track line color (default: {DEFAULT_LINE_COLOR})")
    p.add_argument("--line-width", type=int, default=DEFAULT_LINE_WIDTH,
                   help=f"track line width in px (default: {DEFAULT_LINE_WIDTH})")
    p.add_argument("--arrow-spacing", type=int, default=DEFAULT_ARROW_SPACING,
                   help=f"px between direction arrows (default: {DEFAULT_ARROW_SPACING})")
    p.add_argument("--tile-url", default=DEFAULT_TILE_URL, help=f"tile URL template (default: {DEFAULT_TILE_URL})")
    cache_dir = default_cache_dir()
    p.add_argument("--cache-dir", type=Path, default=cache_dir, help=f"tile cache directory (default: {cache_dir})")
    return p


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, render the trace and save the PNG; returns the exit code."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        r, g, b = ImageColor.getrgb(args.line_color)[:3]
    except ValueError as exc:
        parser.error(str(exc))
    if args.width <= 0 or args.height <= 0 or args.arrow_spacing <= 0 or args.line_width <= 0:
        parser.error("--width, --height, --line-width and --arrow-spacing must be positive")

    try:
        track = load_track(args.input)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    tiles = TileCache(args.tile_url, args.cache_dir)
    style = Style(line_color=(r, g, b, 255), line_width=args.line_width, arrow_spacing=args.arrow_spacing)
    try:
        image = render_track(track, tiles, args.width, args.height, args.padding, style)
    except TileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = args.output or args.input.with_suffix(".png")
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    print(f"{len(track)} fixes, tiles: {tiles.hits} cached / {tiles.fetched} downloaded -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
