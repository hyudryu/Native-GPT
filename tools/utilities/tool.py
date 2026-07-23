"""Utilities Strands tools — deterministic everyday conversions and hashing.

Multi-tool folder: `TOOL` is a list of Strands tools. Everything here is pure
stdlib computation; `hash_file` reads files through `tools/_lib/paths.py`
(allowed-root confinement), loaded by file path like the other tools.

`parse_datetime` supports only DETERMINISTIC formats (documented in its
docstring): ISO 8601 plus a small list of common explicit date/datetime
layouts. Relative natural language ("tomorrow", "next Friday") is NOT
supported by design — ambiguity has no place in a deterministic utility.
"""

from __future__ import annotations

import hashlib
import importlib.util
import uuid as _uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from strands import tool

# Load the shared `_lib/paths.py` as a module (no package context when the
# runtime imports this file standalone).
_LIB_PATH = Path(__file__).resolve().parent.parent / "_lib" / "paths.py"
_spec = importlib.util.spec_from_file_location("agentgpt_tools_paths", _LIB_PATH)
assert _spec is not None and _spec.loader is not None
_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_paths)
resolve_under_root = _paths.resolve_under_root
PathEscapeError = _paths.PathEscapeError

HASH_CHUNK_BYTES = 64 * 1024
HASH_ALGORITHMS = ("sha256", "sha512", "sha1", "md5")


