#!/bin/bash
# Install the head<->chest Bluetooth PAN link. Idempotent — safe to re-run.
#
#   sudo deploy/pan/install.sh head  <chest-bdaddr>   # head Pi  (NAP server, 10.0.0.1)
#   sudo deploy/pan/install.sh chest <head-bdaddr>    # chest Pi (client,     10.0.0.2)
#
# The peer's Bluetooth address is an argument, not a constant in this repo:
# adapters differ per machine, so hard-coding a pair would make this installable
# on exactly one robot. Find each Pi's own address with:
#
#   bluetoothctl show | grep Controller
#
# Pairing is NOT done here — it is a one-time manual step, see PAIRING below.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROLE="${1:-}"
PEER="${2:-}"
CLIENT_UNIT=/etc/systemd/system/pan-client.service

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

case "$ROLE" in
  head|chest) ;;
  *) echo "usage: sudo $0 head|chest <peer-bluetooth-address>"; exit 1 ;;
esac

# Re-run with no address: keep whatever the last install used, so this stays
# idempotent as advertised rather than demanding the address every time.
if [ -z "$PEER" ] && [ "$ROLE" = chest ] && [ -f "$CLIENT_UNIT" ]; then
    PEER="$(sed -n 's/.*bt-network -c \([0-9A-Fa-f:]\{17\}\).*/\1/p' "$CLIENT_UNIT" | head -1)"
    [ -n "$PEER" ] && echo "==> reusing peer address from $CLIENT_UNIT: $PEER"
fi

if [ -n "$PEER" ]; then
    printf '%s' "$PEER" | grep -qE '^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$' || {
        echo "not a Bluetooth address: $PEER"; exit 1; }
    PEER="$(printf '%s' "$PEER" | tr 'a-f' 'A-F')"
fi

if [ "$ROLE" = chest ] && [ -z "$PEER" ]; then
    echo "the chest needs the head's Bluetooth address — it is what it dials:"
    echo "    sudo $0 chest AA:BB:CC:DD:EE:FF"
    echo "find it by running this on the head:  bluetoothctl show | grep Controller"
    exit 1
fi

echo "==> installing bluez-tools (provides bt-network)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bluez-tools

if [ "$ROLE" = head ]; then
    # BlueZ's NAP server asks for service-level authorisation on every incoming
    # BNEP connection. It only auto-approves a *trusted* peer; for anyone else it
    # needs an agent to ask, and with no agent running it just logs
    # "Authentication attempt without agent" + "auth_cb() Access denied" and
    # rejects. Pairing alone is NOT enough -- the chest can be Paired and Bonded
    # and still be turned away every retry, forever. Trusting is persistent
    # (stored in /var/lib/bluetooth/.../info), so this only has to happen once,
    # but it is cheap and idempotent so do it on every install.
    if [ -n "$PEER" ]; then
        echo "==> trusting the chest ($PEER) so incoming NAP connections are auto-approved"
        bluetoothctl trust "$PEER" || \
            echo "    (failed -- chest not paired yet? re-run after the PAIRING step)"
    else
        echo "==> no chest address given; skipping the trust step"
        echo "    re-run as: sudo $0 head <chest-bdaddr>   (see PAIRING below)"
    fi

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
    # The repo's unit is a template; the head's address is baked in on install.
    sed "s/@PEER_BDADDR@/$PEER/g" "$HERE/pan-client.service" > "$CLIENT_UNIT"
    chmod 0644 "$CLIENT_UNIT"

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

SELF="$(bluetoothctl show 2>/dev/null |
        sed -n 's/^Controller \([0-9A-F:]\{17\}\).*/\1/p' | head -1)"
echo
echo "Addresses:  this Pi ($ROLE) = ${SELF:-unknown}   peer = ${PEER:-not given}"

cat <<'PAIRING'

PAIRING (one-time, and again if either adapter's address ever changes)
  Substitute your own two addresses below — each Pi reports its own with
  "bluetoothctl show | grep Controller".

  On the head:
      bt-agent -c NoInputNoOutput &
      bluetoothctl -- pairable on
      bluetoothctl -- discoverable on

  On the chest (one bluetoothctl session; the BR/EDR transport matters --
  the default LE-only scan will not find the head):
      bt-agent -c NoInputNoOutput &
      { echo "menu scan"; sleep 1; echo "transport bredr"; sleep 1; echo back
        sleep 1; echo "scan on"; sleep 20; echo "scan off"; sleep 2
        echo "pair <head-bdaddr>";  sleep 12
        echo "trust <head-bdaddr>"; sleep 3; echo quit; } | bluetoothctl

  Then on the head, trust the chest and close discovery back up. The trust is
  not optional and it is not symmetric with the chest's: the chest is the side
  that dials out, so the head is the side that receives the NAP authorisation
  request. An untrusted chest is rejected on every retry, forever, with only
  "Authentication attempt without agent" in the head's bluetoothd log to say so.
      bluetoothctl -- trust <chest-bdaddr>
      bluetoothctl -- discoverable off
      bluetoothctl -- pairable off

  Verify from the head:  ping 10.0.0.2
PAIRING
