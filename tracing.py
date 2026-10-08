"""Structured JSONL tracing with redaction of sensitive identifiers.

Policy: never log full DOB, phone number or other sensitive identifiers, and
never log secrets. User text is NOT logged verbatim (it may contain PHI); we log
its length, the classified intent, workflow state, API calls, latency, outcomes.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import uuid

_PHONE = re.compile(r"(?<!\d)\d{3}[-.\s]?\d{4}(?!\d)")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return "***-**" + digits[-2:] if len(digits) >= 2 else "***"


def hash_id(value: str) -> str:
    """Short one-way token so a patient can be correlated across log lines."""
    return hashlib.sha256(("trace-salt:" + (value or "")).encode()).hexdigest()[:8]


def redact_params(params: dict | None) -> dict:
    out = {}
    for key, val in (params or {}).items():
        if val is None or val == "":
            continue
        if key == "phone":
            out[key] = mask_phone(str(val))
        elif key in ("dob", "zip"):
            out[key] = "<redacted>"
        elif key == "patientId":
            out[key] = hash_id(str(val))
        else:
            out[key] = val
    return out


def scrub(text: str) -> str:
    """Defensive scrub for free-text fields such as upstream error messages."""
    return _ISO_DATE.sub("<date>", _PHONE.sub("<phone>", text or ""))


class Tracer:
    def __init__(self, path: str | None = None, echo: bool = False):
        self.path = path
        self.echo = echo
        self.session_id = uuid.uuid4().hex[:8]
        self.events: list[dict] = []  # kept in memory so tests/evals can assert on them

    def event(self, kind: str, **fields) -> dict:
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session": self.session_id, "kind": kind}
        rec.update(fields)
        self.events.append(rec)
        line = json.dumps(rec, default=str)
        if self.path:
            with open(self.path, "a") as fh:
                fh.write(line + "\n")
        if self.echo:
            print("\033[2m  [trace] " + line + "\033[0m", file=sys.stderr)
        return rec
