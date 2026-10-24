"""Where the agent's credential comes from, and the refusal when there is none.

The token used to come from whatever shell launched the harness, and three times it was
not there. A shell opened before the `.bashrc` line was added carries no
CLAUDE_CODE_OAUTH_TOKEN, nothing about that shell looks wrong, and every case then dies in
seconds with `Not logged in`. Attended, that costs one run and five minutes. Unattended, it
is a whole corpus spent producing four hundred identical failures that look like results
until somebody opens one.

So the token is read from a file, which either has it or does not, and a run that cannot
find one does not start. A refusal costs a second. A batch that runs unauthenticated costs
the night, and the failures it banks are indistinguishable from findings.

Nothing in this module may import from the rest of the package: it runs before staging,
from two entry points, one of which lives outside the repo tree.
"""
import os
import stat
import sys

TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN"

# A plain KEY=value file. Read, never sourced -- sourcing a file to get one string means
# an unattended batch executes whatever else is in it.
#
# Where it is: $PRS_HARNESS_TOKEN_FILE; else claude.env in the harness's private configuration
# directory, beside the launch-gate tokens, when it exists there; else /root/.env, where the
# first Linux box kept it. A box where the harness does not run as root has no /root/.env to
# write, so the default could not stay a path only root can create.
TOKEN_FILE_ENV = "PRS_HARNESS_TOKEN_FILE"
TOKEN_FILE_PRIVATE = os.path.expanduser("~/.config/prs-harness/claude.env")
TOKEN_FILE_LEGACY = "/root/.env"


def token_file(environ=None):
    """The token file this process reads, in the order above."""
    environ = os.environ if environ is None else environ
    named = (environ.get(TOKEN_FILE_ENV) or "").strip()
    if named:
        return named
    if os.path.isfile(TOKEN_FILE_PRIVATE):
        return TOKEN_FILE_PRIVATE
    return TOKEN_FILE_LEGACY


# macOS reaches the CLI's credential through the login keychain, which is what USER and
# LOGNAME are load-bearing for in agents.py's environment, and no token file has ever existed
# there. The refusal below is for the boxes where the token IS the mechanism: a headless
# Linux VM has no keychain and no browser login path, so a missing token there is a run
# that cannot possibly authenticate. Refusing on macOS would stop every run that works
# today, which is the opposite of the point.
KEYCHAIN_PLATFORMS = ("darwin",)


# ---------------------------------------------------------------------------
# Keeping the credential out of the evidence
#
# The sandbox environment is recorded in the manifest -- once per sandbox by the launch
# gate, and once per agent invocation -- and the manifest is copied into
# results/<arm>/<pass>/<case>/, which may be committed. So a token passed through
# build_env lands in the evidence tree four times a run unless it is redacted on the way
# in. This is not hypothetical: manifests already written on the VM carry it.
#
# Redaction is BY KEY NAME, never by the shape of the value. A redactor that greps for
# `sk-ant-` works until the day the token format changes, then silently stops, and the way
# you find out is a credential in a public repository. The key name is the part the harness
# controls and the part that does not drift.
#
# The key is kept and its value replaced, rather than the key being dropped. A reader has
# to be able to tell that auth WAS configured for a run -- that is exactly the question a
# run which died with `Not logged in` raises, and a missing key cannot answer it.
# ---------------------------------------------------------------------------

SECRET_ENV_KEYS = frozenset({
    TOKEN_VAR,
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
})

# Belt as well as braces. build_env is an allow-list, so the set above is complete by
# construction today -- and it will not stay complete by itself, because the next
# credential added to that dict is one nobody thinks to add here as well. So any variable
# whose NAME contains one of these words is treated as a secret: caught by default rather
# than leaking by default. Anywhere in the name, not just at the end -- TOKEN_FOR_X is as
# much a credential as X_TOKEN.
SECRET_ENV_WORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")

REDACTED = "<redacted: set, %d chars>"

# Redaction happens twice on purpose -- once at the call sites that hand an environment to
# something other than the manifest, and once again where the manifest is written -- so it
# has to be idempotent. Without this the second pass redacts the first pass's marker and
# reports ITS length, so a manifest would state a length that is not the credential's. A
# wrong number in the evidence is worse than no number.
REDACTED_PREFIX = "<redacted:"


def _already_redacted(value):
    return isinstance(value, str) and value.startswith(REDACTED_PREFIX)


def is_secret_env_key(key):
    """Does this ENVIRONMENT VARIABLE's value need to be kept out of the record?

    Deliberately greedy, because it is applied to environment dicts, where every key is a
    variable name and a false positive costs one unreadable line in a manifest. Matching is
    case-insensitive so a lower-case variable -- `https_proxy` is already one -- cannot slip
    past on spelling.
    """
    upper = str(key).upper()
    return upper in SECRET_ENV_KEYS or any(w in upper for w in SECRET_ENV_WORDS)


def _is_env_var_name(key):
    """Is this key an environment variable name, or a field that merely shares a word?

    The distinction matters because this codebase uses `token` for something else
    entirely, and uses it far more often: the launch gate records every answer-key token it
    grepped for, as {"path": ..., "token": ...}, and a real manifest carries 113 of
    them. Redacting those would destroy the report the gate exists to produce -- it would
    take the harness's central piece of evidence and replace it with a row of markers.

    Environment variables are upper case by convention and these field names are lower
    snake case. Checked against a real 316 KB manifest: every upper-case key in it is an
    environment variable, and every key containing one of the secret words is lower case.
    So this separates them cleanly on real data rather than in principle.
    """
    k = str(key)
    return k.isupper()


