"""Deterministic safety nets that run on EVERY turn, independent of the LLM.

The LLM may *also* flag medical advice / human requests; we OR the signals, so
a missed classification by the model can never route a symptom question into a
booking flow. False positives are cheap (a human follows up); false negatives
are not.
"""
from __future__ import annotations

import re

_RED_FLAGS = re.compile(
    r"chest pain|can'?t breathe|cannot breathe|short(ness)? of breath|heart attack|stroke|overdose|"
    r"suicid|kill myself|self[- ]harm|severe bleeding|unconscious|seizure|allergic reaction|passed out", re.I)

_ADVICE_CUE = re.compile(
    r"\bshould i\b|\bdo i need\b|\bis (it|this|that) (serious|normal|safe|dangerous|bad|okay|ok)\b|"
    r"\bwhat (should|could|might) (i|this)\b|\bwhat('s| is) wrong\b|\bcan i take\b|\bhow much\b.*\b(take|dose)\b|"
    r"\bdiagnos|\bwhat (medication|medicine|drug)\b|\bam i (having|dying|sick)\b|\bshould (i|we) (wait|worry)\b", re.I)

_SYMPTOM = re.compile(
    r"\b(pain|ache|aching|fever|cough|rash|bleeding|dizzy|dizziness|nausea|vomit\w*|swelling|swollen|lump|"
    r"symptoms?|infection|medication|medicine|dosage|dose|prescription|side effects?|headache|mole)\b", re.I)

_HUMAN = re.compile(
    r"\b(human|live agent|live person|real person|representative|operator|receptionist|customer service)\b|"
    r"\b(talk|speak|chat|connect|transfer)\b.{0,25}\b(person|someone|somebody|agent|staff|team member)\b", re.I)


def is_medical_advice(text: str) -> bool:
    return bool(_RED_FLAGS.search(text) or (_ADVICE_CUE.search(text) and _SYMPTOM.search(text)))


def wants_human(text: str) -> bool:
    return bool(_HUMAN.search(text))
