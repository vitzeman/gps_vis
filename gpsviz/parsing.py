"""NMEA parsing: turn GGA/RMC sentences into a Track of Fix objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path

import pynmea2

KNOTS_TO_KMH = 1.852


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

    @property
    def speed_kmh(self) -> float | None:
        return None if self.speed_knots is None else self.speed_knots * KNOTS_TO_KMH


@dataclass
class Track:
    fixes: list[Fix] = field(default_factory=list)
    source: str | None = None

    def __len__(self) -> int:
        return len(self.fixes)

    @property
    def points(self) -> list[tuple[float, float]]:
        return [(f.lat, f.lon) for f in self.fixes]

    @property
    def bounds(self) -> list[list[float]]:
        lats = [f.lat for f in self.fixes]
        lons = [f.lon for f in self.fixes]
        return [[min(lats), min(lons)], [max(lats), max(lons)]]

    @property
    def start(self) -> Fix:
        return self.fixes[0]

    @property
    def end(self) -> Fix:
        return self.fixes[-1]


def _key(t: time) -> str:
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
    path = Path(path)
    with path.open(encoding="ascii", errors="replace") as fh:
        track = parse_lines(fh, source=str(path), check_checksum=check_checksum)
    if not track.fixes:
        raise ValueError(f"No valid RMC fixes found in {path}")
    return track
