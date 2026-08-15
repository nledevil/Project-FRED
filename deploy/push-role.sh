#!/usr/bin/env bash
# Push exactly one machine's role onto it, and nothing else.
#
#   deploy/push-role.sh head  dietpi@10.0.0.10
#   deploy/push-role.sh chest dietpi@10.0.0.11 --dry-run
#   deploy/push-role.sh head  dietpi@10.0.0.10 --list-extra
#
# The manifests in deploy/manifests/ are the definition of what belongs on each
# machine. That is the point of this script: the repo stays whole — the client
# and server halves of four protocols still change in one commit — while each Pi
# carries only the files its own services import.
#
# Transfers with tar over ssh rather than rsync: rsync is on the Pis but not on
# the NUC, and tar is on all three. No dependency to install on a robot.
#
# --dry-run     report NEW / CHANGED / unchanged per file, touch nothing
# --list-extra  list files on the target the manifest does NOT claim. That is
#               the strip list. It never deletes — read it, then remove what you
#               agree with.
set -euo pipefail

ROLE="${1:-}"; TARGET="${2:-}"
shift 2 2>/dev/null || true
DRY=0; LIST_EXTRA=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)    DRY=1 ;;
    --list-extra) LIST_EXTRA=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$REPO/deploy/manifests/${ROLE}.txt"

if [ -z "$ROLE" ] || [ -z "$TARGET" ] || [ ! -f "$MANIFEST" ]; then
  echo "usage: $(basename "$0") <role> <user@host> [--dry-run] [--list-extra]" >&2
  echo "roles: $(cd "$REPO/deploy/manifests" 2>/dev/null && ls ./*.txt 2>/dev/null | sed 's|\./||;s|\.txt||' | tr '\n' ' ')" >&2
  exit 2
fi

# ---- parse the manifest ----------------------------------------------------
ROOT=""; FLATTEN="no"; SYNC=(); SEED=(); KEEP=()
while IFS= read -r raw; do
  line="$(echo "${raw%%$'\r'}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  case "$line" in
    '# root:'*|'root:'*)       ROOT="${line#*: }" ;;
    '# flatten:'*|'flatten:'*) FLATTEN="$(echo "${line#*: }" | awk '{print $1}')" ;;
    'seed:'*)                  SEED+=("${line#*: }") ;;
    'keep:'*)                  KEEP+=("${line#*: }") ;;
    '#'*|'')                   : ;;
    *)                         SYNC+=("$line") ;;
  esac
done < "$MANIFEST"

[ -n "$ROOT" ] || { echo "manifest has no 'root:'" >&2; exit 2; }

# Where each file lands on the target.
dest_of() { [ "$FLATTEN" = "yes" ] && echo "$ROOT/$(basename "$1")" || echo "$ROOT/$1"; }

echo "role=$ROLE  target=$TARGET  root=$ROOT  flatten=$FLATTEN"
echo "  ${#SYNC[@]} synced, ${#SEED[@]} seed-only, ${#KEEP[@]} kept$([ $DRY -eq 1 ] && echo '   (DRY RUN)')"

# A manifest typo must fail loudly here, not silently under-deploy the robot.
missing=0
for f in ${SYNC[@]+"${SYNC[@]}"} ${SEED[@]+"${SEED[@]}"}; do
  [ -e "$REPO/$f" ] || { echo "  MISSING IN REPO: $f" >&2; missing=1; }
done
[ "$missing" -eq 0 ] || { echo "aborting: manifest names files that do not exist" >&2; exit 1; }

# So must a name that does not exist. The chest Pi has no linter and no pip, and
# a NameError in a branch nothing imports — `PowerMenu(log=log)` in the settings
# menu — reaches the panel as a blank screen when someone presses the gear, with
# the traceback in child-stderr.log where nobody is looking. The tests import
# page classes directly and never launched the menu, so nothing caught it.
#
# Only "undefined name" is fatal. pyflakes' other opinions (unused imports, the
# `global` declarations menu_ui assigns through globals()[k]) are style at best
# and wrong at worst, and a deploy is the wrong place to argue about them.
if "$REPO/venv/bin/python" -m pyflakes --version >/dev/null 2>&1; then
  undef=0
  for f in ${SYNC[@]+"${SYNC[@]}"}; do
    case "$f" in *.py) ;; *) continue ;; esac
    out="$("$REPO/venv/bin/python" -m pyflakes "$REPO/$f" 2>/dev/null \
           | grep 'undefined name')" || true
    [ -z "$out" ] || { echo "$out" >&2; undef=1; }
  done
  [ "$undef" -eq 0 ] || { echo "aborting: undefined names above would crash on the Pi" >&2; exit 1; }