class UtilityError(ValueError):
    """Any utilities-tool failure; `code` becomes the result's error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _result(summary: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "summary": summary, "data": data, "error": None}


def _failure(code: str, summary: str, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "summary": summary,
        "data": data or {},
        "error": {"code": code, "message": message},
    }


# ── Unit conversion ─────────────────────────────────────────────────────────

# Each linear category maps unit alias -> factor to the category base unit.
_LENGTH_M = {
    "mm": 0.001, "millimeter": 0.001, "millimeters": 0.001,
    "cm": 0.01, "centimeter": 0.01, "centimeters": 0.01,
    "m": 1.0, "meter": 1.0, "meters": 1.0,
    "km": 1000.0, "kilometer": 1000.0, "kilometers": 1000.0,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
    "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
}
_MASS_G = {
    "mg": 0.001, "milligram": 0.001, "milligrams": 0.001,
    "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "t": 1_000_000.0, "tonne": 1_000_000.0, "tonnes": 1_000_000.0,
    "oz": 28.349523125, "ounce": 28.349523125, "ounces": 28.349523125,
    "lb": 453.59237, "pound": 453.59237, "pounds": 453.59237,
}
_VOLUME_L = {
    "ml": 0.001, "milliliter": 0.001, "milliliters": 0.001,
    "l": 1.0, "liter": 1.0, "liters": 1.0,
    "m3": 1000.0, "cubic_meter": 1000.0,
    "tsp": 0.00492892159375, "teaspoon": 0.00492892159375,
    "tbsp": 0.01478676478125, "tablespoon": 0.01478676478125,
    "floz": 0.0295735295625, "fluid_ounce": 0.0295735295625,
    "cup": 0.2365882365,
    "pt": 0.473176473, "pint": 0.473176473,
    "qt": 0.946352946, "quart": 0.946352946,
    "gal": 3.785411784, "gallon": 3.785411784,
}
_DATA_BYTES = {
    "b": 1.0, "byte": 1.0, "bytes": 1.0,
    "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12, "pb": 1e15,
    "kib": 1024.0, "mib": 1024.0**2, "gib": 1024.0**3, "tib": 1024.0**4, "pib": 1024.0**5,
    "bit": 0.125, "bits": 0.125,
}
_SPEED_MPS = {
    "m/s": 1.0, "mps": 1.0,
    "km/h": 1 / 3.6, "kmh": 1 / 3.6, "kph": 1 / 3.6,
    "mph": 0.44704,
    "kn": 0.514444, "knot": 0.514444, "knots": 0.514444,
    "ft/s": 0.3048, "fps": 0.3048,
}
_AREA_M2 = {
    "mm2": 1e-6, "cm2": 1e-4, "m2": 1.0, "km2": 1e6,
    "ha": 1e4, "hectare": 1e4, "hectares": 1e4,
    "ft2": 0.09290304, "sqft": 0.09290304,
    "yd2": 0.83612736,
    "acre": 4046.8564224, "acres": 4046.8564224,
    "mi2": 2_589_988.110336,
}

_CATEGORIES: dict[str, dict[str, float]] = {
    "length": _LENGTH_M,
    "mass": _MASS_G,
    "volume": _VOLUME_L,
    "data": _DATA_BYTES,
    "speed": _SPEED_MPS,
    "area": _AREA_M2,
}

_TEMPERATURE_ALIASES = {
    "c": "celsius", "celsius": "celsius", "°c": "celsius",
    "f": "fahrenheit", "fahrenheit": "fahrenheit", "°f": "fahrenheit",
    "k": "kelvin", "kelvin": "kelvin",
}


def _normalize_unit(unit: Any) -> str:
    if not isinstance(unit, str) or not unit.strip():
        raise UtilityError("invalid_unit", "units must be non-empty strings")
    return unit.strip().lower().replace(" ", "_")


def _to_celsius(value: float, unit: str) -> float:
    canonical = _TEMPERATURE_ALIASES[unit]
    if canonical == "celsius":
        return value
    if canonical == "fahrenheit":
        return (value - 32.0) * 5.0 / 9.0
    return value - 273.15


def _from_celsius(value: float, unit: str) -> float:
    canonical = _TEMPERATURE_ALIASES[unit]
    if canonical == "celsius":
        return value
    if canonical == "fahrenheit":
        return value * 9.0 / 5.0 + 32.0
    return value + 273.15


def _convert_units(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    src = _normalize_unit(from_unit)
    dst = _normalize_unit(to_unit)

    src_is_temp = src in _TEMPERATURE_ALIASES
    dst_is_temp = dst in _TEMPERATURE_ALIASES
    if src_is_temp or dst_is_temp:
        if not (src_is_temp and dst_is_temp):
            raise UtilityError(
                "incompatible_units",
                f"cannot convert between temperature and a non-temperature unit "
                f"({from_unit!r} -> {to_unit!r})",
            )
        result = _from_celsius(_to_celsius(value, src), dst)
        return _result(
            f"{value} {from_unit} = {result:g} {to_unit}",
            {
                "value": value,
                "from_unit": from_unit,
                "to_unit": to_unit,
                "result": result,
                "category": "temperature",
            },
        )

    src_category = next((c for c, table in _CATEGORIES.items() if src in table), None)
    dst_category = next((c for c, table in _CATEGORIES.items() if dst in table), None)
    if src_category is None:
        raise UtilityError("unknown_unit", f"unknown unit: {from_unit!r}")
    if dst_category is None:
        raise UtilityError("unknown_unit", f"unknown unit: {to_unit!r}")
    if src_category != dst_category:
        raise UtilityError(
            "incompatible_units",
            f"cannot convert {src_category} unit {from_unit!r} to "
            f"{dst_category} unit {to_unit!r}",
        )
    table = _CATEGORIES[src_category]
    result = value * table[src] / table[dst]
    return _result(
        f"{value} {from_unit} = {result:g} {to_unit}",
        {
            "value": value,
            "from_unit": from_unit,
            "to_unit": to_unit,
            "result": result,
            "category": src_category,
        },
    )


@tool
def convert_units(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    """Convert a numeric value between units of the same category.

    Categories and example units:
      length      mm cm m km in ft yd mi
      mass        mg g kg t oz lb
      temperature c f k (celsius/fahrenheit/kelvin)
      volume      ml l m3 tsp tbsp floz cup pt qt gal
      data        b kb mb gb tb pb (decimal) and kib mib gib tib pib (binary)
      speed       m/s km/h mph kn ft/s
      area        mm2 cm2 m2 km2 ha ft2 yd2 acre mi2

    Args:
        value: The numeric amount to convert.
        from_unit: Source unit (case-insensitive; common aliases accepted).
        to_unit: Target unit. Must belong to the same category.

    Returns:
        `{ok, summary, data: {value, from_unit, to_unit, result, category},
        error}`. Errors: unknown_unit, incompatible_units.
    """

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return _failure("invalid_value", "value must be numeric", f"value must be numeric: {value!r}")
    try:
        return _convert_units(numeric, from_unit, to_unit)
    except UtilityError as exc:
        return _failure(exc.code, "conversion failed", str(exc))


# ── Datetime helpers ────────────────────────────────────────────────────────

# Deterministic layouts tried in order after ISO 8601. No relative/natural
# language forms ("tomorrow", "next week") — deliberately unsupported.
_DATETIME_LAYOUTS = (
    "%Y-%m-%d %H:%M:%S",   # 2026-07-23 14:00:00
    "%Y-%m-%d %H:%M",      # 2026-07-23 14:00
    "%Y-%m-%dT%H:%M",      # 2026-07-23T14:00 (ISO without seconds)
    "%Y-%m-%d",            # 2026-07-23
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
    "%B %d %Y",            # July 23 2026
    "%B %d, %Y",           # July 23, 2026
    "%b %d %Y",            # Jul 23 2026
    "%b %d, %Y",
    "%d %B %Y",            # 23 July 2026
    "%d %b %Y",
    "%m/%d/%Y",            # 07/23/2026 (US)
    "%m/%d/%Y %H:%M",
    "%m-%d-%Y",
)

def _parse_datetime(text: str) -> tuple[datetime, str]:
    """Parse `text` into a datetime; returns (dt, matched_format_name).

    Naive unless the text itself carries an offset (ISO 8601).
    """
    if not isinstance(text, str) or not text.strip():
        raise UtilityError("unparseable_datetime", "datetime text must be a non-empty string")
    cleaned = text.strip()
    # ISO 8601 first (Python 3.11+ fromisoformat handles 'Z' and offsets).
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        return parsed, "iso8601"
    except ValueError:
        pass
    for layout in _DATETIME_LAYOUTS:
        try:
            return datetime.strptime(cleaned, layout), layout
        except ValueError:
            continue
    raise UtilityError(
        "unparseable_datetime",
        f"could not parse {text!r}; supported forms: ISO 8601 "
        "(2026-07-23, 2026-07-23T14:00:00+02:00), '2026-07-23 14:00', "
        "'July 23 2026', '23 July 2026', '07/23/2026'. Relative natural "
        "language ('tomorrow') is not supported.",
    )


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UtilityError("unknown_timezone", f"unknown IANA timezone: {name!r}") from exc


@tool
def parse_datetime(text: str, timezone: str | None = None) -> dict[str, Any]:
    """Parse a deterministic date/datetime string into a structured result.

    Supported forms (deterministic only):
      - ISO 8601: "2026-07-23", "2026-07-23T14:00:00", "...+02:00", "...Z"
      - "2026-07-23 14:00" / "2026-07-23 14:00:00"
      - "July 23 2026", "July 23, 2026", "Jul 23 2026"
      - "23 July 2026", "23 Jul 2026"
      - "07/23/2026" (US month-first) and "2026/07/23"
    Relative natural language ("tomorrow", "next Friday") is NOT supported.

    Args:
        text: The date/datetime string.
        timezone: Optional IANA name (e.g. "America/New_York"). Attached to
            naive inputs; offsets already present in the text win.

    Returns:
        `{ok, summary, data: {iso, date, time, timezone, utc_offset,
        unix_timestamp, weekday, matched_format}, error}`.
    """

    try:
        parsed, matched = _parse_datetime(text)
        if timezone is not None and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_zone(timezone))
        data = {
            "iso": parsed.isoformat(),
            "date": parsed.date().isoformat(),
            "time": parsed.time().isoformat(),
            "timezone": str(parsed.tzinfo) if parsed.tzinfo else None,
            "utc_offset": (
                parsed.utcoffset().total_seconds() / 3600 if parsed.tzinfo else None
            ),
            "unix_timestamp": parsed.timestamp() if parsed.tzinfo else None,
            "weekday": parsed.strftime("%A"),
            "matched_format": matched,
        }
        return _result(f"parsed {text!r} -> {data['iso']}", data)
    except UtilityError as exc:
        return _failure(exc.code, "could not parse datetime", str(exc), {"text": text})


@tool
def convert_timezone(datetime: str, from_timezone: str, to_timezone: str) -> dict[str, Any]:
    """Convert a datetime from one IANA timezone to another.

    Args:
        datetime: Date/datetime string (same deterministic forms as
            parse_datetime). Naive values are interpreted in `from_timezone`.
        from_timezone: IANA name, e.g. "Europe/Berlin".
        to_timezone: IANA name, e.g. "America/New_York".

    Returns:
        `{ok, summary, data: {input, from_timezone, to_timezone, result_iso,
        utc_offset_hours, unix_timestamp}, error}`.
    """

    try:
        src = _zone(from_timezone)
        dst = _zone(to_timezone)
        parsed, _ = _parse_datetime(datetime)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=src)
        converted = parsed.astimezone(dst)
        data = {
            "input": datetime,
            "from_timezone": from_timezone,
            "to_timezone": to_timezone,
            "result_iso": converted.isoformat(),
            "utc_offset_hours": (converted.utcoffset() or parsed.utcoffset()).total_seconds() / 3600,
            "unix_timestamp": converted.timestamp(),
        }
        return _result(
            f"{parsed.isoformat()} = {converted.isoformat()}",
            data,
        )
    except UtilityError as exc:
        return _failure(exc.code, "timezone conversion failed", str(exc), {"datetime": datetime})


def _whole_months(start: date, end: date) -> int:
    """Calendar-aware whole months between two dates (sign-aware)."""
    if start == end:
        return 0
    sign = 1
    if end < start:
        start, end = end, start
        sign = -1
    months = (end.year - start.year) * 12 + (end.month - start.month)
    # Subtract one if the end day hasn't reached the start day-of-month.
    if end.day < start.day:
        months -= 1
    return sign * months


@tool
def date_difference(start: str, end: str, unit: str | None = None) -> dict[str, Any]:
    """Compute the difference between two dates/datetimes.

    Args:
        start: Start date/datetime (same forms as parse_datetime).
        end: End date/datetime.
        unit: Optional single unit to report: "days", "weeks", "months",
            "years", or "seconds". Months/years are calendar-aware (whole
            months between calendar dates). Omit to get all units at once.

    Returns:
        `{ok, summary, data: {start, end, days, weeks, months, years,
        seconds} (or just the requested unit), error}`. Signed: negative when
        `end` is before `start`.
    """

    try:
        start_dt, _ = _parse_datetime(start)
        end_dt, _ = _parse_datetime(end)
    except UtilityError as exc:
        return _failure(exc.code, "could not parse dates", str(exc))

    delta_seconds = (end_dt - start_dt).total_seconds()
    days = delta_seconds / 86400.0
    months = _whole_months(start_dt.date(), end_dt.date())
    all_units = {
        "seconds": delta_seconds,
        "days": days,
        "weeks": days / 7.0,
        "months": months,
        "years": months / 12.0,
    }
    if unit is not None:
        key = unit.strip().lower()
        if key not in all_units:
            return _failure(
                "unknown_unit",
                "unknown difference unit",
                f"unit must be one of {sorted(all_units)} (got {unit!r})",
            )
        return _result(
            f"{start} -> {end}: {all_units[key]:g} {key}",
            {"start": start, "end": end, "unit": key, "value": all_units[key]},
        )
    return _result(
        f"{start} -> {end}: {all_units['days']:g} days ({months} months)",
        {"start": start, "end": end, **all_units},
    )


# ── UUIDs and hashing ───────────────────────────────────────────────────────


@tool
def generate_uuid(version: int = 4) -> dict[str, Any]:
    """Generate a UUID.

    Args:
        version: 4 (random, default) or 1 (timestamp + node).

    Returns:
        `{ok, summary, data: {uuid, version}, error}`.
    """

    try:
        v = int(version)
    except (TypeError, ValueError):
        return _failure("invalid_version", "version must be 1 or 4", f"version must be 1 or 4: {version!r}")
    if v == 4:
        value = str(_uuid.uuid4())
    elif v == 1:
        value = str(_uuid.uuid1())
    else:
        return _failure("invalid_version", "version must be 1 or 4", f"unsupported UUID version: {v}")
    return _result(value, {"uuid": value, "version": v})


def _new_hasher(algorithm: Any) -> Any:
    if not isinstance(algorithm, str):
        raise UtilityError("unknown_algorithm", "algorithm must be a string")
    name = algorithm.strip().lower().replace("-", "")
    if name not in HASH_ALGORITHMS:
        raise UtilityError(
            "unknown_algorithm",
            f"unsupported hash algorithm {algorithm!r}; supported: {', '.join(HASH_ALGORITHMS)}",
        )
    return hashlib.new(name)


@tool
def hash_text(content: str, algorithm: str = "sha256") -> dict[str, Any]:
    """Hash text content with sha256 (default), sha512, sha1, or md5.

    Args:
        content: The text to hash (UTF-8 encoded before hashing).
        algorithm: "sha256" | "sha512" | "sha1" | "md5".

    Returns:
        `{ok, summary, data: {algorithm, hexdigest, bytes}, error}`.
    """

    if not isinstance(content, str):
        return _failure("invalid_content", "content must be a string", "content must be a string")
    try:
        hasher = _new_hasher(algorithm)
    except UtilityError as exc:
        return _failure(exc.code, "unknown algorithm", str(exc))
    encoded = content.encode("utf-8")
    hasher.update(encoded)
    name = hasher.name
    return _result(
        f"{name}:{hasher.hexdigest()}",
        {"algorithm": name, "hexdigest": hasher.hexdigest(), "bytes": len(encoded)},
    )


@tool
def hash_file(path: str, algorithm: str = "sha256") -> dict[str, Any]:
    """Hash a file under the allowed roots, read in 64 KB chunks.

    Args:
        path: Path relative to the repo root, or absolute inside an allowed
            root.
        algorithm: "sha256" (default) | "sha512" | "sha1" | "md5".

    Returns:
        `{ok, summary, data: {path, algorithm, hexdigest, bytes}, error}`.
    """

    try:
        hasher = _new_hasher(algorithm)
        if not isinstance(path, str) or not path.strip():
            raise UtilityError("invalid_path", "path must be a non-empty string")
        resolved = resolve_under_root(path)
        if not resolved.is_file():
            raise UtilityError("not_found", f"file not found: {path}")
        size = 0
        with resolved.open("rb") as fp:
            while chunk := fp.read(HASH_CHUNK_BYTES):
                hasher.update(chunk)
                size += len(chunk)
    except (UtilityError, PathEscapeError) as exc:
        code = exc.code if isinstance(exc, UtilityError) else "path_escape"
        return _failure(code, "could not hash file", str(exc), {"path": path})
    except OSError as exc:
        return _failure("io_error", "OS error hashing file", str(exc), {"path": path})
    return _result(
        f"{hasher.name}:{hasher.hexdigest()} ({size} bytes)",
        {"path": path, "algorithm": hasher.name, "hexdigest": hasher.hexdigest(), "bytes": size},
    )


TOOL = [
    convert_units,
    convert_timezone,
    parse_datetime,
    date_difference,
    generate_uuid,
    hash_text,
    hash_file,
]
