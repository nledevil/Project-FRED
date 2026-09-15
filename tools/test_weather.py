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
             "shortForecast": "Chance Showers", "probabilityOfPrecipitation": {"value": 52}}]}},
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
    check("a four-hour-old observation is left out; the forecast stays",
          not said.startswith("Right now") and said.startswith("Today,"), said)
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

    print("the matcher hears the question")
    for text in ("what's the weather like today", "fred what's the forecast",
                 "is it going to rain", "how hot is it outside", "do i need an umbrella"):
        got = C.match_local(text)
        check(f"{text!r:36} -> say_weather", got == ("say_weather", {}), str(got))
    for text, want in (("how hot are you", "say_temp"), ("what's your cpu temperature", "say_temp")):
        got = C.match_local(text)
        check(f"{text!r:36} -> {want} (his own chip)", got is not None and got[0] == want, str(got))
    class FakeWeather:
        def report(self):
            return "Right now it's 66 degrees."
    ctx = types.SimpleNamespace(controller=None, weather=FakeWeather())
    check("the action speaks the report", C.execute_action(ctx, "say_weather") == "Right now it's 66 degrees.")
    check("the tool reaches the same place",
          C.run_tool(ctx, "get_weather", {}) == "Right now it's 66 degrees.")
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
