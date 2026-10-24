#!/usr/bin/env python3
"""Transport. Carries a message out of this session and brings the reply back.

This is the only way to reach the person or the reference provider from here. There is no
interactive stdin: nobody is watching this terminal.

It computes nothing and decides nothing. It does not know what a good question is, it does
not judge an answer, and it cannot build a reference distribution. It moves a payload and
waits.

  ask-person          ask the person one question; prints their answer
  request-reference   hand an INT-1 request to the provider; prints where the artifact landed
  await-reference     collect the reply to a request-reference sent with --no-wait

ask-person and request-reference block until a reply arrives. Building a reference can take
hours, longer than one command is allowed to run, so request-reference --no-wait prints a
request id at once and await-reference --id <it> waits up to --timeout for that reply, exiting
6 if the provider is still running. Run await-reference again until it exits with anything
else. The person can still be asked questions meanwhile.

Any of them can come back with an outcome that is not an answer -- the person may end the
session, the provider may report that no usable reference could be built, nobody may reply at
all. Those are real results and they are printed as such; they are not errors to retry around.

Usage:
  python3 harness_client.py ask-person \
      --stage select \
      --question "Have you ever been diagnosed with this?" \
      --why "Some models were built on people already diagnosed and others on a general
             population, and which one fits you changes which model I should use." \
      [--hard-blocker] [--option "Yes, go ahead" --option "No" [--other]]

  A question whose answer is one of a known few carries them as choices, each with --option,
  in the person's words; --other also takes a free-text answer beside them, where one makes
  sense. Without --other the person picks one of the choices. A question answered in the
  person's own words has none.

  python3 harness_client.py request-reference --no-wait \
      --request runs/<RUN_ID>/interpret/request.json
  python3 harness_client.py await-reference --id <the id it printed> --timeout 540

Exit codes:
  0  a reply arrived (read it on stdout); with --no-wait, the request was sent and its id
     is on stdout
  2  the person ended the session
  3  no usable reference could be built, or none was supplied
  4  nobody replied within the time allowed (the blocking forms). For request-reference the
     request id is printed; collect the reply with await-reference --id <it>
  5  the message was malformed and was not sent, no such request was sent from here, or the
     harness replied with something other than an answer -- the reply is printed. A
     request-reference refused as busy also prints the id of the one already being built
  6  await-reference only: the provider has not replied yet; run it again
"""
import argparse
import json
import os
import re
import sys
import time
import uuid

COMMS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "comms")
COMMS = os.path.abspath(COMMS)
OUTBOX = os.path.join(COMMS, "outbox")
INBOX = os.path.join(COMMS, "inbox")

POLL = 0.3


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _send(payload):
    os.makedirs(OUTBOX, exist_ok=True)
    os.makedirs(INBOX, exist_ok=True)
    mid = payload["message_id"]
    # Write to a temp name and rename into place. The harness polls this directory, and a
    # rename is atomic -- without it the harness can read a half-written message and reject
    # it as malformed.
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
            # The harness writes by rename too, so the file is complete when it appears.
            # The retry loop covers the case where it is not yet visible to this process.
            for _ in range(10):
                try:
                    with open(path) as fh:
                        return json.load(fh)
                except (json.JSONDecodeError, OSError):
                    time.sleep(0.05)
        time.sleep(POLL)
    return None


MAX_OPTIONS, MAX_OPTION_CHARS = 8, 120
# The requirement codes the specs and skills use (INT-4, SEL-12, R1...): words for the record,
# never for the person. The same pattern is in harness_client.py, demo_site/worker.py and
# lib_run.py (tests/test_question_choices.py holds them equal).
INTERNAL_CODE = re.compile(r"\b(?:INTAKE|INT|ANC|SEL|SCO|JC|REP|ARCH|DATA|FRAME)-\d+[a-z]?\b|\bR\d{1,2}\b")


def cmd_ask_person(args):
    options = [option.strip() for option in (args.option or [])]
    problem = None
    if args.other and not options:
        problem = ("--other offers free text beside choices, and there are none: without --option "
                   "the person answers in their own words anyway.")
    elif options and not 2 <= len(options) <= MAX_OPTIONS:
        problem = "offer 2 to %d choices, not %d." % (MAX_OPTIONS, len(options))
    elif any(not option for option in options):
        problem = "a choice is empty."
    elif any(len(option) > MAX_OPTION_CHARS for option in options):
        problem = "a choice is longer than %d characters: a choice is a short answer." % MAX_OPTION_CHARS
    elif len({option.casefold() for option in options}) != len(options):
        problem = "the same choice is offered twice."
    coded = sorted({code for words in [args.question, args.why or ""] + options
                    for code in INTERNAL_CODE.findall(words)})
    if not problem and coded:
        problem = ("the question names %s: an internal code, which the person never reads. Say what it "
                   "means in plain words." % ", ".join(coded))
    if problem:
        sys.stderr.write("refusing to send: %s\n" % problem)
        return 5
    if not args.why or not args.why.strip():
        sys.stderr.write(
            "refusing to send: --why is required. The person is assumed to know nothing "
            "about genetics, and a question with no stated reason is what makes people "
            "guess at an answer.\n")
        return 5
    if not args.question.strip():
        sys.stderr.write("refusing to send: --question is empty.\n")
        return 5

    mid = "q-" + uuid.uuid4().hex[:12]
    _send({
        "message_id": mid,
        "kind": "ask-person",
        "sent_utc": _now(),
        "stage": args.stage,
        "question": args.question.strip(),
        "why_it_matters": args.why.strip(),
        "hard_blocker": bool(args.hard_blocker),
        "options": options,
        "other": bool(args.other),
    })

    reply = _await(mid, args.timeout)
    if reply is None:
        sys.stderr.write("no reply within %ds.\n" % args.timeout)
        print("[no reply from the person]")
        return 4

    status = reply.get("status")
    if status == "answer":
        # skipped means the person chose "I don't know" on a question that allowed it. The
        # text is printed either way; the marker is what says the uncertainty is real rather
        # than a guess, and it has to survive into the stage's recorded judgment.
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


