# Private lab demo

The `demo-site` branch imports the frontend from `5cb6737` and adds an authenticated
demo app. The public preview files remain unchanged. The web process binds only
`127.0.0.1:8765`; a separate root worker accepts a fixed Unix-socket protocol from
the web UID. Its only run command is the harness's human backend with `--demo`, a
generated upload path, and normal local imputation. No browser-supplied commands,
paths, persona selection, or harness flags are accepted.

## Install and start on this VM

Run these **after reviewing the branch**. Installation starts no services or Funnel.
It installs root-owned source under `/srv/prs-demo`, creates the unprivileged
`prs-demo` account and a root-only random master secret outside Git, and hard-links
the existing VM reference data into the installation. It never edits the original
repository. Do not chmod or write to those linked reference files.

```sh
cd <your checkout>
sudo bash demo_site/deploy/install.sh
sudo systemctl start prs-demo-worker.service prs-demo-web.service
```

Open `http://localhost:8765` **on the VM**, or use an SSH tunnel:

```sh
ssh -N -L 8765:127.0.0.1:8765 root@VM
# Open http://localhost:8765 in a browser that supports Secure cookies on localhost.
```

The web service always sets Secure, HttpOnly, SameSite=Strict cookies, including
locally. For the public demo, HTTPS is required. One command prints today's grouped
password and its New York expiry, never the master secret:

```sh
sudo sh -c 'cd /srv/prs-demo && /usr/bin/python3 -m demo_site.auth password'
```

The daily password is HMAC-SHA256 over the `America/New_York` date, encoded as four groups
of five characters. Rotation needs no cron job.

One fixed password replaces it once its owner writes it, on its first line, into
`fixed-password` beside the master secret (`/var/lib/prs-demo/auth/fixed-password` on the VM,
`state/auth/fixed-password` on the gb10), a plain file of the worker's account. Nothing prints
or logs it. It is read at every sign-in, so writing the file is all it takes; while the file
is missing or empty (or not a plain file of that account's own, or longer than 128
characters) the daily password stays the password, so the site is never open. The day the
worker first sees it (it looks every 15 s) is the day it goes live, recorded in
`fixed-password-since`: that day's daily password also works until midnight in New York.
`python3 -m demo_site.auth status` says which is in use and since when, never what it is.

A sign-in lasts 24 hours. A session that ends with a demo still going, or with its result
still there to read, stays its visitor's for that demo until it is ended or purged: its
status, its questions, End demo and the page, never a new upload or a sample, which need the
password again. Re-login in the same browser resumes its run. Login attempts have persistent per-client and global limits, with growing
cooldowns that expire. All POSTs require an exact Origin and session CSRF token.

The worker needs cgroup v2 with `cgroup.kill` and systemd delegation. It retains the
harness's existing sandbox/dependency/authentication requirements, including the
root credential files already configured on this VM. Credentials are not copied
into the website. Reference staging uses hard links and needs the installation and
demo state on the same filesystem. No model call or real genome analysis was run
during development.

## Install and run without root (the gb10)

On an account with no root and no systemd user services, web and worker run as that one
account (no `prs-demo` user, no delegated cgroup: a run's processes are found by its run id),
and a cron watchdog keeps them up. Everything lives under one directory, BASE:

```sh
bash demo_site/deploy/install-user.sh "$HOME/prs-agent/demo" "$HOME/prs-agent/home"
# then add the line it prints to the crontab:
*/3 * * * * $HOME/prs-agent/demo/bin/demo-watchdog >> $HOME/prs-agent/demo/state/watchdog.log 2>&1
# today's password:
(cd "$HOME/prs-agent/demo/app" && python3 -m demo_site.auth password --secret-file ../state/auth/master.key)
```

The second argument is the HOME the harness runs with (SETUP.md section 7, "Without root").
Each watchdog pass (`demo_site/watchdog.py`) restarts the worker, then the web, if either
died; ends a dead worker's unfinished runs as failures first; counts restarts over 24 h and
stops at 5 (remove `state/restarts.json` to reset); and starts nothing while
`state/claude-login-expired` exists. Its settings, `BASE/watchdog.json`, take the port and
origin. Same-account processes can read and stop all of it: a boundary this layout does not
have, unlike the VM's root worker.

Every agent runs in a bubblewrap sandbox, which this account can make only while the kernel
lets an unprivileged account make a user namespace. Ubuntu's kernels may not (AppArmor's
`kernel.apparmor_restrict_unprivileged_userns`): the gb10's did not after its 2026-10-20
reboot into 6.17, and every run then ended before step 1. The worker checks when it starts
and every 15 s. While it cannot make one, no new analysis starts, the page says so before
anyone chooses a file, and one `sandbox` line goes to `state/failures.log` per outage. The
fix needs root: an AppArmor profile that lets `/usr/bin/bwrap` alone make user namespaces.

