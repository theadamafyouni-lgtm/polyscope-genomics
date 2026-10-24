#!/bin/bash
# Adam runs this after review. It installs files but starts no services or Funnel.
set -euo pipefail
umask 022
test "$(id -u)" = 0 || { echo 'Run as root.' >&2; exit 1; }
cd "$(dirname "$0")/../.."
test "$(git branch --show-current)" = demo-site || { echo 'Use the demo-site branch.' >&2; exit 1; }
test -z "$(git status --porcelain --untracked-files=no)" || { echo 'Commit tracked changes before installation.' >&2; exit 1; }
test ! -e /srv/prs-demo || { echo '/srv/prs-demo already exists; review an update explicitly.' >&2; exit 1; }
test -d /sys/fs/cgroup || { echo 'Cgroup v2 is required.' >&2; exit 1; }
test -f /sys/fs/cgroup/cgroup.controllers || { echo 'Cgroup v2 is required.' >&2; exit 1; }
test -d /root/CliGenAI-Lab/prs-agent-product/agent/refdata/ancestry-ref/phase3 || {
  echo 'The VM reference data is missing; complete the harness setup first.' >&2; exit 1;
}
for program in python3 bwrap sandbox-exec claude plink2 bcftools tabix bgzip java; do
  command -v "$program" >/dev/null || { echo "Missing harness dependency: $program" >&2; exit 1; }
done
test -x /usr/bin/sandbox-exec || { echo 'The harness requires /usr/bin/sandbox-exec.' >&2; exit 1; }
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -c 'import numpy, scipy'
test -f /root/CliGenAI-Lab/prs-agent-product/agent/refdata/bin/beagle.jar || {
  echo 'The normal imputation reference bundle is incomplete.' >&2; exit 1;
}
for binary in plink2 liftOver; do
  test -x "/root/CliGenAI-Lab/prs-agent-product/agent/refdata/bin/$binary" || {
    echo "Missing bundled reference tool: $binary" >&2; exit 1;
  }
done
if ! id prs-demo >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin prs-demo
fi
install -d -m 0755 /srv/prs-demo
# Explicit source paths: no personas, genomes, benchmark results, Git history or secrets.
git archive HEAD frontend demo_site harness agent/.claude agent/tools agent/interfaces \
  agent/AGENT.md agent/JUDGMENT-CORE.md reference-provider | tar -x -C /srv/prs-demo
chmod 0700 /srv/prs-demo/harness /srv/prs-demo/agent /srv/prs-demo/reference-provider
# The harness stages refdata with hard links and read-only sandbox mounts already.
# Linking here reads the original tree; never chmod linked files or write through them.
cp -al /root/CliGenAI-Lab/prs-agent-product/agent/refdata /srv/prs-demo/agent/refdata
install -d -m 0700 /var/lib/prs-demo /var/lib/prs-demo/auth /var/lib/prs-demo/private
install -d -m 0700 -o prs-demo -g prs-demo /var/lib/prs-demo-web
if ! test -e /var/lib/prs-demo/auth/master.key; then
  (cd /srv/prs-demo && /usr/bin/python3 -m demo_site.auth init)
fi
install -m 0644 demo_site/deploy/prs-demo-worker.service /etc/systemd/system/prs-demo-worker.service
install -m 0644 demo_site/deploy/prs-demo-web.service /etc/systemd/system/prs-demo-web.service
systemctl daemon-reload
echo 'Installed. No service or Funnel was started. Follow DEMO-README.md.'
