#!/bin/bash
# Screenshot the web panel at the widths that matter, with the snap dance
# already fought through.
#
# This is render_pages.py's job for the web: layout on these pages is checked
# by looking at it, and "looks fine at my window size" is how the phone layout
# stayed broken for months. Run it after touching index.html, admin.html or
# ui.css and actually look at the output.
#
#   web/tools/screenshot.sh [outdir]     # defaults to /tmp/webshots
#
# The dance, because each step cost a failed attempt:
#   - chromium is a snap, and snapd refuses to launch it from inside another
#     service's cgroup ("is not a snap cgroup") — hence systemd-run --scope
#   - there is no user session bus on this headless machine, so the scope has
#     to be a *system* one, which means root
#   - root's snap can only write inside /root, so shots land in /root/webshots
#     first and are copied out after
#
# The admin page will show the PIN keypad over everything unless this machine
# is in the trusted set — the keypad popping IS the 401 flow working. For a
# clean look at the layout underneath, check the phone/desktop shots of
# index.html (whose boot reads are unlocked) or unlock a session first.
set -euo pipefail

OUT="${1:-/tmp/webshots}"
BASE="${BASE:-http://10.0.0.1:8080}"
mkdir -p "$OUT"
sudo mkdir -p /root/webshots
sudo rm -f /root/webshots/shot_*.png

shot() {  # shot <name> <WxH> <url>
  local name="$1" size="$2" url="$3"
  sudo systemd-run --scope --collect -q -- snap run chromium \
    --headless --disable-gpu --no-sandbox --hide-scrollbars \
    --window-size="${size/x/,}" --virtual-time-budget=9000 \
    --screenshot="/root/webshots/shot_${name}.png" "$url" 2>/dev/null
  echo "  ${name} (${size})"
}

echo "panel:"
shot idx_phone    390x844   "$BASE/"
shot idx_tablet   768x1024  "$BASE/"
shot idx_desktop  1280x900  "$BASE/"
shot idx_wide     1680x1000 "$BASE/"
shot view_talk    390x844   "$BASE/?view=talk"
shot view_servos  390x844   "$BASE/?view=servos"
shot view_cart    390x844   "$BASE/?view=cart"
echo "admin:"
shot adm_phone    390x900   "$BASE/admin"
shot adm_desktop  900x1100  "$BASE/admin"

sudo sh -c "cp /root/webshots/shot_*.png '$OUT'/ && chown $(id -u) '$OUT'/shot_"*.png
echo "wrote $(ls "$OUT"/shot_*.png | wc -l) shots to $OUT"
