"""The weather where FRED is, from the National Weather Service — not from a search.

Asked "what's the weather in 60440", he searched the web and read back 92
degrees on a 67-degree morning: search results carry page snippets days old,
and a model cannot tell a cached number from a live one. The NWS API is the
opposite kind of source — the actual observation from the nearest station,
with a timestamp, and the forecast for the grid he stands in — and it is free,
keyless, and US-only, which is where he lives.

Three calls, two of them cached for the life of the process:

  points/{lat},{lon}     -> which forecast grid and station list cover here
  stations/{id}/observations/latest   -> now: temperature, sky, wind, humidity
  gridpoints/.../forecast             -> today and tonight, in the NWS's words

Observations are cached ten minutes, the forecast thirty; a station reports
hourly, so anything fresher is the same number again. Every failure is a
sentence ("I can't reach the weather service right now"), never an exception:
this is spoken to a child, and "I don't know" beats a stack trace or, worse,
a confident stale number.

The location is ``brain.weather`` in settings (lat, lon, and the name he
says); the defaults are the robot's home. Requests carry a User-Agent because
the NWS refuses anonymous ones.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime, timezone

NWS = "https://api.weather.gov"
USER_AGENT = "ProjectFRED/1.0 (InMoov robot; https://github.com/nledevil/Project-FRED)"
TIMEOUT = 5.0
OBS_FRESH_S = 600.0          # a station reports hourly; re-ask every ten minutes
FORECAST_FRESH_S = 1800.0
STALE_OBS_S = 3 * 3600.0     # older than this and "right now" would be a lie

DEFAULT_LAT, DEFAULT_LON, DEFAULT_NAME = 41.6986, -88.0684, "Bolingbrook"

CANT = "I can't reach the weather service right now."


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


def parse_observation(doc: dict, now: float | None = None) -> dict | None:
    """The bits of an observation he can say, or None if it has no temperature."""
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
    """The first two periods (today/tonight, or tonight/tomorrow), trimmed."""
    periods = ((doc or {}).get("properties") or {}).get("periods") or []
    out = []
    for p in periods[:2]:
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        out.append({"name": str(p.get("name") or ""),
                    "temp_f": p.get("temperature"),
                    "daytime": bool(p.get("isDaytime")),
                    "short": str(p.get("shortForecast") or "").strip(),
                    "pop": int(pop) if pop is not None else None})
    return out


def spoken(now: dict | None, forecast: list[dict], name: str) -> str:
    """One or two spoken sentences: right now, then today."""
    parts = []
    if now and (now.get("age_s") is None or now["age_s"] < STALE_OBS_S):
        sky = now.get("sky", "").lower()
        line = f"Right now it's {now['temp_f']} degrees"
        if sky:
            line += f" and {sky}"
        line += f" in {name}."
        parts.append(line)
    if forecast:
        p = forecast[0]
        short = p["short"].lower().rstrip(".")
        hi_lo = "high" if p["daytime"] else "low"
        line = f"{p['name']}, {short}, with a {hi_lo} near {p['temp_f']}"
        if p.get("pop") is not None and p["pop"] >= 30:
            line += f" and a {p['pop']} percent chance of rain"
        parts.append(line + ".")
    return " ".join(parts) if parts else CANT


class Weather:
    """The NWS client, cached and never raising out of ``report``."""

    def __init__(self, lat: float = DEFAULT_LAT, lon: float = DEFAULT_LON,
                 name: str = DEFAULT_NAME, fetch=_get, log=print):
        self.lat, self.lon, self.name = float(lat), float(lon), name or DEFAULT_NAME
        self._fetch = fetch
        self._log = log
        self._lock = threading.Lock()
        self._urls: dict | None = None            # forecast + station, once
        self._obs: tuple[float, dict | None] = (0.0, None)
        self._forecast: tuple[float, list] = (0.0, [])
        self.last_error = ""

    # ---- the three calls ----------------------------------------------------
    def _resolve(self) -> dict:
        if self._urls is None:
            pts = self._fetch(f"{NWS}/points/{self.lat:.4f},{self.lon:.4f}")["properties"]
            stations = self._fetch(pts["observationStations"])["features"]
            self._urls = {"forecast": pts["forecast"],
                          "station": stations[0]["properties"]["stationIdentifier"]}
        return self._urls

    def current(self) -> dict | None:
        at, obs = self._obs
        if time.monotonic() - at < OBS_FRESH_S:
            return obs
        urls = self._resolve()
        obs = parse_observation(self._fetch(f"{NWS}/stations/{urls['station']}/observations/latest"))
        self._obs = (time.monotonic(), obs)
        return obs

    def forecast(self) -> list[dict]:
        at, fc = self._forecast
        if time.monotonic() - at < FORECAST_FRESH_S:
            return fc
        urls = self._resolve()
        fc = parse_forecast(self._fetch(urls["forecast"]))
        self._forecast = (time.monotonic(), fc)
        return fc

    # ---- what he says -----------------------------------------------------------
    def report(self) -> str:
        """The weather as a sentence or two; an honest sentence when it can't."""
        with self._lock:
            try:
                now, fc = self.current(), self.forecast()
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001 - offline, NWS down, bad JSON
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"[Weather] {self.last_error}")
                return CANT
        return spoken(now, fc, self.name)

    def status(self) -> dict:
        at, obs = self._obs
        return {"name": self.name, "lat": self.lat, "lon": self.lon,
                "station": (self._urls or {}).get("station"),
                "observed_temp_f": (obs or {}).get("temp_f"),
                "cached_s": round(time.monotonic() - at) if at else None,
                "error": self.last_error}


def make(cfg: dict | None, log=print) -> Weather:
    cfg = cfg or {}
    return Weather(lat=cfg.get("lat", DEFAULT_LAT), lon=cfg.get("lon", DEFAULT_LON),
                   name=str(cfg.get("name") or DEFAULT_NAME), log=log)
