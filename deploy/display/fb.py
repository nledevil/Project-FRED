"""Minimal framebuffer blitter for headless Raspberry Pi (no X needed).

Draw into a numpy (H, W, 3) uint8 RGB array, then Framebuffer.show(arr) packs it
into the panel's native pixel format and blits the whole frame via mmap.

Two formats, because the Pi has two ways of putting this panel up:

  16bpp RGB565   the legacy fbdev the panel comes up in today
  32bpp XRGB8888 what the fbdev emulation gives you once vc4-kms-v3d is
                 loaded — i.e. the moment anything wants the GPU

That second one is why this file knows about depth at all. It used to raise on
anything but 16bpp, which meant enabling KMS for one thing would have silently
stopped every animation on the panel.
"""
import os
import mmap
import numpy as np


def pack(rgb, bpp):
    """Pack an (H, W, 3) uint8 RGB array into the framebuffer's pixel format.

    Split out of Framebuffer so it can be tested without a panel — the depth
    that is not currently plugged in is exactly the one no test could reach.
    """
    if bpp == 16:
        r = rgb[..., 0].astype(np.uint16)
        g = rgb[..., 1].astype(np.uint16)
        b = rgb[..., 2].astype(np.uint16)
        # little-endian RGB565 (Pi is little-endian, numpy matches)
        return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    if bpp == 32:
        # XRGB8888 is 0xXXRRGGBB, and this machine is little-endian, so the
        # bytes land in memory as B, G, R, X. Getting that order backwards
        # shows up as a blue robot, which is at least an obvious failure.
        h, w = rgb.shape[:2]
        out = np.empty((h, w, 4), dtype=np.uint8)
        out[..., 0] = rgb[..., 2]
        out[..., 1] = rgb[..., 1]
        out[..., 2] = rgb[..., 0]
        out[..., 3] = 0xFF
        return out
    raise ValueError(f"unsupported depth: {bpp}bpp")


class Framebuffer:
    def __init__(self, dev="/dev/fb0"):
        self.dev = dev
        fbname = os.path.basename(dev)                       # e.g. "fb0"
        sysfs = f"/sys/class/graphics/{fbname}"
        w, h = open(f"{sysfs}/virtual_size").read().strip().split(",")
        self.w, self.h = int(w), int(h)
        self.bpp = int(open(f"{sysfs}/bits_per_pixel").read().strip())
        self.stride = int(open(f"{sysfs}/stride").read().strip())   # bytes/line
        if self.bpp not in (16, 32):
            raise RuntimeError(f"{dev} is {self.bpp}bpp; this helper does "
                               f"RGB565 (16bpp) and XRGB8888 (32bpp)")

        self.size = self.stride * self.h
        self.fd = os.open(dev, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, self.size)
        self._rowbytes = self.w * (self.bpp // 8)
        self._padded = self.stride != self._rowbytes        # is there row padding?

    def show(self, rgb):
        """Blit an (H, W, 3) uint8 RGB array to the screen."""
        raw = pack(rgb, self.bpp).tobytes()
        if not self._padded:
            self.mm[:] = raw
            return
        # A padded stride used to be copied a row at a time in Python — 480
        # slice assignments per frame. Build the padded image once instead and
        # write it in one go.
        h, rb, st = self.h, self._rowbytes, self.stride
        padded = np.zeros((h, st), dtype=np.uint8)
        padded[:, :rb] = np.frombuffer(raw, dtype=np.uint8).reshape(h, rb)
        self.mm[:] = padded.tobytes()

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
