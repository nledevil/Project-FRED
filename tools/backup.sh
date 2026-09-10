#!/bin/bash
# Back up the state that exists on exactly one disk and nowhere in git.
#
# A dead NUC drive today loses: the PIN digest, the Anthropic API key, every
# device-name tuning that took a bench session to derive (plughw:C16K6Ch,
# mic_channels, DOA calibration), the phrase deck, the device tokens set
# 2026-09-09 — and the Pis' installed unit files plus the root-only token
# drop-ins, which live outside push-role's manifests on purpose. servos.json
# calibration is safe (git-tracked); everything gathered here is not.
#
#   tools/backup.sh                # dated archive into ~/fred-backups
#   DEST=/media/usb tools/backup.sh
#
# The archive HOLDS SECRETS (PIN digest, API key, all four device tokens).
# It is chmod 600 and must be treated like the secrets it contains: copy it to
# a second medium — a USB stick, another machine — and never into this repo.
# tools/preflight.py warns when the newest archive here is over a month old,
# which only helps if "here" is not the same disk that dies.
#
# Read-only against the robot: nothing is written to any machine except the
# archive on this one. Pi files are read over ssh with sudo (tokens are
# root-only); a Pi that is off is reported and skipped, not fatal — half a
# backup beats none, and the summary says exactly what was missed.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${DEST:-$HOME/fred-backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
MISSED=()

grab() {                       # grab <src> <dest-subpath>   (local file)
  if [ -r "$1" ]; then
    mkdir -p "$WORK/$(dirname "$2")"
    cp -p "$1" "$WORK/$2"
  else
    MISSED+=("$1")
  fi
}

grab_ssh() {                   # grab_ssh <host> <remote-path> <dest-subpath>
  mkdir -p "$WORK/$(dirname "$3")"
  if ssh -o BatchMode=yes -o ConnectTimeout=5 "$1" "sudo cat '$2'" \
       > "$WORK/$3" 2>/dev/null && [ -s "$WORK/$3" ]; then
    :
  else
    rm -f "$WORK/$3"
    MISSED+=("$1:$2")
  fi
}

echo "gathering into $WORK"

# ---- NUC: the irreplaceable config -----------------------------------------
grab "$REPO/config/settings.json"       nuc/config/settings.json
grab "$REPO/config/phrases.json"        nuc/config/phrases.json
[ -r "$REPO/config/secrets.env" ] && grab "$REPO/config/secrets.env" nuc/config/secrets.env
sudo cat /etc/fred/anthropic.env        > "$WORK/nuc/etc-fred-anthropic.env" 2>/dev/null \
  || MISSED+=("/etc/fred/anthropic.env")
# Installed units + drop-ins: what systemd actually runs, which has drifted
# from the repo copies before (the head's servo unit lost its eye lock without
# the repo noticing). Files, not the directory: -maxdepth 2 picks up the
# *.service.d/ drop-ins alongside the units.
while IFS= read -r u; do
  rel="nuc/systemd/${u#/etc/systemd/system/}"
  mkdir -p "$WORK/$(dirname "$rel")"
  sudo cat "$u" > "$WORK/$rel" 2>/dev/null || MISSED+=("$u")
done < <(sudo find /etc/systemd/system -maxdepth 2 -type f \
              \( -name 'fred-*' -o -name 'link-watchdog*' \) 2>/dev/null)
# The load-bearing network files (also now tracked in deploy/net-nuc/, but the
# backup takes the LIVE ones — tracking proves drift, backup survives fire).
for f in /etc/netplan/00-installer-config.yaml /etc/netplan/60-fred-uplink.yaml \
         /etc/dnsmasq.d/fred.conf /etc/chrony/conf.d/robot-lan.conf \
         /etc/nftables.conf /etc/sysctl.d/99-fred-nat.conf \
         /etc/udev/rules.d/99-respeaker-xvf3800.rules; do
  rel="nuc/etc/${f#/etc/}"
  mkdir -p "$WORK/$(dirname "$rel")"
  if sudo cat "$f" > "$WORK/$rel" 2>/dev/null && [ -s "$WORK/$rel" ]; then
    :
  else
    rm -f "$WORK/$rel"
    MISSED+=("$f")
  fi
done

# ---- head Pi ----------------------------------------------------------------
for u in servo-server led-server camera-stream inmoov-hotspot; do
  grab_ssh head "/etc/systemd/system/$u.service" "head/systemd/$u.service"
done
grab_ssh head /etc/systemd/system/servo-server.service.d/token.conf \
              head/systemd/servo-server.service.d/token.conf
grab_ssh head /etc/systemd/system/led-server.service.d/token.conf \
              head/systemd/led-server.service.d/token.conf
grab_ssh head /etc/network/interfaces head/etc/network-interfaces

# ---- chest Pi ---------------------------------------------------------------
grab_ssh chest /etc/systemd/system/inmoov-display.service \
               chest/systemd/inmoov-display.service
grab_ssh chest /etc/systemd/system/inmoov-display.service.d/token.conf \
               chest/systemd/inmoov-display.service.d/token.conf
grab_ssh chest /home/dietpi/display/state.json  chest/display-state.json
grab_ssh chest /home/dietpi/display/pin.json    chest/display-pin.json
grab_ssh chest /etc/network/interfaces          chest/etc/network-interfaces

# ---- the archive ------------------------------------------------------------
mkdir -p "$DEST"
OUT="$DEST/fred-backup-$STAMP.tar.gz"
tar -C "$WORK" -czf "$OUT" .
chmod 600 "$OUT"

echo
echo "wrote $OUT ($(du -h "$OUT" | cut -f1)) — chmod 600, HOLDS SECRETS"
if [ "${#MISSED[@]}" -gt 0 ]; then
  echo "MISSED (not fatal, but know it):"
  printf '  %s\n' "${MISSED[@]}"
fi
echo "copy it OFF this machine — the disk it guards against losing is this one."
[ "${#MISSED[@]}" -eq 0 ]
