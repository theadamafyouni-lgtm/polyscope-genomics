"""The demo's password, and durable, bounded login throttling.

One fixed password, set by the demo's owner in fixed-password beside the master secret and read
at every sign-in, so writing the file is all it takes. Until there is one (the file missing or
empty, or not a plain file of this account's own) the daily password, derived from the master
secret and the date in New York, is the password: the site is never open. On the day a fixed
password goes live, the day the service first sees it, that day's daily password also works,
until midnight in New York, so nobody who already has it is locked out. A sign-in lasts 24 hours.
Neither password is ever printed by the service or logged.

The master secret belongs to the worker's account: root on the VM, where the web runs as
another user and cannot read it; the one account on a box without root."""
import argparse
import hashlib
import hmac
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_SECRET = "/var/lib/prs-demo/auth/master.key"
# Beside the master secret: the owner's fixed password, and the day it went live.
FIXED_NAME, SINCE_NAME = "fixed-password", "fixed-password-since"
# The web takes a password of at most 128 characters.
MAX_PASSWORD = 128
SIGN_IN_SECONDS = 24 * 3600


def day_window(now=None):
    local = datetime.fromtimestamp(time.time() if now is None else now, NEW_YORK)
    tomorrow = local.date() + timedelta(days=1)
    midnight = datetime.combine(tomorrow, datetime.min.time(), NEW_YORK)
    return local.date().isoformat(), midnight.timestamp()


def daily_password(secret, now=None):
    day, expiry = day_window(now)
    digest = hmac.new(secret, day.encode("ascii"), hashlib.sha256).digest()
    # 96 bits, no visually ambiguous I/L/O/0/1. HMAC is computed over the NY date.
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    number = int.from_bytes(digest[:12], "big")
    letters = ""
    for _ in range(20):
        number, index = divmod(number, len(alphabet))
        letters += alphabet[index]
    return "-".join(letters[i:i + 5] for i in range(0, len(letters), 5)), expiry


