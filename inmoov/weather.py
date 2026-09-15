"""The weather, anywhere, from weather services — not from a search.

Asked "what's the weather in 60440", he searched the web and read back 92
degrees on a 67-degree morning: search results carry page snippets days old,
and a model cannot tell a cached number from a live one. Weather services
are the opposite kind of source — a number with a timestamp — and two of
them are free and keyless:

  National Weather Service   US only. The *actual observation* from the
                             nearest station, and the forecast for the grid,
                             in the forecaster's own words. Used wherever
                             the place resolves to the US, because a station
                             reading is what "right now" should mean.
  Open-Meteo                 everywhere else. Model-derived current
                             conditions and a daily forecast; and its
                             geocoder, which turns "Paris" or "Tokyo, Japan"
                             into coordinates. US zip codes go through
                             Zippopotam, which the geocoder does not know.

Every place is cached once resolved, observations for ten minutes and
forecasts for thirty, so a queue of children asking the same thing costs one
request. Every failure is a sentence ("I can't reach the weather service
right now"; "I don't know where X is"), never an exception: this is spoken
to a child, and "I don't know" beats a stack trace or, worse, a confident
stale number. Fahrenheit throughout — he is an American robot — and an
observation older than three hours is not read out as "right now".

The home location is ``brain.weather`` in settings (lat, lon, the name he
says); asking with no place means home. Requests carry a User-Agent because
the NWS refuses anonymous ones.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

NWS = "https://api.weather.gov"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
ZIPPO = "https://api.zippopotam.us/us"
USER_AGENT = "ProjectFRED/1.0 (InMoov robot; https://github.com/nledevil/Project-FRED)"
TIMEOUT = 5.0
OBS_FRESH_S = 600.0          # a station reports hourly; re-ask every ten minutes
FORECAST_FRESH_S = 1800.0
STALE_OBS_S = 3 * 3600.0     # older than this and "right now" would be a lie

DEFAULT_LAT, DEFAULT_LON, DEFAULT_NAME = 41.6986, -88.0684, "Bolingbrook"

CANT = "I can't reach the weather service right now."

# WMO weather interpretation codes, as Open-Meteo reports them.
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
       45: "foggy", 48: "foggy", 51: "drizzling", 53: "drizzling", 55: "drizzling",
       56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "raining",
       65: "raining hard", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
       73: "snowing", 75: "snowing hard", 77: "snow grains", 80: "showers",
       81: "showers", 82: "heavy showers", 85: "snow showers", 86: "snow showers",
       95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail"}


@dataclass(frozen=True)
class Place:
    lat: float
    lon: float
    name: str
    country: str = "US"      # ISO code; decides the provider

    @property
    def us(self) -> bool:
        return self.country.upper() == "US"


def _get(url: str, timeout: float = TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/geo+json, application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def c_to_f(c: float) -> int:
    return int(round(c * 9.0 / 5.0 + 32.0))


def kmh_to_mph(kmh: float) -> int:
    return int(round(kmh * 0.621371))


def _age_s(stamp: str, now: float | None = None) -> float | None:
    try:
        t = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    now = now if now is not None else time.time()
    return now - t.astimezone(timezone.utc).timestamp()


# ------------------------------------------------------------------ parsing --
def parse_observation(doc: dict, now: float | None = None) -> dict | None:
    """An NWS observation as the bits he can say, or None without a temperature."""
    p = (doc or {}).get("properties") or {}
    temp = (p.get("temperature") or {}).get("value")
    if temp is None:
        return None
    out = {"temp_f": c_to_f(float(temp)),
           "sky": (p.get("textDescription") or "").strip(),
           "age_s": _age_s(p.get("timestamp") or "", now)}
    wind = (p.get("windSpeed") or {}).get("value")
    if wind is not None:
        out["wind_mph"] = kmh_to_mph(float(wind))
    hum = (p.get("relativeHumidity") or {}).get("value")
    if hum is not None:
        out["humidity"] = int(round(float(hum)))
    return out


def parse_forecast(doc: dict) -> list[dict]:
    """NWS periods — today, tonight, tomorrow, ... — trimmed to what he says."""
    periods = ((doc or {}).get("properties") or {}).get("periods") or []
    out = []
    for p in periods[:6]:
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        out.append({"name": str(p.get("name") or ""),
                    "temp_f": p.get("temperature"),
                    "daytime": bool(p.get("isDaytime")),
                    "short": str(p.get("shortForecast") or "").strip(),
                    "pop": int(pop) if pop is not None else None})
    return out


def parse_open_meteo(doc: dict) -> tuple[dict | None, list[dict]]:
    """Open-Meteo's current block and daily rows, in the NWS shapes above."""
    cur = (doc or {}).get("current") or {}
    now = None
    if cur.get("temperature_2m") is not None:
        now = {"temp_f": int(round(float(cur["temperature_2m"]))),
               "sky": WMO.get(int(cur.get("weather_code") or 0), ""),
               "age_s": 0.0}
        if cur.get("wind_speed_10m") is not None:
            now["wind_mph"] = int(round(float(cur["wind_speed_10m"])))
        if cur.get("relative_humidity_2m") is not None:
            now["humidity"] = int(round(float(cur["relative_humidity_2m"])))
    daily = (doc or {}).get("daily") or {}
    out = []
    for i, day in enumerate(daily.get("time") or []):
        hi = (daily.get("temperature_2m_max") or [None] * 9)[i]
        pop = (daily.get("precipitation_probability_max") or [None] * 9)[i]
        code = (daily.get("weather_code") or [0] * 9)[i]
        out.append({"name": "Today" if i == 0 else "Tomorrow" if i == 1 else str(day),
                    "temp_f": int(round(float(hi))) if hi is not None else None,
                    "daytime": True, "short": WMO.get(int(code or 0), ""),
                    "pop": int(pop) if pop is not None else None})
    return now, out


