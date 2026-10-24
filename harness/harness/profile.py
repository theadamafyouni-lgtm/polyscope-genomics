"""Bubblewrap profile generation. The Linux counterpart of the Seatbelt profile.

Same three functions as the macOS module, same call signature, so orchestrator.py
does not change. What comes out is not SBPL: it is one bubblewrap argument per
line, consumed by the sandbox-exec shim (config.SANDBOX_EXEC).

The boundary inverts, in our favour. Seatbelt could not be made deny-default
without killing the dynamic loader, so the macOS profile is allow-default with
/Users denied and the staged subtrees re-allowed, and a path the profile forgets
is permitted. Bubblewrap is the other way round: the mount namespace starts
empty and a path that is not bound does not exist. A path forgotten here is
invisible rather than open.

Consequences worth stating rather than discovering:

  Nothing under /root is visible except the sandbox itself. The answer key, the
  vault, agent/runs, the personas and the other sandboxes are not denied by a
  rule; there is no rule, because there is no such path inside the namespace.

  The ancestor metadata exception the macOS profile needs for the JVM does not
  apply. bwrap creates every ancestor of a bind target inside the namespace, so
  stat() on them succeeds by construction.

  The keychain read does not apply either. On Linux the CLI reads
  ~/.claude/.credentials.json, so the token is bound in as a single read-only
  file rather than a whole keychain directory being opened.

  Network is NOT unshared. The allow-listing proxy runs on loopback in the host
  namespace, so unsharing the network would cut the agent off from the one route
  it is allowed. Host filtering happens in the proxy, exactly as it does on
  macOS, where SBPL has no dependable remote-host predicate either.
"""
import os

from . import config

# Read-only system trees. /run matters more than it looks: on Ubuntu
# /etc/resolv.conf is a symlink into /run/systemd/resolve/, so without it DNS
# fails inside the namespace and the CLI reports "Unable to connect to API
# (ENOTIMP)", which does not read like a DNS problem.
SYSTEM_RO = ["/usr", "/etc", "/bin", "/sbin", "/lib", "/lib32", "/lib64",
             "/run", "/opt", "/usr/local"]

# The uid the agent runs as inside the namespace. Any non-zero value does the
# job; 1000 is the conventional first human user. It does not have to exist in
# /etc/passwd and no account is created on the host.
SANDBOX_UID = 1000
SANDBOX_GID = 1000


def cli_tmp_parent():
    """Present for signature parity with the macOS module.

    The CLI's per-session scratch directory lives under TMPDIR here, and TMPDIR
    points inside the sandbox, so there is no shared /tmp/claude-<uid> to carve
    an exception for. /tmp is a private tmpfs that dies with the run.
    """
    return "/tmp/claude-%d" % os.getuid()


def cli_tmp_for(sandbox_root):
    return os.path.join(cli_tmp_parent(),
                        os.path.abspath(sandbox_root).replace("/", "-"))


def ancestor_literals(sandbox_root):
    """Kept so the manifest's profile_exceptions record still resolves.

    Returns the same list the macOS module returns, but nothing is granted on
    the strength of it: bwrap materialises these directories as part of creating
    the bind target. Recorded rather than removed so the two manifests stay
    comparable.
    """
    parts = []
    p = os.path.abspath(sandbox_root)
    while True:
        parts.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return [x for x in reversed(parts) if x != "/"]


def _credentials_source():
    """The real token file, outside the sandbox.

    This is the Linux equivalent of the macOS keychain read, and it is narrower:
    one file, read-only, rather than ~/Library/Keychains entire. It is bound
    because the CLI cannot authenticate without it and a jail HOME by definition
    does not have it.
    """
    p = os.path.expanduser("~/.claude/.credentials.json")
    return p if os.path.isfile(p) else None