def load_secret(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("master secret must be a regular file with mode 0600 owned by the "
                             "account that reads it")
        secret = os.read(fd, 33)
        if len(secret) != 32:
            raise ValueError("master secret must contain exactly 32 bytes")
        return secret
    finally:
        os.close(fd)


def read_fixed(directory):
    """The fixed password, its first line, and None with why when there is none to use: no file,
    an empty one, one that is not a plain file of this account's own, or one too long to type."""
    try:
        fd = os.open(Path(directory) / FIXED_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "no fixed password"
    except OSError:
        return None, "the fixed-password file is not a plain file"
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return None, "the fixed-password file is not a plain file of this account's own"
        raw = os.read(fd, 4 * MAX_PASSWORD + 8)
    finally:
        os.close(fd)
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None, "the fixed-password file is not UTF-8 text"
    value = lines[0] if lines else ""
    if not value.strip():
        return None, "the fixed-password file is empty"
    if len(value) > MAX_PASSWORD:
        return None, "the fixed password is longer than %d characters" % MAX_PASSWORD
    return value, None


def read_since(directory):
    """The day in New York a fixed password went live, as YYYY-MM-DD, or None."""
    try:
        value = (Path(directory) / SINCE_NAME).read_text().strip()
    except OSError:
        return None
    return value if len(value) == 10 else None


class PasswordService:
    def __init__(self, secret_path, database_path, clock=time.time):
        self.secret = load_secret(secret_path)
        self.directory = Path(secret_path).parent
        self.database = str(database_path)
        self.clock = clock
        self.said = None
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS attempts (client TEXT, at REAL)")
            db.execute("CREATE INDEX IF NOT EXISTS attempt_time ON attempts(at)")
            db.execute("CREATE TABLE IF NOT EXISTS failures (client TEXT PRIMARY KEY, count INTEGER, until REAL, at REAL)")
        os.chmod(self.database, 0o600)
        self.notice()

    def notice(self, now=None):
        """The password in use: (the fixed password, the day it went live), or (None, None) for
        the daily one. Records the day a fixed password is first seen, and forgets it once the file
        is gone, so a later one goes live on its own day. The worker calls this every 15 s, and
        every sign-in does. What changed is said on stderr; a password never is."""
        now = self.clock() if now is None else now
        fixed, why = read_fixed(self.directory)
        since = read_since(self.directory)
        if fixed is None and since is not None:
            (self.directory / SINCE_NAME).unlink(missing_ok=True)
            since = None
        elif fixed is not None and since is None:
            since = day_window(now)[0]
            temporary = self.directory / (SINCE_NAME + ".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(since + "\n")
            os.replace(temporary, self.directory / SINCE_NAME)
        said = ("fixed password in use since %s; that day's daily password works until midnight in "
                "New York" % since if fixed is not None else "%s: the daily password is in use" % why)
        if said != self.said:
            print("demo password: " + said, file=sys.stderr, flush=True)
            self.said = said
        return fixed, since

    def connect(self):
        return sqlite3.connect(self.database, timeout=10)

    def login(self, password, client):
        now = self.clock()
        # Store a keyed digest, not IP addresses or passwords.
        client = hmac.new(self.secret, str(client).encode(), hashlib.sha256).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE at <= ?", (now - 60,))
            db.execute("DELETE FROM failures WHERE at <= ?", (now - 3600,))
            row = db.execute("SELECT count, until FROM failures WHERE client=?", (client,)).fetchone()
            global_attempts = db.execute("SELECT at FROM attempts ORDER BY at").fetchall()
            client_attempts = db.execute("SELECT at FROM attempts WHERE client=? ORDER BY at", (client,)).fetchall()
            until = row[1] if row else 0
            if len(global_attempts) >= 30:
                until = max(until, global_attempts[-30][0] + 60)
            if len(client_attempts) >= 5:
                until = max(until, client_attempts[-5][0] + 60)
            if until > now:
                # Refused before the password is looked at: too many tries, right or wrong.
                return {"ok": False, "retry_after": max(1, math.ceil(until - now)), "reason": "throttled"}
            db.execute("INSERT INTO attempts VALUES (?, ?)", (client, now))
            fixed, since = self.notice(now)
            expected, _ = daily_password(self.secret, now)
            normalize = lambda s: str(s).replace("-", "").replace(" ", "").upper()
            supplied = normalize(password)
            # The daily password: until there is a fixed one, and on the day one goes live.
            daily = ((fixed is None or since == day_window(now)[0]) and len(supplied) < MAX_PASSWORD
                     and hmac.compare_digest(supplied.encode(), normalize(expected).encode()))
            # The fixed one exactly as it is set.
            exact = (fixed is not None and len(str(password)) <= MAX_PASSWORD
                     and hmac.compare_digest(str(password).encode("utf-8"), fixed.encode("utf-8")))
            if daily or exact:
                db.execute("DELETE FROM failures WHERE client=?", (client,))
                return {"ok": True, "retry_after": 0, "expires_at": now + SIGN_IN_SECONDS}
            count = min(10, (row[0] if row else 0) + 1)
            delay = min(300, 2 ** (count - 1))
            db.execute("INSERT OR REPLACE INTO failures VALUES (?, ?, ?, ?)", (client, count, now + delay, now))
            # The password was looked at and is wrong.
            return {"ok": False, "retry_after": delay, "reason": "incorrect"}


def main():
    parser = argparse.ArgumentParser(description="Manage the private demo's password: init makes the "
                                     "master secret, password prints today's daily password, status says "
                                     "whether a fixed password is in use and since when, never what it is")
    parser.add_argument("command", choices=("init", "password", "status"))
    parser.add_argument("--secret-file", default=DEFAULT_SECRET)
    args = parser.parse_args()
    # As the worker's account: root on the VM, so the web service never has the secret.
    path = Path(args.secret_file)
    if args.command == "init":
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(secrets.token_bytes(32))
            out.flush()
            os.fsync(out.fileno())
        print("Master secret created. Its contents are never printed.")
    elif args.command == "status":
        fixed, why = read_fixed(path.parent)
        since = read_since(path.parent)
        if fixed is None:
            print("%s: the daily password is in use" % why)
        elif since is None:
            print("fixed password set, not yet seen by the worker (it looks every 15 s)")
        else:
            print("fixed password in use since %s; that day's daily password works until midnight in "
                  "New York" % since)
    else:
        password, expiry = daily_password(load_secret(path))
        print(password)
        print("Expires:", datetime.fromtimestamp(expiry, NEW_YORK).isoformat(), "(America/New_York)")


if __name__ == "__main__":
    main()
