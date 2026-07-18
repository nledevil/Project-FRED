#!/bin/bash
# Install the InMoov WiFi auto-hotspot fallback. Idempotent — safe to re-run
# after editing hostapd.conf (e.g. to change the SSID/password).
#
#   sudo deploy/hotspot/install.sh
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DEPLOY="$(dirname "$HERE")"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

echo "==> installing packages (hostapd dnsmasq iw)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq hostapd dnsmasq iw

echo "==> the AP owns hostapd/dnsmasq itself — disabling their system services"
systemctl unmask hostapd 2>/dev/null || true
systemctl disable --now hostapd 2>/dev/null || true
systemctl disable --now dnsmasq 2>/dev/null || true

echo "==> installing config + script"
install -D -m 0644 "$HERE/hostapd.conf"          /etc/hostapd/inmoov-hostapd.conf
install -D -m 0644 "$HERE/dnsmasq-hotspot.conf"  /etc/inmoov/hotspot-dnsmasq.conf
install -D -m 0755 "$HERE/inmoov-autohotspot"    /usr/local/sbin/inmoov-autohotspot

echo "==> installing systemd units"
install -m 0644 "$DEPLOY/inmoov-hotspot.service"        /etc/systemd/system/
install -m 0644 "$DEPLOY/inmoov-hotspot-check.service"  /etc/systemd/system/
install -m 0644 "$DEPLOY/inmoov-hotspot.timer"          /etc/systemd/system/
systemctl daemon-reload
systemctl enable inmoov-hotspot.service inmoov-hotspot.timer

echo
echo "Installed. The fallback AP activates only when no saved WiFi is in range."
echo "SSID/password: see /etc/hostapd/inmoov-hostapd.conf"
echo "Test now:  sudo /usr/local/sbin/inmoov-autohotspot status"
echo "Real test: boot the head where none of your saved networks exist, then"
echo "           join the AP and open http://192.168.50.1:8080"
