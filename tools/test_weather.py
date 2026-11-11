#!/usr/bin/env python3
"""The weather comes from a weather service, and a failure is a sentence.

Asked "what's the weather in 60440", FRED searched the web and read back 92
degrees on a 67-degree morning — search snippets are days old and a model
cannot tell a cached number from a live one. Now the weather is the National
Weather Service's own observation and forecast (inmoov/weather.py), answered
by the matcher without a model and offered to the model as get_weather in
place of a search. This pins what that promises:

**The numbers are the station's.** Celsius to whole Fahrenheit, the sky in
the station's words, today's period with its chance of rain when it matters.

**Old is not now.** An observation older than three hours is not read as
"right now"; the forecast still is.

**It never raises.** Offline, a refused request, bad JSON — the report is
"I can't reach the weather service right now", and the next call tries again.

**The matcher hears the question**, and still knows the difference between
the weather outside and the temperature of his own chip.

Canned NWS documents; no network.

    python3 tools/test_weather.py

Exits non-zero on the first failure.
"""
from __future__ import annotations

import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from inmoov import commands as C                          # noqa: E402
from inmoov import weather as W                           # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILURES.append(label)


def docs(temp_c=19.0, stamp=None, pop=69):
    stamp = stamp or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    return {
        "points": {"properties": {"forecast": "F", "observationStations": "S"}},
        "S": {"features": [{"properties": {"stationIdentifier": "KLOT", "name": "Lewis"}}]},
        "obs": {"properties": {"timestamp": stamp, "textDescription": "Partly Cloudy",
                               "temperature": {"value": temp_c},
                               "windSpeed": {"value": 7.56},
                               "relativeHumidity": {"value": 88.2}}},
        "F": {"properties": {"periods": [
            {"name": "Today", "temperature": 82, "isDaytime": True,
             "shortForecast": "Showers And Thunderstorms Likely",
             "probabilityOfPrecipitation": {"value": pop}},
            {"name": "Tonight", "temperature": 64, "isDaytime": False,
             "shortForecast": "Chance Showers", "probabilityOfPrecipitation": {"value": 52}},
            {"name": "Wednesday", "temperature": 74, "isDaytime": True,
             "shortForecast": "Chance Showers And Thunderstorms",
             "probabilityOfPrecipitation": {"value": 48}}]}},
    }


def fetcher(d, fail=False):
    calls = []

    def fetch(url, timeout=5.0):
        calls.append(url)
        if fail:
            raise OSError("no route to host")
        if "/points/" in url:
            return d["points"]
        if url.endswith("/observations/latest"):
            return d["obs"]
        return d[url]
    fetch.calls = calls
    return fetch


