#!/bin/sh
# Re-run DHCP when a wired interface has carrier but no address.
#
# Why this exists: /etc/network/interfaces says "allow-hotplug eth0", and
# hotplug means a *device* event -- the driver loading, the NIC appearing. Pulling
# and reseating an ethernet *cable* is a carrier change; the device never goes
# away, so ifupdown is never told anything happened and does nothing. The
# dhclient started at boot stays running but backs off to very long retries, so
# a Pi that booted with its cable out (or was unplugged long enough to lose the
# lease) sits there with a live link and no IPv4 essentially forever.
#
# That is exactly what happened to the chest Pi on 2026-08-09: link up, MAC
# visible in the NUC's bridge table, dhclient alive since boot, and no address.
#
# Run from link-watchdog.timer every 30s. Deliberately dumb and idempotent: it
# does nothing at all unless the interface is both carrier-up and address-less,
# which is a state that only occurs when something is actually wrong.
set -eu

IFACE="${IFACE:-eth0}"

# No such interface -- nothing to do (and don't log about it every 30s).
[ -e "/sys/class/net/${IFACE}/carrier" ] || exit 0

# No cable in it. Not a fault; the whole point is to wait for one.
# (carrier reads as an error rather than 0 when the interface is admin-down.)
carrier="$(cat "/sys/class/net/${IFACE}/carrier" 2>/dev/null || echo 0)"
[ "$carrier" = "1" ] || exit 0

# Already has an address: the normal case, every 30s, forever. Stay quiet.
if ip -4 addr show dev "$IFACE" 2>/dev/null | grep -q 'inet '; then
    exit 0
fi

logger -t link-watchdog "${IFACE}: carrier up but no IPv4 — re-running DHCP"

# ifup --force does NOT clean up a dhclient that is already running for this
# interface, and two of them on one link fight over renewals. Kill ours first.
# The pattern cannot match wlan0's dhclient: that command line carries wlan0
# throughout and never mentions eth0.
pkill -f "dhclient.*${IFACE}" 2>/dev/null || true

# --force because ifupdown's state file still believes the interface is up, so
# a plain ifup would decline to do anything.
if ifup --force "$IFACE" >/dev/null 2>&1; then
    addr="$(ip -4 -br addr show dev "$IFACE" 2>/dev/null || true)"
    logger -t link-watchdog "${IFACE}: recovered — ${addr}"
else
    logger -t link-watchdog "${IFACE}: ifup failed; will retry on the next tick"
fi
