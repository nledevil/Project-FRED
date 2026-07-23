# FRED stomach sensor node — Raspberry Pi Pico W (MicroPython)
#
# A "smart sensor node": it reads the HC-SR04 ultrasonic + PIR motion sensors,
# does the echo timing / filtering / event detection itself, and pushes clean
# JSON to the InMoov app on the Pi. The Pi does no GPIO and no pulse timing — an
# RP2040 hardware-timed measurement (time_pulse_us) is far more accurate than a
# jittery Linux GPIO read, which is the whole point of offloading it here.
#
# Transport: prefers WiFi -> HTTP POST to /api/sensors/ingest; if there's no
# network it falls back to printing the same JSON, one object per line, over USB
# serial (the Pi reads /dev/ttyACM0 — see inmoov/sensors.py SerialSensorReader).
#
# WIRING (Pico GPIO is 3.3V and NOT 5V-tolerant!):
#   HC-SR04  VCC -> VBUS (pin 40, 5V)     GND -> GND
#            TRIG -> GP3 (3.3V drive is fine)
#            ECHO -> voltage divider -> GP2   [ECHO]--1k--(GP2)--2k--[GND]
#                    (5V * 2k/(1k+2k) = 3.3V — do NOT wire ECHO straight to GP2)
#   PIR HC-SR501  VCC -> VBUS (5V)   GND -> GND   OUT -> GP4 (3.3V out, safe)
#
# SETUP: flash MicroPython (Pico W UF2) then copy this file as main.py (Thonny,
# or `mpremote cp main.py :main.py`). Edit the CONFIG block below.

import json
import time

import machine
import network

# ---- CONFIG (edit these) --------------------------------------------------
WIFI_SSID = "YOUR_WIFI"
WIFI_PASS = "YOUR_WIFI_PASSWORD"
PI_HOST   = "192.168.68.74"          # the Pi's LAN IP (or hostname)
PI_PORT   = 8080
INGEST    = "/api/sensors/ingest"
TOKEN     = ""                       # must match settings.json sensors.token ("" = none)
NODE_ID   = "stomach"

TRIG_PIN, ECHO_PIN, PIR_PIN = 3, 2, 4

# behaviour tuning
NEAR_CM        = 120.0               # closer than this fires an "approach" event
HYST_CM        = 25.0               # must back out past NEAR_CM+HYST before "depart"
MAX_CM         = 400.0             # readings beyond this are treated as "nothing there"
LOOP_MS        = 150                # sample period
HEARTBEAT_MS   = 1000               # send readings at least this often (even with no event)
# ---------------------------------------------------------------------------

trig = machine.Pin(TRIG_PIN, machine.Pin.OUT)
echo = machine.Pin(ECHO_PIN, machine.Pin.IN)
pir = machine.Pin(PIR_PIN, machine.Pin.IN)
led = machine.Pin("LED", machine.Pin.OUT)        # Pico W onboard LED (status)
wlan = network.WLAN(network.STA_IF)
wdt = machine.WDT(timeout=8000)                  # auto-reset if the loop wedges


def connect_wifi(timeout_s=10):
    """Try to join WiFi; return True if connected. Non-fatal — we fall back to
    serial when this returns False."""
    try:
        wlan.active(True)
        if not wlan.isconnected():
            wlan.connect(WIFI_SSID, WIFI_PASS)
            t0 = time.ticks_ms()
            while not wlan.isconnected():
                if time.ticks_diff(time.ticks_ms(), t0) > timeout_s * 1000:
                    return False
                wdt.feed()
                time.sleep_ms(200)
        return wlan.isconnected()
    except Exception:
        return False


def _one_ping():
    """One HC-SR04 reading in cm, or None on timeout/no echo."""
    trig.low()
    time.sleep_us(2)
    trig.high()
    time.sleep_us(10)
    trig.low()
    us = machine.time_pulse_us(echo, 1, 30000)   # measure the HIGH echo, 30ms (~5m) timeout
    if us < 0:
        return None
    return us / 58.0                             # speed of sound -> cm (round trip)


def read_distance_cm():
    """Median of a few pings — rejects the odd spurious echo."""
    s = []
    for _ in range(5):
        d = _one_ping()
        if d is not None and d <= MAX_CM:
            s.append(d)
        time.sleep_ms(8)
    if not s:
        return None
    s.sort()
    return s[len(s) // 2]


def http_post(body):
    """POST the JSON bytes to the Pi. Returns True on a 2xx-ish reply."""
    import socket
    addr = socket.getaddrinfo(PI_HOST, PI_PORT)[0][-1]
    s = socket.socket()
    s.settimeout(5)
    try:
        s.connect(addr)
        head = "POST %s HTTP/1.0\r\nHost: %s\r\nContent-Type: application/json\r\n" % (INGEST, PI_HOST)
        if TOKEN:
            head += "X-Sensor-Token: %s\r\n" % TOKEN
        head += "Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)
        s.send(head.encode() + body)
        resp = s.recv(32)
        return b" 200 " in resp or b" 2" in resp
    except Exception:
        return False
    finally:
        s.close()


def send(payload):
    """Prefer WiFi/HTTP; fall back to a JSON line over USB serial."""
    body = json.dumps(payload).encode()
    if wlan.isconnected() and http_post(body):
        led.on()                                 # solid = streaming over WiFi
        return
    # WiFi down or POST failed -> serial fallback (the Pi reads these lines),
    # and try to bring WiFi back for next time.
    print(json.dumps(payload))                   # -> USB CDC
    led.toggle()                                 # blinking = serial fallback
    if not wlan.isconnected():
        connect_wifi(timeout_s=3)


def main():
    connect_wifi()
    near = False                                 # "someone is close" latch (hysteresis)
    motion = False
    last_send = time.ticks_ms()
    while True:
        wdt.feed()
        dist = read_distance_cm()
        pir_now = bool(pir.value())
        events = []

        # distance -> approach / depart, with hysteresis so it doesn't chatter
        if dist is not None:
            if not near and dist < NEAR_CM:
                near = True
                events.append({"sensor": "dist_center", "event": "approach", "cm": round(dist, 1)})
            elif near and dist > NEAR_CM + HYST_CM:
                near = False
                events.append({"sensor": "dist_center", "event": "depart", "cm": round(dist, 1)})

        # PIR edges
        if pir_now and not motion:
            motion = True
            events.append({"sensor": "pir_center", "event": "motion_start"})
        elif not pir_now and motion:
            motion = False
            events.append({"sensor": "pir_center", "event": "motion_stop"})

        now = time.ticks_ms()
        if events or time.ticks_diff(now, last_send) >= HEARTBEAT_MS:
            readings = {
                "dist_center": {"type": "distance", "cm": round(dist, 1) if dist is not None else None},
                "pir_center": {"type": "motion", "active": pir_now},
            }
            send({"node": NODE_ID, "uptime_ms": time.ticks_ms(),
                  "readings": readings, "events": events})
            last_send = now

        time.sleep_ms(LOOP_MS)


main()
