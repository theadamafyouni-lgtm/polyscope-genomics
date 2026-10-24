"""The demo's failure log: one tab-separated line per failure, appended under a lock.

    run<TAB>case<TAB>what failed<TAB>why<TAB>when (UTC ISO)

The worker writes one for a demo run that ended without finishing, the watchdog one for what
it could not keep running. Nothing in a line comes from the person: case is `demo` for a demo
run, which has no case id, `-` stands for a field with nothing in it, and tabs and newlines
inside a value become spaces, so every line has five fields.

    python3 -m demo_site.failures --log PATH RUN CASE WHAT WHY
"""
import argparse
import fcntl
import os
import time


def _field(value):
    text = " ".join(str("" if value is None else value).split())
    return text or "-"


def append(path, run, case, what, why, when=None):
    when = when or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = "\t".join(_field(v) for v in (run, case, what, why, when)) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return line


def main(argv=None):
    parser = argparse.ArgumentParser(description="Append one line to the demo's failure log")
    parser.add_argument("--log", required=True)
    for name in ("run", "case", "what", "why"):
        parser.add_argument(name)
    args = parser.parse_args(argv)
    append(args.log, args.run, args.case, args.what, args.why)


if __name__ == "__main__":
    main()
