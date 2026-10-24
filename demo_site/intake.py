"""What the person answers on the upload form, before their run starts: the questions the agent
asked at the start of every run, so that nobody waits for a first question. The page sends them
with the file, in one request header (base64 of a JSON object: never in the URL, which the front
door logs); the web reads them and refuses an upload without them, and the worker checks them
again and keeps them in the run's intake.json for its agent. They go with the run's working
files when it ends.

Only the condition and the agreement are required. Every other answer may be left out, and left
out it is absent: the person's answer, not a gap."""
import base64
import binascii
import json

HEADER = "X-PRS-Intake"
# The person's own words, each at most this long. The longest answers still fit one request
# header line at the front door, which takes 8 KiB.
WORDS = {"condition": 200, "reason": 600, "ancestry": 300}
CHOICES = {"sex": ("female", "male"), "diagnosed": ("no", "yes", "not-sure")}
FLAGS = ("doctor_note",)
MAX_HEADER = 7000

INVALID = "Fill in the form again before starting."
NO_CONDITION = "Say which condition or trait you would like looked at."
NO_CONSENT = "Tick the box to agree before starting."
LABELS = {"condition": "the condition", "reason": "what made you look into it", "ancestry": "your ancestry"}


class IntakeError(ValueError):
    """An answer the form cannot take, said as the person should read it."""


def _words(text):
    # Control characters other than a line break or a tab are dropped; the rest is theirs.
    return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32 and ord(ch) != 127).strip()


def check(value):
    """The answers as the agent gets them: each in the person's own words, trimmed, with what
    was left blank or unticked left out. IntakeError says what to fix."""
    if not isinstance(value, dict) or set(value) - set(WORDS) - set(CHOICES) - set(FLAGS) - {"consent"}:
        raise IntakeError(INVALID)
    if value.get("consent") is not True:
        raise IntakeError(NO_CONSENT)
    answers = {}
    for field, limit in WORDS.items():
        text = value.get(field)
        if text is None:
            continue
        if not isinstance(text, str):
            raise IntakeError(INVALID)
        if len(text) > limit:
            raise IntakeError("Keep %s to %d characters." % (LABELS[field], limit))
        if _words(text):
            answers[field] = _words(text)
    if "condition" not in answers:
        raise IntakeError(NO_CONDITION)
    for field, allowed in CHOICES.items():
        if value.get(field) is None:
            continue
        if value[field] not in allowed:
            raise IntakeError(INVALID)
        answers[field] = value[field]
    for flag in FLAGS:
        if value.get(flag) not in (None, False, True):
            raise IntakeError(INVALID)
        if value.get(flag) is True:
            answers[flag] = True
    answers["consent"] = True
    return answers


def from_header(raw):
    """The answers from the upload's header, checked."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_HEADER:
        raise IntakeError(INVALID)
    try:
        value = json.loads(base64.b64decode(raw.encode("ascii"), validate=True).decode("utf-8"))
    except (UnicodeError, binascii.Error, ValueError) as exc:
        raise IntakeError(INVALID) from exc
    return check(value)
