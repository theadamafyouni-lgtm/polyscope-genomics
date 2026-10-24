#!/bin/bash
# Prepare a fresh Ubuntu machine to run the harness, the demo and the tests. Linux only.
#
#   sudo bash setup.sh
#   python3 preflight.py
#
# Idempotent: safe to run twice. It installs what is missing, fixes what is
# wrong, and then runs preflight.py and refuses to claim success unless preflight
# agrees.
#
# WHAT IT CANNOT DO, AND WHY THAT MATTERS
#
# Two things are left for a person, and the demo does not work without them.
#
#   The reference data. agent/refdata is about 49 GB: the HGDP+1kGP panel,
#   1000 Genomes phase 3, a GRCh37 FASTA, liftOver chains, recombination maps and
#   the imputation panel. It is far past what git will hold, so only its manifest
#   is in this repository.
#
#   Since 2026-09-24 it is rebuilt from public sources, each file checked by
#   sha256 against the copy that produced the published results:
#     python3 fetch_refdata.py;  python3 fetch_refdata.py --verify
#     python3 build_refdata.py;  python3 build_refdata.py --check
#   (Not chained with &&: on Linux a correct rebuild exits 2, not 0, because a
#   few files cannot come out identical. SETUP.md section 8 says which and why.)
#   A few files cannot be reproduced (bin/liftOver among them); SETUP.md
#   section 8 lists them, and says what to copy off a working box first.
#
#   Signing in to Claude Code. It is interactive and cannot be scripted. Note the
#   order, because it is not the obvious one: sign in FIRST, then pin the CLI
#   version, because signing in silently upgrades the CLI and the newer versions
#   cannot open their scratch directory inside the sandbox. Getting this backwards
#   produces cases that die in under a second with an EPERM that reads like a
#   permissions problem and is a version problem.
#
# So `git clone && bash setup.sh` does not give you a working machine. It gives
# you a machine that is missing exactly two things and tells you what they are.
# The demo is installed after that, by demo_site/deploy/install-user.sh (README.md).

set -u
CLI_VERSION=2.1.293   # claude-opus-5-5 needs >= 2.1.280; the Sept 2026 corpus ran 2.1.224
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REFDATA="$REPO/agent/refdata"
say(){ echo "==> $*"; }
warn(){ echo "    ! $*"; }

if [ "$(id -u)" != "0" ]; then
  echo "run this as root: sudo bash setup.sh"
  exit 1
fi

say "system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# bubblewrap is the sandbox. The python packages are NOT optional: tools/ancestry.py
# imports numpy, and without it the ancestry stage refuses, select refuses for want
# of an ancestry to rank on, and every score case fails for a reason that has
# nothing to do with the agent.
#
# apt rather than pip on purpose: the harness runs behind an allow-list proxy that
# blocks PyPI with a 403, so pip cannot reach anything from inside a run.
# tabix provides bgzip and zstd unpacks the HGDP+1kGP tarball; build_refdata.py needs
# both, and bcftools does not pull either in. python3-pytest runs the tests.
apt-get install -y -qq \
    bubblewrap bcftools tabix zstd python3-numpy python3-scipy python3-pandas \
    python3-pytest default-jre curl unzip zip tmux >/dev/null
say "  bubblewrap $(bwrap --version 2>&1 | awk '{print $2}')"

say "node and the CLI"
if ! command -v node >/dev/null; then
  apt-get install -y -qq nodejs npm >/dev/null
fi
NODE_MAJOR=$(node --version 2>/dev/null | sed 's/^v//' | cut -d. -f1)
if [ "${NODE_MAJOR:-0}" -lt 22 ]; then
  warn "node $(node --version) is older than the CLI wants (>=22)."
  warn "It runs with an EBADENGINE warning. Upgrade if anything behaves oddly."
fi
if ! command -v claude >/dev/null; then
  npm install -g "@anthropic-ai/claude-code@${CLI_VERSION}" >/dev/null 2>&1
fi
say "  claude $(claude --version 2>&1 | head -1)"

say "frontend packages"
# For the frontend's tests (three.js); the preview and the demo need none. As the account
# that ran sudo, so that node_modules is not root's.
as_user=(); [ -n "${SUDO_USER:-}" ] && as_user=(sudo -u "$SUDO_USER")
if "${as_user[@]}" npm ci --prefix "$REPO/frontend" --ignore-scripts --no-audit --no-fund >/dev/null 2>&1; then
  say "  frontend/node_modules from package-lock.json"
else
  warn "npm ci in frontend/ failed; the frontend tests need it"
fi