def pick_period(forecast: list[dict], day: str) -> dict | None:
    """Today's daytime period, or tomorrow's, from either provider's list."""
    if not forecast:
        return None
    if day != "tomorrow":
        return forecast[0]
    daytime = [p for p in forecast if p.get("daytime")]
    # NWS: the first daytime period that is not today's ("Tomorrow" or a
    # weekday name). Open-Meteo: every row is a day, so the second row.
    later = [p for p in daytime if p["name"] not in ("Today", "This Afternoon")]
    return later[0] if later else (daytime[1] if len(daytime) > 1 else None)


def spoken(now: dict | None, forecast: list[dict], name: str, day: str = "today") -> str:
    """One or two spoken sentences: right now, then the day asked about."""
    parts = []
    if day != "tomorrow" and now and (now.get("age_s") is None or now["age_s"] < STALE_OBS_S):
        sky = (now.get("sky") or "").lower()
        line = f"Right now it's {now['temp_f']} degrees"
        if sky:
            line += f" and {sky}"
        parts.append(line + f" in {name}.")
    p = pick_period(forecast, day)
    if p and p.get("temp_f") is not None:
        short = (p["short"] or "").lower().rstrip(".")
        hi_lo = "high" if p["daytime"] else "low"
        where = "" if parts else f" in {name}"
        line = f"{p['name']}{where}, {short}, with a {hi_lo} near {p['temp_f']}" if short \
            else f"{p['name']}{where}, a {hi_lo} near {p['temp_f']}"
        if p.get("pop") is not None and p["pop"] >= 30:
            line += f" and a {p['pop']} percent chance of rain"
        parts.append(line + ".")
    return " ".join(parts) if parts else CANT


# ------------------------------------------------------------------ places --
_ZIP = re.compile(r"^\s*(\d{5})\s*$")


def geocode(query: str, fetch=_get) -> Place | None:
    """Coordinates for a spoken place, or None when nobody knows where it is."""
    q = " ".join((query or "").split())
    if not q:
        return None
    m = _ZIP.match(q)
    if m:
        doc = fetch(f"{ZIPPO}/{m.group(1)}")
        places = doc.get("places") or []
        if not places:
            return None
        p = places[0]
        return Place(float(p["latitude"]), float(p["longitude"]),
                     f"{p.get('place name', q)}, {p.get('state abbreviation', '')}".strip(", "),
                     "US")
    # The geocoder wants a place name, not "Sydney Australia": try the whole
    # thing, then drop words off the end — "Los Angeles California" becomes
    # "Los Angeles" — before giving up.
    words = q.replace(",", " ").split()
    r = None
    for n in range(len(words), 0, -1):
        name_q = " ".join(words[:n])
        doc = fetch(f"{GEOCODE}?{urllib.parse.urlencode({'name': name_q, 'count': 1, 'language': 'en', 'format': 'json'})}")
        results = doc.get("results") or []
        if results:
            r = results[0]
            break
    if r is None:
        return None
    name = str(r.get("name") or q)
    region = r.get("admin1")
    country = str(r.get("country_code") or "").upper()
    if country == "US" and region:
        name = f"{name}, {region}"
    elif r.get("country") and country != "US":
        name = f"{name}, {r['country']}"
    return Place(float(r["latitude"]), float(r["longitude"]), name, country)


