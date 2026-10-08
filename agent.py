"""Scheduling workflow: a small deterministic state machine.

Who decides what
----------------
  LLM (nlu.py)   : intent + specialty/location from free text. Nothing else.
  Code (here)    : which question to ask next, identity resolution, which slots may be
                   offered, what counts as a confirmation, when to hand off, all wording.
  API            : truth about providers, patients, availability, bookings.

Every user-facing sentence is a template filled with API data, so the assistant
cannot invent a slot, a patient or a confirmation number.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

import extract
import safety
from api import ApiError, SchedulingClient
from tracing import Tracer

# workflow states
IDLE, NEED_INFO, NEED_ZIP, PICK_SLOT, CONFIRM, NO_AVAIL = (
    "idle", "need_info", "need_zip", "pick_slot", "confirm", "no_availability")

MAX_ID_ATTEMPTS = 2
MAX_SLOTS_SHOWN = 5
PRETTY = {"primary_care": "primary care", "dermatology": "dermatology"}
HELP = ("I can help you find providers, book an appointment, or look up your upcoming appointments. "
        "What would you like to do?")


@dataclass
class Session:
    state: str = IDLE
    intent: str | None = None            # sticky current workflow
    specialty: str | None = None
    location: str | None = None
    location_any: bool = False
    phone: str | None = None             # held in memory only until the patient search, then dropped
    dob: str | None = None
    id_attempts: int = 0
    candidates: list = field(default_factory=list)   # ambiguous matches awaiting zip (memory only)
    patient_id: str | None = None
    patient_first: str | None = None
    offered: list = field(default_factory=list)
    chosen: dict | None = None
    failed_slots: set = field(default_factory=set)
    alt_locations: list = field(default_factory=list)


def fmt_time(iso: str) -> str:
    dt = datetime.fromisoformat(iso)
    return "{} {}, {}:{:02d} {}".format(dt.strftime("%a %b"), dt.day, dt.hour % 12 or 12, dt.minute,
                                         "AM" if dt.hour < 12 else "PM")


class Assistant:
    def __init__(self, api: SchedulingClient, nlu, tracer: Tracer):
        self.api, self.nlu, self.tracer = api, nlu, tracer
        self.s = Session()
        self._provider_cache: dict | None = None

    # ------------------------------------------------------------------ entry
    def respond(self, text: str) -> str:
        started = time.perf_counter()
        before = self.s.state
        awaiting = {NEED_INFO: "missing booking details (specialty/location/phone/dob)", NEED_ZIP: "zip code",
                    PICK_SLOT: "slot choice", CONFIRM: "yes/no confirmation",
                    NO_AVAIL: "alternate location or human handoff"}.get(before)
        n0 = time.perf_counter()
        parsed = self.nlu.parse(text, awaiting)
        nlu_ms = round((time.perf_counter() - n0) * 1000, 1)
        reply = self._route(text, parsed)
        self.tracer.event("turn", intent=self.s.intent or parsed.intent, nlu_intent=parsed.intent,
                          nlu_source=parsed.source, state_before=before, state_after=self.s.state,
                          user_chars=len(text), nlu_ms=nlu_ms,
                          total_ms=round((time.perf_counter() - started) * 1000, 1))
        return reply

    # ----------------------------------------------------------------- router
    def _route(self, text: str, p) -> str:
        s = self.s
        # 1. Hard interrupts, valid in any state. Deterministic guard OR model signal.
        if p.intent == "medical_advice" or safety.is_medical_advice(text):
            return self._handoff(
                "medical_advice", "User asked a medical-advice question; assistant gave none.",
                "I'm not able to give medical advice or assess symptoms, and I don't want to guess about "
                "something that could matter for your health. If this could be an emergency, please call 911 "
                "(or your local emergency number) right away. ")
        if p.intent == "human" or safety.wants_human(text):
            reason = "no_availability" if s.state == NO_AVAIL else "user_requested"
            return self._handoff(reason, "User asked to speak with a person.", "Of course. ")

        # 2. Confirmation is evaluated before anything else so no other input can slip through it.
        if s.state == CONFIRM:
            return self._on_confirm(text)

        # 3. Unsupported specialty/location -> handoff per policy.
        if p.specialty == "other" or p.location == "other":
            return self._handoff("unsupported_request", "Requested a specialty or location that is not supported.",
                                 "I can currently only schedule primary care and dermatology at downtown, uptown "
                                 "or lakeside, so I can't handle that request myself. ")

        if s.state == NEED_ZIP:
            return self._on_zip(text)
        if s.state == PICK_SLOT:
            return self._on_pick(text, p)

        changed = self._absorb(text, p)
        if p.intent in ("provider_lookup", "book", "my_appointments"):
            if p.intent != s.intent:
                s.intent = p.intent
                s.id_attempts = 0
            return self._advance()
        if s.state == NO_AVAIL:
            return self._on_no_avail(text, changed)
        if s.state == NEED_INFO:
            return self._advance()
        if s.intent == "provider_lookup" and changed:   # e.g. "what about uptown?"
            return self._advance()
        if extract.is_no(text):
            return "No problem. " + HELP if s.intent else HELP
        return HELP

    def _absorb(self, text: str, p) -> bool:
        """Merge newly provided entities into the session. Returns True if anything changed."""
        s, changed = self.s, False
        dob, rest = extract.find_dob(text)
        phone, _ = extract.find_phone(rest)
        for attr, val in (("dob", dob), ("phone", phone)):
            if val and getattr(s, attr) != val:
                setattr(s, attr, val)
                changed = True
        if p.specialty and p.specialty != s.specialty:
            s.specialty, changed = p.specialty, True
        if p.location == "any":
            s.location, s.location_any, changed = None, True, True
        elif p.location and p.location != s.location:
            s.location, s.location_any, changed = p.location, False, True
        return changed

    # ------------------------------------------------------------- main flow
    def _advance(self) -> str:
        s = self.s
        if s.intent == "provider_lookup":
            return self._lookup()

        missing = []
        if s.intent == "book":
            if not s.specialty:
                missing.append("the type of care (primary care or dermatology)")
            if not s.location and not s.location_any:
                missing.append("a preferred location (downtown, uptown, lakeside, or 'any')")
        if not s.patient_id and not (s.phone and s.dob):
            if not s.phone:
                missing.append("your phone number")
            if not s.dob:
                missing.append("your date of birth (YYYY-MM-DD)")
        if missing:
            s.state = NEED_INFO
            known = ""
            if s.intent == "book" and (s.specialty or s.location):
                known = " ({})".format(" ".join(filter(None, [PRETTY.get(s.specialty), "at " + s.location if s.location else None])))
            ident = [m for m in missing if "phone" in m or "birth" in m]
            others = [m for m in missing if m not in ident]
            msg = "Happy to help{}. ".format(known)
            if others:
                msg += "First I need " + _join(others) + ". "
            if ident:
                msg += ("To verify your record, I also need " if others else "To verify your record, I need ") + _join(ident) + "."
            return msg.strip()

        if not s.patient_id:
            return self._verify()
        if s.intent == "book":
            return self._search_slots()
        return self._list_appointments()

    # -------------------------------------------------------------- identity
    def _verify(self) -> str:
        s = self.s
        phone, dob = s.phone, s.dob
        s.phone = s.dob = None  # do not retain identifiers longer than needed
        try:
            matches = self.api.search_patients(phone, dob)
        except ApiError as err:
            return self._api_failed(err)
        if len(matches) == 1:
            s.patient_id, s.patient_first = matches[0]["patientId"], matches[0]["firstName"]
            s.id_attempts, s.state = 0, IDLE
            self.tracer.event("identity", result="verified")
            return "Thanks, I found your record, {}. ".format(s.patient_first) + self._advance()
        if len(matches) == 0:
            self.tracer.event("identity", result="no_match", attempt=s.id_attempts + 1)
            return self._identity_retry("I couldn't find a patient record matching those details.")
        s.candidates, s.state = matches, NEED_ZIP
        self.tracer.event("identity", result="multiple", count=len(matches))
        return ("More than one record matches those details, so I can't tell which one is yours. "
                "What is the ZIP code on your record?")

    def _identity_retry(self, lead: str) -> str:
        s = self.s
        s.id_attempts += 1
        if s.id_attempts >= MAX_ID_ATTEMPTS:
            return self._handoff("identity_unclear", "Could not verify the patient after repeated attempts.",
                                 lead + " Since I can't confirm who you are, I can't book or show appointment "
                                 "details. ")
        s.state = NEED_INFO
        return (lead + " Please re-enter your phone number and date of birth (YYYY-MM-DD), "
                "or say 'human' and I'll connect you with someone.")

    def _on_zip(self, text: str) -> str:
        s = self.s
        zip_code = extract.find_zip(text)
        match = [c for c in s.candidates if zip_code and c.get("zipCode") == zip_code]
        if len(match) == 1:
            s.patient_id, s.patient_first = match[0]["patientId"], match[0]["firstName"]
            s.candidates, s.state, s.id_attempts = [], IDLE, 0
            self.tracer.event("identity", result="verified_by_zip")
            return "Thank you, that's confirmed. " + self._advance()
        s.id_attempts += 1
        if s.id_attempts >= MAX_ID_ATTEMPTS:
            s.candidates = []
            return self._handoff("identity_unclear", "Multiple patient records matched and the ZIP did not resolve it.",
                                 "I still can't tell which record is yours, so I can't proceed with your "
                                 "appointment details. ")
        return "That ZIP doesn't match a single record. Could you check it and try again, or say 'human'?"

    # -------------------------------------------------------------- providers
    def _provider_names(self) -> dict:
        if self._provider_cache is None:
            try:
                self._provider_cache = {p["providerId"]: p["name"] for p in self.api.providers()}
            except ApiError:
                return {}  # cosmetic only: fall back to provider IDs
        return self._provider_cache

    def _lookup(self) -> str:
        s = self.s
        try:
            providers = self.api.providers(s.specialty, s.location)
        except ApiError as err:
            return self._api_failed(err)
        where = " at {}".format(s.location) if s.location else ""
        what = PRETTY.get(s.specialty, "")
        if not providers:
            return ("I don't see any {} providers{} in the scheduling system. You can try another location "
                    "or specialty, or say 'human'.".format(what, where).replace("  ", " "))
        lines = ["- {} ({}; {}; {})".format(p["name"], PRETTY.get(p["specialty"], p["specialty"]),
                                            ", ".join(p["locations"]), " / ".join(m.replace("_", "-") for m in p["modalities"]))
                 for p in providers]
        return ("Here are the {} providers{}:\n".format(what, where).replace("  ", " ") + "\n".join(lines) +
                "\nWould you like to book an appointment? Just say so, or ask about another location.")

    # ----------------------------------------------------------- availability
    def _search_slots(self) -> str:
        s = self.s
        try:
            slots = self.api.availability(s.patient_id, s.specialty, s.location)
        except ApiError as err:
            return self._api_failed(err)
        # Offer only what the API returned, minus slots that already failed on booking this session.
        slots = sorted((x for x in slots if x.get("available", True) and x["slotId"] not in s.failed_slots),
                       key=lambda x: x["startTime"])[:MAX_SLOTS_SHOWN]
        if not slots:
            return self._no_availability()
        s.offered, s.state = slots, PICK_SLOT
        names = self._provider_names()
        lines = ["{}. {} - {} with {} ({})".format(i, fmt_time(x["startTime"]), PRETTY.get(x["specialty"], x["specialty"]),
                                                  names.get(x["providerId"], x["providerId"]), x["location"])
                 for i, x in enumerate(slots, 1)]
        return "Here are the available appointments:\n" + "\n".join(lines) + "\nWhich one would you like? (e.g. '1')"

    def _no_availability(self) -> str:
        s = self.s
        where = " at {}".format(s.location) if s.location else ""
        other = " other" if s.failed_slots else ""
        s.alt_locations = []
        try:  # real data only: where does a provider for this specialty actually work?
            locs = {l for p in self.api.providers(s.specialty) for l in p["locations"]}
            s.alt_locations = sorted(locs - {s.location})
        except ApiError:
            pass
        s.state, s.location, s.location_any, s.offered = NO_AVAIL, None, False, []
        msg = "I don't see any{} open {} appointments{} right now.".format(other, PRETTY.get(s.specialty, ""), where)
        if s.alt_locations:
            msg += " {} providers are listed at: {}. I can search there instead".format(
                PRETTY.get(s.specialty, "").capitalize(), ", ".join(s.alt_locations))
            msg += " (listing doesn't guarantee openings), or connect you with a person."
        else:
            msg += " I can try a different specialty, or connect you with a person."
        return msg

    def _on_no_avail(self, text: str, changed: bool) -> str:
        s = self.s
        if changed and (s.location or s.location_any or s.specialty):
            return self._advance()
        if extract.is_yes(text) and len(s.alt_locations) == 1:
            s.location = s.alt_locations[0]
            return self._advance()
        if extract.is_no(text):
            s.state, s.intent = IDLE, None
            return "Okay, I won't book anything. " + HELP
        return "Tell me another location to search, or say 'human' and I'll connect you with a person."

    # ------------------------------------------------------- slot + confirmation
    def _on_pick(self, text: str, p) -> str:
        s = self.s
        slot = extract.choose_slot(text, s.offered)
        if slot:
            s.chosen, s.state = slot, CONFIRM
            names = self._provider_names()
            return ("To confirm: {} with {} at {} on {}.\nShall I book this appointment? (yes / no)".format(
                PRETTY.get(slot["specialty"], slot["specialty"]), names.get(slot["providerId"], slot["providerId"]),
                slot["location"], fmt_time(slot["startTime"])))
        if extract.is_no(text):
            s.state, s.intent, s.offered = IDLE, None, []
            return "No problem, nothing has been booked. " + HELP
        return "I didn't catch which one. Please reply with a number from the list (1-{}), or 'cancel'.".format(len(s.offered))

    def _on_confirm(self, text: str) -> str:
        s = self.s
        if extract.is_yes(text):
            return self._book()
        if extract.is_no(text):
            s.chosen, s.state = None, PICK_SLOT
            return "Okay, I haven't booked anything. Pick another number from the list above, or say 'cancel'."
        return "I need a clear 'yes' to book this appointment, or 'no' to choose a different one."

    def _book(self) -> str:
        s = self.s
        slot = s.chosen
        try:
            appt = self.api.book(s.patient_id, slot["slotId"], user_confirmed=True)
        except ApiError as err:
            if err.status == 409:
                s.failed_slots.add(slot["slotId"])
                s.chosen = None
                self.tracer.event("booking", result="slot_conflict")
                return ("Sorry, that time was just taken by another booking, so nothing has been booked. " +
                        self._search_slots())
            if err.is_outage:
                return self._api_failed(err)
            return self._handoff("other", "Booking request was rejected unexpectedly ({}).".format(err.code),
                                 "I wasn't able to complete that booking, and nothing has been booked. ")
        self.tracer.event("booking", result="booked")
        names = self._provider_names()
        msg = ("You're booked! {} with {} at {} on {}. Confirmation number: {}.".format(
            PRETTY.get(appt["specialty"], appt["specialty"]), names.get(appt["providerId"], appt["providerId"]),
            appt["location"], fmt_time(appt["startTime"]), appt["appointmentId"]))
        s.state, s.intent, s.chosen, s.offered, s.failed_slots = IDLE, None, None, [], set()
        s.specialty = s.location = None
        return msg + " Is there anything else I can help with?"

    # --------------------------------------------------------- appointments
    def _list_appointments(self) -> str:
        s = self.s
        try:
            appts = self.api.patient_appointments(s.patient_id)
        except ApiError as err:
            return self._api_failed(err)
        s.intent = None
        if not appts:
            return "You don't have any appointments on file."
        names = self._provider_names()
        lines = ["- {} - {} with {} ({}, {})".format(fmt_time(a["startTime"]), PRETTY.get(a["specialty"], a["specialty"]),
                                                    names.get(a["providerId"], a["providerId"]), a["location"], a["status"])
                 for a in sorted(appts, key=lambda a: a["startTime"])]
        return "Here are your appointments:\n" + "\n".join(lines)

    # ------------------------------------------------------------- handoff
    def _api_failed(self, err: ApiError) -> str:
        self.tracer.event("api_failure", status=err.status, code=err.code)
        return self._handoff("api_failure", "Scheduling API unavailable ({}).".format(err.code),
                             "The scheduling system isn't responding right now, so I can't look anything up or "
                             "book - and nothing has been booked. ")

    def _handoff(self, reason: str, summary: str, lead: str) -> str:
        """Create a handoff. Summary is built by code and contains no PHI or identifiers."""
        s = self.s
        self.tracer.event("handoff", reason=reason, state=s.state)
        try:
            h = self.api.handoff(reason, summary, s.patient_id)
            tail = "I've asked a team member to follow up (reference {}).".format(h.get("handoffId"))
        except ApiError:
            tail = ("I also tried to queue a request for a team member but couldn't reach that system, so "
                    "nothing has been sent. Please contact the clinic directly or try again shortly.")
        self.s = Session()  # clear everything, including verified identity
        return lead + tail


def _join(items: list) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