def jvm_dirs():
    """Where this machine's JVMs live, to hide from a sandbox that must not run Beagle here.

    Only /usr/lib/jvm. A java that resolves anywhere else is not hidden by this, and the gb10
    route's denial probe names this machine's java by its resolved path, so such a java fails
    the launch gate instead of being reachable.
    """
    return [d for d in ("/usr/lib/jvm",) if os.path.isdir(d)]


def build_profile(sandbox_root, jail_home, proxy_port, allow_keychain=True,
                  extra_read=(), extra_write=(), label="", hide=()):
    """Emit the bubblewrap argument list for one agent, one argument per line.

    Signature matches the macOS module exactly, plus `hide`: directories inside the
    read-only system trees that this sandbox gets as an empty read-only tmpfs instead.
    `allow_keychain` is honoured as "bind the credentials file", which is the thing it
    stands for here.
    """
    sandbox_root = os.path.abspath(sandbox_root)
    refdata = os.path.join(sandbox_root, "refdata")

    L = []
    A = L.append

    A("# generated by the PRS run harness (bubblewrap) -- %s" % (label or "agent"))
    A("# one bwrap argument per line; read by %s" % config.SANDBOX_EXEC)
    A("#")
    A("# The namespace starts EMPTY. Everything reachable is listed below and")
    A("# nothing else exists. That is the whole boundary.")
    A("")
    A("# --- read-only system -------------------------------------------------")
    A("# On a usrmerge system /bin, /sbin, /lib and /lib64 are symlinks into /usr.")
    A("# Binding /usr alone is not enough: the symlinks themselves do not exist in a")
    A("# fresh namespace, so an interpreter that resolves a library through /lib gets")
    A("# ENOENT while /usr/bin/python3 is plainly there. Each one is bound from its")
    A("# resolved target under its own name, which makes both paths work.")
    seen = set()
    for d in SYSTEM_RO:
        if not os.path.exists(d):
            continue
        target = os.path.realpath(d)
        if not os.path.isdir(target):
            continue
        if (d, target) in seen:
            continue
        seen.add((d, target))
        A("--ro-bind")
        A(target)
        A(d)
    for d in hide:
        if os.path.isdir(d):
            A("")
            A("# hidden: an empty tmpfs over %s, sealed read-only. Nothing under it" % d)
            A("# exists in this sandbox.")
            A("--tmpfs")
            A(d)
            A("--remount-ro")
            A(d)
    A("--proc")
    A("/proc")
    A("--dev")
    A("/dev")
    A("")
    A("# --- private temp -----------------------------------------------------")
    A("# TMPDIR points inside the sandbox. This tmpfs exists so that anything")
    A("# writing to /tmp regardless of TMPDIR gets a private one that dies with")
    A("# the run, and so no previous run's scratch can be picked up.")
    A("--tmpfs")
    A("/tmp")
    A("")
    A("# --- the sandbox ------------------------------------------------------")
    A("--bind")
    A(sandbox_root)
    A(sandbox_root)

    if jail_home and not os.path.abspath(jail_home).startswith(sandbox_root):
        A("--bind")
        A(os.path.abspath(jail_home))
        A(os.path.abspath(jail_home))

    # ------------------------------------------------------------------
    # Additional read-only inputs.
    #
    # A nested bind is not enough on its own. bwrap creates every mount point it
    # needs, and a directory it creates is writable, so binding
    # refdata/ancestry-ref/phase3 alone leaves the whole of refdata writable
    # inside the namespace. Binding the parent read-only instead is worse: it
    # exposes the parent's other children, and refdata/ancestry-ref/build is on
    # the denial list precisely because it was built through a patient's own
    # positions.
    #
    # What is correct is three operations at an anchor directory: mount an empty
    # tmpfs there, bind the wanted path inside it, then remount the anchor
    # read-only. The result is a directory that contains exactly what was asked
    # for, whose siblings do not exist rather than being denied, and which cannot
    # be written to. Order matters: the remount has to come after the bind.
    #
    # The anchor is REFDATA_SRC for anything under it, because that is the path
    # the write probe checks and the no-cache guarantee is about. Anything else
    # anchors at its own parent.
    reads = [os.path.abspath(d) for d in (extra_read or ()) if os.path.exists(d)]
    if reads:
        refsrc = os.path.abspath(config.REFDATA_SRC)
        anchors = {}
        for d in reads:
            if d == refsrc or d.startswith(refsrc + os.sep):
                anchors.setdefault(refsrc, []).append(d)
            else:
                anchors.setdefault(os.path.dirname(d), []).append(d)

        for anchor, paths in sorted(anchors.items()):
            A("")
            A("# read-only input under %s" % anchor)
            A("# empty tmpfs, then the wanted paths, then the anchor sealed read-only")
            A("--tmpfs")
            A(anchor)
            for d in sorted(paths):
                A("--ro-bind")
                A(d)
                A(d)
            A("--remount-ro")
            A(anchor)

    for d in extra_write or ():
        if os.path.exists(d):
            A("")
            A("# additional writable input")
            A("--bind")
            A(os.path.abspath(d))
            A(os.path.abspath(d))

    if allow_keychain:
        cred = _credentials_source()
        if cred and jail_home:
            A("")
            A("# The OAuth token. One file, read-only. The macOS profile has to open")
            A("# the whole login keychain here because the token is not a file there;")
            A("# this is the narrower version of the same exception.")
            A("--ro-bind")
            A(cred)
            A(os.path.join(os.path.abspath(jail_home), ".claude", ".credentials.json"))

    A("")
    A("# --- refdata is READ-ONLY ---------------------------------------------")
    A("# Emitted after the sandbox bind so it overrides it: later mounts win.")
    A("# This is what makes the no-cache guarantee hold, because nothing can be")
    A("# deposited in refdata for a later run to pick up.")
    if os.path.isdir(refdata):
        A("--ro-bind")
        A(refdata)
        A(refdata)

    A("")
    A("# --- process ----------------------------------------------------------")
    A("--chdir")
    A(sandbox_root)
    A("")
    A("# NOT root inside the namespace. Claude Code refuses to skip its permission")
    A("# prompts when running as root or under sudo, and an unattended run cannot")
    A("# answer a prompt, so the agent has to be a normal user. The earlier VM")
    A("# harness solved this by creating an unprivileged account and using")
    A("# `sudo -n -u prsagent`, which then needed the whole sandbox chowned to it.")
    A("#")
    A("# A user namespace does the same job without either. The single-entry uid")
    A("# map sends outside-uid-0 to inside-uid-%d, so the process is unprivileged" % SANDBOX_UID)
    A("# inside while every root-owned file it was given still belongs to it. The")
    A("# host is untouched: nothing is chowned and no account is created.")
    A("--unshare-user")
    A("--uid")
    A(str(SANDBOX_UID))
    A("--gid")
    A(str(SANDBOX_GID))
    A("")
    A("# --unshare-pid goes with the user namespace rather than being an extra")
    A("# hardening step: mounting a fresh procfs inside a user namespace is only")
    A("# permitted when the pid namespace is unshared too, and --proc above needs")
    A("# it. The agent becomes pid 1 in its own namespace and --die-with-parent")
    A("# reaps it.")
    A("--unshare-pid")
    A("--unshare-uts")
    A("--unshare-ipc")
    A("--die-with-parent")
    A("--new-session")
    A("")
    A("# Network is deliberately NOT unshared: the allow-listing proxy is on")
    if proxy_port:
        A("# loopback port %d in the host namespace, and unsharing would cut the" % proxy_port)
    else:
        A("# loopback in the host namespace, and unsharing would cut the")
    A("# agent off from the only egress it is permitted. Host filtering is the")
    A("# proxy's job on both platforms.")
    A("")
    return "\n".join(L) + "\n"


def write_profile(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o444)
    return path
