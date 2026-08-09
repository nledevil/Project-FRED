#!/bin/sh
# fred — attach to this machine's shared "claude" tmux session, creating it if
# it isn't there yet. Installed as /usr/local/bin/fred on the head and chest Pis.
#
# The point is that there is only ever ONE session per machine. The panel's
# terminal buttons open `ssh -t <pi> tmux new-session -A -s claude`, and the
# head's own inmoov-terminal opens the same name, so whether you arrive through
# the browser or by sshing in and typing `fred`, you land on the same work. Start
# a long claude run in the browser, close the tab, ssh in later, type `fred`, and
# it is still going.
#
# It also means your work is not hostage to the connection you started it on:
# the session lives on this Pi, so a dropped ethernet cable leaves it running.
#
# Named `fred` because `claude` is already a real binary here (~/.local/bin/claude)
# and shadowing it would be a nasty surprise.
set -eu

SESSION="${FRED_SESSION:-claude}"

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
# which is why this is allowed to be a best guess: the head keeps the robot code
# in ~/inmoov, the chest keeps the touchscreen code in ~/display.
START_DIR="${FRED_SESSION_DIR:-}"
if [ -z "$START_DIR" ]; then
    for d in "$HOME/inmoov" "$HOME/display"; do
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
