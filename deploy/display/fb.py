"""Minimal framebuffer blitter for headless Raspberry Pi (no X/DRM needed).

Draw into a numpy (H, W, 3) uint8 RGB array, then Framebuffer.show(arr)
converts to the panel's native RGB565 and blits the whole frame via mmap.

Tested on the official 7" 800x480 touchscreen (BCM2708 FB, 16bpp, RGB565).
"""
import os
import mmap
import numpy as np


class Framebuffer:
    def __init__(self, dev="/dev/fb0"):
        self.dev = dev
        fbname = os.path.basename(dev)                       # e.g. "fb0"
        sysfs = f"/sys/class/graphics/{fbname}"
        w, h = open(f"{sysfs}/virtual_size").read().strip().split(",")
        self.w, self.h = int(w), int(h)
        self.bpp = int(open(f"{sysfs}/bits_per_pixel").read().strip())
        self.stride = int(open(f"{sysfs}/stride").read().strip())   # bytes/line
        if self.bpp != 16:
            raise RuntimeError(f"{dev} is {self.bpp}bpp; this helper only does RGB565 (16bpp)")

        self.size = self.stride * self.h
        self.fd = os.open(dev, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, self.size)
        self._padded = self.stride != self.w * 2            # is there row padding?

    def show(self, rgb):
        """Blit an (H, W, 3) uint8 RGB array to the screen."""
        r = rgb[..., 0].astype(np.uint16)
        g = rgb[..., 1].astype(np.uint16)
        b = rgb[..., 2].astype(np.uint16)
        # pack into little-endian RGB565 (Pi is little-endian, numpy matches)
        px = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        if not self._padded:
            self.mm[:] = px.tobytes()
        else:
            raw = px.tobytes()
            rowbytes = self.w * 2
            for y in range(self.h):
                off = y * self.stride
                self.mm[off:off + rowbytes] = raw[y * rowbytes:(y + 1) * rowbytes]

    def clear(self):
        self.mm[:] = b"\x00" * self.size

    def close(self):
        try:
            self.mm.close()
        finally:
            os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def hide_cursor():
    """Stop the text console cursor from blinking over the animation."""
    try:
        with open("/sys/class/graphics/fbcon/cursor_blink", "w") as f:
            f.write("0")
    except OSError:
        pass  # not fatal; may need root
