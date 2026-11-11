#!/bin/sh
# Restart a service that is alive to systemd and dead to the world.
#
# Why this exists: every FRED unit is Restart= guarded, but Restart only fires
# when the process EXITS. The documented failure class on this robot is the
# other one — wedged-but-running. A deadlocked panel thread, a stuck ALSA
# read, a hung display daemon all sit "active (running)" indefinitely, and at
# a venue the operator's tool for noticing is the thing that is wedged.
# "Froze mid-show" used to mean finding a laptop with an audience watching.
#
# Same shape as link-watchdog.sh, for the same reason: a dumb timer-driven
# probe that does nothing at all when nothing is wrong. Every 30 s it curls
# one URL the service must answer; after THRESHOLD consecutive failures it
# restarts the unit and says so to the journal. The consecutive counter lives
# in /run so a reboot clears it.
#
# Two deliberate refusals:
#   * A unit that is not active is left alone. systemctl stop is an operator
#     decision, and a watchdog that fights the operator is worse than none.
#   * One probe failing once does nothing. Venue WiFi is not on this path
#     (everything probes loopback or the wired robot LAN), but a busy box can
#     miss one 5 s window without being dead.
#
# Parameterised by environment, one timer+service pair per watched unit:
#   UNIT=fred-panel  URL=http://127.0.0.1:8080/api/state  service-watchdog
set -eu

UNIT="${UNIT:?UNIT not set}"
URL="${URL:?URL not set}"
THRESHOLD="${THRESHOLD:-3}"
TIMEOUT="${TIMEOUT:-5}"
STATE="/run/service-watchdog-${UNIT}.fails"

# Operator stopped it, it crashed and systemd is mid-restart, or it was never
# enabled here: none of those are ours to fix.
[ "$(systemctl is-active "$UNIT" 2>/dev/null || true)" = "active" ] || {
    rm -f "$STATE"
    exit 0
}

if curl -fsS --max-time "$TIMEOUT" -o /dev/null "$URL" 2>/dev/null; then
    rm -f "$STATE"
    exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"

if [ "$fails" -lt "$THRESHOLD" ]; then
    logger -t service-watchdog "${UNIT}: probe failed (${fails}/${THRESHOLD}) — ${URL}"
    exit 0
fi

rm -f "$STATE"
logger -t service-watchdog "${UNIT}: ${THRESHOLD} consecutive probe failures — restarting"
systemctl restart "$UNIT" || logger -t service-watchdog "${UNIT}: restart FAILED"
# The one thing a fresh fred-panel does not bring back on its own: the wake
# listener starts disabled by design, so a mid-show restart leaves him deaf
# until somebody flips the toggle. Say so where the postmortem will look.
[ "$UNIT" = "fred-panel" ] && \
    logger -t service-watchdog "fred-panel: restarted — NOTE: voice listener comes back OFF; re-arm from a panel"
exit 0
