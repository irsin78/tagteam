#!/usr/bin/env bash
# Native-platform measurement for the notes this repo still marks UNVERIFIED.
#
# docs/platform-notes-linux.md was derived from a WSL2 lane, so several of its
# items say "inferred, unverified". This script measures those items on the
# machine it runs on and prints one report; paste the report back to update
# the note and the README support table together (they must move as a pair).
#
#   bash tools/linux-probe.sh            # from the repo root
#   bash tools/linux-probe.sh /path/to/clone
#
# Read-only apart from a temp directory it creates and removes. It installs
# nothing and needs no credentials.
REPO=${1:-$(cd "$(dirname "$0")/.." && pwd)}
OUT=${OUT:-/tmp/harness-platform-report.txt}
exec > >(tee "$OUT") 2>&1
say(){ printf '\n=== %s ===\n' "$*"; }

say "A. environment"
uname -a
echo "arch: $(uname -m)"
[ -r /etc/os-release ] && . /etc/os-release && echo "distro: $PRETTY_NAME"
echo "bash: ${BASH_VERSION:-unknown}"
for c in python python3 git curl node npm flock bwrap sudo socat xdg-open; do
  printf '%-9s ' "$c"; command -v "$c" || echo "(absent)"
done
printf 'timeout : '; timeout --version 2>/dev/null | head -1 || echo "(absent)"
echo "pid1    : $(cat /proc/1/comm 2>/dev/null || echo n/a)"

say "B. check-posix.sh (note 1 predicts a WARN when 'python' is absent)"
if [ -f "$REPO/check-posix.sh" ]; then
  ( cd "$REPO" && git log --oneline -1 2>/dev/null; cd "$REPO" && bash check-posix.sh )
  echo "check-posix exit: $?"
else
  echo "SKIP: no check-posix.sh under $REPO"
fi

say "C. note 7 - filename normalization (NFC vs NFD)"
d=$(mktemp -d) && ( cd "$d"
  : > "$(printf 'a\xea\xb0\x80')"                  # NFC  U+AC00
  : > "$(printf 'a\xe1\x84\x80\xe1\x85\xa1')"      # NFD  decomposed
  n=$(ls -1 | wc -l)
  echo "distinct files: $n   (2 = stores bytes as written; 1 = filesystem normalizes)" )
rm -rf "$d"

say "D. note 2 - login-shell PATH"
echo "current   : $PATH"
echo "login     : $(env -i HOME="$HOME" bash -lc 'echo $PATH' 2>&1)"
echo "empty env : $(env -i bash -c 'echo $PATH' 2>&1)"

say "E. note 5 - bubblewrap sandboxing"
if command -v bwrap >/dev/null; then
  bwrap --version
  bwrap --ro-bind / / --unshare-net --dev /dev true 2>&1 &&
    echo "non-root bwrap run: OK" || echo "non-root bwrap run: FAILED (see message above)"
else
  echo "bwrap absent. apt candidate:"
  apt-cache policy bubblewrap 2>/dev/null | head -3 || echo "(apt-cache unavailable)"
fi

say "F. notes 3/4 - headless CLI readiness (presence only; installs nothing)"
for c in claude codex agy; do printf '%-7s ' "$c"; command -v "$c" || echo "(absent)"; done
echo "DISPLAY='${DISPLAY:-}'  (empty = headless, so CLI logins need a device/URL flow)"

say "DONE - report written to $OUT"
