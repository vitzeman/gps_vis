"""Command-line front-end. Precedence: dataclass defaults < --config YAML < CLI flags."""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

from .export import PngExportError, page_url, save_html, save_png, serve_directory
from .options import MapOptions
from .parsing import load_track
from .render import build_map


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gpsviz", description="Visualize an NMEA GPS trace on a map (HTML + PNG).")
    p.add_argument("input", nargs="?", type=Path, help="NMEA file (e.g. output.nmea)")
    p.add_argument("-c", "--config", type=Path, help="YAML file with MapOptions")
    p.add_argument("-o", "--out-dir", type=Path, help="output directory (default: next to input)")
    p.add_argument("--dump-config", action="store_true", help="print the effective config as YAML and exit")

    bool_flag = dict(action=argparse.BooleanOptionalAction, default=None)
    g = p.add_argument_group("map features")
    g.add_argument("--speed-colored-line", **bool_flag, help="color the track by speed")
    g.add_argument("--start-end-markers", **bool_flag, help="show start/end markers")
    g.add_argument("--fix-popups", **bool_flag, help="show a clickable dot per fix")
    g.add_argument("--heading-arrows", **bool_flag, help="show heading arrows")
    g.add_argument("--arrow-every-n", type=int, help="draw an arrow every N fixes")
    g.add_argument("--colormap", help="speed colormap (viridis, plasma, inferno, magma, ylorrd, rdylgn)")
    g.add_argument("--basemap", dest="basemap_default", help="default basemap tiles")
    g.add_argument("--layer-switcher", **bool_flag, help="add extra basemaps + layer control")

    g = p.add_argument_group("output")
    g.add_argument("--png", dest="png_enabled", **bool_flag, help="export PNG")
    g.add_argument("--png-width", type=int)
    g.add_argument("--png-height", type=int)
    g.add_argument("--png-wait", dest="png_wait_s", type=float, help="seconds to wait for tiles")
    g.add_argument("--open", dest="open_browser", **bool_flag,
                   help="serve the HTML on localhost and open it in a browser (Ctrl+C to stop)")
    g.add_argument("--port", dest="serve_port", type=int, help="port for the local server (default: any free)")
    return p


def _overrides(a: argparse.Namespace) -> dict:
    return {
        "speed_colored_line": a.speed_colored_line,
        "start_end_markers": a.start_end_markers,
        "fix_popups": a.fix_popups,
        "heading_arrows": a.heading_arrows,
        "arrow_every_n": a.arrow_every_n,
        "colormap": a.colormap,
        "open_browser": a.open_browser,
        "serve_port": a.serve_port,
        "basemap": {"default": a.basemap_default, "layer_switcher": a.layer_switcher},
        "png": {"enabled": a.png_enabled, "width": a.png_width, "height": a.png_height, "wait_s": a.png_wait_s},
    }


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    try:
        opts = MapOptions.from_yaml(args.config) if args.config else MapOptions()
        opts = opts.merged(_overrides(args))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.dump_config:
        print(opts.to_yaml(), end="")
        return 0
    if args.input is None:
        parser.error("input file is required")

    try:
        track = load_track(args.input)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_dir = args.out_dir or args.input.parent
    html_path = out_dir / f"{args.input.stem}.html"
    png_path = out_dir / f"{args.input.stem}.png"

    save_html(build_map(track, opts), html_path)
    print(f"{len(track)} fixes -> {html_path}")

    if opts.png.enabled:
        try:
            save_png(html_path, png_path, opts.png)
            print(f"PNG -> {png_path}")
        except PngExportError as exc:
            print(f"warning: PNG skipped: {exc}", file=sys.stderr)

    if opts.open_browser:
        # OSM tiles are blocked for file:// pages (no Referer), so serve over HTTP.
        try:
            with serve_directory(html_path.parent, opts.serve_port) as base_url:
                url = page_url(base_url, html_path)
                print(f"Serving {url}  (Ctrl+C to stop)")
                webbrowser.open(url)
                threading.Event().wait()
        except OSError as exc:
            print(f"error: cannot start local server: {exc}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
