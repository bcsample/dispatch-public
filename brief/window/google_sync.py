"""Dispatch's OWN read-only Google Calendar credential (P1-C).

Supersedes the earlier sys.path borrow of the voice assistant project's google_sync --
that worked but meant Dispatch had no credential of its own and would break
silently if Jarvis's repo ever moved. This is a separate OAuth client,
calendar.readonly ONLY, with its own encrypted-at-rest tokens
(brief/secretbox.py, its own Keychain entry "dispatch-tokens" -- distinct
from Jarvis's "jarvis-tokens" so a leaked token from either project can't be
used against the other).

MULTI-ACCOUNT, calendar-only. the operator has TWO calendars -- personal and work
(2026-09-08) -- and a live client call is at least as likely to sit on the work
one. Read one and the answer to "is he in a meeting" is False whenever the
meeting is on the other, which is the SAME answer the check gives when he is
genuinely free: it would not look broken, it would just read the news over a
call one day. So every stored account is consulted, and the first live call
found wins.

This was a real regression for one day. The sys.path shim this replaced called
the voice assistant project's multi-account is_in_meeting_now(); P1-C's first version was
single-account against calendarId="primary" only, narrowing two calendars to
one while presenting as an upgrade. Written down rather than quietly fixed,
because "the degraded mode is indistinguishable from the working one" is the
project's most expensive recurring shape.

Set up once PER ACCOUNT (labels are yours; "work" and "personal" are the two
that matter here):

    .venv/bin/python -m brief.window.google_sync authorize work
    .venv/bin/python -m brief.window.google_sync authorize personal

Fail-soft is the caller's job (see calendar_check.py): this module raises when
it cannot answer at all. The one case it must never do quietly is a PARTIAL
answer -- some accounts readable, some not -- because a False built from half
the calendars is the dangerous one; that logs at WARNING before returning.

STATUS 2026-09-09 (verified, not assumed). The Desktop OAuth client JSON is
present at data/google_credentials.json (mode 0600, dated 2026-08-16). No token
exists yet, so consent has never been granted, is_in_meeting_now() raises, and
the board behaves exactly as if this signal did not exist.

THE NEXT STEP, and it is the operator's alone: this client lives in the Google Cloud
project `bionic-comfort-407714` (NOT the project Jarvis's client uses), whose
OAuth consent screen is in Testing mode with his account not added as a test
user. He hit the Google-side error mid-flow on 2026-08-16 and said to hold. Add
BOTH accounts under Test users at
console.cloud.google.com/apis/credentials/consent for that project -- both, or
the second authorize fails the same way the first did -- then run the two
commands above. Nothing else is outstanding.

An earlier note in this repo's record said this was blocked on "creating the
consent screen / OAuth client". Wrong on both counts: the client had existed
for three weeks, and the blocker is a test-user entry on an existing consent
screen. Corrected here rather than quietly, per the project rule.
"""

from __future__ import annotations

import datetime as _dt
import os
from pathlib import Path

from .. import applog

CAL_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"

_ROOT = Path(__file__).resolve().parents[2]
_CREDS = Path(
    os.environ.get(
        "DISPATCH_GOOGLE_CREDENTIALS", str(_ROOT / "data" / "google_credentials.json")
    )
)
_TOKEN_DIR = _ROOT / "data" / "google"
# The single-account path P1-C shipped with for one day. Nothing ever wrote one
# (consent was never granted), but it is read as an account labelled "default"
# so a token created between that commit and this one is not silently ignored.
_LEGACY_TOKEN_PATH = _ROOT / "data" / "google_token.json"

_CALL_KEYWORDS = ("zoom.us", "teams.microsoft.com", "meet.google.com", "webex.com")

log = applog.get(__name__)


def _token_path(label: str) -> Path:
    return _TOKEN_DIR / f"{label}.json"


def _token_files() -> list[Path]:
    """Every stored account token, newest naming scheme first. This is the
    count of what SHOULD be readable -- compared against what actually loads,
    it is how a partial answer is detected instead of assumed away."""
    paths = sorted(_TOKEN_DIR.glob("*.json")) if _TOKEN_DIR.exists() else []
    if _LEGACY_TOKEN_PATH.exists():
        paths.append(_LEGACY_TOKEN_PATH)
    return paths


def _label_for(path: Path) -> str:
    return "default" if path == _LEGACY_TOKEN_PATH else path.stem


def _write_token(path: Path, creds) -> None:
    from .. import secretbox

    _TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_TOKEN_DIR, 0o700)
    except OSError:
        pass
    secretbox.atomic_write_bytes(
        path, secretbox.encrypt(creds.to_json().encode("utf-8"))
    )


def _read_token_info(path: Path) -> dict | None:
    import json

    from .. import secretbox

    if not path.exists():
        return None
    raw = path.read_bytes()
    if secretbox.is_encrypted(raw):
        return json.loads(secretbox.decrypt(raw))
    info = json.loads(raw)  # legacy plaintext -> migrate in place
    secretbox.atomic_write_bytes(path, secretbox.encrypt(raw))
    log.info("migrated dispatch google token to encrypted-at-rest: %s", path.name)
    return info


