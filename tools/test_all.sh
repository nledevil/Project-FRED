#!/bin/bash
# Run every narrative test in the repo, from one command.
#
# The tests are the project's postmortems — each one a real failure made
# executable — but they were nineteen-then-twenty separate invocations across
# two directories, so in practice only the test touching that day's work got
# run. That is exactly how test_auth.py sat red until an audit found it. This
# is the "before I commit" gate the style was missing: glob both directories,
# run each under the venv, print one line each, exit non-zero if any failed.
#
#   tools/test_all.sh            # everything
#   tools/test_all.sh mic doa    # only tests whose path matches a filter
#   VERBOSE=1 tools/test_all.sh  # show each failing test's full output
#
# Every test is hardware-free by design and passes on this machine — including
# the chest Pi's, which are stdlib and run fine here under the venv (verified),
# so this needs no ssh and no robot. A test that starts needing hardware breaks
# that contract and should be split, not special-cased here.
#
# Keyed on the exit code, never the wording: the tests say "all checks passed",
# "OK: all checks passed", and other things, but every one exits non-zero on
# failure, which is the one promise the whole style rests on.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/venv/bin/python"
[ -x "$PY" ] || PY="python3"        # a fresh checkout before the venv exists
FILTERS=("$@")

# Collect every test. The chest ones run from their own directory so their
# `sys.path[:0] = [_HERE, dirname(_HERE)]` shim resolves the display modules,
# exactly as they do on the Pi.
declare -a TESTS
while IFS= read -r f; do TESTS+=("$f"); done < <(
  find "$REPO/tools" "$REPO/deploy/display/tools" -maxdepth 1 \
       -name 'test_*.py' -type f | sort)

matches() {                          # no filters = everything; else substring OR
  [ "${#FILTERS[@]}" -eq 0 ] && return 0
  for want in "${FILTERS[@]}"; do case "$1" in *"$want"*) return 0;; esac; done
  return 1
}

pass=0; fail=0; skip=0
failed_names=()
start=$(date +%s)

for t in "${TESTS[@]}"; do
  rel="${t#"$REPO"/}"
  if ! matches "$rel"; then skip=$((skip+1)); continue; fi
  # Chest tests want their own dir as CWD; NUC tests are indifferent to it.
  dir="$(dirname "$t")"
  if out="$(cd "$dir" && "$PY" "$(basename "$t")" 2>&1)"; then
    printf "  \033[32mPASS\033[0m  %s\n" "$rel"
    pass=$((pass+1))
  else
    printf "  \033[31mFAIL\033[0m  %s\n" "$rel"
    fail=$((fail+1)); failed_names+=("$rel")
    # Always show the failing test's last line (its own summary); the full
    # output only when asked, so a green run stays a clean table.
    echo "$out" | tail -n "$([ -n "${VERBOSE:-}" ] && echo 40 || echo 1)" \
      | sed 's/^/          /'
  fi
done

elapsed=$(( $(date +%s) - start ))
echo
if [ "$fail" -eq 0 ]; then
  printf "\033[32m%d passed\033[0m" "$pass"
else
  printf "\033[31m%d failed\033[0m, %d passed" "$fail" "$pass"
fi
[ "$skip" -gt 0 ] && printf ", %d skipped by filter" "$skip"
printf "  (%ds)\n" "$elapsed"
[ "$fail" -eq 0 ] || printf "  failed: %s\n" "${failed_names[*]}"
[ "$fail" -eq 0 ]
