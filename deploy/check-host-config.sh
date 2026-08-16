#!/bin/bash
# Compare the host-level config files this repo tracks against the machines.
#
# push-role.sh covers everything that lives under one root per Pi. These are the
# few files that do not: they belong to /etc, they are installed by hand, and
# until something compares them the repo's copy is a guess. Read-only — it
# diffs and reports, and never writes to a machine.
#
#   deploy/check-host-config.sh
#   HEAD_HOST=10.0.0.10 deploy/check-host-config.sh
#
# Exits non-zero if anything differs or could not be read, so it can gate a
# deploy the way push-role.sh's own checks do.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HEAD_HOST="${HEAD_HOST:-10.0.0.10}"
HEAD_USER="${HEAD_USER:-dietpi}"

# repo path : host ("-" = this machine, the NUC) : path on that host
FILES=(
  "deploy/net-head/interfaces:$HEAD_USER@$HEAD_HOST:/etc/network/interfaces"
  "deploy/hotspot-nuc/fred-nat.nft:-:/etc/nftables.conf"
  "deploy/hotspot-nuc/99-fred-nat.conf:-:/etc/sysctl.d/99-fred-nat.conf"
)

bad=0
for entry in "${FILES[@]}"; do
  repo="${entry%%:*}"
  rest="${entry#*:}"
  host="${rest%%:*}"
  path="${rest#*:}"
  label="$([ "$host" = "-" ] && echo "nuc" || echo "${host#*@}")"

  if [ ! -f "$REPO/$repo" ]; then
    echo "  MISSING IN REPO  $repo"
    bad=1
    continue
  fi

  if [ "$host" = "-" ]; then
    live="$(sudo cat "$path" 2>/dev/null)"
  else
    live="$(ssh -o ConnectTimeout=5 "$host" "sudo cat '$path' 2>/dev/null" 2>/dev/null)"
  fi

  if [ -z "$live" ]; then
    echo "  UNREADABLE       $label:$path"
    bad=1
    continue
  fi

  if diff -q <(printf '%s\n' "$live") "$REPO/$repo" >/dev/null 2>&1; then
    echo "  same             $label:$path"
  else
    echo "  DIFFERS          $label:$path"
    diff <(printf '%s\n' "$live") "$REPO/$repo" | sed 's/^/                   /' | head -20
    bad=1
  fi
done

echo
if [ "$bad" -eq 0 ]; then
  echo "host config matches the repo."
else
  echo "host config has drifted — '<' is the machine, '>' is the repo." >&2
fi
exit "$bad"