```sh
printf 'abi <abi/4.0>,\ninclude <tunables/global>\n\nprofile bwrap /usr/bin/bwrap flags=(unconfined) {\n  userns,\n  include if exists <local/bwrap>\n}\n' \
  | sudo tee /etc/apparmor.d/bwrap >/dev/null && sudo apparmor_parser -r /etc/apparmor.d/bwrap
bwrap --ro-bind / / --unshare-user /bin/true && echo sandbox OK   # as the demo's account
```

### On the tailnet without root: a self-signed HTTPS front

`tailscale serve` needs root or the tailscale operator. Until then `demo_site/tls_proxy.py`
gives the demo HTTPS on the host's own tailnet addresses (never a wildcard or LAN address)
with a self-signed certificate, so the tailnet can reach it and each browser warns once about
the certificate: Chrome and Edge "Your connection is not private" (Advanced, then Proceed),
Firefox "Warning: Potential Security Risk Ahead" (Advanced, then Accept the Risk and
Continue), Safari "This Connection Is Not Private" (Show Details, then visit this website).
The link is `https://<machine>.<tailnet>.ts.net:8443/`, the one Tailscale Serve keeps later.

```sh
BASE=$HOME/prs-agent/demo
NAME=$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
(cd "$BASE/app" && python3 -m demo_site.tls_proxy cert --host "$NAME" --cert ../state/tls/cert.pem --key ../state/tls/key.pem)
# prints the SHA-256 fingerprint, for people to compare with the one their browser shows
```

Then in `$BASE/watchdog.json`, beside `harness_home`:

```json
"origin": "https://<NAME>:8443", "trust_funnel": true,
"tls_proxy": {"listen": ["<tailscale ip -4>:8443", "[<tailscale ip -6>]:8443"]}
```

`trust_funnel` keys the login limit on each person's tailnet address, which the front writes
into `X-Forwarded-For` in place of any the browser sent, as Tailscale does. The web reads its
origin only when it starts, so restart it once; the same pass starts the front:

```sh
kill "$(cat "$BASE/state/web.pid")"; "$BASE/bin/demo-watchdog"
```

The certificate lasts 397 days. To renew it, remove `state/tls/*.pem`, run `cert` again and
stop the front (`kill "$(cat "$BASE/state/tls_proxy.pid")"`): the next pass starts it with
the new certificate, and every browser warns once more.

**Switching to Tailscale Serve** keeps the link, the origin and `trust_funnel`; only the
warning goes.

1. The tailnet admin enables HTTPS certificates (admin console, DNS page), and root runs
   `tailscale set --operator=<user>` once, or runs step 3 itself.
2. Remove `tls_proxy` from `watchdog.json`. The next pass stops the front and frees port
   8443; run `$BASE/bin/demo-watchdog` to do it at once.
3. `tailscale serve --bg --https=8443 http://127.0.0.1:8765`, then `tailscale serve status`.

### A public link through a front door

The demo stays on the demo host. A machine with a public address and root (the front door)
runs nginx with a Let's Encrypt certificate for a public name and forwards over Tailscale to
the demo host's HTTPS front, accepting only that front's self-signed certificate, which it
pins. Visitors see no certificate warning. `demo_site/deploy/front-door-nginx.conf` is the
site; its placeholders are listed at its top.

On the front door, as root, with the public name's A record already pointing at it:

```sh
apt-get install -y nginx certbot
install -d /etc/nginx/prs-demo /var/www/letsencrypt
# the demo host's BASE/state/tls/cert.pem; compare its fingerprint with the one `cert` printed
install -m 644 cert.pem /etc/nginx/prs-demo/demo-host.pem
openssl x509 -in /etc/nginx/prs-demo/demo-host.pem -noout -fingerprint -sha256
ufw allow 80/tcp; ufw allow 443/tcp
```

Fill in the template; until the certificate exists, enable only its port-80 servers
(`nginx -t` refuses a missing certificate), then:

```sh
certbot certonly --webroot -w /var/www/letsencrypt -d <public name> \
  --register-unsafely-without-email --agree-tos --non-interactive --deploy-hook "systemctl reload nginx"
# now the whole template in /etc/nginx/sites-enabled/, then
nginx -t && systemctl reload nginx
```

certbot's timer renews the certificate and the hook reloads nginx. When the demo host's own
certificate is renewed, copy the new one to the pin, or the front door refuses it and shows its
offline page.

