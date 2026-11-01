"""Visualization options: a dataclass tree loadable from / dumpable to YAML."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class BasemapOptions:
    default: str = "OpenStreetMap"
    extra_layers: list[str] = field(default_factory=lambda: ["OpenTopoMap", "Esri.WorldImagery"])
    layer_switcher: bool = True


@dataclass
class PngOptions:
    enabled: bool = True
    width: int = 1600
    height: int = 1000
    wait_s: float = 2.0


@dataclass
class MapOptions:
    speed_colored_line: bool = True
    line_color: str = "#3388ff"
    line_weight: int = 5
    colormap: str = "viridis"
    start_end_markers: bool = True
    fix_popups: bool = True
    heading_arrows: bool = True
    arrow_every_n: int = 3
    fit_bounds: bool = True
    open_browser: bool = True
    serve_port: int = 0  # local HTTP server port when opening the browser; 0 = any free port
    basemap: BasemapOptions = field(default_factory=BasemapOptions)
    png: PngOptions = field(default_factory=PngOptions)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MapOptions":
        return _merge(cls(), data or {})

    @classmethod
    def from_yaml(cls, path: str | Path) -> "MapOptions":
        with Path(path).open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data is not None and not isinstance(data, dict):
            raise ValueError(f"{path}: top level must be a mapping")
        return cls.from_dict(data)

    def merged(self, overrides: dict[str, Any]) -> "MapOptions":
        """Return a copy with (possibly nested) overrides applied; None values are ignored."""
        return _merge(dataclasses.replace(self), overrides)


def _merge(obj, data: dict[str, Any], prefix: str = ""):
    names = {f.name: f for f in dataclasses.fields(obj)}
    unknown = set(data) - set(names)
    if unknown:
        raise ValueError(f"Unknown option(s): {', '.join(prefix + u for u in sorted(unknown))}")
    changes = {}
    for key, value in data.items():
        if value is None:
            continue
        current = getattr(obj, key)
        if dataclasses.is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"Option {prefix}{key} must be a mapping")
            changes[key] = _merge(dataclasses.replace(current), value, f"{prefix}{key}.")
        else:
            changes[key] = value
    return dataclasses.replace(obj, **changes)