def _reference_reply(reply):
    """Print a request-reference reply and return its exit code."""
    status = reply.get("status")
    if status == "delivered":
        # Print only the path. The caller passes it to reference_adapter.py, which is what
        # validates the artifact against the contract -- this tool must not pre-empt that
        # judgment by summarising or checking the contents.
        print(reply["artifact_path"])
        return 0
    if status == "unusable":
        print("[no usable reference could be built] " + (reply.get("reason") or ""))
        if reply.get("artifact_path"):
            # The provider said usable:false in a well-formed artifact. That is INT-2's
            # refusal and a correct outcome, so the artifact still goes to the adapter --
            # which will record it as such rather than as a missing reference.
            print(reply["artifact_path"])
        return 3
    if status == "unavailable":
        print("[no reference distribution was supplied] " + (reply.get("reason") or ""))
        return 3
    if status in ("error", "busy"):
        # The harness did not hand the request over: a path outside the run, an ambiguous
        # input, or a request already being built -- whose id a busy reply names, so it can
        # still be collected. Not a reference outcome; say why.
        print("[the harness did not hand the request over] " + (reply.get("reason") or ""))
        if status == "busy" and reply.get("in_flight"):
            print("[request already being built] %s" % reply["in_flight"])
        return 5
    sys.stderr.write("unexpected reply status %r\n" % status)
    print(json.dumps(reply))
    return 5


def cmd_request_reference(args):
    req = os.path.abspath(args.request)
    if not os.path.exists(req):
        sys.stderr.write("no request at %s\n" % req)
        return 5
    try:
        with open(req) as fh:
            json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        sys.stderr.write("request is not readable JSON: %s\n" % exc)
        return 5

    mid = "r-" + uuid.uuid4().hex[:12]
    _send({
        "message_id": mid,
        "kind": "request-reference",
        "sent_utc": _now(),
        "request_path": req,
    })

    if args.no_wait:
        print(mid)
        return 0

    # The blocking form can be cut off by whatever runs it. The id is the only handle on the
    # request once that happens, so it is said first.
    sys.stderr.write("request id %s\n" % mid)
    reply = _await(mid, args.timeout)
    if reply is None:
        sys.stderr.write("no reply within %ds. The request is still with the harness; "
                         "collect it with: await-reference --id %s\n" % (args.timeout, mid))
        print("[no reference was delivered yet] %s" % mid)
        return 4
    return _reference_reply(reply)


def cmd_await_reference(args):
    mid = (args.id or "").strip()
    if not re.fullmatch(r"r-[0-9a-f]{12}", mid):
        sys.stderr.write("not a request id: %r. request-reference --no-wait prints one.\n"
                         % mid)
        return 5
    # The router never removes a message from the outbox, so a request sent from here is
    # always findable there. One that is not was never sent, and waiting on it would wait
    # forever.
    if not os.path.exists(os.path.join(OUTBOX, mid + ".json")):
        sys.stderr.write("no request %s was sent from here.\n" % mid)
        return 5
    reply = _await(mid, args.timeout)
    if reply is None:
        print("[still running] %s" % mid)
        return 6
    return _reference_reply(reply)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask-person", help="ask the person one question and wait")
    a.add_argument("--stage", required=True)
    a.add_argument("--question", required=True)
    a.add_argument("--why", required=True,
                   help="why this question matters, in plain language. Required.")
    a.add_argument("--hard-blocker", action="store_true",
                   help="set when you genuinely cannot proceed without an answer")
    a.add_argument("--option", action="append",
                   help="a choice, when the answer is one of a known few; repeat for each (2 to 8)")
    a.add_argument("--other", action="store_true",
                   help="also take a free-text answer beside the choices, where one makes sense")
    a.add_argument("--timeout", type=int, default=900)
    a.set_defaults(fn=cmd_ask_person)

    r = sub.add_parser("request-reference", help="hand an INT-1 request to the provider")
    r.add_argument("--request", required=True)
    r.add_argument("--timeout", type=int, default=1800)
    r.add_argument("--no-wait", action="store_true",
                   help="send, print the request id and return at once; collect the reply "
                        "with await-reference")
    r.set_defaults(fn=cmd_request_reference)

    w = sub.add_parser("await-reference",
                       help="wait up to --timeout for the reply to a --no-wait request")
    w.add_argument("--id", required=True, help="the id request-reference --no-wait printed")
    w.add_argument("--timeout", type=int, default=540,
                   help="seconds to wait in this call (default 540, inside a 600 s command "
                        "limit). Exit 6 means still running: call again.")
    w.set_defaults(fn=cmd_await_reference)

    args = ap.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
