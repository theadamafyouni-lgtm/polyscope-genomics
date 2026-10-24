"""Opaque server-side sessions; CSRF and run ownership survive web restarts."""
import hashlib
import os
import secrets
import sqlite3
import time


class Sessions:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = str(path), clock
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, csrf TEXT, owner TEXT, expires REAL, authenticated INTEGER, created REAL)")
        os.chmod(self.path, 0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def get(self, token, expired=False):
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE token=?", (self.digest(token),)).fetchone()
        if row and (expired or row["expires"] > self.clock()):
            return dict(row)

    def create(self, expires=None, owner=None, authenticated=False, previous=None):
        now = self.clock()
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        owner = owner or secrets.token_hex(32)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # Expired records permit the same browser to recover its run after re-login,
            # but never grant authenticated access. Drop them after one day.
            db.execute("DELETE FROM sessions WHERE expires < ?", (now - 86400,))
            if previous:
                db.execute("DELETE FROM sessions WHERE token=?", (self.digest(previous),))
            # Anonymous requests cannot grow the server's database without bound.
            # Active authenticated sessions are never evicted by public visitors.
            db.execute("DELETE FROM sessions WHERE token IN (SELECT token FROM sessions WHERE authenticated=0 ORDER BY created DESC LIMIT -1 OFFSET 1999)")
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                       (self.digest(token), csrf, owner, expires or now + 3600, int(authenticated), now))
        return token, self.get(token)
