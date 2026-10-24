#!/usr/bin/env python3
"""Sends a message and waits for the reply. There is no interactive stdin.

  ask-person   send one question; prints the reply on stdout

Exit codes:
  0  a reply arrived
  2  the person ended the session
  4  nobody replied in time
  5  the message was malformed and was not sent
"""
# ---------------------------------------------------------------------------
# This is the MINIMAL transport, staged by `run --transport minimal`. It exists
# because the full one is not neutral.
#
# harness_client.py's docstring and argparse help carry, between them: a worked
# --why example naming the diagnosed-versus-general-cohort criterion that SEL-3
# is about; the stage name `select`; the INT-1 reference provider and its whole
# request-reference subcommand; the hard-blocker concept; the assertion that the
# person knows nothing about genetics; and the statement that a refusal is a
# correct outcome. Every one of those is specification content, and it was being
# staged into a sandbox described as having no specification.
#
# So this file says how to send a message and nothing else. What is deliberately
# absent, and must stay absent:
#
#   - request-reference. Naming a reference provider tells the agent that a
#     population distribution is a thing that exists and can be asked for. Two
#     control runs made INT-1 requests with nothing else to have told them.
#   - --why as a required argument. Requiring a reason is a rule about how to
#     ask, and the refusal message explaining it is FRAME-1 restated.
#
# One thing that could not simply be dropped. The router refuses a message whose
# why_it_matters is empty and replies with a string naming FRAME-1, which the
# client would print into the sandbox -- the rule arriving through the error
# channel of the arm built to withhold it. So an unsupplied --why is sent as a
# fixed placeholder that states nothing. The router is satisfied, the agent is
# taught nothing, and the person sees a note that says the reason was not given.
#   - --hard-blocker. The distinction between a question you can proceed past
#     and one you cannot is a judgment the specification teaches.
#   - the worked example, the stage vocabulary, and any sentence about what a
#     good question is or what an answer means.
#
# What could not be removed: the fact that a transport exists at all. The agent
# has no terminal, so if it is not told there is a way to reach the person it
# cannot reach them, and the run measures nothing. That is the floor, and it is
# stated in the paper rather than left for a reader to find.
#
# The wire format, the atomic rename, the polling and the exit codes are
# identical to the full transport, so the harness cannot tell the two apart and
# no other part of the system changes.
# ---------------------------------------------------------------------------
import argparse
import json
import os
import sys
import time
import uuid

COMMS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "comms")
COMMS = os.path.abspath(COMMS)
OUTBOX = os.path.join(COMMS, "outbox")
INBOX = os.path.join(COMMS, "inbox")

POLL = 0.3

# Sent when the caller supplies no --why. The router rejects an empty one with a
# message naming FRAME-1, and that rejection is printed into the sandbox; this is
# what keeps the rule out of the arm without changing the router for every run.
WHY_UNSTATED = "(no reason given)"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _send(payload):
    os.makedirs(OUTBOX, exist_ok=True)
    os.makedirs(INBOX, exist_ok=True)
    mid = payload["message_id"]
    # Write to a temp name and rename into place. The harness polls this
    # directory, and a rename is atomic -- without it the harness can read a
    # half-written message and reject it as malformed.
    tmp = os.path.join(OUTBOX, "." + mid + ".tmp")
    final = os.path.join(OUTBOX, mid + ".json")
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, final)
    return final


def _await(message_id, timeout):
    path = os.path.join(INBOX, message_id + ".json")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            for _ in range(10):
                try:
                    with open(path) as fh:
                        return json.load(fh)
                except (json.JSONDecodeError, OSError):
                    time.sleep(0.05)
        time.sleep(POLL)
    return None


def cmd_ask_person(args):
    if not args.question.strip():
        sys.stderr.write("refusing to send: --question is empty.\n")
        return 5

    mid = "q-" + uuid.uuid4().hex[:12]
    # The payload keys are the wire format and are not the agent's to choose, so
    # the ones it does not supply are sent empty rather than omitted. `stage` is
    # a free-text label the router echoes back; it is required by the schema and
    # defaults to a word that names nothing.
    _send({
        "message_id": mid,
        "kind": "ask-person",
        "sent_utc": _now(),
        "stage": args.stage,
        "question": args.question.strip(),
        "why_it_matters": (args.why or "").strip() or WHY_UNSTATED,
        "hard_blocker": False,
        "options": list(args.option or []),
    })

    reply = _await(mid, args.timeout)
    if reply is None:
        sys.stderr.write("no reply within %ds.\n" % args.timeout)
        print("[no reply from the person]")
        return 4

    status = reply.get("status")
    if status == "answer":
        if reply.get("skipped"):
            print("[the person did not know] " + (reply.get("text") or "").strip())
        else:
            print((reply.get("text") or "").strip())
        return 0
    if status == "session_end":
        print("[the person ended the session] " + (reply.get("note") or "").strip())
        return 2
    sys.stderr.write("unexpected reply status %r\n" % status)
    print(json.dumps(reply))
    return 5


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask-person", help="send one question and wait")
    a.add_argument("--question", required=True)
    a.add_argument("--stage", default="q", help="a label on the message")
    a.add_argument("--why", default="", help="an optional note sent with the message")
    a.add_argument("--option", action="append",
                   help="an offered answer; may be repeated")
    a.add_argument("--timeout", type=int, default=900)
    a.set_defaults(fn=cmd_ask_person)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
