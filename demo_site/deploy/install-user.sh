#!/bin/bash
# Install the demo for an account without root (the gb10): the code, reference data hard links,
# private state, a master key and the watchdog, all under BASE. Starts nothing and edits no
# crontab; it prints the line to add. install.sh is the root and systemd installer for the VM.
#   bash demo_site/deploy/install-user.sh BASE HARNESS_HOME
# HARNESS_HOME is the HOME the harness runs with: its .config/prs-harness/ holds tokens.json,
# host.json and claude.env (SETUP.md section 7, "Without root").
set -euo pipefail
umask 077
BASE=${1:?usage: install-user.sh BASE HARNESS_HOME}
HHOME=${2:?usage: install-user.sh BASE HARNESS_HOME}
cd "$(dirname "$0")/../.."
REPO=$(pwd)
test "$(id -u)" != 0 || { echo 'As root, use install.sh.' >&2; exit 1; }
test -z "$(git status --porcelain --untracked-files=no)" || { echo 'Commit tracked changes before installation.' >&2; exit 1; }
test ! -e "$BASE/app" || { echo "$BASE/app already exists; review an update explicitly." >&2; exit 1; }
test -d "$REPO/agent/refdata/ancestry-ref/phase3" || { echo 'The reference data is missing.' >&2; exit 1; }
test -f "$REPO/agent/refdata/bin/beagle.jar" || { echo 'The imputation reference bundle is incomplete.' >&2; exit 1; }
for f in tokens.json host.json claude.env; do
  test -f "$HHOME/.config/prs-harness/$f" || { echo "Missing $HHOME/.config/prs-harness/$f" >&2; exit 1; }
done
for program in bwrap java; do
  command -v "$program" >/dev/null || { echo "Missing harness dependency: $program" >&2; exit 1; }
done
mkdir -p "$BASE/app" "$BASE/bin" "$BASE/run" "$BASE/state/private" "$BASE/state/web" "$BASE/state/auth"
chmod 700 "$BASE" "$BASE/app" "$BASE/bin" "$BASE/run" "$BASE/state" "$BASE/state/private" \
  "$BASE/state/web" "$BASE/state/auth"
# Explicit source paths: no personas, genomes, benchmark results, Git history or secrets.
git archive HEAD frontend demo_site harness agent/.claude agent/tools agent/interfaces \
  agent/AGENT.md agent/JUDGMENT-CORE.md reference-provider | tar -x -C "$BASE/app"
git rev-parse HEAD > "$BASE/app/REVISION"
# Hard links, as the harness stages refdata itself: never chmod or write through them.
cp -al "$REPO/agent/refdata" "$BASE/app/agent/refdata"
install -m 0700 demo_site/deploy/demo-watchdog "$BASE/bin/demo-watchdog"
test -e "$BASE/watchdog.json" || printf '{"harness_home": "%s"}\n' "$HHOME" > "$BASE/watchdog.json"
if ! test -e "$BASE/state/auth/master.key"; then
  (cd "$BASE/app" && /usr/bin/python3 -m demo_site.auth init --secret-file "$BASE/state/auth/master.key")
fi
echo "Installed under $BASE. Nothing was started. To run it, add this crontab line:"
echo "*/3 * * * * $BASE/bin/demo-watchdog >> $BASE/state/watchdog.log 2>&1"
