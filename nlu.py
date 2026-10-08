"""Intent + slot understanding.

This is the ONLY place a model is used. It maps free text to a small, validated
structure (intent, specialty, location). It never writes replies, never sees API
responses or patient data, and never decides confirmations or identity.

Two interchangeable implementations:
  RuleNLU    - keyword rules; offline, deterministic, used for tests and as fallback
  OpenAINLU  - OpenAI chat completions with strict JSON-schema output; any failure
               (no key, timeout, bad JSON, out-of-enum value) falls back to RuleNLU
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from extract import redact_for_llm
from tracing import Tracer

INTENTS = ("provider_lookup", "book", "my_appointments", "human", "medical_advice", "other")
SPECIALTIES = ("primary_care", "dermatology")
LOCATIONS = ("downtown", "uptown", "lakeside")


@dataclass
class Parsed:
    intent: str = "other"
    specialty: str | None = None   # supported value, "other" (unsupported) or None
    location: str | None = None    # supported value, "any", "other" (unsupported) or None
    source: str = "rules"


class RuleNLU:
    _OTHER_SPECIALTY = re.compile(
        r"cardio|heart doctor|ortho|pediatric|paediatric|dentist|dental|neuro|ophthalm|eye doctor|gyn|obgyn|"
        r"psychiat|psycholog|therap|\bent\b|urolog|oncolog|radiolog|endocrin|allerg|podiatr|surgeon|surgery", re.I)

    def parse(self, text: str, awaiting: str | None = None) -> Parsed:
        low = text.lower()
        p = Parsed(source="rules")

        if re.search(r"\bmy (upcoming |next |existing |current )?(appointments?|visits?|bookings?)\b|"
                     r"\b(look ?up|check|see|when is) (my|the) (appointment|visit)", low):
            p.intent = "my_appointments"
        elif re.search(r"\b(book|schedule|reserve|set up|make|need|want|get)\b.{0,30}\b(appointment|visit|slot|"
                       r"checkup|check-up)\b|\bbook\b|\bschedule\b|\bappointment\b", low):
            p.intent = "book"
        elif re.search(r"\b(providers?|doctors?|physicians?|clinicians?|dermatologists?|who|which)\b", low):
            p.intent = "provider_lookup"

        if self._OTHER_SPECIALTY.search(low):
            p.specialty = "other"
        elif re.search(r"primary[ _-]?care|\bpcp\b|family (doctor|medicine|physician)|general practi|\bgp\b|"
                       r"check-?up|physical exam|internal medicine", low):
            p.specialty = "primary_care"
        elif re.search(r"dermatolog|skin", low):
            p.specialty = "dermatology"

        for loc in LOCATIONS:
            if re.search(r"\b" + loc + r"\b", low):
                p.location = loc
        if p.location is None and re.search(r"any ?where|any location|no preference|doesn'?t matter|don'?t care|"
                                            r"whichever|any clinic", low):
            p.location = "any"
        return p


class OpenAINLU:
    SYSTEM = (
        "You classify messages for a medical appointment scheduling assistant. Output JSON only; never answer "
        "the user. intent: provider_lookup (asks which providers/doctors exist), book (wants to book an "
        "appointment), my_appointments (asks about their existing appointments), human (asks for a person), "
        "medical_advice (asks for diagnosis, symptom guidance, medication/dosage, 'should I wait/worry', urgency "
        "triage), other (anything else, including greetings or short answers). specialty: primary_care or "
        "dermatology if clearly meant; 'other' if a different specialty is requested; null if not mentioned. "
        "location: downtown, uptown, lakeside, 'any' if the user has no preference, 'other' if a different place "
        "is requested; null if not mentioned. Tokens like [PHONE], [DOB], [ZIP] are redacted identifiers. "
        "'awaiting' says what the assistant just asked for; short replies answer it."
    )
    SCHEMA = {
        "type": "object", "additionalProperties": False,
        "required": ["intent", "specialty", "location"],
        "properties": {
            "intent": {"type": "string", "enum": list(INTENTS)},
            "specialty": {"type": ["string", "null"], "enum": ["primary_care", "dermatology", "other", None]},
            "location": {"type": ["string", "null"], "enum": ["downtown", "uptown", "lakeside", "any", "other", None]},
        },
    }

    def __init__(self, tracer: Tracer, fallback: RuleNLU | None = None, model: str | None = None,
                 api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0):
        self.tracer = tracer
        self.fallback = fallback or RuleNLU()
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def parse(self, text: str, awaiting: str | None = None) -> Parsed:
        rules = self.fallback.parse(text, awaiting)
        if not self.available:
            return rules
        started = time.perf_counter()
        try:
            llm = self._call(redact_for_llm(text), awaiting)
            self.tracer.event("llm_call", ok=True, model=self.model, ms=round((time.perf_counter() - started) * 1000, 1))
        except Exception as err:  # noqa: BLE001 - any failure degrades to rules, never to a crash
            self.tracer.event("llm_call", ok=False, model=self.model, error=type(err).__name__,
                              ms=round((time.perf_counter() - started) * 1000, 1))
            rules.source = "rules(fallback)"
            return rules
        # LLM leads on intent; rules fill any slot the model left empty.
        intent = llm.intent if llm.intent not in ("other",) else rules.intent
        return Parsed(intent=intent, specialty=llm.specialty or rules.specialty,
                      location=llm.location or rules.location, source="llm")

    def _call(self, redacted_text: str, awaiting: str | None) -> Parsed:
        body = {
            "model": self.model, "temperature": 0,
            "messages": [
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": json.dumps({"awaiting": awaiting or "nothing", "message": redacted_text})},
            ],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "parse", "strict": True, "schema": self.SCHEMA}},
        }
        req = urllib.request.Request(
            self.base_url + "/chat/completions", data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            content = json.loads(resp.read())["choices"][0]["message"]["content"]
        data = json.loads(content)
        # Never trust model output: validate against our own enums.
        intent = data.get("intent")
        spec, loc = data.get("specialty"), data.get("location")
        if intent not in INTENTS:
            raise ValueError("bad intent")
        if spec not in (*SPECIALTIES, "other", None) or loc not in (*LOCATIONS, "any", "other", None):
            raise ValueError("bad slot value")
        return Parsed(intent=intent, specialty=spec, location=loc, source="llm")
