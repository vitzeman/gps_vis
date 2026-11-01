# gps_vis

Visualize NMEA GPS traces on an interactive map (HTML) and export a static PNG.

![example output](output.png)

- Parses NMEA `RMC` + `GGA` sentences (checksums validated, malformed lines skipped)
- Interactive [folium](https://python-visualization.github.io/folium/) / Leaflet map:
  - track line colored by speed (km/h) with a legend
  - start / end markers
  - clickable dot per fix (time, speed, course, satellites, fix quality, position)
  - heading arrows in the direction of travel
  - switchable basemaps (OpenStreetMap, OpenTopoMap, Esri satellite)
- PNG export via headless Chrome (screenshot of the same HTML, so both match)
- Every feature can be switched on/off from a YAML config or CLI flags
- UI-agnostic core API, ready to be reused from notebooks or a future GUI

## Installation

Uses the mamba environment `general` and [uv](https://docs.astral.sh/uv/) for packages.

```bash
mamba activate general
pip install uv                                           # once, into the env
uv pip install --python "$CONDA_PREFIX/bin/python" -e ".[png]"
```

The `[png]` extra installs `selenium`. PNG export also needs **Google Chrome**
(selenium downloads a matching chromedriver automatically). Without the extra, or
without Chrome, the HTML is still produced and the PNG step is skipped with a warning.

## Quick start

```bash
gpsviz output.nmea
```

Output files are written next to the input:

```
output.nmea  ->  output.html  (interactive map)
                 output.png   (1600x1000 screenshot)
```

Then it serves the output folder on `http://127.0.0.1:<port>/`, opens the map in
your browser, and keeps running until you press **Ctrl+C**. Use `--no-open` to
just write the files and exit.

`python -m gpsviz output.nmea` works too.

### Why a local server? (OSM tile usage policy)

OpenStreetMap's volunteer-run tile servers require a valid `Referer` header.
When `output.html` is opened directly as a `file://` page, browsers send no Referer.
OSM then returns a 403 *"Access blocked – App is not following the tile usage
policy"* image instead of the map. Serving the page over `http://127.0.0.1` gives
it a proper Referer, and the PNG export uses the same mechanism.

To view an already generated HTML later, serve its folder yourself:

```bash
python -m http.server -d . 8000    # then open http://127.0.0.1:8000/output.html
```

Double-clicking the `.html` file will still show blocked OSM tiles, though the
other basemaps (Esri satellite) usually work.

## CLI usage examples

```bash
# Don't serve/open the browser (e.g. on a server / in scripts)
gpsviz output.nmea --no-open

# Serve on a fixed port (handy for refreshing the same URL)
gpsviz output.nmea --port 8000

# Write outputs somewhere else
gpsviz output.nmea -o results/

# Single-color line, no arrows, no per-fix dots
gpsviz output.nmea --no-speed-colored-line --no-heading-arrows --no-fix-popups

# Fewer arrows and a different colormap
gpsviz output.nmea --arrow-every-n 5 --colormap plasma

# Satellite imagery as the default basemap (also used for the PNG)
gpsviz output.nmea --basemap Esri.WorldImagery

# Only HTML, no PNG
gpsviz output.nmea --no-png

# Bigger PNG, wait longer for tiles on a slow connection
gpsviz output.nmea --png-width 2560 --png-height 1440 --png-wait 5

# Load settings from YAML, override one of them on the command line
gpsviz output.nmea --config my.yaml --no-heading-arrows

# Print the effective configuration (defaults < YAML < CLI) and exit
gpsviz --config my.yaml --dump-config
```

Run `gpsviz --help` for the full list of flags. Every boolean option has a
`--flag` / `--no-flag` pair.

## Configuration

Options live in the `MapOptions` dataclass (`gpsviz/options.py`). They can be set,
from lowest to highest priority, by:

1. **dataclass defaults**
2. **YAML file** passed with `--config`
3. **CLI flags** (only the flags you actually pass)

Unknown keys in the YAML file are rejected with an error, so typos don't go unnoticed.
A fully commented template with all defaults is in
[`config.example.yaml`](config.example.yaml):

```yaml
speed_colored_line: true   # color the track by speed (false -> single-color line)
line_color: "#3388ff"      # line color when speed_colored_line is false
line_weight: 5
colormap: viridis          # viridis | plasma | inferno | magma | ylorrd | rdylgn
start_end_markers: true
fix_popups: true
heading_arrows: true
arrow_every_n: 3
fit_bounds: true
open_browser: true         # serve on localhost + open browser
serve_port: 0              # 0 = any free port

basemap:
  default: OpenStreetMap   # any folium / xyzservices tile name
  extra_layers: [OpenTopoMap, Esri.WorldImagery]
  layer_switcher: true

png:
  enabled: true
  width: 1600
  height: 1000
  wait_s: 2.0              # seconds to wait for tiles before the screenshot
```

A YAML file only needs the keys you want to change:

```yaml
heading_arrows: false
basemap:
  default: Esri.WorldImagery
png:
  width: 1920
  height: 1080
```

> Note: CartoDB tiles now require an API key, so they are not in the default layers.

## Python API

The core never prints or reads `argv`, so you can call it directly from a
notebook, another script, or a GUI that binds widgets to `MapOptions`:

```python
import gpsviz

track = gpsviz.load_track("output.nmea")
print(len(track), track.start.timestamp, track.start.speed_kmh)

opts = gpsviz.MapOptions.from_yaml("config.example.yaml")
opts = opts.merged({"heading_arrows": False, "png": {"width": 1920}})

m = gpsviz.build_map(track, opts)        # folium.Map (displays inline in Jupyter)
html = gpsviz.save_html(m, "out/track.html")

try:
    gpsviz.save_png(html, "out/track.png", opts.png)
except gpsviz.PngExportError as exc:
    print("PNG skipped:", exc)

# View it over HTTP (needed for OSM tiles), e.g. inside a GUI web view
with gpsviz.serve_directory("out") as base_url:
    url = gpsviz.page_url(base_url, html)    # http://127.0.0.1:<port>/track.html
    ...
```

## How it works

```
output.nmea ─► parsing.load_track ─► Track[Fix] ─► render.build_map ─► folium.Map
                                                        ▲                  │
                         options.MapOptions ────────────┘                  ▼
                       (defaults < YAML < CLI)                export.save_html ─► .html
                                                                          │
                                                       export.save_png ◄──┘ (headless Chrome)
                                                                          ▼
                                                                        .png
```

| Module | Responsibility |
|---|---|
| `gpsviz/parsing.py` | Reads NMEA with `pynmea2`. Valid `RMC` sentences (status `A`) give time, position, speed and course; `GGA` sentences with the same timestamp add satellites, fix quality, HDOP and altitude. Returns a `Track` of `Fix` objects. |
| `gpsviz/options.py` | `MapOptions` / `BasemapOptions` / `PngOptions` dataclasses, `from_yaml`, `to_yaml`, `merged` (nested overrides, `None` = "not set", unknown keys raise). |
| `gpsviz/render.py` | Builds the folium map. Each feature is a separate `_add_*` function controlled by one option. |
| `gpsviz/export.py` | `save_html`; `serve_directory` (background localhost HTTP server); `save_png` (loads the page over that server in headless Chrome, waits for tiles, takes a screenshot). PNG failures raise `PngExportError`. |
| `gpsviz/cli.py` | `argparse` front-end: resolves the config, writes the outputs, handles the PNG fallback, serves the page and opens the browser. |

Units: NMEA speed is in knots and is converted to km/h (× 1.852). Course is in
degrees true; heading arrows are triangles rotated to that course.

## Extending

- **New map feature:** add a field to `MapOptions`, an `_add_<feature>()` function
  in `render.py` called from `build_map`, an optional CLI flag in `cli.py`
  (plus a line in `_overrides`), and document it in `config.example.yaml`.
- **New input format (GPX, CSV…):** write a loader that returns a `Track`;
  rendering and export don't depend on NMEA.
- **GUI:** bind widgets to a `MapOptions` instance and call
  `build_map` → `save_html` / `save_png`, or embed the HTML in a web view.

## Project layout

```
pyproject.toml          package metadata, deps, `gpsviz` console script
config.example.yaml     all options with defaults
gpsviz/
  __init__.py           public API
  __main__.py           `python -m gpsviz`
  parsing.py  options.py  render.py  export.py  cli.py
output.nmea             sample trace (Prague, 26 fixes @ 1 Hz)
```