# ------------------------------------------------------------------ client --
class Weather:
    """The weather for home or any named place, cached, never raising out of report()."""

    def __init__(self, lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON,
                 name: str = DEFAULT_NAME, fetch=_get, log=print):
        self.home = Place(float(lat), float(lon), name or DEFAULT_NAME, "US")
        self._fetch = fetch
        self._log = log
        self._lock = threading.Lock()
        self._places: dict[str, Place | None] = {}
        self._nws_urls: dict[Place, dict] = {}
        self._obs: dict[Place, tuple[float, dict | None]] = {}
        self._fc: dict[Place, tuple[float, list]] = {}
        self.last_error = ""
        self.last_place: str = ""

    # ---- NWS, for the US ---------------------------------------------------
    def _nws(self, place: Place) -> dict:
        if place not in self._nws_urls:
            pts = self._fetch(f"{NWS}/points/{place.lat:.4f},{place.lon:.4f}")["properties"]
            stations = self._fetch(pts["observationStations"])["features"]
            self._nws_urls[place] = {"forecast": pts["forecast"],
                                     "station": stations[0]["properties"]["stationIdentifier"]}
        return self._nws_urls[place]

    def _nws_current(self, place: Place) -> dict | None:
        at, obs = self._obs.get(place, (0.0, None))
        if time.monotonic() - at < OBS_FRESH_S:
            return obs
        urls = self._nws(place)
        obs = parse_observation(self._fetch(f"{NWS}/stations/{urls['station']}/observations/latest"))
        self._obs[place] = (time.monotonic(), obs)
        return obs

    def _nws_forecast(self, place: Place) -> list[dict]:
        at, fc = self._fc.get(place, (0.0, []))
        if time.monotonic() - at < FORECAST_FRESH_S:
            return fc
        fc = parse_forecast(self._fetch(self._nws(place)["forecast"]))
        self._fc[place] = (time.monotonic(), fc)
        return fc

    # ---- Open-Meteo, for the rest of the world ---------------------------------
    def _open_meteo(self, place: Place) -> tuple[dict | None, list[dict]]:
        at, obs = self._obs.get(place, (0.0, None))
        _, fc = self._fc.get(place, (0.0, []))
        if time.monotonic() - at < OBS_FRESH_S:
            return obs, fc
        url = f"{OPEN_METEO}?" + urllib.parse.urlencode({
            "latitude": f"{place.lat:.4f}", "longitude": f"{place.lon:.4f}",
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
            "timezone": "auto", "forecast_days": 2})
        obs, fc = parse_open_meteo(self._fetch(url))
        self._obs[place] = (time.monotonic(), obs)
        self._fc[place] = (time.monotonic(), fc)
        return obs, fc

    # ---- what he says --------------------------------------------------------------
    def resolve(self, query: str) -> Place | None:
        key = " ".join((query or "").lower().split())
        if not key or key in ("here", "home", "outside"):
            return self.home
        if key not in self._places:
            self._places[key] = geocode(query, self._fetch)
        return self._places[key]

    def report(self, place: str = "", day: str = "today") -> str:
        """The weather as a sentence or two; an honest sentence when it can't."""
        day = "tomorrow" if "tomorrow" in (day or "").lower() else "today"
        with self._lock:
            try:
                where = self.resolve(place)
                if where is None:
                    self.last_error = ""
                    return f"I don't know where {place.strip()} is."
                self.last_place = where.name
                if where.us:
                    now, fc = self._nws_current(where), self._nws_forecast(where)
                else:
                    now, fc = self._open_meteo(where)
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001 - offline, service down, bad JSON
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"[Weather] {self.last_error}")
                return CANT
        return spoken(now, fc, where.name, day)

    def status(self) -> dict:
        at, obs = self._obs.get(self.home, (0.0, None))
        return {"name": self.home.name, "lat": self.home.lat, "lon": self.home.lon,
                "station": self._nws_urls.get(self.home, {}).get("station"),
                "observed_temp_f": (obs or {}).get("temp_f"),
                "cached_s": round(time.monotonic() - at) if at else None,
                "places": len(self._places), "last_place": self.last_place,
                "error": self.last_error}


def make(cfg: dict | None, log=print) -> Weather:
    cfg = cfg or {}
    return Weather(lat=cfg.get("lat", DEFAULT_LAT), lon=cfg.get("lon", DEFAULT_LON),
                   name=str(cfg.get("name") or DEFAULT_NAME), log=log)
