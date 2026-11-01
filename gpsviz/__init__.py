"""gpsviz - visualize NMEA GPS traces on a map.

The core API is UI-agnostic so it can be reused from the CLI, a notebook or a GUI:

    track = load_track("output.nmea")
    opts = MapOptions.from_yaml("config.yaml")
    m = build_map(track, opts)
    save_html(m, "track.html")
    save_png("track.html", "track.png", opts.png)
"""

from .export import PngExportError, page_url, save_html, save_png, serve_directory
from .options import BasemapOptions, MapOptions, PngOptions
from .parsing import Fix, Track, load_track
from .render import build_map

__all__ = [
    "BasemapOptions",
    "Fix",
    "MapOptions",
    "PngExportError",
    "PngOptions",
    "Track",
    "build_map",
    "load_track",
    "page_url",
    "save_html",
    "save_png",
    "serve_directory",
]