While the demo host can't be reached (after 10 s), or its web is down, the front door answers
with its own plain page: "The demo is offline right now. Please try again later." The page loads
the address again every minute. Every request still goes to the demo host first, so the demo is
back by itself, with nothing to change on the front door, as soon as the host is.

On the demo host, in `BASE/watchdog.json`:

```json
"origin": ["https://<public name>", "https://<NAME>:8443"], "trust_funnel": true,
"tls_proxy": {"listen": [...as before...],
              "trusted_proxies": ["<front door's tailscale ip -4>", "<its tailscale ip -6>"]},
"max_runs": 2, "run_limit_minutes": 240, "typical_run_minutes": 80
```

Each origin is one host the demo answers to, so the tailnet link keeps working. The front door
puts the visitor's address alone in `X-Forwarded-For`; the front keeps it from the trusted
addresses only (anyone else's is replaced, as before), so the login limit is per visitor.
`max_runs` demos run at once; a visitor who arrives when they are all in use sees when one
should be free (the earliest a running demo is due to end, after the median run time below), and
the page opens the upload form when one is. How long a run takes is measured, not set: the worker
times each completed run that reached the end of its score stage, less the time it waited for its
person's answers, into `state/private/run-times.jsonl`, with the kind of file it read: a chip's
genotype file or a whole genome (what the door judged it, before that the file's own format). Each
kind has its own estimate, the range of its last ten (the median is when a slot should be free);
the upload card shows both. Until five of a kind are timed, `demo_site/run-times-measured.json`,
the runs measured after the demo speedups, counts beside them; `typical_run_minutes` is used only
when there is neither. A run still going after `run_limit_minutes` is stopped
and ended as a failure its person sees. Two at once suits a 20-core demo host: one run's
imputation keeps about 12 cores busy for half an hour, and each run makes about 75 model
requests on the Claude account the harness uses.

When a run ends, its upload, the agent's sandbox and working files and the person's typed answers
are deleted; the result the page shows (the report with its comparison data, and the progress) is
kept in the run's `result.json` for 24 hours, then deleted. The finish screen says when. End
deletes it at once.

Each run has a private link, `/r/<token>` (256 random bits, shown only to the
run's owner), that opens its progress and result from any browser, a private window included,
without the password, until the result is deleted: its questions and End, never a new
upload or a sample. The front door does not log `/r/` paths.

Sample genomes signed-in people may download go in `BASE/samples/` (plain file names, each no
larger than an upload); the upload card lists what is there. Offer only files whose full run has
passed on this host, and never one that names a person (a zip's member name included).

The worker reads its settings only when it starts, and restarting it ends any demo in
progress, so restart it only when none is running; then the web and the front:

```sh
for c in worker web tls_proxy; do kill "$(cat "$BASE/state/$c.pid")"; done; "$BASE/bin/demo-watchdog"
```

To close the public link, stop nginx on the front door (`systemctl stop nginx`); the tailnet
link is unaffected.

## Put it behind Funnel (Adam's step)

Review existing Funnel configuration first. Set the exact public origin, restart
only the web service, then explicitly enable the localhost proxy:

```sh
sudo tailscale funnel status
demo_dns=$(sudo tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
printf 'PRS_DEMO_ORIGIN=https://%s\n' "$demo_dns" | sudo tee /etc/prs-demo-web.conf
sudo systemctl restart prs-demo-web.service
sudo tailscale funnel --bg --https=443 http://127.0.0.1:8765
sudo tailscale funnel status
```

Open the HTTPS URL printed by Tailscale. If prompted, Adam must approve enabling
Funnel/HTTPS in the tailnet. Tailscale handles the certificate. Follow the
[official Funnel instructions](https://tailscale.com/docs/reference/tailscale-cli/funnel).
The service trusts only the loopback proxy's single `X-Forwarded-For` address for
per-client limiting; the global limiter remains effective independently. Local
OS users are within the VM trust boundary. No Funnel setting was changed here.

To keep the app running after VM reboot, use
`sudo systemctl enable prs-demo-worker prs-demo-web`.
To remove this public proxy, use `sudo tailscale funnel --https=443 off`.

## Files and cleanup

Demo uploads, live events, answers, logs, vaults, and sandboxes live under
`/var/lib/prs-demo/private/runs/demo-*`. They are marked `demo: true`; `--demo`
explicitly bypasses benchmark publication to `results/`. The web user cannot read
this tree directly. It receives only its own questions, progress, and report.
Once `max_runs` demos (default 1) are running, everyone else sees busy, with when a slot
should be free. The worker's file lock and state
survive a web restart. A worker restart stops unfinished demos and ends each as a failure
its person sees (`python3 -m demo_site.worker end-interrupted` does the same while no worker
runs); they are purged with the rest an hour later, never silently dropped.

Every demo run that fails gets one line in `failures.log` beside the worker's state
directory: `run<TAB>case<TAB>what failed<TAB>why<TAB>when`, case `demo`, why from the
harness's own environment verdict where it made one (`demo_site/failures.py`). A run that
could not authenticate also writes `claude-login-expired` there, which stops the watchdog
restarting anything until a person logs in again and removes it.

End demo kills the entire run cgroup and deletes all its files. Completed and
failed demos are purged after one hour (swept every 15 seconds). The lock is released
only after leftover run processes stop. All HTTP responses use `no-store`; the UI
keeps data only in memory and clears it at logout, expiry, or page exit. Session and
throttle databases contain authentication metadata, not genome/report/answer data.

Uploads accept one UTF-8 genotype table, VCF, or Complete Genomics var or masterVar file:
plain, gzip, bzip2, or a single-file ZIP (ZIP64 included). Whole genomes and exomes are
uploads: the agent sets a call-quality policy for a sequencing file at the door, and converts
a Complete Genomics file with `cg_to_vcf.py`.
Names are generated, expansion is bounded and archive paths are never extracted. An upload
goes from the front door to the demo host as it arrives, never whole in memory or in a file
on the front door; other requests' bodies are held to 64 KiB there. The web
receives an upload into a file that never has a name, so nothing of it outlives the request,
and hands the worker that open file; an incomplete one never reaches the worker. The web
gives an upload the time its size needs at 1 Mbit/s, lets go of a link that stalls for 60 s,
and takes two at a time; a signed-in visitor's upload refused before it is read (demo full,
or a run already going) is read and dropped first, so the answer isn't lost to a reset
connection. The worker expands the file into the run a block at a time and
checks every line, on a thread of its own: a file checked within 10 s is answered at once
(refused with its reason, and no run is left), a longer check is the run's first step, and
End demo stops it. No run starts with less free space than room for two expanded genomes
and 16 GiB. The limits are **1 GiB uploaded / 8 GiB expanded**, above the largest whole-genome
files people receive (a consumer genome VCF is 200-800 MB, a Complete Genomics var file about
270 MB as bzip2 and 1.5-1.6 GB expanded). See
[input-limits.json](demo_site/input-limits.json) for measurements. There is no hash
allowlist. **The four tracked demo personas have no input-genome mapping in their
files; their sizes still need checking once Adam identifies the four input files.**

## Local fake tests

All run tests use synthetic genomes and a fake agent. The integration test uses the
real harness client, human router, live JSONL question/answer route, HTTP parser,
restricted worker dispatch, and frontend renderer. Nothing calls a model or Beagle.

```sh
cd <your checkout>
mkdir -p .test-tmp
TMPDIR="$PWD/.test-tmp" PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q \
  tests/test_demo_auth.py tests/test_demo_uploads.py tests/test_demo_worker.py \
  tests/test_demo_web.py tests/test_demo_flow.py tests/test_demo_deploy.py \
  --basetemp="$PWD/.test-tmp/pytest"
npm test
```

The review sandbox denies socket creation. HTTP and RPC parser tests therefore use
in-memory connections; TCP/Unix-socket smoke tests skip only when the OS denies
sockets. Re-run on the VM to exercise those listeners. Browser rendering and real
cgroup/kernel cleanup could not be exercised here; cgroup behavior has fake tests.
The shipped service units pass `systemd-analyze verify`.

Recorded output: [DEMO-TESTS.txt](DEMO-TESTS.txt) — 38 demo tests passed, two
socket smoke checks skipped; 18 existing fake regression tests passed; all five
frontend test files passed (including eight focused demo UI tests).

## Asked up front

The routine questions are on the upload form, answered before the run starts: which condition
or trait (required), what made them look into it, their family's ancestry, sex, whether a doctor
has diagnosed or treats it, a note for their doctor (optional), and the agreement (required). They
go with the file in one header, `X-PRS-Intake` (never the URL); the web and the worker check them
(`demo_site/intake.py`) and the worker keeps them in the run's `intake.json`, deleted with its
working files. The harness (`run --demo --intake`) stages them for the agent with the demo brief:
the agent asks only what the file and the answers cannot settle and the result depends on, never
to confirm the model or to go ahead, and the report says which method it picked, why, and what
it costs. A benchmark run has no answers file and its brief is unchanged.
