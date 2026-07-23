#!/usr/bin/env bash
# Reset the P10S USB audio device in software — the equivalent of unplugging and
# replugging it. Use when audio goes silent (aplay "succeeds" / exits 0 but no
# sound comes out): the cheap MV-SILICON codec can wedge its USB streaming
# endpoint under load, and a full device reset clears it.
#
#   sudo ~/inmoov/deploy/reset-audio.sh
#
# (Needs root for the USB reset ioctl. Stop the app first isn't required, but the
#  listener will reconnect on its own afterwards.)
set -euo pipefail

VID=1234        # MV-SILICON P10S
PID=5684

dev=""
for d in /sys/bus/usb/devices/*; do
  [ -f "$d/idVendor" ] || continue
  if [ "$(cat "$d/idVendor")" = "$VID" ] && [ "$(cat "$d/idProduct")" = "$PID" ]; then
    printf -v dev "/dev/bus/usb/%03d/%03d" "$(cat "$d/busnum")" "$(cat "$d/devnum")"
    break
  fi
done

if [ -z "$dev" ]; then
  echo "P10S audio device (${VID}:${PID}) not found — is it plugged in?" >&2
  exit 1
fi

echo "Resetting USB audio device at $dev ..."
python3 - "$dev" <<'PY'
import fcntl, sys
USBDEVFS_RESET = (ord('U') << 8) | 20   # from <linux/usbdevice_fs.h>
with open(sys.argv[1], "wb") as f:
    fcntl.ioctl(f, USBDEVFS_RESET, 0)
print("USB reset issued.")
PY

sleep 2
amixer -c 0 sset 'PCM' 85% >/dev/null 2>&1 || true   # fresh enumeration can drop the volume
echo "Done. Test with: aplay -D plughw:0,0 ~/inmoov/sounds/startup.wav"