def authorize(label: str) -> str:
    """One-time browser consent for ONE account. Opens a local browser window;
    run this on the same machine Dispatch runs on (Skynet). Run it once per
    account -- the label is just the filename, so "work" and "personal" are
    fine."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    label = label.strip()
    if not label or "/" in label or label.startswith("."):
        return f"Bad account label {label!r} -- use a plain name like 'work'."
    if not _CREDS.exists():
        return (
            f"Missing {_CREDS} -- create a Desktop OAuth client and save its "
            "JSON there first."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(_CREDS), [CAL_SCOPE])
    creds = flow.run_local_server(port=0)
    path = _token_path(label)
    _write_token(path, creds)
    return f"Authorized calendar.readonly for '{label}', token stored at {path}."


def _accounts() -> list[tuple[str, object]]:
    """(label, refreshed Credentials) for every stored account token.

    One unreadable token never takes the others down -- it is logged and
    skipped, and the caller compares this list's length against _token_files()
    to notice it is answering from fewer calendars than exist."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    out: list[tuple[str, object]] = []
    for path in _token_files():
        label = _label_for(path)
        try:
            info = _read_token_info(path)
            if info is None:
                continue
            creds = Credentials.from_authorized_user_info(info, info.get("scopes"))
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                _write_token(path, creds)
            if creds and creds.valid:
                out.append((label, creds))
            else:
                log.warning("dispatch google token for %r is not valid", label)
        except Exception as exc:  # noqa: BLE001 — one bad token must not blind the rest
            log.warning("dispatch google token load failed for %r: %r", label, exc)
    return out


def _calendar_ids() -> list[str]:
    """Calendars to read on each account: 'primary', plus any shared-in
    calendar ids in DISPATCH_EXTRA_CALENDARS (comma-separated) -- the escape
    hatch for a calendar shared into one account rather than owned by it."""
    extra = [
        c.strip()
        for c in os.environ.get("DISPATCH_EXTRA_CALENDARS", "").split(",")
        if c.strip()
    ]
    return ["primary", *extra]


def _event_span(ev: dict) -> tuple[_dt.datetime, _dt.datetime] | None:
    s, e = ev.get("start", {}), ev.get("end", {})
    if "dateTime" not in s or "dateTime" not in e:
        return None
    try:
        return _dt.datetime.fromisoformat(s["dateTime"]), _dt.datetime.fromisoformat(
            e["dateTime"]
        )
    except ValueError:
        return None


def _looks_like_a_call(ev: dict) -> bool:
    attendees = ev.get("attendees") or []
    if any(not a.get("self") for a in attendees):
        return True
    if ev.get("conferenceData") or ev.get("hangoutLink"):
        return True
    text = f"{ev.get('location', '')} {ev.get('description', '')}".lower()
    return any(kw in text for kw in _CALL_KEYWORDS)


def _events_today(creds, calendar_id: str = "primary") -> list[dict]:
    from googleapiclient.discovery import build

    now = _dt.datetime.now().astimezone()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day1 = day0 + _dt.timedelta(days=1)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return (
        service.events()
        .list(
            calendarId=calendar_id,
            timeMin=day0.isoformat(),
            timeMax=day1.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=20,
        )
        .execute()
        .get("items", [])
    )


def is_in_meeting_now() -> bool:
    """True if ANY authorized account's calendar shows a real call (see
    _looks_like_a_call) covering right now. Raises when no account can be read
    at all -- callers fail-soft.

    Returns True on the first hit rather than surveying everything: the answer
    only ever gets safer with more calendars, so an early exit cannot cause a
    false "free"."""
    expected = _token_files()
    if not expected:
        raise RuntimeError(
            "no Dispatch google token -- run "
            "`python -m brief.window.google_sync authorize <label>` first"
        )
    accounts = _accounts()
    if not accounts:
        raise RuntimeError(
            f"{len(expected)} Dispatch google token(s) present but none readable"
        )

    now = _dt.datetime.now().astimezone()
    unread: list[str] = []
    for label, creds in accounts:
        for cal_id in _calendar_ids():
            try:
                events = _events_today(creds, cal_id)
            except Exception as exc:  # noqa: BLE001 — a dead calendar is partial, not fatal
                log.warning(
                    "calendar %r on account %r unreadable: %r", cal_id, label, exc
                )
                unread.append(f"{label}:{cal_id}")
                continue
            for ev in events:
                span = _event_span(ev)
                if span is None:
                    continue
                start, end = span
                if start <= now < end and _looks_like_a_call(ev):
                    return True

    # A False built from fewer calendars than exist is the dangerous answer --
    # it is indistinguishable from "genuinely free". Never return it silently.
    missing_accounts = len(expected) - len(accounts)
    if missing_accounts or unread:
        log.warning(
            "answering 'not in a meeting' from PARTIAL calendar visibility: "
            "%d of %d accounts readable, unreadable calendars=%s",
            len(accounts),
            len(expected),
            unread or "none",
        )
    return False


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 2 and sys.argv[1] == "authorize":
        print(authorize(sys.argv[2]))
    else:
        print("usage: python -m brief.window.google_sync authorize <label>")
        print("       e.g. authorize work   /   authorize personal")
