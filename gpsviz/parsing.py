"""NMEA parsing: turn RMC/GGA/GNS/GLL/VTG/ZDA sentences into a Track of Fix objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path

import pynmea2

KNOTS_TO_KMH = 1.852


@dataclass
class Fix:
    """A single position fix, merged from all sentences sharing its UTC time."""

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


def _float(v) -> float | None:
    """Convert an NMEA field to float; empty or malformed fields become None."""
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _int(v) -> int | None:
    """Convert an NMEA field to int; empty or malformed fields become None."""
    f = _float(v)
    return int(f) if f is not None else None


def _position(msg) -> tuple[float, float] | None:
    """(lat, lon) of a sentence that carries a valid position, else None."""
    if not msg.lat or not msg.lon:
        return None
    if isinstance(msg, pynmea2.RMC) and msg.status != "A":
        return None
    if isinstance(msg, pynmea2.GLL) and msg.status != "A":
        return None
    if isinstance(msg, pynmea2.GGA) and not _int(msg.gps_qual):
        return None
    if isinstance(msg, pynmea2.GNS) and set(msg.mode_indicator or "N") <= {"N"}:
        return None
    try:
        return msg.latitude, msg.longitude
    except (TypeError, ValueError):
        return None


@dataclass
class _Epoch:
    """Everything reported for one fix time, gathered from any sentence type."""

    utc_time: time
    utc_date: date | None = None
    pos: tuple[float, float] | None = None
    data: dict = field(default_factory=dict)

    def set(self, **values) -> None:
        """Fill in values not already known (earlier sentences take priority)."""
        for k, v in values.items():
            if v is not None and self.data.get(k) is None:
                self.data[k] = v


def parse_lines(lines, source: str | None = None, check_checksum: bool = True) -> Track:
    """Parse an iterable of NMEA lines. Malformed or unknown sentences are skipped.

    A fix needs a position from any of RMC, GGA, GNS or GLL; sentences with the
    same UTC time are merged into one fix. VTG (no time field) adds speed/course
    to the fix being reported, and ZDA/RMC supply the date. Fixes without any
    date sentence keep a time-only timestamp.
    """
    epochs: list[_Epoch] = []
    current_date: date | None = None

    for raw in lines:
        start = raw.find("$")  # tolerate logger prefixes before the sentence
        if start < 0:
            continue
        try:
            msg = pynmea2.parse(raw[start:].strip(), check=check_checksum)
        except (pynmea2.ParseError, ValueError, AttributeError):
            continue

        if isinstance(msg, pynmea2.VTG):
            if epochs:
                epochs[-1].set(
                    speed_knots=_float(msg.spd_over_grnd_kts), course_deg=_float(msg.true_track)
                )
            continue

        t = getattr(msg, "timestamp", None)
        if not isinstance(t, time):
            continue
        if isinstance(msg, pynmea2.ZDA):
            if isinstance(msg.datestamp, date):
                current_date = msg.datestamp
            continue
        if not isinstance(msg, (pynmea2.RMC, pynmea2.GGA, pynmea2.GNS, pynmea2.GLL)):
            continue

        # Sentences of one fix are emitted together; a new time starts a new fix.
        if not epochs or epochs[-1].utc_time != t:
            epochs.append(_Epoch(utc_time=t, utc_date=current_date))
        ep = epochs[-1]

        pos = _position(msg)
        if pos is not None and ep.pos is None:
            ep.pos = pos
        if isinstance(msg, pynmea2.RMC):
            if isinstance(msg.datestamp, date):
                current_date = ep.utc_date = msg.datestamp
            if pos is not None:
                ep.set(speed_knots=_float(msg.spd_over_grnd), course_deg=_float(msg.true_course))
        elif isinstance(msg, pynmea2.GGA) and pos is not None:
            ep.set(
                altitude_m=_float(msg.altitude),
                num_sats=_int(msg.num_sats),
                fix_quality=_int(msg.gps_qual),
                hdop=_float(msg.horizontal_dil),
            )
        elif isinstance(msg, pynmea2.GNS) and pos is not None:
            ep.set(altitude_m=_float(msg.altitude), num_sats=_int(msg.num_sats), hdop=_float(msg.hdop))

    fixes = []
    for ep in epochs:
        if ep.pos is None:
            continue
        ts: datetime | time = ep.utc_time
        if ep.utc_date is not None:
            ts = datetime.combine(ep.utc_date, ep.utc_time).replace(tzinfo=timezone.utc)
        fixes.append(Fix(timestamp=ts, lat=ep.pos[0], lon=ep.pos[1], **ep.data))
    return Track(fixes=fixes, source=source)


def load_track(path: str | Path, check_checksum: bool = True) -> Track:
    path = Path(path)
    with path.open(encoding="ascii", errors="replace") as fh:
        track = parse_lines(fh, source=str(path), check_checksum=check_checksum)
    if not track.fixes:
        raise ValueError(f"No valid position fixes (RMC/GGA/GNS/GLL) found in {path}")
    return track
