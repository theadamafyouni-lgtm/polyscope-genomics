import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from unittest.mock import patch

from demo_site import auth as auth_module
from demo_site.auth import PasswordService, daily_password, day_window, load_secret
from demo_site.sessions import Sessions


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize("before,after", [
    ("2026-10-13T03:59:59+00:00", "2026-10-13T04:00:00+00:00"),
    ("2026-01-14T04:59:59+00:00", "2026-01-14T05:00:00+00:00"),
    ("2026-03-09T03:59:59+00:00", "2026-03-09T04:00:00+00:00"),
    ("2026-11-02T04:59:59+00:00", "2026-11-02T05:00:00+00:00"),
])
def test_password_changes_at_new_york_midnight(before, after):
    secret = bytes(range(32))
    a, expiry = daily_password(secret, stamp(before))
    b, _ = daily_password(secret, stamp(after))
    assert a != b
    assert expiry == stamp(after)
    assert a == daily_password(secret, stamp(before) - 1)[0]
    assert "0" not in a and "1" not in a and "I" not in a and "O" not in a


def test_dst_day_lengths():
    for date, hours in [("2026-03-08", 23), ("2026-11-01", 25)]:
        now = datetime.fromisoformat(date).replace(tzinfo=ZoneInfo("America/New_York")).timestamp()
        assert day_window(now)[1] - now == hours * 3600


@pytest.fixture
def auth(tmp_path):
    path = tmp_path / "secret"
    path.write_bytes(bytes(range(32)))
    path.chmod(0o600)
    clock = [stamp("2026-10-12T20:00:00+00:00")]
    service = PasswordService(path, tmp_path / "attempts.sqlite", clock=lambda: clock[0])
    return service, clock, path


def test_root_secret_permissions(auth):
    service, _, path = auth
    assert len(load_secret(path)) == 32
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_secret(path)
    link = path.parent / "link"
    link.symlink_to(path)
    with pytest.raises(OSError):
        load_secret(link)
    path.chmod(0o600)
    # Owned by the account that reads it: root's on the VM, the one account's without root.
    with patch.object(os, "geteuid", return_value=os.geteuid() + 1):
        with pytest.raises(ValueError):
            load_secret(path)


def test_a_wrong_password_is_told_apart_from_being_throttled(auth):
    # A checked-and-wrong password says so; a request refused before the password is even looked
    # at (too many tries) says to wait. The web turns the first into "that password is not
    # correct" and the second into "too many attempts".
    service, clock, _ = auth
    correct = daily_password(service.secret, clock[0])[0]
    assert service.login("wrong", "a") == {"ok": False, "retry_after": 1, "reason": "incorrect"}
    # Now "a" is in cooldown: the next try is refused without checking the password, right or not.
    assert service.login(correct, "a") == {"ok": False, "retry_after": 1, "reason": "throttled"}
    # A fresh client, throttled only by the global limit, is also "throttled", never "incorrect".
    for i in range(30):
        service.login(correct, "flood-%d" % i)
    refused = service.login(correct, "fresh")
    assert refused["ok"] is False and refused["reason"] == "throttled"
    # A correct password never carries a reason.
    clock[0] += 3600
    assert "reason" not in service.login(daily_password(service.secret, clock[0])[0], "b")


def test_cooldowns_persist_and_expire(auth):
    service, clock, path = auth
    assert service.login("wrong", "a") == {"ok": False, "retry_after": 1, "reason": "incorrect"}
    fresh = PasswordService(path, service.database, clock=lambda: clock[0])
    correct = daily_password(service.secret, clock[0])[0]
    assert not fresh.login(correct, "a")["ok"]
    clock[0] += 1
    assert fresh.login("wrong", "a")["retry_after"] == 2
    clock[0] += 2
    assert fresh.login("wrong", "a")["retry_after"] == 4
    clock[0] += 3601
    assert fresh.login(daily_password(service.secret, clock[0])[0], "a")["ok"]


def test_per_client_and_global_limits(auth):
    service, clock, _ = auth
    correct = daily_password(service.secret, clock[0])[0]
    for _ in range(5):
        assert service.login(correct, "one")["ok"]
    assert not service.login(correct, "one")["ok"]
    for i in range(25):
        assert service.login(correct, str(i))["ok"]
    assert not service.login(correct, "another")["ok"]
    clock[0] += 60
    assert service.login(correct, "another")["ok"]


def test_sign_ins_last_24_hours_and_a_prior_days_password_is_refused(auth):
    service, clock, _ = auth
    old = daily_password(service.secret, clock[0])[0]
    assert service.login(old, "a")["expires_at"] == clock[0] + 24 * 3600
    # 30 s before midnight in New York: no longer cut short at midnight.
    clock[0] = stamp("2026-10-13T03:59:30+00:00")
    assert service.login(old, "a")["expires_at"] == clock[0] + 24 * 3600
    clock[0] += 30
    assert not service.login(old, "a")["ok"]


# A test value. The real one is the owner's, in state/auth/fixed-password, and is never printed.
FIXED = "correct horse battery staple"


def write_fixed(secret, value=FIXED):
    fixed = secret.parent / "fixed-password"
    fixed.write_text(value + "\n")
    fixed.chmod(0o600)
    return fixed


