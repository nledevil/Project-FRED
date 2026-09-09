#!/usr/bin/env python3
"""Standalone libcamera → MJPEG streamer for the InMoov head camera.

A tiny, self-contained MJPEG-over-HTTP server for the Pi Camera Module 3 (imx708)
via picamera2/libcamera. The brain's Camera object consumes it over the robot
LAN (inmoov/camera.py, backend "mjpeg"), and anything else that reads an MJPEG
URL — a browser, curl — works the same way.

Deliberately owns ONLY the camera — no I2C, no audio — so a servo_server restart
never drops the stream and vice versa.

Endpoints:
  GET /stream.mjpg   multipart/x-mixed-replace MJPEG stream
  GET /snapshot.jpg  a single, freshly captured JPEG frame
  GET /              a bare HTML page showing the stream (for eyeballing it)

Config via env: CAM_STREAM_PORT (8081), CAM_STREAM_SIZE (640x480),
CAM_STREAM_FPS (15), CAM_LENS_POSITION (2.0 dioptres ~0.5 m; manual focus —
continuous AF hunts on this rig), CAM_AF_MODE (0=manual, 2=continuous),
CAM_FLIP (0/1, 180° rotate if mounted inverted).
"""
from __future__ import annotations

import io
import os
import socketserver
import threading
from http import server

from libcamera import Transform
from picamera2 import Picamera2
from picamera2.encoders import MJPEGEncoder
from picamera2.outputs import FileOutput

PORT = int(os.environ.get("CAM_STREAM_PORT", "8081"))
_size = os.environ.get("CAM_STREAM_SIZE", "640x480").lower().split("x")
SIZE = (int(_size[0]), int(_size[1]))
FPS = float(os.environ.get("CAM_STREAM_FPS", "15"))
AF_MODE = int(os.environ.get("CAM_AF_MODE", "0"))            # 0 manual, 2 continuous
LENS_POSITION = float(os.environ.get("CAM_LENS_POSITION", "2.0"))
FLIP = os.environ.get("CAM_FLIP", "0") not in ("0", "", "false", "False")

_PAGE = f"""<!doctype html><title>InMoov camera</title>
<body style="margin:0;background:#111;text-align:center">
<img src="/stream.mjpg" style="max-width:100%;height:auto">
</body>""".encode()


class StreamOutput(io.BufferedIOBase):
    """Latest JPEG frame + a condition the stream handlers wait on."""

    def __init__(self):
        self.frame: bytes | None = None
        self.seq = 0                      # bumps once per frame; see _snapshot
        self.cond = threading.Condition()

    def write(self, buf) -> int:
        with self.cond:
            self.frame = bytes(buf)
            self.seq += 1
            self.cond.notify_all()
        return len(buf)


class Handler(server.BaseHTTPRequestHandler):
    def log_message(self, *a):        # keep the journal quiet (one line per frame otherwise)
        pass

    def do_GET(self):
        if self.path in ("/stream.mjpg", "/stream"):
            self._stream()
        elif self.path in ("/snapshot.jpg", "/snapshot"):
            self._snapshot()
        elif self.path == "/":
            self._page()
        else:
            self.send_error(404)

    def _page(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)

    def _snapshot(self):
        # Wait for a frame captured AFTER this request, never for merely "a
        # frame". The old wait() returned on any notify or on timeout and took
        # whatever was lying in the buffer — so a stalled encoder served the
        # same stale JPEG forever, indistinguishable from a live one. The NUC
        # had this exact bug in Camera.snapshot() (a child was described her
        # absent father from an hours-old frame); this is the same fix on the
        # same pattern, one machine over. 503 on timeout: "no picture" is an
        # answer somebody can act on, a stale picture is a lie nobody can see.
        with output.cond:
            seen = output.seq
            output.cond.wait_for(lambda: output.seq != seen, timeout=5.0)
            frame = output.frame if output.seq != seen else None
        if not frame:
            self.send_error(503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.end_headers()
        self.wfile.write(frame)

    def _stream(self):
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            while True:
                with output.cond:
                    output.cond.wait(timeout=5.0)
                    frame = output.frame
                if not frame:
                    continue
                self.wfile.write(b"--frame\r\n")
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass                      # client disconnected — normal


class StreamServer(socketserver.ThreadingMixIn, server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


output = StreamOutput()


def main() -> None:
    picam = Picamera2()
    transform = Transform(hflip=1, vflip=1) if FLIP else Transform()
    cfg = picam.create_video_configuration(main={"size": SIZE}, transform=transform,
                                           controls={"FrameRate": FPS})
    picam.configure(cfg)
    ctrls = {"AfMode": AF_MODE}
    if AF_MODE == 0:                  # manual focus
        ctrls["LensPosition"] = LENS_POSITION
    try:
        picam.set_controls(ctrls)
    except Exception as exc:          # noqa: BLE001 - focus is best-effort
        print(f"[camera_stream] focus set failed: {exc}")
    picam.start_recording(MJPEGEncoder(), FileOutput(output))
    print(f"[camera_stream] serving MJPEG on :{PORT} "
          f"({SIZE[0]}x{SIZE[1]} @ {FPS}fps, af_mode={AF_MODE}) "
          f"-> http://0.0.0.0:{PORT}/stream.mjpg")
    try:
        StreamServer(("0.0.0.0", PORT), Handler).serve_forever()
    finally:
        picam.stop_recording()


if __name__ == "__main__":
    main()
