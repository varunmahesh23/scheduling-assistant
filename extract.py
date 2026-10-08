"""Deterministic parsing of high-stakes inputs. The LLM is never trusted with these.

* identifiers (phone, DOB, zip) -> regex, so they never need to be sent to a model
* slot selection              -> matched against the list we actually offered
* yes / no confirmation       -> strict whitelist; ambiguity means "ask again"
"""
from __future__ import annotations

import re
from datetime import date

PHONE_RE = re.compile(r"(?<!\d)(\d{3})[-.\s]?(\d{4})(?!\d)")
ZIP_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_US = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)")
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MDY = re.compile(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", re.I)
_DMY = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?,?\s+(\d{4})\b", re.I)


def _valid(y, m, d):
    try:
        dt = date(int(y), int(m), int(d))
    except ValueError:
        return None
    return dt.isoformat() if dt <= date.today() else None


def _month(name):
    name = name.lower()[:3]
    return _MONTHS.index(name) + 1 if name in _MONTHS else None


def find_dob(text: str):
    """Return (iso_dob or None, text with the matched date removed)."""
    for rx, order in ((_ISO, "ymd"), (_US, "mdy")):
        m = rx.search(text)
        if m:
            a, b, c = m.groups()
            iso = _valid(a, b, c) if order == "ymd" else _valid(c, a, b)
            if iso:
                return iso, text[:m.start()] + " " + text[m.end():]
    m = _MDY.search(text)
    if m and _month(m.group(1)):
        iso = _valid(m.group(3), _month(m.group(1)), m.group(2))
        if iso:
            return iso, text[:m.start()] + " " + text[m.end():]
    m = _DMY.search(text)
    if m and _month(m.group(2)):
        iso = _valid(m.group(3), _month(m.group(2)), m.group(1))
        if iso:
            return iso, text[:m.start()] + " " + text[m.end():]
    return None, text


def find_phone(text: str):
    """Fixture phones look like 555-0101 (no area code); extra digits before are ignored."""
    m = PHONE_RE.search(text)
    if not m:
        return None, text
    return "{}-{}".format(m.group(1), m.group(2)), text[:m.start()] + " " + text[m.end():]


def find_zip(text: str):
    m = ZIP_RE.search(text)
    return m.group(1) if m else None


def redact_for_llm(text: str) -> str:
    """Replace identifiers with placeholders before text may be sent to a model."""
    for rx in (_ISO, _US, _MDY, _DMY):
        text = rx.sub("[DOB]", text)
    text = PHONE_RE.sub("[PHONE]", text)
    return ZIP_RE.sub("[ZIP]", text)


# ---- yes / no ---------------------------------------------------------------
_FILLER = {"please", "thanks", "thank", "you", "pls"}
_YES = {"yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "i confirm", "yes confirm", "confirm it",
        "book it", "yes book it", "go ahead", "yes go ahead", "please book it", "book it please", "yes i confirm"}
_NO_START = {"no", "n", "nope", "nah", "cancel", "stop", "nevermind", "wait", "wrong"}


def _norm(text: str) -> str:
    words = re.sub(r"[^a-z' ]", " ", text.lower()).split()
    while words and words[-1] in _FILLER:
        words.pop()
    return " ".join(words)


def is_yes(text: str) -> bool:
    return _norm(text) in _YES


def is_no(text: str) -> bool:
    n = _norm(text)
    return bool(n) and (n.split()[0] in _NO_START or "never mind" in n or "don't" in n or "do not" in n)


# ---- slot choice ------------------------------------------------------------
_ORD = {"first": 0, "1st": 0, "earliest": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
        "fourth": 3, "4th": 3, "fifth": 4, "5th": 4}


def choose_slot(text: str, offered: list):
    """Map the user's reply onto one of the slots we actually showed. None if unclear."""
    low = text.lower()
    for slot in offered:
        if slot["slotId"].lower() in low:
            return slot
    if re.search(r"\b(last|latest)\b", low):
        return offered[-1]
    for word, idx in _ORD.items():
        if re.search(r"\b" + word + r"\b", low) and idx < len(offered):
            return offered[idx]
    nums = re.findall(r"(?<![\d:/-])(\d{1,2})(?![\d:/-])", low)
    if len(nums) == 1 and 1 <= int(nums[0]) <= len(offered):
        return offered[int(nums[0]) - 1]
    return None
