#!/bin/sh
# fred — attach to a FRED machine's shared tmux session, creating it if it isn't
# there yet. Installed as /usr/local/bin/fred on ALL THREE machines.
#
# NOTE: /usr/local/bin is outside both deploy roots (head lands in ~/inmoov,
# chest in ~/display), so this file is NOT in deploy/manifests/ and push-role.sh
# will never update it. After editing, push it by hand to all three:
#   sudo install -m 755 deploy/fred-session.sh /usr/local/bin/fred
#   for m in head chest; do scp deploy/fred-session.sh $m:/tmp/fred.new &&
#       ssh $m 'sudo install -m 755 /tmp/fred.new /usr/local/bin/fred'; done
#
#   fred          attach to THIS machine's session
#   fred nuc      attach to the brain's session (hops by ssh if you aren't there)
#   fred head     attach to the head Pi's session
#   fred chest    attach to the chest Pi's session
#
# The point is that there is only ever ONE session per machine. The panel's
# terminal buttons open `ssh -t <pi> tmux new-session -A -s claude`, the NUC's
# own terminals open `-s nuc`, and this script uses the same names — so whether
# you arrive through the browser, through Tailscale, or by sshing in and typing
# `fred`, you land on the same work. Start a long claude run in the browser,
# close the tab, ssh in later, type `fred`, and it is still going.
#
# It also means your work is not hostage to the connection you started it on:
# the session lives on the target machine, so a dropped ethernet cable or a
# flaky wifi dongle leaves it running.
#
# Named `fred` because `claude` is already a real binary here (~/.local/bin/claude)
# and shadowing it would be a nasty surprise.
set -eu

# --- who am I ---------------------------------------------------------------
# By robot-LAN address, NOT hostname: both Pis report "DietPi", so the hostname
# cannot tell head from chest. The NUC is the one exception worth short-circuiting
# — it answers to `fred` and owns 10.0.0.1 as the LAN's gateway.
this_machine() {
    if [ "$(hostname 2>/dev/null)" = "fred" ]; then echo nuc; return; fi
    # /usr/sbin and /sbin are not on the default PATH on the NUC; be explicit.
    for ipbin in ip /usr/sbin/ip /sbin/ip /usr/bin/ip; do
        command -v "$ipbin" >/dev/null 2>&1 || continue
        for addr in $("$ipbin" -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1); do
            case "$addr" in
                10.0.0.1)  echo nuc;   return ;;
                10.0.0.10) echo head;  return ;;
                10.0.0.11) echo chest; return ;;
            esac
        done
        break
    done
    echo unknown
}

# --- session name per machine -----------------------------------------------
# These must match the ttyd units: fred-terminal*.service open `-s nuc` on the
# brain and `-s claude` on both Pis. Change one, change the other, or you get
# two sessions on the same box and the "one session" promise quietly breaks.
session_for() {
    case "$1" in
        nuc) echo nuc ;;
        *)   echo claude ;;
    esac
}

HERE="$(this_machine)"
TARGET="${1:-$HERE}"

case "$TARGET" in
    nuc|head|chest) ;;
    -h|--help)
        sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
        exit 0 ;;
    *)
        echo "fred: unknown machine '$TARGET' (expected: nuc, head, chest)" >&2
        exit 1 ;;
esac

# --- hop to another machine -------------------------------------------------
# Delegate to the target's own `fred` rather than reimplementing the attach over
# ssh: it knows its own session name and start directory. -t forces a tty, which
# tmux needs. Safe to do from inside tmux — an ssh out is not a nested session.
if [ "$TARGET" != "$HERE" ]; then
    if [ "$HERE" = "unknown" ]; then
        echo "fred: could not identify this machine; hopping to '$TARGET' anyway." >&2
    fi
    echo "Connecting to $TARGET…"
    exec ssh -t "$TARGET" fred
fi

SESSION="${FRED_SESSION:-$(session_for "$HERE")}"

if ! command -v tmux >/dev/null 2>&1; then
    echo "fred: tmux is not installed (apt install tmux)" >&2
    exit 1
fi

# Nesting a session inside itself is confusing and the keybindings fight. If we
# are already in tmux, say so and offer the thing the user probably meant.
if [ -n "${TMUX:-}" ]; then
    current="$(tmux display-message -p '#S' 2>/dev/null || echo '?')"
    if [ "$current" = "$SESSION" ]; then
        echo "fred: you are already in the '$SESSION' session." >&2
    else
        echo "fred: already inside tmux (session '$current'); refusing to nest." >&2
        echo "      switch with:  tmux switch-client -t $SESSION" >&2
    fi
    exit 1
fi

# Where a NEW session should start. Ignored when attaching to an existing one,
# which is why this is allowed to be a best guess: the brain keeps its code in
# the repo, the head keeps the robot code in ~/inmoov, the chest keeps the
# touchscreen code in ~/display.
START_DIR="${FRED_SESSION_DIR:-}"
if [ -z "$START_DIR" ]; then
    for d in "$HOME/fred/Project-FRED" "$HOME/inmoov" "$HOME/display"; do
        if [ -d "$d" ]; then START_DIR="$d"; break; fi
    done
    [ -n "$START_DIR" ] || START_DIR="$HOME"
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Attaching to '$SESSION' on $(hostname) — your work is as you left it."
else
    echo "Starting '$SESSION' on $(hostname) in $START_DIR."
fi

# -A: attach if it exists, create if it doesn't. exec so tmux replaces this
# shell and Ctrl-b d puts you straight back at your prompt.
exec tmux new-session -A -s "$SESSION" -c "$START_DIR"
