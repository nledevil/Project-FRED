#!/bin/bash
# One answer to "is what's running what's in the repo?" — across all three.
#
# The pieces existed but were separate manual invocations, so in practice none
# of them got run and the chest drifted (two .bak/scratch files outside its
# manifest, found only when someone went looking). This runs them together and
# summarises: per-role file drift (push-role --dry-run), unclaimed files on each
# Pi (--list-extra), the NUC's /etc config (check-host-config.sh), and — the new
# part — each Pi's DEPLOYED stamp against the current HEAD, so "the chest is
# running an old build" is a line you read rather than a thing you discover.
#
#   tools/check_deploy.sh
#   HEAD_TARGET=dietpi@10.0.0.10 CHEST_TARGET=dietpi@10.0.0.11 tools/check_deploy.sh
#
# Read-only: every constituent is read-only (dry-run, list, diff, cat), nothing
# is written or restarted anywhere. Exits non-zero if anything drifted, so it
# can gate a "before I leave for the venue" check the way the tests gate a
# commit.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
HEAD_TARGET="${HEAD_TARGET:-dietpi@10.0.0.10}"
CHEST_TARGET="${CHEST_TARGET:-dietpi@10.0.0.11}"
HEAD_ROOT="/home/dietpi/inmoov"
CHEST_ROOT="/home/dietpi/display"
drift=0

REV="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
DIRTY=""; [ -n "$(git -C "$REPO" status --porcelain 2>/dev/null)" ] && DIRTY=" (repo dirty)"
echo "repo at $REV$DIRTY"
echo

# ---- per-role file drift ---------------------------------------------------
role_drift() {                 # role  target
  local role="$1" target="$2"
  echo "== $role ($target) =="
  local changed
  changed="$("$REPO/deploy/push-role.sh" "$role" "$target" --dry-run 2>/dev/null \
             | grep -cE '^  (NEW|CHANGED)' || true)"
  if [ "$changed" -gt 0 ]; then
    echo "  $changed file(s) differ from the repo:"
    "$REPO/deploy/push-role.sh" "$role" "$target" --dry-run 2>/dev/null \
      | grep -E '^  (NEW|CHANGED)' | sed 's/^/  /'
    drift=1
  else
    echo "  files: in sync"
  fi
  local extra
  # Only the lines between push-role's "--- present ... ---" and "--- end ---"
  # markers; its role/target/count banner prints to the same stream and is not
  # part of the strip list.
  extra="$("$REPO/deploy/push-role.sh" "$role" "$target" --list-extra 2>/dev/null \
           | awk '/^--- present/{on=1; next} /^--- end/{on=0} on' \
           | grep -vE '^$' || true)"
  if [ -n "$extra" ]; then
    echo "  unclaimed on the target (strip candidates):"
    printf '%s\n' "$extra" | sed 's/^/    /'
    drift=1
  else
    echo "  extras: none"
  fi
}

role_drift head  "$HEAD_TARGET"
echo
role_drift chest "$CHEST_TARGET"

# ---- each Pi's deployed stamp vs HEAD --------------------------------------
echo
echo "== deployed revision =="
stamp_check() {                # label  target  root
  local label="$1" target="$2" root="$3"
  local stamp
  stamp="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$target" \
           "cat '$root/DEPLOYED' 2>/dev/null" || true)"
  if [ -z "$stamp" ]; then
    echo "  $label: no DEPLOYED stamp — never pushed with the stamping push-role"
    drift=1
    return
  fi
  local commit
  commit="$(printf '%s' "$stamp" | tr ' ' '\n' | sed -n 's/^commit=//p')"
  if [ "$commit" = "$REV" ]; then
    echo "  $label: $commit — matches HEAD"
  else
    echo "  $label: $commit — DIFFERS from HEAD ($REV)"
    echo "         $stamp"
    drift=1
  fi
}
stamp_check head  "$HEAD_TARGET"  "$HEAD_ROOT"
stamp_check chest "$CHEST_TARGET" "$CHEST_ROOT"

# ---- the NUC's /etc config --------------------------------------------------
echo
echo "== NUC host config =="
if "$REPO/deploy/check-host-config.sh" >/dev/null 2>&1; then
  echo "  matches the repo"
else
  echo "  DRIFTED — run deploy/check-host-config.sh for the diff"
  drift=1
fi

echo
[ "$drift" -eq 0 ] && echo "all three machines match the repo" \
                    || echo "DRIFT above — push-role the roles that differ, or reconcile /etc"
[ "$drift" -eq 0 ]
