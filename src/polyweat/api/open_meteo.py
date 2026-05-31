"""Open-Meteo client - free weather forecast & geocoding (no API key).

Docs: https://open-meteo.com/en/docs and
      https://open-meteo.com/en/docs/geocoding-api
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from polyweat.api._http import get_json
from polyweat.logger import get_logger
from polyweat.models import WeatherForecast

log = get_logger("open_meteo")


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso_naive(s: str, tz: Optional[str]) -> Optional[datetime]:
    """Open-Meteo returns naive ISO timestamps relative to the requested TZ."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None and tz and ZoneInfo is not None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        except Exception:
            dt = dt.replace(tzinfo=timezone.utc)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class OpenMeteoClient:
    def __init__(
        self,
        forecast_base: str,
        geocode_base: str,
        timeout: float = 15.0,
    ):
        self.forecast_base = forecast_base.rstrip("/")
        self.geocode_base = geocode_base.rstrip("/")
        self.timeout = timeout

    # ----- geocoding -----

    def geocode(self, name: str) -> Optional[Tuple[float, float, str, str]]:
        """Resolve a city name to (lat, lon, timezone, canonical_name)."""
        if not name:
            return None
        try:
            data = get_json(
                f"{self.geocode_base}/search",
                params={"name": name, "count": 1, "language": "en", "format": "json"},
                timeout=self.timeout,
            )
        except Exception as exc:
            log.warning("Geocoding failed for %s: %s", name, exc)
            return None

        results = data.get("results") or []
        if not results:
            return None
        r = results[0]
        lat = _f(r.get("latitude"))
        lon = _f(r.get("longitude"))
        tz = r.get("timezone") or "UTC"
        canonical = r.get("name") or name
        if lat is None or lon is None:
            return None
        return (lat, lon, tz, canonical)

    # ----- forecast -----

    def fetch_forecast(
        self,
        lat: float,
        lon: float,
        tz: str = "auto",
        *,
        days: int = 3,
    ) -> Optional[Dict[str, Any]]:
        """Fetch hourly + daily temperature forecast in CELSIUS."""
        params = {
            "latitude": lat,
            "longitude": lon,
            "timezone": tz,
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "hourly": "temperature_2m",
            "daily": "temperature_2m_max,temperature_2m_min",
            "forecast_days": days,
            "past_days": 0,
        }
        try:
            return get_json(
                f"{self.forecast_base}/forecast",
                params=params,
                timeout=self.timeout,
            )
        except Exception as exc:
            log.warning("Forecast fetch failed (%s,%s): %s", lat, lon, exc)
            return None

    def build_forecast(
        self,
        city: str,
        lat: float,
        lon: float,
        tz: str,
        target_date: Optional[datetime],
    ) -> Optional[WeatherForecast]:
        """Hit the API and return a WeatherForecast scoped to the target day.

        ``target_date`` is the LOCAL day (in the city's TZ) the market will
        resolve. We compute window high/low from the hourly array on that
        day, plus the daily extremes returned by Open-Meteo as a sanity check.
        """
        raw = self.fetch_forecast(lat, lon, tz=tz)
        if raw is None:
            return None

        # Open-Meteo returns the *resolved* timezone in the response payload.
        # We must use that one (NOT the request value) to interpret hourly
        # timestamps - otherwise tz="auto" would silently parse as UTC.
        resolved_tz = raw.get("timezone") or (tz if tz and tz != "auto" else "UTC")

        hourly = raw.get("hourly") or {}
        daily = raw.get("daily") or {}
        h_times = hourly.get("time") or []
        h_temps = hourly.get("temperature_2m") or []

        parsed_times: List[datetime] = []
        parsed_temps: List[float] = []
        for ts, tval in zip(h_times, h_temps):
            dt = _parse_iso_naive(ts, resolved_tz)
            tv = _f(tval)
            if dt is not None and tv is not None:
                parsed_times.append(dt)
                parsed_temps.append(tv)

        # Daily extremes - prefer the day matching target_date in the
        # *resolved* TZ.
        d_times = daily.get("time") or []
        d_max = daily.get("temperature_2m_max") or []
        d_min = daily.get("temperature_2m_min") or []
        target_day_str: Optional[str] = None
        if target_date is not None:
            try:
                local = target_date
                if ZoneInfo is not None and resolved_tz:
                    local = target_date.astimezone(ZoneInfo(resolved_tz))
                target_day_str = local.date().isoformat()
            except Exception:
                target_day_str = None

        daily_high: Optional[float] = None
        daily_low: Optional[float] = None
        if target_day_str and target_day_str in d_times:
            i = d_times.index(target_day_str)
            daily_high = _f(d_max[i] if i < len(d_max) else None)
            daily_low = _f(d_min[i] if i < len(d_min) else None)
        elif d_max:
            daily_high = _f(d_max[0])
            daily_low = _f(d_min[0]) if d_min else None

        # Window high/low: the hourly samples that fall inside the local day.
        win_high: Optional[float] = None
        win_low: Optional[float] = None
        if target_day_str:
            same_day = [
                t for ts, t in zip(parsed_times, parsed_temps)
                if ts.date().isoformat() == target_day_str
            ]
            if same_day:
                win_high = max(same_day)
                win_low = min(same_day)
        if win_high is None:
            win_high = daily_high
        if win_low is None:
            win_low = daily_low

        return WeatherForecast(
            city=city,
            lat=lat,
            lon=lon,
            tz=resolved_tz,
            fetched_at=datetime.now(timezone.utc),
            hourly_times=parsed_times,
            hourly_temps_c=parsed_temps,
            daily_high_c=daily_high,
            daily_low_c=daily_low,
            forecast_window_high_c=win_high,
            forecast_window_low_c=win_low,
            raw_provider="open_meteo",
        )