def redact_tree(value):
    """redact_env, applied to any nested structure on its way into a written record.

    redact_env covers the environment dicts we know about; this covers the ones nobody has
    written yet. A manifest is assembled from a dozen call sites and a rule that depends on
    each of them remembering is a rule that holds until the next one is added -- which is
    exactly how a credential reached /agents/*/env in the first place. Applied where the
    manifest is serialised, so an environment cannot reach the file however it got in.

    Stricter than is_secret_env_key on its own: the key has to look like an environment
    variable as well as name a secret. See _is_env_var_name for what that protects.
    """
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if (_is_env_var_name(key) and is_secret_env_key(key)
                    and not isinstance(item, (dict, list, tuple)) and item
                    and not _already_redacted(item)):
                out[key] = REDACTED % len(str(item))
            else:
                out[key] = redact_tree(item)
        return out
    if isinstance(value, (list, tuple)):
        return [redact_tree(v) for v in value]
    return value


def redact_env(env):
    """A copy of an environment that is safe to write into a manifest, a log or a repo.

    Every credential value is replaced by a marker giving its length, so the record still
    answers the two questions a run that died unauthenticated actually raises -- was a
    token configured, and was it non-empty -- while carrying none of it.

    Returns a NEW dict and does not touch the one passed in. The caller keeps the real
    environment: handing this copy to a subprocess would produce precisely the `Not logged
    in` failure the token exists to prevent.
    """
    out = {}
    for key, value in env.items():
        if is_secret_env_key(key) and value and not _already_redacted(value):
            out[key] = REDACTED % len(str(value))
        else:
            out[key] = value
    return out


class MissingToken(Exception):
    """No credential, and no way to get one. The message is meant to be printed and acted on."""


def parse_env_file(text):
    """KEY=value lines, and nothing clever.

    No interpolation, no multi-line values, no continuations -- this reads one token out of
    a small file, and every feature beyond that is a way for the file to mean something
    other than it looks like it means.

    `export KEY=value` is accepted and quotes are stripped, because the line people already
    have is the one from SETUP.md's `.bashrc` instruction and it will be pasted verbatim.
    """
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _readable_by_others(path):
    """Is this credential file readable by anyone but its owner?"""
    mode = stat.S_IMODE(os.stat(path).st_mode)
    return bool(mode & (stat.S_IRGRP | stat.S_IROTH))


def load_token(env_file=None, environ=None, platform=None):
    """Put the OAuth token in the environment, or raise MissingToken saying why it cannot.

    Order, and each step is there for a reason:

      1. An environment that already carries the token wins, and the file is not read at
         all. Whatever works today keeps working, including a shell that did pick up its
         `.bashrc` and any caller that sets the variable deliberately.
      2. The file. Present and carrying the key means the value is put into the
         environment, where every child process this harness spawns will inherit it.
      3. macOS, where the credential is in the login keychain and no token is needed.
      4. Nothing. This is the refusal, and it is a refusal rather than a warning because a
         warning at the top of an unattended batch is read by nobody until morning.

    Returns (source, line) -- a short source name, and one line fit to print. Neither
    contains the token: this is called by two scripts that log their own output to files.
    """
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    env_file = token_file(environ) if env_file is None else env_file

    existing = (environ.get(TOKEN_VAR) or "").strip()
    if existing:
        return ("environment",
                "%s was already set; %s was not read" % (TOKEN_VAR, env_file))

    if os.path.isfile(env_file):
        if _readable_by_others(env_file):
            raise MissingToken(
                "%s holds a credential and is readable by more than its owner.\n"
                "  Fix it with:  chmod 600 %s\n"
                "Refusing to start rather than reading a secret out of a file anyone on "
                "this box can read." % (env_file, env_file))
        try:
            with open(env_file) as fh:
                text = fh.read()
        except OSError as exc:
            raise MissingToken(
                "%s exists but could not be read: %s\n"
                "That file is where the OAuth token comes from, so there is no credential "
                "to start with." % (env_file, exc))
        value = (parse_env_file(text).get(TOKEN_VAR) or "").strip()
        if value:
            environ[TOKEN_VAR] = value
            return ("file", "%s read from %s" % (TOKEN_VAR, env_file))
        raise MissingToken(
            "%s exists but carries no %s.\n"
            "  Add the line:  %s=<token>\n"
            "  Get a token with `claude setup-token` on a machine with a browser.\n"
            "Refusing to start: every case would die in seconds with `Not logged in`, and "
            "an unattended batch would spend itself producing identical failures."
            % (env_file, TOKEN_VAR, TOKEN_VAR))

    if platform in KEYCHAIN_PLATFORMS:
        return ("keychain",
                "no %s and no %s; on %s the CLI authenticates through the login keychain"
                % (TOKEN_VAR, env_file, platform))

    raise MissingToken(
        "no credential: %s is not set and %s does not exist.\n"
        "  Create it with:  printf '%s=%%s\\n' \"<token>\" > %s && chmod 600 %s\n"
        "  Get a token with `claude setup-token` on a machine with a browser.\n"
        "Refusing to start: there is no keychain on this platform (%s) and no browser "
        "login path, so every case would die in seconds with `Not logged in`."
        % (TOKEN_VAR, env_file, TOKEN_VAR, env_file, env_file, platform))


def require_token(env_file=None, environ=None, platform=None):
    """load_token, but exit the process on failure instead of raising.

    For the two entry points, which want the message and a non-zero exit rather than a
    traceback -- a traceback at the top of an overnight log reads as a bug in the harness
    rather than as a box that was never set up. Takes load_token's arguments unchanged so
    that the refusal can be exercised on a machine that would not otherwise produce it.
    """
    try:
        return load_token(env_file=env_file, environ=environ, platform=platform)
    except MissingToken as exc:
        raise SystemExit("\nCANNOT START -- %s\n" % exc)