def main() -> int:
    print("the numbers are the station's")
    w = W.Weather(fetch=fetcher(docs()), log=lambda *a: None)
    said = w.report()
    check("66 degrees, partly cloudy, in Bolingbrook",
          said.startswith("Right now it's 66 degrees and partly cloudy in Bolingbrook."), said)
    check("today's forecast with its chance of rain",
          "Today, showers and thunderstorms likely, with a high near 82 and a 69 percent chance of rain."
          in said, said)
    check("celsius rounds to whole fahrenheit", W.c_to_f(19) == 66 and W.c_to_f(-3.4) == 26)
    n = len(w._fetch.calls)
    w.report()
    check("a second ask inside the cache window makes no request", len(w._fetch.calls) == n)
    st = w.status()
    check("status names the station and the last reading",
          st["station"] == "KLOT" and st["observed_temp_f"] == 66, str(st))

    print("old is not now")
    old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 4 * 3600))
    w = W.Weather(fetch=fetcher(docs(stamp=old)), log=lambda *a: None)
    said = w.report()
    check("a four-hour-old observation is left out; the forecast stays, and names the place",
          not said.startswith("Right now") and said.startswith("Today in Bolingbrook,"), said)
    w = W.Weather(fetch=fetcher(docs(pop=10)), log=lambda *a: None)
    check("a small chance of rain is not mentioned", "percent" not in w.report())
    w = W.Weather(fetch=fetcher({"points": {"properties": {"forecast": "F", "observationStations": "S"}},
                                 "S": {"features": [{"properties": {"stationIdentifier": "X"}}]},
                                 "obs": {"properties": {"temperature": {"value": None}}},
                                 "F": {"properties": {"periods": []}}}), log=lambda *a: None)
    check("nothing usable at all is the honest sentence", w.report() == W.CANT)

    print("it never raises")
    w = W.Weather(fetch=fetcher(docs(), fail=True), log=lambda *a: None)
    check("offline: the honest sentence", w.report() == W.CANT)
    check("...and the error is on record", "OSError" in w.last_error)
    w._fetch = fetcher(docs())
    check("the next ask tries again and succeeds", w.report().startswith("Right now"))

    print("any place: zips through Zippopotam, names through the geocoder, the rest of the world through Open-Meteo")
    geo = {"points": {"properties": {"forecast": "F", "observationStations": "S"}},
           "S": {"features": [{"properties": {"stationIdentifier": "KORD"}}]},
           "obs": docs()["obs"], "F": docs()["F"]}
    def fetch(url, timeout=5.0):
        fetch.calls.append(url)
        if url.startswith(W.ZIPPO):
            return {"places": [{"place name": "Chicago", "state abbreviation": "IL",
                                "latitude": "41.85", "longitude": "-87.65"}]}
        if url.startswith(W.GEOCODE):
            q = url.split("name=")[1].split("&")[0].replace("+", " ")
            if q == "Paris":
                return {"results": [{"name": "Paris", "country": "France", "country_code": "FR",
                                     "latitude": 48.85, "longitude": 2.35}]}
            if q == "Sydney":
                return {"results": [{"name": "Sydney", "country": "Australia", "country_code": "AU",
                                     "latitude": -33.87, "longitude": 151.2}]}
            return {"results": []}
        if url.startswith(W.OPEN_METEO):
            return {"current": {"temperature_2m": 88.6, "weather_code": 0, "wind_speed_10m": 4.2,
                                "relative_humidity_2m": 40},
                    "daily": {"time": ["2026-09-15", "2026-09-16"], "weather_code": [3, 61],
                              "temperature_2m_max": [91.2, 70.4], "temperature_2m_min": [70, 60],
                              "precipitation_probability_max": [5, 100]}}
        if "/points/" in url:
            return geo["points"]
        if url.endswith("/observations/latest"):
            return geo["obs"]
        return geo[url]
    fetch.calls = []
    w = W.Weather(fetch=fetch, log=lambda *a: None)
    said = w.report("60606")
    check("a US zip: NWS, named by the zip's town",
          said.startswith("Right now it's 66 degrees and partly cloudy in Chicago, IL."), said)
    said = w.report("Paris, France")
    check("a foreign city: Open-Meteo, in fahrenheit, with the country",
          said == "Right now it's 89 degrees and clear in Paris, France. Today, overcast, with a high near 91.", said)
    said = w.report("Paris", "tomorrow")
    check("tomorrow, elsewhere: the second daily row, rain when likely",
          said == "Tomorrow in Paris, France, light rain, with a high near 70 and a 100 percent chance of rain.", said)
    n = len(fetch.calls)
    w.report("paris")
    check("a place is resolved once, then cached", len(fetch.calls) == n)
    said = w.report("Sydney Australia")
    check("'Sydney Australia': the geocoder is asked again with the last word dropped",
          said.startswith("Right now it's 89 degrees") and "Sydney, Australia" in said, said)
    check("an unknown place is said to be unknown, not guessed",
          w.report("Xyzzyville") == "I don't know where Xyzzyville is.")
    check("no place, 'here' and 'home' all mean home",
          w.resolve("") == w.home and w.resolve("here") == w.home and w.resolve("home") == w.home)
    said = w.report("", "tomorrow")
    check("tomorrow at home: NWS's next daytime period, not today's or tonight's",
          said == "Wednesday in Bolingbrook, chance showers and thunderstorms, with a high near 74 "
                  "and a 48 percent chance of rain.", said)

    print("the matcher hears the question, and the place in it")
    for text, want in (("what's the weather like today", {"place": "", "day": "today"}),
                       ("fred what's the forecast", {"place": "", "day": "today"}),
                       ("is it going to rain tomorrow", {"place": "", "day": "tomorrow"}),
                       ("how hot is it outside", {"place": "", "day": "today"}),
                       ("do i need an umbrella", {"place": "", "day": "today"}),
                       ("what's the weather in chicago", {"place": "chicago", "day": "today"}),
                       ("what's the weather like in paris, france tomorrow",
                        {"place": "paris, france", "day": "tomorrow"}),
                       ("weather for 60440?", {"place": "60440", "day": "today"}),
                       ("what's the weather in the morning", {"place": "", "day": "today"}),
                       ("is it raining in town", {"place": "", "day": "today"})):
        got = C.match_local(text)
        check(f"{text!r:46} -> {want['place'] or 'home'} {want['day']}",
              got == ("say_weather", want), str(got))
    for text, want in (("how hot are you", "say_temp"), ("what's your cpu temperature", "say_temp")):
        got = C.match_local(text)
        check(f"{text!r:36} -> {want} (his own chip)", got is not None and got[0] == want, str(got))
    class FakeWeather:
        def report(self, place="", day="today"):
            return f"{place or 'home'}/{day}"
    ctx = types.SimpleNamespace(controller=None, weather=FakeWeather())
    check("the action passes the place and day", C.execute_action(ctx, "say_weather", place="Tokyo", day="tomorrow") == "Tokyo/tomorrow")
    check("the tool reaches the same place",
          C.run_tool(ctx, "get_weather", {"place": "Paris"}) == "Paris/today"
          and C.run_tool(ctx, "get_weather", {}) == "home/today")
    check("a build without the service says so",
          "weather service" in C.execute_action(types.SimpleNamespace(controller=None), "say_weather"))
    check("get_weather is offered to the model", any(t["name"] == "get_weather" for t in C.CLAUDE_TOOLS))

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
