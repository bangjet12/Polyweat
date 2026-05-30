"""Static dictionary of city aliases used by Polymarket weather markets.

Resolution order is:
  1. Exact-match an alias from this table (fast, no network call)
  2. Fall back to Open-Meteo geocoding for anything not in this list

Coordinates and TZ are filled at parse-time only when we *don't* hit the
geocoder, so the values here are intentionally conservative & well-known.
"""

from __future__ import annotations

from typing import Dict, Tuple


# alias -> (canonical_name, lat, lon, tz)
KNOWN_CITIES: Dict[str, Tuple[str, float, float, str]] = {
    # ----- US majors (most Polymarket weather markets target these) -----
    "nyc":            ("New York",      40.7128,  -74.0060, "America/New_York"),
    "new york":       ("New York",      40.7128,  -74.0060, "America/New_York"),
    "new york city":  ("New York",      40.7128,  -74.0060, "America/New_York"),
    "manhattan":      ("New York",      40.7831,  -73.9712, "America/New_York"),
    "central park":   ("New York",      40.7829,  -73.9654, "America/New_York"),
    "la":             ("Los Angeles",   34.0522, -118.2437, "America/Los_Angeles"),
    "los angeles":    ("Los Angeles",   34.0522, -118.2437, "America/Los_Angeles"),
    "chicago":        ("Chicago",       41.8781,  -87.6298, "America/Chicago"),
    "miami":          ("Miami",         25.7617,  -80.1918, "America/New_York"),
    "houston":        ("Houston",       29.7604,  -95.3698, "America/Chicago"),
    "phoenix":        ("Phoenix",       33.4484, -112.0740, "America/Phoenix"),
    "philadelphia":   ("Philadelphia",  39.9526,  -75.1652, "America/New_York"),
    "philly":         ("Philadelphia",  39.9526,  -75.1652, "America/New_York"),
    "dallas":         ("Dallas",        32.7767,  -96.7970, "America/Chicago"),
    "san francisco":  ("San Francisco", 37.7749, -122.4194, "America/Los_Angeles"),
    "sf":             ("San Francisco", 37.7749, -122.4194, "America/Los_Angeles"),
    "seattle":        ("Seattle",       47.6062, -122.3321, "America/Los_Angeles"),
    "boston":         ("Boston",        42.3601,  -71.0589, "America/New_York"),
    "denver":         ("Denver",        39.7392, -104.9903, "America/Denver"),
    "atlanta":        ("Atlanta",       33.7490,  -84.3880, "America/New_York"),
    "las vegas":      ("Las Vegas",     36.1699, -115.1398, "America/Los_Angeles"),
    "vegas":          ("Las Vegas",     36.1699, -115.1398, "America/Los_Angeles"),
    "austin":         ("Austin",        30.2672,  -97.7431, "America/Chicago"),
    "washington":     ("Washington",    38.9072,  -77.0369, "America/New_York"),
    "washington dc":  ("Washington",    38.9072,  -77.0369, "America/New_York"),
    "dc":             ("Washington",    38.9072,  -77.0369, "America/New_York"),
    "detroit":        ("Detroit",       42.3314,  -83.0458, "America/Detroit"),
    "orlando":        ("Orlando",       28.5383,  -81.3792, "America/New_York"),
    "portland":       ("Portland",      45.5152, -122.6784, "America/Los_Angeles"),
    "minneapolis":    ("Minneapolis",   44.9778,  -93.2650, "America/Chicago"),
    "honolulu":       ("Honolulu",      21.3069, -157.8583, "Pacific/Honolulu"),
    "san diego":      ("San Diego",     32.7157, -117.1611, "America/Los_Angeles"),
    "tampa":          ("Tampa",         27.9506,  -82.4572, "America/New_York"),
    "san antonio":    ("San Antonio",   29.4241,  -98.4936, "America/Chicago"),
    "kansas city":    ("Kansas City",   39.0997,  -94.5786, "America/Chicago"),
    "salt lake city": ("Salt Lake City",40.7608, -111.8910, "America/Denver"),
    "anchorage":      ("Anchorage",     61.2181, -149.9003, "America/Anchorage"),
    "buffalo":        ("Buffalo",       42.8864,  -78.8784, "America/New_York"),
    "milwaukee":      ("Milwaukee",     43.0389,  -87.9065, "America/Chicago"),
    "cleveland":      ("Cleveland",     41.4993,  -81.6944, "America/New_York"),
    "pittsburgh":     ("Pittsburgh",    40.4406,  -79.9959, "America/New_York"),
    "st louis":       ("St. Louis",     38.6270,  -90.1994, "America/Chicago"),
    "saint louis":    ("St. Louis",     38.6270,  -90.1994, "America/Chicago"),

    # ----- World cities (occasionally appear) -----
    "london":         ("London",        51.5074,   -0.1278, "Europe/London"),
    "paris":          ("Paris",         48.8566,    2.3522, "Europe/Paris"),
    "berlin":         ("Berlin",        52.5200,   13.4050, "Europe/Berlin"),
    "madrid":         ("Madrid",        40.4168,   -3.7038, "Europe/Madrid"),
    "rome":           ("Rome",          41.9028,   12.4964, "Europe/Rome"),
    "amsterdam":      ("Amsterdam",     52.3676,    4.9041, "Europe/Amsterdam"),
    "moscow":         ("Moscow",        55.7558,   37.6173, "Europe/Moscow"),
    "istanbul":       ("Istanbul",      41.0082,   28.9784, "Europe/Istanbul"),
    "tokyo":          ("Tokyo",         35.6762,  139.6503, "Asia/Tokyo"),
    "seoul":          ("Seoul",         37.5665,  126.9780, "Asia/Seoul"),
    "beijing":        ("Beijing",       39.9042,  116.4074, "Asia/Shanghai"),
    "shanghai":       ("Shanghai",      31.2304,  121.4737, "Asia/Shanghai"),
    "hong kong":      ("Hong Kong",     22.3193,  114.1694, "Asia/Hong_Kong"),
    "singapore":      ("Singapore",      1.3521,  103.8198, "Asia/Singapore"),
    "dubai":          ("Dubai",         25.2048,   55.2708, "Asia/Dubai"),
    "delhi":          ("Delhi",         28.7041,   77.1025, "Asia/Kolkata"),
    "mumbai":         ("Mumbai",        19.0760,   72.8777, "Asia/Kolkata"),
    "sydney":         ("Sydney",       -33.8688,  151.2093, "Australia/Sydney"),
    "melbourne":      ("Melbourne",   -37.8136,  144.9631, "Australia/Melbourne"),
    "toronto":        ("Toronto",       43.6532,  -79.3832, "America/Toronto"),
    "vancouver":      ("Vancouver",     49.2827, -123.1207, "America/Vancouver"),
    "montreal":       ("Montreal",      45.5019,  -73.5674, "America/Toronto"),
    "mexico city":    ("Mexico City",   19.4326,  -99.1332, "America/Mexico_City"),
    "rio de janeiro": ("Rio de Janeiro",-22.9068, -43.1729, "America/Sao_Paulo"),
    "sao paulo":      ("São Paulo",   -23.5505,  -46.6333, "America/Sao_Paulo"),
    "buenos aires":   ("Buenos Aires", -34.6037,  -58.3816, "America/Argentina/Buenos_Aires"),
    "cairo":          ("Cairo",         30.0444,   31.2357, "Africa/Cairo"),
    "lagos":          ("Lagos",          6.5244,    3.3792, "Africa/Lagos"),
    "johannesburg":   ("Johannesburg", -26.2041,   28.0473, "Africa/Johannesburg"),
}


def lookup_city(name: str):
    """Return (canonical_name, lat, lon, tz) or None."""
    if not name:
        return None
    return KNOWN_CITIES.get(name.strip().lower())