def test_one_fixed_password_once_its_file_is_written(auth):
    service, clock, secret = auth
    daily = lambda: daily_password(service.secret, clock[0])[0]
    assert service.login(daily(), "a")["ok"]
    assert not service.login(FIXED, "b")["ok"]               # no file: the daily password, never open
    write_fixed(secret)
    service.notice()                                         # the worker looks every 15 s
    assert service.login(FIXED, "c") == {"ok": True, "retry_after": 0, "expires_at": clock[0] + 24 * 3600}
    # On the day it goes live, that day's daily password still works, until midnight in New York.
    assert service.login(daily(), "d")["ok"]
    clock[0] = stamp("2026-10-13T04:00:00+00:00")            # midnight in New York
    assert not service.login(daily(), "e")["ok"]
    assert service.login(FIXED, "f")["ok"]
    # Exactly as it is set, not the daily password's forgiving form.
    for near in (FIXED.upper(), FIXED.replace(" ", ""), FIXED + "x", " " + FIXED):
        clock[0] += 61
        assert not service.login(near, "g")["ok"]
    # The sign-in limits are the same for it.
    clock[0] += 3601
    for _ in range(5):
        assert service.login(FIXED, "h")["ok"]
    assert not service.login(FIXED, "h")["ok"]


def test_the_go_live_day_is_the_day_it_is_first_seen_and_an_edit_does_not_reopen_the_daily_password(auth):
    service, clock, secret = auth
    fixed = write_fixed(secret)
    os.utime(fixed, (stamp("2026-10-09T12:00:00+00:00"),) * 2)   # written days before the deploy
    service.notice()                                          # first seen today: today's works today
    assert service.login(daily_password(service.secret, clock[0])[0], "a")["ok"]
    assert (secret.parent / "fixed-password-since").read_text().strip() == "2026-10-12"
    clock[0] = stamp("2026-10-13T16:00:00+00:00")             # the next day, an edit
    write_fixed(secret, FIXED + " 2")
    service.notice()
    assert not service.login(daily_password(service.secret, clock[0])[0], "b")["ok"]
    assert service.login(FIXED + " 2", "c")["ok"]
    # Removed: the daily password again; written again later, that is a new day it goes live.
    fixed.unlink()
    service.notice()
    assert service.login(daily_password(service.secret, clock[0])[0], "d")["ok"]
    assert not (secret.parent / "fixed-password-since").exists()
    clock[0] = stamp("2026-10-15T16:00:00+00:00")
    write_fixed(secret)
    assert service.login(daily_password(service.secret, clock[0])[0], "e")["ok"]   # seen at the sign-in
    assert service.login(FIXED, "f")["ok"]


def test_an_empty_file_or_one_not_plainly_this_accounts_keeps_the_daily_password(auth):
    service, clock, secret = auth
    fixed = secret.parent / "fixed-password"
    for make, undo in ((lambda: fixed.write_text(" \n"), fixed.unlink), (fixed.mkdir, fixed.rmdir),
                       (lambda: fixed.symlink_to(secret.parent / "elsewhere"), fixed.unlink),
                       (lambda: fixed.write_text("x" * 129), fixed.unlink)):
        make()
        service.notice()
        clock[0] += 61
        assert service.login(daily_password(service.secret, clock[0])[0], "a")["ok"]
        undo()
    (secret.parent / "elsewhere").write_text(FIXED)
    write_fixed(secret)
    with patch.object(os, "geteuid", return_value=os.geteuid() + 1):
        service.notice()
        clock[0] += 61
        assert not service.login(FIXED, "b")["ok"]


def test_the_fixed_password_is_never_printed_or_logged(auth, capsys, monkeypatch):
    service, clock, secret = auth
    write_fixed(secret)
    service.notice()
    service.login(FIXED, "a")
    service.login("wrong", "b")
    monkeypatch.setattr("sys.argv", ["auth", "status", "--secret-file", str(secret)])
    auth_module.main()
    said = capsys.readouterr()
    assert FIXED not in said.out + said.err
    assert "fixed password in use since 2026-10-12" in said.out


def test_sessions_expire_rotate_and_survive_restart(tmp_path):
    now = [100]
    path = tmp_path / "sessions.sqlite"
    sessions = Sessions(path, clock=lambda: now[0])
    token, record = sessions.create(expires=110, authenticated=True)
    restored = Sessions(path, clock=lambda: now[0])
    assert restored.get(token)["owner"] == record["owner"]
    now[0] = 110
    assert restored.get(token) is None
    assert restored.get(token, expired=True)["owner"] == record["owner"]
    new_token, new_record = restored.create(owner=record["owner"], previous=token)
    assert new_token != token and new_record["csrf"] != record["csrf"]
    assert restored.get(token, expired=True) is None
    assert not new_record["authenticated"]


def test_public_session_growth_is_bounded_without_evicting_authenticated_users(tmp_path):
    sessions = Sessions(tmp_path / "sessions.sqlite")
    token, _ = sessions.create(authenticated=True)
    with sessions.connect() as db:
        db.executemany("INSERT INTO sessions VALUES (?, 'csrf', 'owner', ?, 0, ?)",
                       [(f"anonymous-{i}", 9999999999, i) for i in range(2100)])
    sessions.create()
    with sessions.connect() as db:
        assert db.execute("SELECT count(*) FROM sessions WHERE authenticated=0").fetchone()[0] == 2000
    assert sessions.get(token)["authenticated"]


def test_the_worker_looks_for_a_fixed_password_every_round_whatever_else_fails():
    from demo_site import worker
    calls = []

    class Runs:
        def sweep(self):
            calls.append("sweep")
            raise OSError("a purge failed")

    class Auth:
        def notice(self):
            calls.append("notice")
    worker._tend(Runs(), Auth())
    assert calls == ["sweep", "notice"]
