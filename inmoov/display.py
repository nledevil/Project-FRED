"""Client for the chest display Pi's animation control API.

A second Pi drives the 7" DSI panel in FRED's chest. It runs display_control.py
(see deploy/display/), which supervises the framebuffer animation and exposes a
small HTTP API. This is the head's side of that link: the admin panel uses it to
list the presets and switch the look.

Everything here is best-effort and short-timeout on purpose — the chest Pi is a
decoration, not a dependency. If it's off, unplugged, or mid-reboot, the head's
web panel must not hang or fail; calls raise DisplayError and the admin panel
shows the box as offline. Configured live from the admin screen; see
``display`` in config/settings.json.
"""
from __future__ import annotations

import threading

import requests


class DisplayError(Exception):
    """The chest display Pi is unreachable, unauthorised, or unhappy."""


class DisplayClient:
    """Talks to display_control.py on the chest Pi. Thread-safe by being stateless."""

    def __init__(self, host: str = "", port: int = 8081, token: str = "",
                 timeout: float = 2.0):
        self.host = host
        self.port = port
        self.token = token
        self.timeout = timeout            # keep short: the admin page waits on this

    def configure(self, host: str | None = None, port: int | None = None,
                  token: str | None = None) -> None:
        """Point at a different chest Pi (admin save applies this live)."""
        if host is not None:
            self.host = host.strip()
        if port is not None:
            self.port = int(port)
        if token is not None:
            self.token = token

    def configured(self) -> bool:
        """False when no host is set — the feature is simply off."""
        return bool(self.host)

    def _url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def _headers(self) -> dict:
        return {"X-Display-Token": self.token} if self.token else {}

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.configured():
            raise DisplayError("no chest display configured")
        try:
            r = requests.request(method, self._url(path), json=payload,
                                 headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as e:
            raise DisplayError(f"cannot reach display at {self.host}:{self.port}") from e
        try:
            data = r.json()
        except ValueError:
            raise DisplayError(f"display returned non-JSON (HTTP {r.status_code})") from None
        if r.status_code >= 400:
            raise DisplayError(data.get("error") or f"display error (HTTP {r.status_code})")
        return data

    def animations(self) -> list[dict]:
        """The preset list, straight from the display Pi, so the dropdown always
        matches what that Pi can actually render — no list to keep in sync here."""
        return self._request("GET", "/api/animations").get("animations", [])

    def state(self) -> dict:
        """Current preset + whether its process is alive."""
        return self._request("GET", "/api/state")

    def select(self, animation: str) -> dict:
        """Switch the chest animation now."""
        return self._request("POST", "/api/animation", {"animation": animation})

    def set_metrics(self, enabled: bool) -> dict:
        """Show or hide the live sensor readout over whatever is playing.

        Independent of the animation pick — the chest Pi overlays it on all of
        them, so this doesn't restart or disturb the current one.
        """
        return self._request("POST", "/api/metrics", {"enabled": bool(enabled)})

    def push_voice(self, payload: dict) -> dict:
        """Send FRED's voice state (and, on a new clip, the whole envelope)."""
        return self._request("POST", "/api/voice", payload)


class VoicePusher(threading.Thread):
    """Feeds the chest display FRED's voice state and speech envelope.

    Polls the assistant rather than hooking into it: the same shape the browser's
    face already uses (watch ``mouth_seq``, refetch on change), and it keeps the
    voice path free of any knowledge of the chest Pi — nothing FRED says can be
    delayed or broken by a display that's unplugged.

    Only *changes* go over the wire. The envelope is the trick that makes this
    cheap: it's published whole, once per utterance, with ``starts_in`` saying
    when frame 0 is audible, so the display animates it against its own clock at
    30fps with no streaming. Speech has a lead-in of silence (see sound.lead_in),
    which is plenty of headroom for a 10Hz poll plus a LAN hop.
    """

    def __init__(self, client: DisplayClient, assistant, interval: float = 0.1):
        super().__init__(name="display-voice", daemon=True)
        self._client = client
        self._assistant = assistant
        self._interval = interval
        self._stop = threading.Event()
        self._last: tuple | None = None

    def stop(self) -> None:
        self._stop.set()

    def _state(self) -> str:
        if self._assistant.is_speaking():
            return "speaking"
        if self._assistant.is_thinking():
            return "thinking"
        if self._assistant.listener.is_running():
            return "listening"
        return "idle"

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            if not self._client.configured():
                self._last = None       # re-send state when a display appears
                continue
            try:
                state, seq = self._state(), self._assistant.mouth_seq()
            except Exception:
                continue                # a half-built assistant must not kill this
            if (state, seq) == self._last:
                continue

            payload = {"state": state, "seq": seq}
            if not self._last or seq != self._last[1]:
                # New clip: ship the envelope with it, so the display has the
                # whole waveform before the first sample is audible.
                mouth = self._assistant.mouth()
                if mouth and mouth.get("seq") == seq:
                    payload.update(frame_dt=mouth["frame_dt"],
                                   starts_in=mouth["starts_in"],
                                   levels=mouth["levels"])
            try:
                self._client.push_voice(payload)
            except DisplayError:
                # Chest Pi offline/rebooting: drop it. Retry on the next change,
                # and re-send this state then by not recording it as sent.
                continue
            self._last = (state, seq)
