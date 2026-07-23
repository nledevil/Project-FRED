#!/bin/bash
# Install the head<->chest Bluetooth PAN link. Idempotent — safe to re-run.
#
#   sudo deploy/pan/install.sh head     # on the head Pi   (NAP server, 10.0.0.1)
#   sudo deploy/pan/install.sh chest    # on the chest Pi  (client,     10.0.0.2)
#
# Pairing is NOT done here — it is a one-time manual step, see PAIRING below.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROLE="${1:-}"

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

case "$ROLE" in
  head|chest) ;;
  *) echo "usage: sudo $0 head|chest"; exit 1 ;;
esac

echo "==> installing bluez-tools (provides bt-network)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bluez-tools

if [ "$ROLE" = head ]; then
    echo "==> installing pan-server.service (NAP server on 10.0.0.1)"
    install -m 0644 "$HERE/pan-server.service" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable pan-server.service
    systemctl restart pan-server.service   # restart, not start: pick up unit edits on re-run
    echo
    echo "Head done. NAP server registered; it now waits for the chest."

else
    # The on-board BCM4345C0 has no firmware patch in a stock DietPi install, so
    # it comes up as AA:AA:AA:AA:AA:AA with almost no profiles and PAN cannot
    # work at all. These two packages + the udev rule are what fix that.
    echo "==> installing Bluetooth firmware + pi-bluetooth (BCM4345C0.hcd, bthelper)"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bluez-firmware pi-bluetooth

    echo "==> wiring bthelper up (upstream udev rule never fires on DietPi)"
    install -D -m 0644 "$HERE/bthelper-no-hciuart.conf" \
        /etc/systemd/system/bthelper@.service.d/no-hciuart.conf
    install -D -m 0644 "$HERE/91-inmoov-bthelper.rules" \
        /etc/udev/rules.d/91-inmoov-bthelper.rules
    udevadm control --reload-rules

    echo "==> installing pan-client.service (client on 10.0.0.2)"
    # Older installs kept these settings in a drop-in; they are inline in the
    # unit now, so clear the drop-in to avoid configuring the same keys twice.
    rm -rf /etc/systemd/system/pan-client.service.d
    install -m 0644 "$HERE/pan-client.service" /etc/systemd/system/

    echo "==> installing the link watchdog (pan-check)"
    install -D -m 0755 "$HERE/inmoov-pan-check" /usr/local/sbin/inmoov-pan-check
    install -m 0644 "$HERE/pan-check.service" /etc/systemd/system/
    install -m 0644 "$HERE/pan-check.timer"   /etc/systemd/system/

    systemctl daemon-reload
    systemctl enable pan-client.service
    systemctl restart pan-client.service   # restart, not start: pick up unit edits on re-run
    systemctl enable --now pan-check.timer

    echo
    echo "Chest done. A REBOOT is required the first time: the BCM4345C0"
    echo "firmware patch only loads during HCI device setup, and until it does"
    echo "the adapter keeps the bogus AA:AA:AA:AA:AA:AA address."
fi

cat <<'PAIRING'

PAIRING (one-time, and again if either adapter's MAC ever changes)
  Head MAC:  E4:5F:01:49:60:BB     Chest MAC: DC:A6:32:C8:F6:17

  On the head:
      bt-agent -c NoInputNoOutput &
      bluetoothctl -- pairable on
      bluetoothctl -- discoverable on

  On the chest (one bluetoothctl session; the BR/EDR transport matters --
  the default LE-only scan will not find the head):
      bt-agent -c NoInputNoOutput &
      { echo "menu scan"; sleep 1; echo "transport bredr"; sleep 1; echo back
        sleep 1; echo "scan on"; sleep 20; echo "scan off"; sleep 2
        echo "pair E4:5F:01:49:60:BB";  sleep 12
        echo "trust E4:5F:01:49:60:BB"; sleep 3; echo quit; } | bluetoothctl

  Then on the head, put discoverable/pairable back:
      bluetoothctl -- discoverable off
      bluetoothctl -- pairable off

  Verify from the head:  ping 10.0.0.2
PAIRING
