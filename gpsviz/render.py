"""Build a folium map from a Track and MapOptions."""

from __future__ import annotations

import branca.colormap as cm
import folium

from .options import MapOptions
from .parsing import Fix, Track

# Colormap names accepted in MapOptions.colormap -> branca built-ins
_COLORMAPS = {
    "viridis": cm.linear.viridis,
    "plasma": cm.linear.plasma,
    "inferno": cm.linear.inferno,
    "magma": cm.linear.magma,
    "ylorrd": cm.linear.YlOrRd_09,
    "rdylgn": cm.linear.RdYlGn_11,
}


def build_map(track: Track, opts: MapOptions | None = None) -> folium.Map:
    opts = opts or MapOptions()
    m = folium.Map(location=(track.start.lat, track.start.lon), tiles=None, control_scale=True)

    _add_basemaps(m, opts)
    _add_track_line(m, track, opts)
    if opts.heading_arrows:
        _add_heading_arrows(m, track, opts)
    if opts.fix_popups:
        _add_fix_popups(m, track)
    if opts.start_end_markers:
        _add_start_end(m, track)

    if opts.basemap.layer_switcher and opts.basemap.extra_layers:
        folium.LayerControl(collapsed=True).add_to(m)
    if opts.fit_bounds:
        m.fit_bounds(track.bounds, padding=(30, 30))
    return m


def _add_basemaps(m: folium.Map, opts: MapOptions) -> None:
    folium.TileLayer(opts.basemap.default, name=opts.basemap.default, overlay=False, control=True).add_to(m)
    if not opts.basemap.layer_switcher:
        return
    for name in opts.basemap.extra_layers:
        if name != opts.basemap.default:
            folium.TileLayer(name, name=name, overlay=False, control=True, show=False).add_to(m)


def _speed_colormap(track: Track, opts: MapOptions) -> cm.LinearColormap:
    speeds = [f.speed_kmh for f in track.fixes if f.speed_kmh is not None]
    lo, hi = (min(speeds), max(speeds)) if speeds else (0.0, 1.0)
    if hi <= lo:
        hi = lo + 1.0
    base = _COLORMAPS.get(opts.colormap.lower())
    if base is None:
        raise ValueError(f"Unknown colormap {opts.colormap!r}; choose from {sorted(_COLORMAPS)}")
    cmap = base.scale(lo, hi)
    cmap.caption = "Speed [km/h]"
    return cmap


def _add_track_line(m: folium.Map, track: Track, opts: MapOptions) -> None:
    group = folium.FeatureGroup(name="Track")
    if opts.speed_colored_line and len(track) > 1:
        cmap = _speed_colormap(track, opts)
        speeds = [f.speed_kmh or 0.0 for f in track.fixes]
        folium.ColorLine(
            positions=track.points,
            colors=speeds[:-1],
            colormap=cmap,
            weight=opts.line_weight,
        ).add_to(group)
        cmap.add_to(m)
    else:
        folium.PolyLine(track.points, color=opts.line_color, weight=opts.line_weight).add_to(group)
    group.add_to(m)


def _add_heading_arrows(m: folium.Map, track: Track, opts: MapOptions) -> None:
    group = folium.FeatureGroup(name="Heading")
    step = max(1, opts.arrow_every_n)
    for f in track.fixes[::step]:
        if f.course_deg is None:
            continue
        # A triangle's first vertex points east at rotation 0 -> rotate to compass course.
        folium.RegularPolygonMarker(
            location=(f.lat, f.lon),
            number_of_sides=3,
            radius=7,
            rotation=f.course_deg - 90,
            color="#222222",
            weight=1,
            fill=True,
            fill_color="#ffffff",
            fill_opacity=0.9,
        ).add_to(group)
    group.add_to(m)


def _popup_html(f: Fix) -> str:
    rows = [("Time (UTC)", str(f.timestamp))]
    if f.speed_knots is not None:
        rows.append(("Speed", f"{f.speed_kmh:.1f} km/h ({f.speed_knots:.1f} kn)"))
    if f.course_deg is not None:
        rows.append(("Course", f"{f.course_deg:.1f}°"))
    if f.num_sats is not None:
        rows.append(("Satellites", str(f.num_sats)))
    if f.fix_quality is not None:
        rows.append(("Fix quality", str(f.fix_quality)))
    rows.append(("Position", f"{f.lat:.6f}, {f.lon:.6f}"))
    body = "".join(f"<tr><th style='text-align:left;padding-right:8px'>{k}</th><td>{v}</td></tr>" for k, v in rows)
    return f"<table>{body}</table>"


def _add_fix_popups(m: folium.Map, track: Track) -> None:
    group = folium.FeatureGroup(name="Fixes")
    for f in track.fixes:
        folium.CircleMarker(
            location=(f.lat, f.lon),
            radius=3,
            color="#333333",
            weight=1,
            fill=True,
            fill_opacity=0.8,
            popup=folium.Popup(_popup_html(f), max_width=320),
            tooltip=str(f.timestamp),
        ).add_to(group)
    group.add_to(m)


def _add_start_end(m: folium.Map, track: Track) -> None:
    group = folium.FeatureGroup(name="Start / End")
    folium.Marker(
        (track.start.lat, track.start.lon),
        tooltip="Start",
        popup=folium.Popup(_popup_html(track.start), max_width=320),
        icon=folium.Icon(color="green", icon="play"),
    ).add_to(group)
    folium.Marker(
        (track.end.lat, track.end.lon),
        tooltip="End",
        popup=folium.Popup(_popup_html(track.end), max_width=320),
        icon=folium.Icon(color="red", icon="stop"),
    ).add_to(group)
    group.add_to(m)