say "the sandbox-exec shim"
# The harness launches every agent as `sandbox-exec -f <profile> <cmd>`, which is
# a macOS command. Rather than patch that call site in three files and keep them
# in step with the macOS original, a shim of the same name translates the profile
# into bubblewrap arguments, so the harness never learns which platform it is on.
if [ -f "$REPO/harness/sandbox-exec" ]; then
  install -m 755 "$REPO/harness/sandbox-exec" /usr/bin/sandbox-exec
  say "  installed /usr/bin/sandbox-exec"
else
  warn "harness/sandbox-exec not found in the repo; the harness cannot launch"
fi

say "tool binaries in refdata"
mkdir -p "$REFDATA/bin"
# fetch_refdata.py and build_refdata.py write here and run as the user, not as root.
[ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER": "$REFDATA/bin"
# plink2 must be exactly the build the published runs used: PLINK v2.0.0-a.6.6LM
# 64-bit Intel (16 Jan 2025), sha256 below, pinned in refdata_recipe.json.
# build_refdata.py extracts it from the official zip. A system plink2 is copied
# only if it is that same file (copied, not symlinked, because refdata is
# bind-mounted read-only into the sandbox and a symlink out of it would dangle).
PLINK2_SHA256=469bdf52266b984e4acbcb68db7efc69ac4f550d64a95fd257e66989be330d49
if [ ! -x "$REFDATA/bin/plink2" ]; then
  sys_plink2=$(command -v plink2 || true)
  if [ -n "$sys_plink2" ] && \
     [ "$(sha256sum "$sys_plink2" | cut -d' ' -f1)" = "$PLINK2_SHA256" ]; then
    cp "$sys_plink2" "$REFDATA/bin/plink2"
  else
    warn "refdata/bin/plink2 is not in place. python3 build_refdata.py --only plink2"
    warn "installs the pinned build once fetch_refdata.py has downloaded its zip."
  fi
elif [ "$(sha256sum "$REFDATA/bin/plink2" | cut -d' ' -f1)" != "$PLINK2_SHA256" ]; then
  warn "refdata/bin/plink2 is not the pinned build (see SETUP.md section 3). Left as it"
  warn "is; move it aside and run python3 build_refdata.py --only plink2 to replace it."
fi
if [ ! -f "$REFDATA/bin/liftOver" ]; then
  curl -sSL -o "$REFDATA/bin/liftOver" \
    https://hgdownload.soe.ucsc.edu/admin/exe/linux.x86_64/liftOver || \
    warn "could not download liftOver"
  [ -n "${SUDO_USER:-}" ] && [ -f "$REFDATA/bin/liftOver" ] && chown "$SUDO_USER": "$REFDATA/bin/liftOver"
fi
# The execute bit, explicitly, every time.
#
# This is the single most expensive mistake made setting this up. plink2 was
# copied into place mode 644. refdata is read-only inside the sandbox, so the
# agent could not chmod it; it built a shim directory with an executable copy
# instead, which pulled 48 GB of reference data into its own output and filled
# the disk. Nothing looked broken until the disk ran out, because the agent
# documented what it had done and carried on correctly.
chmod 755 "$REFDATA/bin/"* 2>/dev/null
for b in plink2 liftOver; do
  if [ -x "$REFDATA/bin/$b" ]; then
    say "  $b $(("$REFDATA/bin/$b" --version 2>&1 || "$REFDATA/bin/$b" 2>&1) | head -1 | cut -c1-50)"
  fi
done

say "context hygiene"
# Any CLAUDE.md the agent can see is loaded into its context automatically. One
# sat at the filesystem root on this machine and would have entered every single
# run; the launch gate caught it, which is the only reason it was found.
for f in /CLAUDE.md "$HOME/CLAUDE.md" "$HOME/.claude/CLAUDE.md"; do
  if [ -f "$f" ]; then
    mv "$f" "$f.moved-by-setup" && say "  moved $f aside"
  fi
done

say "reference data"
if [ -d "$REFDATA/ancestry-ref" ] && [ -d "$REFDATA/panel" ]; then
  say "  present, $(du -sh "$REFDATA" 2>/dev/null | cut -f1)"
else
  warn "agent/refdata is missing or incomplete. About 49 GB, not in this repo."
  warn "Rebuild it: python3 fetch_refdata.py, then python3 build_refdata.py,"
  warn "each with its check (--verify, --check). See SETUP.md section 8."
fi

echo
say "preflight"
echo
python3 "$REPO/preflight.py"
rc=$?
echo
if [ "$rc" = "0" ]; then
  say "ready. Sign in if you have not: run \`claude\`, then re-pin:"
  say "  npm install -g @anthropic-ai/claude-code@${CLI_VERSION}"
  say "in that order, because signing in upgrades the CLI."
else
  say "not ready. Fix what preflight lists above and run it again."
fi
say "tests and the demo: README.md, 'Run the tests' and 'Run the demo'."
exit $rc