else
  echo "  note: no pyflakes in venv - skipping the undefined-name check" >&2
fi

# ---- --list-extra ----------------------------------------------------------
if [ "$LIST_EXTRA" -eq 1 ]; then
  claimed="$(mktemp)"; trap 'rm -f "$claimed"' EXIT
  {
    for f in ${SYNC[@]+"${SYNC[@]}"} ${SEED[@]+"${SEED[@]}"}; do
      [ "$FLATTEN" = "yes" ] && basename "$f" || echo "$f"
    done
    printf '%s\n' ${KEEP[@]+"${KEEP[@]}"}
  } | sort -u > "$claimed"

  echo "--- present on $TARGET:$ROOT but NOT claimed by the manifest ---"
  # Files only. Directories are implied by the files inside them, and listing
  # them just produces noise you cannot act on ("deploy/ is unclaimed" is not a
  # thing you delete). __pycache__ is excluded as a path, not just its contents.
  ssh "$TARGET" "cd '$ROOT' 2>/dev/null && find . -mindepth 1 -type f \
        -not -path './.git/*' -not -path './venv/*' -not -path '*__pycache__*' \
        -printf '%P\n' 2>/dev/null | sort" \
    | grep -vxF -f "$claimed" || true
  echo "--- end (nothing was deleted) ---"
  exit 0
fi

# ---- dry run: compare hashes ----------------------------------------------
if [ "$DRY" -eq 1 ]; then
  for f in ${SYNC[@]+"${SYNC[@]}"}; do
    d="$(dest_of "$f")"
    local_h="$(sha256sum "$REPO/$f" | cut -d' ' -f1)"
    remote_h="$(ssh "$TARGET" "sha256sum '$d' 2>/dev/null | cut -d' ' -f1" || true)"
    if   [ -z "$remote_h" ];            then echo "  NEW       $d"
    elif [ "$local_h" != "$remote_h" ]; then echo "  CHANGED   $d"
    else                                     echo "  unchanged $d"; fi
  done
  for f in ${SEED[@]+"${SEED[@]}"}; do
    d="$(dest_of "$f")"
    ssh "$TARGET" "test -e '$d'" && echo "  seed kept $d" || echo "  seed NEW  $d"
  done
  echo "done (dry run — nothing was written)."
  exit 0
fi

# ---- sync ------------------------------------------------------------------
ssh "$TARGET" "mkdir -p '$ROOT'"
if [ ${#SYNC[@]} -gt 0 ]; then
  if [ "$FLATTEN" = "yes" ]; then
    # Strip every directory component: the chest Pi's deploy dir is flat.
    tar -C "$REPO" -cf - --transform='s|.*/||' "${SYNC[@]}" \
      | ssh "$TARGET" "tar -C '$ROOT' -xf - && echo '  synced ${#SYNC[@]} files'"
  else
    tar -C "$REPO" -cf - "${SYNC[@]}" \
      | ssh "$TARGET" "tar -C '$ROOT' -xf - && echo '  synced ${#SYNC[@]} files'"
  fi
fi

# ---- seed ------------------------------------------------------------------
for f in ${SEED[@]+"${SEED[@]}"}; do
  d="$(dest_of "$f")"
  if ssh "$TARGET" "test -e '$d'"; then
    echo "  seed kept (already present, not overwritten): $d"
  else
    ssh "$TARGET" "mkdir -p \"\$(dirname '$d')\""
    tar -C "$REPO" -cf - "$f" | ssh "$TARGET" "tar -C '$ROOT' -xf -"
    echo "  seeded: $d"
  fi
done

echo "done."
